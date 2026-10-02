"""
vm_adoption -- adopt VMs that Terraform created as CloudBolt Server records
under the Resource that owns the deployment.

HCP Terraform runs remotely, so CloudBolt never reads the state file. The
contract is an OUTPUT instead: the Terraform module emits ``cloudbolt_vm_ids``,
a list(string) of provider VM identifiers (a single-string ``cloudbolt_vm_id``
or ``vm_id`` output is accepted too). For each identifier this module asks the
resource handler of the deployment's CloudBolt Environment for the VM, then
runs the SAME platform code the Sync VMs job runs (ServerUpdater), so the
Server record ends up as complete as a discovered one: power state, IP, NICs,
disks, OS family, size, tags, and the handler-specific info record
(AzureARMServerInfo / EC2ServerInfo / VmwareServerInfo) that every later
refresh and day-2 action depends on.

Identifier accepted per handler type (the provider is implied by the
Environment's handler; the id's shape is checked against it):

  Azure   the ARM resource ID -- ``azurerm_linux_virtual_machine.<x>.id`` or
          ``azurerm_windows_virtual_machine.<x>.id``. It carries the resource
          group and VM name the handler needs to fetch the VM; the record's
          resource_handler_svr_id becomes the VM's GUID, as Sync VMs sets it.
  AWS     ``aws_instance.<x>.id`` (``i-...``) or ``aws_instance.<x>.arn``. The
          region comes from the ARN when given, else from the Environment's
          aws_region parameter.
  VMware  ``vsphere_virtual_machine.<x>.moid`` (``vm-123``; preferred, CloudBolt
          keys vCenter servers on the managed object ID) or ``.id`` / ``.uuid``
          (the BIOS UUID the vsphere provider uses as the resource id, which
          CloudBolt stores as resource_handler_svr_id).

Every adopted server is flagged with the platform's own marker for
Terraform-created servers: the ``created_by_terraform`` boolean custom field
(seeded by CloudBolt, shown on servers) plus the "Created By Terraform" tag,
so one boolean governs how such servers behave (conditional-display plugins,
reports). The flag is set on EVERY adoption -- including a server the Sync VMs
job created first, which this module only re-parents -- so it is the single
source of truth. Servers without the flag are never touched by this module.

Reconcile semantics (day-2): adopting a list also retires, as HISTORICAL, every
Terraform-flagged server of the same Resource and handler whose id is no longer
in the list -- a replaced VM ("forces replacement") gets a new record and its
old one is closed. Retirement is skipped when any id failed to resolve, so a
transient lookup error never closes a live VM's record. A missing
``cloudbolt_vm_ids`` output means "no contract": nothing is adopted and nothing
is retired.

Teardown: after a successful destroy run, ``retire_servers`` marks the
Resource's Terraform-flagged servers HISTORICAL so CloudBolt's Delete Resource
job spawns no decommission jobs against VMs Terraform already deleted (the
decom path would power off and delete through the handler).

This module never calls HCP Terraform. It talks to CloudBolt and to the
resource handlers only through CloudBolt's own handler methods.

Platform code this mirrors (read before changing a lookup):
  cbhooks/hookmodules/output_parsers/terraform.py -- the Terraform Operation
      output parser: Server.objects.get_or_create keyed on
      resource_handler_svr_id, created_by_terraform, tech details, refresh.
  jobengine/jobmodules/syncvmsjob.py -- ServerUpdater (hydrates a record from
      a handler vm dict) and find_server_obj (how an existing record is matched).
"""

import ast
import json
import re

from django.db.models import Q

from utilities.logger import ThreadLogger

logger = ThreadLogger(__name__)

# The output contract. VM_IDS_OUTPUT is a list(string); VM_ID_OUTPUTS are
# single-string fallbacks, checked in order.
VM_IDS_OUTPUT = "cloudbolt_vm_ids"
VM_ID_OUTPUTS = ("cloudbolt_vm_id", "vm_id")

# CloudBolt's own marker for Terraform-created servers: a BOOL custom field
# seeded by initialize/cb_minimal.py and the tag the Terraform Plan action adds.
CREATED_BY_TERRAFORM_FIELD = "created_by_terraform"
CREATED_BY_TERRAFORM_LABEL = "Created By Terraform"
CREATED_BY_TERRAFORM_TAG = "Created By Terraform"

# Custom fields a deployment may record its Environment in, checked in order.
RESOURCE_ENV_FIELDS = ("tfc_env_id", "env_id", "environment_id")

# Terraform resource types that are VMs on the supported handlers, for the
# "VMs exist but no output" hint built from HCP's workspace-resources listing.
TERRAFORM_VM_RESOURCE_TYPES = (
    "azurerm_linux_virtual_machine",
    "azurerm_windows_virtual_machine",
    "azurerm_virtual_machine",
    "aws_instance",
    "vsphere_virtual_machine",
)

# Docs: https://learn.microsoft.com/en-us/azure/azure-resource-manager/management/resource-name-rules#microsoftcompute
AZURE_VM_ID_RE = re.compile(
    r"^/subscriptions/(?P<subscription>[^/]+)/resourceGroups/(?P<resource_group>[^/]+)"
    r"/providers/Microsoft\.Compute/virtualMachines/(?P<name>[^/]+)/?$",
    re.IGNORECASE,
)
# Docs: https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/resource-ids.html
AWS_INSTANCE_ID_RE = re.compile(r"^i-[0-9a-f]{8,17}$")
# Docs: https://docs.aws.amazon.com/service-authorization/latest/reference/list_amazonec2.html#amazonec2-resources-for-iam-policies
AWS_INSTANCE_ARN_RE = re.compile(
    r"^arn:(?P<partition>aws[a-z-]*):ec2:(?P<region>[a-z0-9-]+):(?P<account>\d*)"
    r":instance/(?P<instance_id>i-[0-9a-f]{8,17})$"
)
VSPHERE_MOID_RE = re.compile(r"^vm-\d+$")
UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE
)


class VMAdoptionError(Exception):
    """A VM id could not be resolved to a VM on the deployment's handler, or
    the handler type is unsupported. Messages are operator-actionable."""


# =============================================================================
# == Output parsing ===========================================================
# =============================================================================

def _normalize_ids(value):
    """Coerce an output value to an ordered, de-duplicated list of id strings.
    Accepts a list, a JSON or Python-repr list string (custom fields store
    str(list)), or a comma/whitespace-separated string."""
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        items = list(value)
    else:
        text = str(value).strip()
        if not text:
            return []
        items = None
        if text[0] in "[(":
            for loader in (json.loads, ast.literal_eval):
                try:
                    loaded = loader(text)
                except (ValueError, SyntaxError):
                    continue
                if isinstance(loaded, (list, tuple)):
                    items = list(loaded)
                    break
        if items is None:
            items = re.split(r"[,\s]+", text)
    ids = []
    for item in items:
        if item is None:
            continue
        text = str(item).strip().strip("'\"")
        if text and text not in ids:
            ids.append(text)
    return ids


def vm_ids_from_outputs(outputs):
    """
    The VM ids a deployment's Terraform outputs declare, as
    ``(ids, source_output_name)``. ``source_output_name`` is None when the
    outputs carry no contract at all (no cloudbolt_vm_ids / cloudbolt_vm_id /
    vm_id output); an empty list with a source means the deployment declares
    it manages NO VMs.
    """
    outputs = outputs or {}
    if VM_IDS_OUTPUT in outputs:
        return _normalize_ids(outputs.get(VM_IDS_OUTPUT)), VM_IDS_OUTPUT
    for name in VM_ID_OUTPUTS:
        # A sensitive-marked output arrives as None; that is not a contract.
        if outputs.get(name) not in (None, ""):
            return _normalize_ids(outputs.get(name)), name
    return [], None


def vm_resource_addresses(workspace_resources):
    """
    Addresses of the VM-type resources in an HCP workspace-resources listing
    (TFCClient.list_workspace_resources: attributes.address /
    attributes.provider-type). Used to tell a module author that Terraform
    created VMs the module exposes no cloudbolt_vm_ids output for.
    """
    addresses = []
    for item in workspace_resources or []:
        attributes = item.get("attributes", {}) or {}
        if attributes.get("provider-type") in TERRAFORM_VM_RESOURCE_TYPES:
            addresses.append(attributes.get("address") or attributes.get("name") or "?")
    return addresses


# =============================================================================
# == Results ==================================================================
# =============================================================================

class AdoptionResult(object):
    """What one adoption pass did. ``status`` is WARNING when any id failed;
    the infrastructure exists regardless, so callers never FAIL a job on it."""

    def __init__(self, source_output=None):
        self.source_output = source_output
        self.created = []   # Server records created
        self.updated = []   # pre-existing Server records linked / refreshed
        self.retired = []   # Terraform-flagged records closed as HISTORICAL
        self.failed = []    # (vm_id, message)

    @property
    def servers(self):
        return self.created + self.updated

    @property
    def status(self):
        return "WARNING" if self.failed else "SUCCESS"

    def summary(self):
        def names(servers):
            return ", ".join(sorted(server.hostname for server in servers))

        parts = []
        if self.created:
            parts.append("{} server record(s) created: {}".format(
                len(self.created), names(self.created)))
        if self.updated:
            parts.append("{} existing server record(s) linked: {}".format(
                len(self.updated), names(self.updated)))
        if self.retired:
            parts.append("{} server record(s) retired: {}".format(
                len(self.retired), names(self.retired)))
        if self.failed:
            parts.append("{} VM id(s) from output '{}' could not be adopted -- {}".format(
                len(self.failed), self.source_output,
                "; ".join("{}: {}".format(vm_id, message) for vm_id, message in self.failed),
            ))
        if not parts:
            return "Output '{}' lists no VMs; no server records to adopt.".format(
                self.source_output)
        return ". ".join(parts) + "."


def outcome(result, no_contract_note=""):
    """
    ``(status, note)`` for a plugin's return tuple from an adoption result;
    the note starts with a space so it appends to a message. ``result`` None
    means the outputs carried no contract: SUCCESS plus ``no_contract_note``
    (build plugins pass missing_output_note(); day-2 actions pass nothing,
    since repeating the hint on every run would be noise).
    """
    if result is None:
        return "SUCCESS", no_contract_note or ""
    return result.status, " " + result.summary()


def missing_output_note(workspace_resources):
    """
    For a build plugin whose outputs carry no contract: a sentence naming the
    output to add and, when HCP's workspace-resources listing shows VM-type
    resources, the addresses Terraform created, so the module author knows
    what to output. The listing carries no state values, hence no secrets.
    """
    addresses = vm_resource_addresses(workspace_resources)
    note = (
        " No '{}' output in the Terraform state, so no CloudBolt server records "
        "were created.".format(VM_IDS_OUTPUT)
    )
    if addresses:
        note += (
            " Terraform created {} VM resource(s) ({}); add a {} output listing "
            "their ids to get server records.".format(
                len(addresses), ", ".join(addresses), VM_IDS_OUTPUT)
        )
    return note


def retired_note(servers):
    """A sentence (leading space) naming the retired servers, or '' when none."""
    if not servers:
        return ""
    return " {} CloudBolt server record(s) retired: {}.".format(
        len(servers), ", ".join(sorted(server.hostname for server in servers)))


# =============================================================================
# == CloudBolt lookups ========================================================
# =============================================================================

def resource_environment(resource):
    """
    The CloudBolt Environment a deployment was ordered into, from the first
    RESOURCE_ENV_FIELDS custom field holding a value (numeric id or ENV-
    global id). None when the resource records none or it no longer exists.
    """
    from infrastructure.models import Environment

    for field_name in RESOURCE_ENV_FIELDS:
        value = resource.get_value_for_custom_field(field_name)
        text = str(value or "").strip()
        if not text:
            continue
        if text.isdigit():
            env = Environment.objects.filter(id=int(text)).first()
        else:
            env = Environment.objects.filter(global_id=text).first()
        if env is not None:
            return env
    return None


def is_terraform_managed(server):
    """True when the server carries CloudBolt's created_by_terraform flag."""
    return bool(server.get_value_for_custom_field(CREATED_BY_TERRAFORM_FIELD))


def _ensure_marker_field():
    """The created_by_terraform custom field as CloudBolt seeds it
    (initialize/cb_minimal.py); recreated here only if an admin removed it."""
    from c2_wrapper import create_custom_field

    create_custom_field(
        CREATED_BY_TERRAFORM_FIELD,
        CREATED_BY_TERRAFORM_LABEL,
        "BOOL",
        show_on_servers=True,
        description=(
            "Used as a way to identify servers that were created from "
            "Terraform Plans within CloudBolt."
        ),
    )


def _mark_terraform_managed(server):
    """Set the created_by_terraform flag and tag on a SAVED server (the flag
    is a custom-field value, so the record must exist first)."""
    from tags.models import CloudBoltTag

    _ensure_marker_field()
    setattr(server, CREATED_BY_TERRAFORM_FIELD, True)
    tag, _ = CloudBoltTag.objects.get_or_create(name=CREATED_BY_TERRAFORM_TAG)
    server.tags.add(tag)
    server.save()


def _find_server(rh, uuid, moid=None):
    """
    The existing Server record for this VM, or None. Mirrors
    syncvmsjob.find_server_obj: match on resource_handler_svr_id, skip
    records still provisioning, and disambiguate vCenter's non-unique BIOS
    UUIDs by managed object ID. Prefers a record on the same handler.
    """
    from infrastructure.models import Server

    candidates = Server.objects.filter(resource_handler_svr_id=uuid).exclude(status="PROV")
    if moid:
        candidates = candidates.filter(
            Q(vmwareserverinfo__moid=moid) | Q(vmwareserverinfo__moid__isnull=True)
        )
    candidates = list(candidates)
    same_handler = [server for server in candidates if server.resource_handler_id == rh.id]
    candidates = same_handler or candidates
    if len(candidates) > 1:
        raise VMAdoptionError(
            "{} CloudBolt server records already carry id '{}' ({}); resolve "
            "the duplicate before adopting.".format(
                len(candidates), uuid,
                ", ".join(server.hostname for server in candidates),
            )
        )
    return candidates[0] if candidates else None


def _refresh(server, vm, created, rh):
    """Hydrate the record from the handler's vm dict exactly as Sync VMs does
    (power, IP, NICs, disks, OS family, size, tags, tech-specific info)."""
    from jobengine.jobmodules.syncvmsjob import ServerUpdater
    from utilities import events

    updater = ServerUpdater(server, vm, created, rh)
    updater.update()
    server.save()
    messages = [
        message.decode() if isinstance(message, bytes) else message
        for message in (updater.get_change_msgs() or [])
    ]
    if created:
        # ONBOARD is CloudBolt's "Server Assimilation Event" (history.models).
        events.add_server_event(
            "ONBOARD", server,
            "Server record created from a Terraform-managed VM (adopted by "
            "CloudBolt from the deployment's Terraform outputs).",
        )
    elif messages:
        events.add_server_event("MODIFICATION", server, "\n".join(messages))


def _adopt_one(resource, environment, rh, vm):
    """Get-or-create the Server for one handler vm dict, parent it to the
    resource, flag it, and hydrate it. Returns ``(server, created)``."""
    from infrastructure.models import Server

    uuid = str(vm.get("uuid") or "").strip()
    if not uuid:
        raise VMAdoptionError(
            "the resource handler returned no unique id for the VM; cannot "
            "key a server record on it"
        )
    hostname = str(vm.get("hostname") or "").strip() or uuid

    server = _find_server(rh, uuid, vm.get("moid"))
    created = server is None
    if created:
        server = Server(
            hostname=hostname,
            resource_handler_svr_id=uuid,
            resource_handler=rh,
            environment=environment,
            group=resource.group,
            owner=resource.owner,
            resource=resource,
        )
        server.save()
    else:
        # Re-parent a record Sync VMs (or an earlier adoption) created: the
        # deployment owns it now. Keep an owner or environment an admin set.
        server.resource = resource
        server.group = resource.group
        if server.owner is None:
            server.owner = resource.owner
        if server.resource_handler_id is None:
            server.resource_handler = rh
        if server.environment_id is None or server.environment.is_unassigned:
            server.environment = environment
        if server.status == "HISTORICAL":
            server.status = "ACTIVE"
        server.save()

    _mark_terraform_managed(server)
    _refresh(server, vm, created, rh)
    return server, created


def _retire(server, reason):
    """Close a record whose VM Terraform destroyed or dropped: HISTORICAL,
    powered off, with a decommission event. Nothing is touched on the
    handler -- Terraform owns the VM's lifecycle."""
    from utilities import events

    server.status = "HISTORICAL"
    server.power_status = "POWEROFF"
    server.save()
    events.add_server_event("DECOMMISSION", server, reason)
    logger.info("Retired server %s (%s): %s", server.hostname, server.global_id, reason)


def _retire_stale(resource, rh, keep_uuids, notify):
    """Retire the resource's Terraform-flagged servers on this handler whose
    id is not in ``keep_uuids``."""
    stale = (
        resource.server_set.filter(resource_handler=rh)
        .exclude(status="HISTORICAL")
        .exclude(resource_handler_svr_id__in=list(keep_uuids))
    )
    retired = []
    for server in stale:
        if not is_terraform_managed(server):
            continue
        _retire(
            server,
            "The VM is no longer among the deployment's Terraform outputs "
            "({}); Terraform replaced or destroyed it.".format(VM_IDS_OUTPUT),
        )
        notify("Retired server record '{}' (VM no longer in the deployment).".format(
            server.hostname))
        retired.append(server)
    return retired


# =============================================================================
# == Handler adapters =========================================================
# =============================================================================

class _AzureAdapter(object):
    """AzureARMHandler: ARM resource ID -> handler vm dict."""

    label = "Azure"

    def fetch(self, rh, environment, vm_id):
        match = AZURE_VM_ID_RE.match(vm_id)
        if not match:
            raise VMAdoptionError(
                "'{}' is not an Azure VM resource ID (/subscriptions/<sub>/"
                "resourceGroups/<rg>/providers/Microsoft.Compute/virtualMachines/"
                "<name>); output the azurerm_linux_virtual_machine / "
                "azurerm_windows_virtual_machine resource's 'id' attribute".format(vm_id)
            )
        handler_subscription = (getattr(rh, "serviceaccount", "") or "").strip()
        if handler_subscription and match.group("subscription").lower() != handler_subscription.lower():
            raise VMAdoptionError(
                "the VM is in subscription {} but environment '{}' is backed by a "
                "handler for subscription {}".format(
                    match.group("subscription"), environment.name, handler_subscription)
            )
        name = match.group("name")
        resource_group = match.group("resource_group")
        try:
            # Same call the handler's own get_vm_dict makes (resourcehandlers/
            # azure_arm/models.py); returns the Sync VMs dict shape, uuid = vm_id.
            vm = rh.get_api_wrapper().convert_vm_to_dict(name, resource_group)
        except Exception as exc:  # azure.core ResourceNotFoundError, auth errors, ...
            raise VMAdoptionError(
                "Azure returned an error fetching VM '{}' in resource group '{}' "
                "through handler '{}': {}".format(name, resource_group, rh.name, exc)
            )
        if not vm:
            raise VMAdoptionError(
                "VM '{}' was not found in resource group '{}'".format(name, resource_group))
        vm.update(rh.get_size_breakdown_dict(
            size=vm.get("node_size"), location_name=vm.get("location")) or {})
        return vm


class _AWSAdapter(object):
    """AWSHandler: instance id or ARN -> handler vm dict."""

    label = "AWS"

    def fetch(self, rh, environment, vm_id):
        region = None
        arn = AWS_INSTANCE_ARN_RE.match(vm_id)
        if arn:
            instance_id = arn.group("instance_id")
            region = arn.group("region")
        elif AWS_INSTANCE_ID_RE.match(vm_id):
            instance_id = vm_id
        else:
            raise VMAdoptionError(
                "'{}' is not an EC2 instance id (i-...) or instance ARN; output the "
                "aws_instance resource's 'id' or 'arn' attribute".format(vm_id)
            )
        region = region or rh.get_env_region(environment)
        if not region:
            raise VMAdoptionError(
                "environment '{}' has no aws_region, so instance {} cannot be looked "
                "up; output the instance ARN (which carries the region) instead".format(
                    environment.name, instance_id)
            )
        try:
            # The handler's own single-VM path (get_vm_dict) is this call; it
            # drops instances in VPCs the handler does not manage.
            vms = rh.get_all_vms(region_names=[region], instance_ids=[instance_id])
        except Exception as exc:  # CloudBoltException wrapping a ClientError
            raise VMAdoptionError(
                "AWS returned an error fetching instance {} in {} through handler "
                "'{}': {}".format(instance_id, region, rh.name, exc)
            )
        if not vms:
            raise VMAdoptionError(
                "instance {} was not found in region {}, or its VPC is not managed "
                "by handler '{}'".format(instance_id, region, rh.name)
            )
        return vms[0]


class _VMwareAdapter(object):
    """VsphereResourceHandler: managed object ID or BIOS UUID -> handler vm dict."""

    label = "VMware vCenter"

    def fetch(self, rh, environment, vm_id):
        from resourcehandlers.vmware import pyvmomi_wrapper

        wrapper = rh.get_api_wrapper()
        try:
            # The wrapper's public methods open their vCenter session through
            # this same call (resourcehandlers/vmware/vmware_41.py get_vm_dict).
            si = wrapper._get_connection()
            if VSPHERE_MOID_RE.match(vm_id):
                vm_obj = pyvmomi_wrapper.get_vm_by_moid(si, vm_id)
            elif UUID_RE.match(vm_id):
                vm_obj = pyvmomi_wrapper.get_vm_by_uuid(si, vm_id)
            else:
                raise VMAdoptionError(
                    "'{}' is not a vSphere managed object id (vm-123) or VM UUID; "
                    "output the vsphere_virtual_machine resource's 'moid' (preferred) "
                    "or 'id' attribute".format(vm_id)
                )
            vm = pyvmomi_wrapper.get_vm_details(vm_obj)
        except VMAdoptionError:
            raise
        except Exception as exc:  # NotFoundException, session errors, ...
            raise VMAdoptionError(
                "vCenter returned an error fetching VM '{}' through handler '{}': "
                "{}".format(vm_id, rh.name, exc)
            )
        if "guest_os" in vm:
            vm["os_family"] = wrapper.guest_id_to_os_family(vm["guest_os"])
        return vm


def _adapter_for(rh):
    from resourcehandlers.aws.models import AWSHandler
    from resourcehandlers.azure_arm.models import AzureARMHandler
    from resourcehandlers.vmware.models import VsphereResourceHandler

    for handler_class, adapter in (
        (AzureARMHandler, _AzureAdapter()),
        (AWSHandler, _AWSAdapter()),
        (VsphereResourceHandler, _VMwareAdapter()),
    ):
        if isinstance(rh, handler_class):
            return adapter
    raise VMAdoptionError(
        "resource handler '{}' ({}) is not supported for server adoption; "
        "supported handlers: Azure, AWS, VMware vCenter".format(
            rh.name, type(rh).__name__)
    )


# =============================================================================
# == Entry points =============================================================
# =============================================================================

def adopt_servers(resource, environment, vm_ids, progress=None, source_output=None):
    """
    Adopt ``vm_ids`` (provider VM identifiers, see the module docstring) as
    Server records under ``resource`` through ``environment``'s handler, then
    retire the resource's Terraform-flagged servers on that handler that are
    not in the list (only when every id resolved). ``progress`` is an optional
    callable taking a message (set_progress). Returns an AdoptionResult; an
    unsupported handler or an environment without one raises VMAdoptionError.
    """
    notify = progress or (lambda message: None)
    result = AdoptionResult(source_output)
    if environment is None or not environment.resource_handler_id:
        raise VMAdoptionError(
            "environment {} has no resource handler to look VMs up through".format(
                getattr(environment, "name", "?"))
        )
    rh = environment.resource_handler.cast()
    adapter = _adapter_for(rh)

    adopted_uuids = set()
    for vm_id in vm_ids:
        vm_id = str(vm_id).strip()
        try:
            vm = adapter.fetch(rh, environment, vm_id)
            server, created = _adopt_one(resource, environment, rh, vm)
        except VMAdoptionError as exc:
            logger.warning("Could not adopt VM '%s' for resource %s: %s",
                           vm_id, resource.global_id, exc)
            result.failed.append((vm_id, str(exc)))
            notify("Could not adopt VM '{}': {}".format(vm_id, exc))
            continue
        except Exception as exc:
            logger.exception("Unexpected error adopting VM '%s' for resource %s",
                             vm_id, resource.global_id)
            result.failed.append((vm_id, "{}: {}".format(type(exc).__name__, exc)))
            notify("Could not adopt VM '{}': {}".format(vm_id, exc))
            continue
        adopted_uuids.add(server.resource_handler_svr_id)
        (result.created if created else result.updated).append(server)
        notify("{} server record '{}' ({}) for {} VM {}.".format(
            "Created" if created else "Linked", server.hostname, server.global_id,
            adapter.label, vm_id))

    if not result.failed:
        result.retired = _retire_stale(resource, rh, adopted_uuids, notify)
    return result


def adopt_from_outputs(resource, environment, outputs, progress=None):
    """
    Adopt the VMs a deployment's Terraform outputs declare. Returns None when
    the outputs carry no contract (no cloudbolt_vm_ids / cloudbolt_vm_id /
    vm_id output) -- callers then record nothing and retire nothing. Every
    other problem (no environment, unsupported handler, unresolvable ids)
    comes back as a WARNING-status AdoptionResult; never raises.
    """
    vm_ids, source = vm_ids_from_outputs(outputs)
    if source is None:
        return None
    notify = progress or (lambda message: None)
    if environment is None:
        result = AdoptionResult(source)
        result.failed.append((
            ", ".join(vm_ids) or "(none)",
            "the deployment records no CloudBolt Environment ({}), so no resource "
            "handler can look the VMs up".format(" / ".join(RESOURCE_ENV_FIELDS)),
        ))
        return result
    notify("Adopting {} VM(s) listed in output '{}' through environment '{}'...".format(
        len(vm_ids), source, environment.name))
    try:
        return adopt_servers(resource, environment, vm_ids, progress=progress,
                             source_output=source)
    except VMAdoptionError as exc:
        result = AdoptionResult(source)
        result.failed.append((", ".join(vm_ids) or "(none)", str(exc)))
        return result


def retire_servers(resource, reason, progress=None):
    """
    Teardown: mark the resource's Terraform-flagged, non-historical servers
    HISTORICAL (``reason`` becomes their decommission event) so the Delete
    Resource job creates no decommission jobs for VMs Terraform destroyed.
    Servers without the created_by_terraform flag are left to CloudBolt's
    normal decommission path. Returns the retired servers.
    """
    notify = progress or (lambda message: None)
    retired = []
    for server in resource.server_set.exclude(status="HISTORICAL"):
        if not is_terraform_managed(server):
            continue
        _retire(server, reason)
        notify("Retired server record '{}' ({}).".format(server.hostname, server.global_id))
        retired.append(server)
    return retired
