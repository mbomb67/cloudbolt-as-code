"""
CloudBolt Day-2 resource action: Deploy Latest Version (HCP Terraform no-code).

Upgrades a no-code deployment -- the "HCP Terraform No-Code Module" blueprint
(BP-00meiwwz), or any blueprint whose build plugin records
tfc_nocode_module_id -- to the module version HCP Terraform currently pins as
no-code ready, through HCP's workspace-upgrade API and the same human
plan-approval pause every other change in this repo goes through. All TFC
REST access goes through the tfc_api shared module (shared_modules/SHM-jlguerjr).

What "latest" means: the version pinned on the no-code module in HCP
Terraform (GET /no-code-modules/:id -> version-pin). HCP owns the pin; moving
it is an admin step in HCP (docs/hcp-no-code-setup.md section 9). This action
moves an existing workspace to that pin.

Idempotent and safe to re-run. Before anything is written it reads the
workspace's current module version (the workspace's source-module-id) and
HCP's own no-code-upgrade-available flag, and the module's version-pin. A
workspace already on the pinned version returns SUCCESS with no run created;
a workspace that is not a no-code deployment (no module recorded, or the
workspace reports no module source) returns WARNING and is skipped; a
workspace with a non-final run returns FAILURE for that target without
touching it (the day-2 fail-fast rule, same as Terraform Update).

Bulk-safe. CloudBolt builds a hook job's kwargs from HookParameters.as_dict()
(CloudBolt source, jobs/models.py): ``resources`` and ``servers`` are ALWAYS
passed (querysets, possibly empty) and the singular ``resource`` / ``server``
only when exactly one target was selected. A bulk run from the resource list
view, or an API/MCP call naming several resources, therefore arrives as ONE
job carrying several resources -- so this plugin iterates every target it was
handed, upgrades each independently (a failure on one does not stop the
others), and returns the worst status with a per-target summary. A server
action pointed at this plugin maps each server to its owning Resource.
Each target that needs an upgrade pauses the job once for its own plan
approval, in order.

Flow per target:
  read coordinates -> get client -> read workspace + module -> decide
  (already current / not no-code / needs upgrade) -> concurrency guard
  (non-final runs -> FAILURE) -> initiate the upgrade (HCP creates a
  configuration version from the pinned module version and queues a run;
  the upgrade ID is the run ID) -> adopt that run into the plan-approval
  pause -> 'Continue Job' confirms it through the documented upgrade
  confirmation endpoint; canceling the job discards the run (nothing to
  revert: no variables were written) -> on apply, re-read the workspace,
  record tfc_nocode_module_version / tfc_run_id / tfc_run_url, refresh every
  tfc_output_* from the applied state, and rename the resource if the
  name/vm_name output changed.

New required input variables: if the pinned version adds a variable with no
default, the plan errors and the job fails with the run URL. Set the variable
on the workspace in HCP Terraform (Variables tab) and run this action again;
Terraform Update cannot add variables a deployment does not already manage.

Approval/reject ownership: the shared engine owns the reject path -- it
catches CancelJobException (a BaseException subclass), discards the run, and
re-raises. This plugin NEVER catches CancelJobException.

Returns a 3-tuple: (status, output_msg, error_msg)
  status: "SUCCESS" | "WARNING" | "FAILURE"
"""

from common.methods import set_progress
from utilities.logger import ThreadLogger

from shared_modules.tfc_api import (
    RUN_CLASS_APPLIED,
    TFCError,
    TFCRunFailedError,
    build_run_message,
    drive_run_with_plan_approval,
    ensure_output_custom_fields,
    get_client,
    workspace_module_info,
)

logger = ThreadLogger(__name__)

ACTION_NAME = "Deploy Latest Version"

# Outputs a module may use to name its deployment; the first one present wins
# (mirrors the Terraform Update plugin). A changed value renames the resource.
NAME_OUTPUTS = ("name", "vm_name")

# Worst-status aggregation for a multi-target run.
STATUS_RANK = {"SUCCESS": 0, "WARNING": 1, "FAILURE": 2}


def _field(resource, name):
    """A custom-field value on the resource as a stripped string ('' when unset)."""
    return str(resource.get_value_for_custom_field(name) or "").strip()


def _targets(job, resource=None, resources=None, server=None, servers=None):
    """Ordered, de-duplicated Resources this run acts on, plus the servers
    that carry no Resource (reported as skipped), in whichever shape CloudBolt
    delivered the targets (see the module docstring on bulk kwargs)."""
    ordered = []
    seen = set()
    orphan_servers = []

    def add(candidate):
        if candidate is None or candidate.id in seen:
            return
        seen.add(candidate.id)
        ordered.append(candidate)

    for item in list(resources or []):
        add(item)
    add(resource)
    for item in list(servers or []) + ([server] if server is not None else []):
        owner = getattr(item, "resource", None)
        if owner is None:
            if item not in orphan_servers:
                orphan_servers.append(item)
        else:
            add(owner)
    if not ordered and not orphan_servers:
        # Last resort for an unusual caller: the resource attached to the job.
        add(job.get_resource())
    return ordered, orphan_servers


def _refresh_outputs(client, resource, workspace_id):
    """Record every output of the applied state as tfc_output_* (fields created
    on the fly for outputs the new version added) and rename the resource if
    its naming output changed. Returns a note for the result message."""
    outputs = client.wait_for_outputs(workspace_id) or {}
    for output_name in ensure_output_custom_fields(sorted(outputs)):
        value = outputs.get(output_name)
        if value is not None:
            resource.set_value_for_custom_field(
                "tfc_output_{}".format(output_name), str(value)
            )
    new_name = next(
        (outputs[key] for key in NAME_OUTPUTS if outputs.get(key) is not None), None
    )
    if new_name is not None and str(new_name) != resource.name:
        note = " Resource renamed '{}' -> '{}'.".format(resource.name, new_name)
        resource.name = str(new_name)
        return note
    return ""


def _deploy_latest(job, resource):
    """Upgrade ONE deployment. Returns (status, message); raises only
    CancelJobException (through the engine). Every TFCError is converted to a
    FAILURE here so a bulk run continues with its remaining targets."""
    label = "{} ({})".format(resource.name, resource.global_id)
    workspace_id = _field(resource, "tfc_workspace_id")
    nocode_module_id = _field(resource, "tfc_nocode_module_id")
    if not workspace_id:
        return (
            "WARNING",
            "{}: no tfc_workspace_id is recorded, so there is no HCP Terraform "
            "workspace to upgrade (provisioning likely did not complete); "
            "skipped.".format(label),
        )
    if not nocode_module_id:
        return (
            "WARNING",
            "{}: not a no-code deployment (no tfc_nocode_module_id recorded); "
            "module-version upgrades do not apply to it; skipped.".format(label),
        )

    try:
        client = get_client(
            _field(resource, "tfc_organization"), _field(resource, "tfc_connection_info")
        )

        # ---- Read current state: workspace version + module pin ------------
        workspace = client.get_workspace(workspace_id)
        attributes = workspace.get("attributes", {}) or {}
        workspace_name = attributes.get("name") or _field(resource, "tfc_workspace_name") or workspace_id
        info = workspace_module_info(workspace)
        module = client.get_no_code_module(nocode_module_id)
        current_version = info["version"] or ""
        pinned_version = module["version_pin"]
        module_label = info["name"] or nocode_module_id

        # Keep the mirror honest even when nothing else happens below.
        if current_version and _field(resource, "tfc_nocode_module_version") != current_version:
            resource.set_value_for_custom_field("tfc_nocode_module_version", current_version)
            resource.save()

        if not info["is_no_code"] and info["upgrade_available"] is None:
            return (
                "WARNING",
                "{}: workspace '{}' does not report a no-code module source "
                "(it was not created from a no-code module, or this HCP "
                "Terraform edition does not expose it); nothing to upgrade; "
                "skipped.".format(label, workspace_name),
            )

        needs_upgrade = bool(info["upgrade_available"]) or (
            bool(current_version) and bool(pinned_version) and current_version != pinned_version
        )
        if not needs_upgrade:
            if not current_version and not pinned_version:
                return (
                    "WARNING",
                    "{}: neither the workspace's module version nor the module's "
                    "version pin could be read, so there is nothing to compare; "
                    "check module {} in HCP Terraform; skipped.".format(
                        label, nocode_module_id
                    ),
                )
            return (
                "SUCCESS",
                "{}: workspace '{}' already runs the pinned version of module {} "
                "(v{}); nothing to do.".format(
                    label, workspace_name, module_label, current_version or pinned_version
                ),
            )
        if not module["enabled"]:
            return (
                "FAILURE",
                "{}: module {} is pinned to v{} but no-code provisioning is "
                "disabled on it in HCP Terraform, so the upgrade cannot be "
                "initiated. Re-enable it (docs/hcp-no-code-setup.md section 4) "
                "and retry.".format(label, nocode_module_id, pinned_version or "?"),
            )

        set_progress(
            "{}: upgrading workspace '{}' from module {} v{} to the pinned v{}.".format(
                label, workspace_name, module_label, current_version or "?",
                pinned_version or "?",
            )
        )

        # ---- Concurrency guard: fail fast on non-final runs ----------------
        # Same rule as Terraform Update: a day-2 action never discards a run
        # that may still be awaiting someone's approval.
        pending_runs = client.list_non_final_runs(workspace_id)
        if pending_runs:
            descriptions = [
                "{} (status '{}')".format(
                    client.run_app_url(pending["id"], workspace_name), pending["status"]
                )
                for pending in pending_runs
            ]
            return (
                "FAILURE",
                "{}: a run is already pending on workspace '{}': {}. Wait for it "
                "to complete (or be approved/rejected), then retry. If its "
                "CloudBolt job is gone, discard it from the resource's "
                "Terraform tab.".format(label, workspace_name, "; ".join(descriptions)),
            )

        # ---- Initiate the upgrade; adopt its run into the approval pause ---
        blueprint = getattr(resource, "blueprint", None)
        message = build_run_message(
            job.id, resource.global_id, getattr(blueprint, "name", "") or "HCP Terraform"
        )
        upgrade = client.initiate_no_code_upgrade(nocode_module_id, workspace_id, message)
        run_id = upgrade["id"]
        if not run_id:
            return (
                "FAILURE",
                "{}: HCP Terraform accepted the upgrade request for workspace "
                "'{}' but returned no run ID (status '{}'); inspect the "
                "workspace in HCP Terraform before retrying.".format(
                    label, workspace_name, upgrade["status"]
                ),
            )
        resource.set_value_for_custom_field("tfc_run_id", run_id)
        resource.save()

        def _confirm_upgrade(comment):
            client.confirm_no_code_upgrade(
                nocode_module_id, workspace_id, run_id, message=comment
            )

        try:
            result = drive_run_with_plan_approval(
                job, client, workspace_id, run_id, confirm=_confirm_upgrade
            )
        except TFCRunFailedError as exc:
            if exc.run_url:
                resource.set_value_for_custom_field("tfc_run_url", exc.run_url)
                resource.save()
            raise
        resource.set_value_for_custom_field("tfc_run_url", result["run_url"])

        # ---- Record the version HCP now reports for the workspace ----------
        upgraded = workspace_module_info(client.get_workspace(workspace_id))
        new_version = upgraded["version"] or ""
        if new_version:
            resource.set_value_for_custom_field("tfc_nocode_module_version", new_version)
        verification = ""
        status = "SUCCESS"
        if pinned_version and new_version and new_version != pinned_version:
            status = "WARNING"
            verification = (
                " The workspace still reports v{} (pinned v{}); check it in HCP "
                "Terraform.".format(new_version, pinned_version)
            )
        elif upgraded["upgrade_available"]:
            status = "WARNING"
            verification = (
                " HCP Terraform still flags an update as available for this "
                "workspace; check it in HCP Terraform."
            )

        rename_note = ""
        if result["status"] == RUN_CLASS_APPLIED:
            rename_note = _refresh_outputs(client, resource, workspace_id)
            resource.save()
            return (
                status,
                "{}: upgraded workspace '{}' to module {} v{} (run {}: {} to add, "
                "{} to change, {} to destroy).{}{} Run URL: {}".format(
                    label, workspace_name, module_label, new_version or pinned_version,
                    result["run_id"], result.get("additions", "?"),
                    result.get("changes", "?"), result.get("destructions", "?"),
                    rename_note, verification, result["run_url"],
                ),
            )
        resource.save()
        return (
            status,
            "{}: upgrade run {} planned no infrastructure changes; workspace '{}' "
            "now runs module {} v{}. State outputs were not touched.{} Run URL: "
            "{}".format(
                label, result["run_id"], workspace_name, module_label,
                new_version or pinned_version, verification, result["run_url"],
            ),
        )

    except TFCError as exc:
        # Config, auth, validation, timeout and failed-run errors all convert
        # to a per-target FAILURE so a bulk run continues. CancelJobException
        # is NOT a TFCError and passes through to end the job.
        logger.exception("Deploy Latest Version failed for resource %s", resource.global_id)
        return "FAILURE", "{}: {}".format(label, exc)


def run(job, resource=None, resources=None, server=None, servers=None, **kwargs):
    """Upgrade every targeted no-code deployment to its module's pinned version."""
    set_progress("Starting {}...".format(ACTION_NAME))
    logger.info("%s day-2 plugin started for job %s", ACTION_NAME, job.id)

    targets, orphan_servers = _targets(job, resource, resources, server, servers)
    if not targets and not orphan_servers:
        return (
            "FAILURE",
            "",
            "No resource is associated with this action run. Launch this action "
            "from a deployed HCP Terraform no-code resource (or select several "
            "in the resource list).",
        )

    outcomes = []
    for orphan in orphan_servers:
        outcomes.append((
            "WARNING",
            "{}: this server is not part of a CloudBolt resource, so it has no "
            "HCP Terraform workspace; skipped.".format(getattr(orphan, "hostname", orphan)),
        ))
    total = len(targets)
    for index, target in enumerate(targets, start=1):
        if total > 1:
            set_progress(
                "[{}/{}] {} ({})".format(index, total, target.name, target.global_id)
            )
        outcomes.append(_deploy_latest(job, target))

    worst = max((status for status, _ in outcomes), key=lambda s: STATUS_RANK.get(s, 2))
    if len(outcomes) == 1:
        summary = outcomes[0][1]
    else:
        counts = {key: sum(1 for status, _ in outcomes if status == key) for key in STATUS_RANK}
        summary = "{} target(s): {} succeeded, {} warning(s), {} failed.\n{}".format(
            len(outcomes), counts["SUCCESS"], counts["WARNING"], counts["FAILURE"],
            "\n".join("- [{}] {}".format(status, message) for status, message in outcomes),
        )
    if worst == "FAILURE":
        return "FAILURE", "", summary
    return worst, summary, ""
