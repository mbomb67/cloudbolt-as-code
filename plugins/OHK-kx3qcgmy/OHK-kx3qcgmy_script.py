"""
Migrate VM to OpenShift

Server action that cold-exports a VMware VM through vCenter (443-only, via
the Export API -- no VDDK, no ESXi:902), runs virt-v2v on the appliance to
convert the guest to KVM (virtio, DHCP) and write QCOW2, then imports the
disks and VM metadata into OpenShift Virtualization (KubeVirt) via the CDI
upload proxy. Single-pass cold migration: the VM is powered off, exported,
converted, and imported in one run.

Prerequisites:
    - virt-v2v on the CloudBolt appliance (`dnf install -y virt-v2v`; add
      virtio-win + libguestfs-winsupport for Windows guests).
    - An OVirt/OpenShift resource handler with an environment accessible to
      the requesting group.
    - After syncing this content, restart CloudBolt (shared-module reload).
"""
import json
import os
import shutil

from accounts.models import Group
from common.methods import set_progress
from infrastructure.models import Environment
from resourcehandlers.ovirt.models import OVirtHandler
from shared_modules.openshift_import import OpenShiftImporter
from shared_modules.vmware_export import cold_export_via_nfc
from utilities.logger import ThreadLogger

logger = ThreadLogger(__name__)


# ------------------------------------------------------------------
# Dynamic dropdown generators
# ------------------------------------------------------------------

def _resolve_group(group):
    """The group kwarg arrives as a Group or as its name depending on caller."""
    if group is None or isinstance(group, Group):
        return group
    return Group.objects.filter(name=str(group)).first()


def generate_options_for_env_id(field, **kwargs):
    """RBAC-aware Environment selector, restricted to OpenShift-backed environments.

    Standard: Group.get_available_environments() returns the environments
    explicitly entitled to the requesting group (and its ancestors) PLUS any
    unconstrained environments (no groups assigned). Users must be able to
    order into both, so never filter Environment by group__in alone.
    See docs/agents/rbac-and-security.md.
    """
    group = _resolve_group(kwargs.get("group"))
    if not group:
        return []

    available_ids = [env.id for env in group.get_available_environments()]
    envs = Environment.objects.filter(
        id__in=available_ids,
        resource_handler__ovirthandler__isnull=False,
    ).order_by("name")
    if not envs.exists():
        return [("", "------ No OpenShift environments available ------")]

    options = [(env.id, env.name) for env in envs]
    options.insert(0, ("", "--- Select an Environment ---"))
    return options


def _get_wrapper_from_env_id(env_id):
    """Resolve an env_id to a cast OVirtHandler and its API wrapper."""
    env = Environment.objects.get(id=int(env_id))
    rh = env.resource_handler.cast()
    if not isinstance(rh, OVirtHandler):
        return None, None
    wrapper = rh.get_api_wrapper()
    return rh, wrapper


def generate_options_for_storage_class(field, control_value=None, **kwargs):
    """List storage classes from the selected OpenShift cluster."""
    if not control_value:
        return [("", "------ Please select an environment first ------")]

    try:
        _, wrapper = _get_wrapper_from_env_id(control_value)
        if wrapper is None:
            return []
        storage_classes = wrapper.get_storage_class()
        if not storage_classes:
            return [("", "------ No storage classes found ------")]
        options = [(sc, sc) if isinstance(sc, str) else (sc.get("name", sc), sc.get("name", sc))
                   for sc in storage_classes]
        options.insert(0, ("", "--- Cluster Default ---"))
        return {"options": options, "sort": False}
    except Exception as exc:
        logger.warning("Failed to list storage classes: %s", exc)
        return [("", f"------ Error: {exc} ------")]


def generate_options_for_network_name(field, control_value=None, **kwargs):
    """List NetworkAttachmentDefinitions from the selected environment's
    OpenShift namespace. The namespace is the environment's ``ovirt_namespace``
    (1:1 with the CloudBolt environment), so this depends on ``env_id`` only.

    Each option is ``(nad_ref, display_name)``. ``get_all_networks`` returns
    dicts whose ``network``/``name`` fields hold the ``namespace/nad-name``
    reference the importer resolves via ``_parse_nad_ref`` -- that reference is
    the option value; the readable name is the label. The ``uid`` is not used.
    """
    if not control_value:
        return [("", "------ Please select an environment first ------")]

    try:
        env = Environment.objects.get(id=int(control_value))
        rh = env.resource_handler.cast()
        if not isinstance(rh, OVirtHandler):
            return []
        namespace = env.ovirt_namespace
        if not namespace:
            return [("", "--- Pod Network (default) ---")]
        networks = rh.get_all_networks(namespace)
        options = [("", "--- Pod Network (default) ---")]
        for net in networks or []:
            if isinstance(net, dict):
                ref = net.get("network") or net.get("name")
                label = net.get("name") or net.get("network")
            else:
                ref = (getattr(net, "network", None)
                       or getattr(net, "name", None) or str(net))
                label = getattr(net, "name", None) or ref
            if not ref:
                continue
            # Skip the pod-network pseudo-entry: the default option above
            # already covers it, and it is not a real NAD reference.
            if str(label).strip().lower() == "pod networking":
                continue
            options.append((ref, label))
        return options
    except Exception as exc:
        logger.warning("Failed to list networks: %s", exc)
        return [("", "--- Pod Network (default) ---")]


def generate_options_for_run_strategy(field, **kwargs):
    return {
        "options": [
            ("Manual", "Manual"),
            ("Always", "Always"),
            ("RerunOnFailure", "RerunOnFailure"),
            ("Halted", "Halted"),
        ],
        "initial_value": "Manual",
    }


def generate_options_for_start_vm(field, **kwargs):
    return {
        "options": [
            ("False", "No"),
            ("True", "Yes"),
        ],
        "initial_value": "False",
    }


# ------------------------------------------------------------------
# Post-migration source cleanup
# ------------------------------------------------------------------

def _quiesce_source_vm(server, suffix="-migrated"):
    """Power off and rename the source vCenter VM after a successful migration.

    Once the disks are safely in OpenShift, the source VM is retired: ensured
    powered off (it already is, post cold-export) and renamed with a
    ``-migrated`` suffix so it is unmistakably the decommissioned source rather
    than a live duplicate.

    Must run BEFORE re-registration, while ``server.resource_handler`` still
    points at the VMware handler (re-registration repoints it at OpenShift).
    Returns the VM's new vCenter name. Idempotent on the suffix.
    """
    # Imported here so the dropdown generators never pull in the VMware stack.
    from shared_modules.vmware import VMwareConnection

    rh = server.resource_handler
    if rh is None:
        raise ValueError(
            "Server has no resource handler; cannot locate the source VM."
        )

    conn = VMwareConnection(rh.cast())
    vc_vm = conn.get_vc_vm_from_server(server)
    current_name = vc_vm.name

    # Ensure powered off. Cold export already powered it off (shutdown_guest
    # returns "already-off" immediately in that case); allow a hard power-off
    # since the disks are already exported and the guest state no longer matters.
    shut = conn.shutdown_guest(server, vc_vm=vc_vm, allow_hard_poweroff=True)
    if not shut.get("powered_off"):
        raise Exception(
            f"could not power off source VM '{current_name}': {shut.get('error')}"
        )

    new_name = current_name if current_name.endswith(suffix) else (
        f"{current_name}{suffix}"
    )
    conn.rename_vm(vc_vm, new_name)
    set_progress(
        f"Source vCenter VM powered off and renamed to '{new_name}'."
    )
    logger.info("Source VM for server %s retired in vCenter as '%s'.",
                server.id, new_name)
    return new_name


# ------------------------------------------------------------------
# Post-migration re-registration
# ------------------------------------------------------------------

def _reregister_server(server, rh, env, namespace, ocp_vm_name, ocp_vm_uid):
    """Re-home the *same* CloudBolt Server record onto the OpenShift handler.

    Corrects the identity fields the platform keys on, swaps the tech-specific
    ServerInfo, then lets ``Server.refresh_info()`` (which drives
    ``ServerUpdater``) rebuild power/IP/CPU/memory/disk/NIC state from the live
    OpenShift VM. Because the existing Server object is reused rather than
    recreated, all of its history, jobs, and rate data are preserved.

    Preconditions the platform enforces (all satisfied here, in this order):

    * ``resource_handler`` must already be the OVirt handler, or
      ``refresh_info()`` would query the old (vCenter) handler.
    * ``environment`` must be on that handler with ``ovirt_namespace`` set;
      ``get_vm_dict()`` reads ``server.environment.ovirt_namespace``.
    * ``hostname`` must equal the KubeVirt VirtualMachine ``metadata.name`` --
      it is the lookup key for ``get_vm_dict()``. The importer already names the
      VM from ``server.hostname`` (which matches the source VM name, not an
      FQDN), so this holds without rewriting the hostname. If a hostname is not
      a valid K8s name the importer's sanitisation can diverge; we warn rather
      than rewrite, and a divergence surfaces as a refresh WARNING.
    * ``resource_handler_svr_id`` must equal the KubeVirt VM ``metadata.uid``,
      or ``ServerUpdater``'s ``assert not resource_handler_svr_id`` fires.
    """
    # Imported here (not at module top) so the dynamic-dropdown generators,
    # which load this module, never pull in the vmware/ovirt server-info models.
    from resourcehandlers.ovirt.models import OVirtServerInfo

    if not ocp_vm_uid:
        raise ValueError(
            "The importer did not return the OpenShift VM UID, so "
            "resource_handler_svr_id cannot be set. Re-registration aborted "
            "to avoid leaving the server matchable by vCenter sync."
        )

    set_progress(
        f"Re-registering CloudBolt server '{server.hostname}' onto OpenShift "
        f"environment '{env.name}'..."
    )

    # Ensure the OpenShift networks exist as CloudBolt ResourceNetworks so the
    # reconciler can attach the VM's NIC to a known network instead of leaving
    # it unresolved. Non-fatal: a failed subnet sync only costs NIC network
    # attribution, not the re-home itself.
    try:
        rh.sync_subnets(env)
    except Exception as exc:
        logger.warning(
            "sync_subnets(%s) failed; NIC networks may not resolve: %s",
            env.name, exc,
        )

    # -- Step 1: identity fields, saved together. resource_handler_svr_id MUST
    # be the KubeVirt uid here (not the stale vCenter UUID) or ServerUpdater
    # asserts. allow_overquota lets the environment move succeed even if the
    # target env/group quota is tight -- this mirrors the VM sync job.
    # refresh_info() looks the VM up in OpenShift by server.hostname, so the
    # OpenShift VM name must match it. The importer names the VM from
    # server.hostname, so we do NOT rewrite the hostname here -- we only warn if
    # K8s-name sanitisation made the two diverge (which would surface as a
    # refresh failure -> WARNING rather than silent corruption).
    if ocp_vm_name and ocp_vm_name != server.hostname:
        logger.warning(
            "OpenShift VM name '%s' differs from server.hostname '%s' (likely "
            "K8s-name sanitisation); refresh_info() may not find the VM.",
            ocp_vm_name, server.hostname,
        )
    server.resource_handler = rh
    server.environment = env
    server.resource_handler_svr_id = ocp_vm_uid
    server.save(allow_overquota=True)

    # -- Step 2: tech-specific ServerInfo. Create the OVirt one up front (day-2
    # disk operations read server.ovirtserverinfo.namespace with no fallback),
    # and delete the stale VMware one so a future vCenter sync cannot re-match
    # this record by its moid.
    OVirtServerInfo.objects.update_or_create(
        server=server, defaults={"namespace": namespace}
    )
    try:
        from resourcehandlers.vmware.models import VmwareServerInfo
        VmwareServerInfo.objects.filter(server=server).delete()
    except Exception as exc:
        logger.warning(
            "Could not remove stale VMware ServerInfo for %s: %s",
            server.hostname, exc,
        )

    # -- Step 3: let the platform reconcile everything derived from the live VM
    # (power/IP/MAC, CPU/memory/disk, OVirtServerInfo fields + tags, OVirtDisk
    # rows replacing the VMware disks, NICs) and write a MODIFICATION history
    # event. NB: refresh_info() calls save() WITHOUT allow_overquota; because
    # CPU/memory were derived from this same server at import time, there is no
    # quota delta here in the normal case.
    server.refresh_info()
    logger.info(
        "Server %s (ID %s) re-registered onto OpenShift env '%s' "
        "(namespace '%s', svr_id %s).",
        server.hostname, server.id, env.name, namespace, ocp_vm_uid,
    )

    # -- Step 4: apply CloudBolt's tags to the OpenShift VM via the handler's
    # out-of-band tagging (group, owner, cost-center, ...), now that the server
    # is re-homed. This replaces the tags/annotations the importer used to stamp
    # by hand. Best-effort: a tagging failure must not fail an otherwise-complete
    # migration.
    try:
        server.resource_handler.cast().update_tags(server, deleted_parameters=None)
        logger.info("Applied CloudBolt tags to OpenShift VM %s.", server.hostname)
    except Exception as exc:
        logger.warning("update_tags failed for %s after re-registration: %s",
                       server.hostname, exc)


def _rm_scratch(manifest_dir):
    """Remove the appliance scratch dir (converted disk, manifest, virt-v2v
    XML) for this migration. Called on every job outcome -- success or failure
    -- so migration files are never left behind on the appliance.
    """
    try:
        shutil.rmtree(manifest_dir)
        logger.info("Removed migration scratch dir %s", manifest_dir)
    except OSError as exc:
        logger.warning("Could not remove scratch dir %s: %s", manifest_dir, exc)


# ------------------------------------------------------------------
# Main entry point
# ------------------------------------------------------------------

def run(job, **kwargs):
    server = job.server_set.last()
    if not server:
        return "FAILURE", "No server found on this job.", ""

    env_id = int("{{ env_id }}")
    storage_class = "{{ storage_class }}" or None
    run_strategy = "{{ run_strategy }}"
    start_vm = "{{ start_vm }}" == "True"
    network_name = "{{ network_name }}" or None

    # CPU and memory are taken from the CloudBolt Server being migrated, not
    # prompted. server.mem_size is a Decimal in GB; the importer wants MiB.
    # Fall back to the manifest (source vCenter values) if either is unset.
    cpu_count = server.cpu_cnt or None
    memory_mb = int(round(float(server.mem_size) * 1024)) if server.mem_size else None

    # -- Resolve the target OpenShift environment up front so we fail before
    # touching the source VM. Namespace is a 1:1 property of the CloudBolt
    # environment (env.ovirt_namespace), not a user-selected input.
    try:
        env = Environment.objects.get(id=env_id)
    except Environment.DoesNotExist:
        return "FAILURE", f"Environment {env_id} not found.", ""
    rh = env.resource_handler.cast()
    if not isinstance(rh, OVirtHandler):
        return "FAILURE", "Selected environment is not an OpenShift handler.", ""
    namespace = env.ovirt_namespace
    if not namespace:
        return "FAILURE", (
            f"Environment '{env.name}' has no OpenShift namespace "
            f"(ovirt_namespace) configured."
        ), ""

    # -- Phase 1: cold export via vCenter:443 (HttpNfcLease) ----------
    # No warm/incremental: the CDI upload path has no checkpoint/consolidation,
    # and this export goes through vCenter on 443 only (no VDDK, no ESXi:902).
    set_progress(f"Cold-exporting {server.hostname} from vCenter (443-only)...")
    logger.info("Cold export initiated for server %s (ID: %s)",
                server.hostname, server.id)

    try:
        manifest_dir = cold_export_via_nfc(server)
    except Exception as exc:
        logger.exception("Cold export failed for %s", server.hostname)
        return "FAILURE", f"Cold export failed: {exc}", ""

    set_progress(
        f"Cold export complete. Uploading VM into OpenShift namespace "
        f"'{namespace}'..."
    )

    # -- Phase 2: import into OpenShift --------------------------------
    try:
        importer_kwargs = {}
        if storage_class:
            importer_kwargs["storage_class"] = storage_class

        importer = OpenShiftImporter.from_handler(
            rh, namespace=namespace, **importer_kwargs
        )

        network_map = None
        if network_name:
            manifest_path = os.path.join(manifest_dir, "manifest.json")
            with open(manifest_path, "r") as f:
                manifest = json.load(f)
            nics = manifest.get("vm", {}).get("nics", [])
            if nics:
                network_map = {
                    nic.get("network_name", ""): network_name
                    for nic in nics
                }
            else:
                network_map = {"": network_name}

        summary = importer.import_vm(
            manifest_dir=manifest_dir,
            vm_name=server.hostname,
            network_map=network_map,
            cpu_count=cpu_count,
            memory_mb=memory_mb,
            run_strategy=run_strategy,
            start_vm=start_vm,
        )
    except Exception as exc:
        logger.exception("OpenShift import failed for %s", server.hostname)
        _rm_scratch(manifest_dir)
        return "FAILURE", f"OpenShift import failed: {exc}", ""

    ocp_vm_name = summary.get("vm_name", server.hostname)
    ocp_vm_uid = summary.get("uid") or ""
    msg = (
        f"VM '{ocp_vm_name}' successfully imported into "
        f"OpenShift namespace '{namespace}'."
    )
    if summary.get("started"):
        guest_ip = summary.get("guest_ip", "pending")
        msg += f" VM is running (IP: {guest_ip})."

    # -- Phase 3: retire the source vCenter VM -----------------------
    # The disks are now in OpenShift, so power off and rename the source VM
    # (-migrated). This runs BEFORE re-registration, while the server still
    # points at the VMware handler. Non-fatal: the VM is already in OpenShift,
    # so a source-cleanup failure is a warning, not a migration failure.
    source_note = ""
    try:
        new_src_name = _quiesce_source_vm(server)
        source_note = (
            f" Source vCenter VM powered off and renamed to '{new_src_name}'."
        )
    except Exception as exc:
        logger.warning("Source vCenter VM cleanup failed for %s: %s",
                       server.hostname, exc)
        source_note = f" (Source vCenter VM cleanup did not complete: {exc})"

    # -- Phase 4: re-register the CloudBolt Server object -------------
    # Re-home the same Server record onto the OpenShift environment/handler so
    # it keeps its history, jobs, and rate data but now reports as residing in
    # OpenShift. The import (the expensive, hard-to-reverse part) has already
    # succeeded, so a re-registration failure is reported as a WARNING with
    # remediation guidance rather than failing the whole migration.
    try:
        _reregister_server(server, rh, env, namespace, ocp_vm_name, ocp_vm_uid)
    except Exception as exc:
        logger.exception("Re-registration failed for %s", server.hostname)
        warn = (
            f"{msg}{source_note} However, the VM was imported but the CloudBolt "
            f"server record could NOT be re-registered onto OpenShift: {exc}. "
            f"The VM is live in OpenShift; reconcile the record by setting its "
            f"resource handler, environment, and resource-handler VM ID "
            f"(the KubeVirt VM UID), then run Refresh Info."
        )
        set_progress(warn)
        _rm_scratch(manifest_dir)
        return "WARNING", warn, str(exc)

    msg += source_note
    msg += (
        f" CloudBolt server record re-registered onto environment "
        f"'{env.name}'; history preserved."
    )
    set_progress(msg)
    logger.info(msg)
    _rm_scratch(manifest_dir)
    return "SUCCESS", msg, ""
