"""
CloudBolt Day-2 resource action: Resize.

Targeted single-variable update action for "HCP Terraform VM" deployments
(BP-b0qm83lh): presents only the Azure VM size with the deployment's current
size pre-selected, then applies the change through a new HCP Terraform (TFC)
run with the same human plan-approval pause the build and the generic
Terraform Update (OHK-lvy5tj0y, shared by every HCP Terraform blueprint) use. Demonstrates the narrow day-2 pattern
on the same shared engine (plan R7). All TFC REST access goes through the
tfc_api shared module (shared_modules/SHM-jlguerjr) -- no vendor API call is
made directly here.

Expected Action Inputs (declared in OHK-9xffkz53_metadata.json; same CF- ID
as the build plugin OHK-pvo05e24 and the generic update OHK-lvy5tj0y -- one
CustomField per name platform-wide):
  - vm_size (STR, required) : Azure VM size (static dropdown)

Dialog defaults: generate_options_for_vm_size reads the tfc_var_vm_size
custom field off ``resource=`` and returns ``{"initial_value": ...}`` --
the current value comes from the custom-field mirror of deployed state, NOT
a live TFC read, so the dialog stays fast.

Flow (plan U6 -- same as the generic update, narrowed to one variable):
  read tfc_workspace_id (missing -> FAILURE: provision incomplete) ->
  CONCURRENCY GUARD: fail fast if the workspace has ANY non-final run (the
  asymmetry with teardown, which discards them, is deliberate -- deletion is
  the designated recovery path for orphaned runs, day-2 is not) ->
  SNAPSHOT the prior values of all allowlisted variables from the tfc_var_*
  custom fields -> FULL-SET upsert (snapshot overlaid with ONLY the new
  vm_size: every allowlisted key every time -- the other variables ride
  along unchanged from the snapshot -- which self-heals stale workspace
  variables) -> run_with_plan_approval with on_reject reverting the
  workspace variables to the snapshot -> on "applied": refresh tfc_var_*
  mirrors AND tfc_output_* (wait_for_outputs), store tfc_run_url; on
  "planned_and_finished" (no-op submit): refresh tfc_var_* mirrors ONLY
  (the upserted variables stayed in the workspace, so the mirrors must
  follow) and report success-with-no-changes -- tfc_output_* is NOT touched.

Unlike the generic update, there is NO rename-on-apply step: this action
never submits a new vm_name (it rides along unchanged from the snapshot),
so the vm_name output cannot legitimately change here -- renaming stays in
the generic Terraform Update.

Depending on the Terraform template, applying a size change may restart the
VM -- the action dialog warns the user before submission.

Approval/reject ownership: the shared engine owns the whole reject path --
it catches CancelJobException (a BaseException subclass), discards the TFC
run, invokes on_reject (which reverts the workspace variables here), and
re-raises. This plugin NEVER catches CancelJobException and never wraps the
engine call in a handler that could swallow BaseException. On reject the
custom fields stay UNCHANGED -- only the workspace is reverted.

No environment / resource-handler coupling here: the build plugin already
wrote the deployment's ARM_SUBSCRIPTION_ID / ARM_TENANT_ID onto the workspace
from the CloudBolt environment it was ordered into, and TFC holds the
credentials, so this action only edits terraform-category variables and never
touches a resource handler (AGENTS.md cardinal rule 4).

Returns a 3-tuple: (status, output_msg, error_msg)
  status: "SUCCESS" | "FAILURE"
"""

from common.methods import set_progress
from utilities.logger import ThreadLogger

from shared_modules.tfc_api import (
    RUN_CLASS_NO_CHANGES,
    TFCError,
    TFCRunFailedError,
    build_run_message,
    ensure_output_custom_fields,
    get_client,
    parse_variable_mirror,
    run_with_plan_approval,
    serialize_variable_mirror,
)

logger = ThreadLogger(__name__)

# Blueprint-specific constants. tfc_api is blueprint-agnostic, so these live
# with the plugins. VM_SIZE_OPTIONS is mirrored verbatim from the build plugin
# (OHK-pvo05e24) and the Terraform Update plugin -- plugins cannot import one
# another, and the only shared surface, tfc_api, must stay generic.
BLUEPRINT_NAME = "HCP Terraform VM"
VM_SIZE_OPTIONS = [
    ("Standard_B2s", "Standard_B2s (2 vCPU, 4 GiB)"),
    ("Standard_B2ms", "Standard_B2ms (2 vCPU, 8 GiB)"),
    ("Standard_D2s_v5", "Standard_D2s_v5 (2 vCPU, 8 GiB)"),
    ("Standard_D4s_v5", "Standard_D4s_v5 (4 vCPU, 16 GiB)"),
    ("Standard_E2s_v5", "Standard_E2s_v5 (2 vCPU, 16 GiB)"),
    ("Standard_F2s_v2", "Standard_F2s_v2 (2 vCPU, 4 GiB)"),
]


def _resource_csv(resource, field_name):
    """Read a comma-separated custom field (tfc_variable_names, seeded by the
    build plugin) into a list of names."""
    raw = resource.get_value_for_custom_field(field_name) or ""
    return [name.strip() for name in str(raw).split(",") if name.strip()]


def _current_var(resource, variable_name):
    """Current tfc_var_<name> mirror value off the resource, or None.

    The mirrors are written by the build plugin and refreshed by every
    successful day-2 run, so they reflect deployed state without a live TFC
    read (dialogs stay fast).
    """
    if resource is None:
        return None
    value = resource.get_value_for_custom_field(
        "tfc_var_{}".format(variable_name)
    )
    if value is None:
        return None
    value = str(value).strip()
    return value or None


def generate_options_for_vm_size(field, resource=None, **kwargs):
    """Static size list (same as the build plugin), current size pre-selected.

    If the deployment's current size is not in the static list (e.g. the
    list changed, or the workspace was edited in TFC), it is prepended so
    the dialog still opens showing the real current value.
    """
    options = list(VM_SIZE_OPTIONS)
    result = {"options": options, "sort": False}
    current = _current_var(resource, "vm_size")
    if current:
        if current not in [value for value, _label in options]:
            options.insert(0, (current, "{} (current size)".format(current)))
        result["initial_value"] = current
    return result


def run(job, resource=None, **kwargs):
    """Apply a VM size change through TFC with a plan-approval pause."""
    set_progress("Starting Resize...")
    logger.info("Resize day-2 plugin started for job %s", job.id)

    if resource is None:
        return (
            "FAILURE",
            "",
            "No resource is associated with this action run. Launch "
            "'Resize' from a deployed '{}' resource.".format(BLUEPRINT_NAME),
        )

    # Cardinal rule 3: every template variable quoted. The value only ever
    # travels to TFC as a plain terraform string variable.
    vm_size = "{{ vm_size }}".strip()

    # ---- Validate input (defense-in-depth on the required flag) ------------
    if not vm_size:
        return "FAILURE", "", "A VM size is required."

    dialog_values = {
        "vm_size": vm_size,
    }

    # ---- The deployment must have its workspace recorded -------------------
    workspace_id = (
        resource.get_value_for_custom_field("tfc_workspace_id") or ""
    ).strip()
    if not workspace_id:
        return (
            "FAILURE",
            "",
            "Resource '{}' has no tfc_workspace_id value, so there is no HCP "
            "Terraform workspace to update -- provisioning likely did not "
            "complete. Re-order the '{}' blueprint, or delete this resource "
            "and start over. See docs/hcp-terraform-setup.md.".format(
                resource.name, BLUEPRINT_NAME
            ),
        )

    # Coordinates seeded on the resource by the build plugin. The connection
    # ref may be blank on a resource provisioned before connection selection
    # existed -- get_client then resolves the single 'tf-cloud'-labeled
    # ConnectionInfo unambiguously.
    tfc_organization = (
        resource.get_value_for_custom_field("tfc_organization") or ""
    ).strip()
    tfc_connection_info = (
        resource.get_value_for_custom_field("tfc_connection_info") or ""
    ).strip()
    variable_names = _resource_csv(resource, "tfc_variable_names")
    hcl_names = set(_resource_csv(resource, "tfc_hcl_variable_names"))

    try:
        client = get_client(tfc_organization, tfc_connection_info)

        workspace_name = (
            resource.get_value_for_custom_field("tfc_workspace_name") or ""
        ).strip()
        if not workspace_name:
            workspace = client.get_workspace(workspace_id)
            workspace_name = (workspace.get("attributes", {}) or {}).get("name", "")

        # ---- Concurrency guard: fail fast on non-final runs ----------------
        # Deliberate asymmetry with teardown (which DISCARDS them): deletion
        # is the designated recovery path for runs orphaned by a killed
        # pause; a day-2 action must never silently throw away a change that
        # is still awaiting someone's approval.
        pending_runs = client.list_non_final_runs(workspace_id)
        if pending_runs:
            descriptions = []
            for pending in pending_runs:
                reference = (
                    client.run_app_url(pending["id"], workspace_name)
                    if workspace_name
                    else pending["id"]
                )
                descriptions.append(
                    "{} (status '{}')".format(reference, pending["status"])
                )
            return (
                "FAILURE",
                "",
                "A change is already pending on this deployment's TFC "
                "workspace '{}': {}. Wait for it to complete (or be approved/"
                "rejected), then retry. If its CloudBolt job is gone -- e.g. "
                "a jobengine restart killed a paused approval -- see the "
                "recovery section of docs/hcp-terraform-setup.md.".format(
                    workspace_name or workspace_id, "; ".join(descriptions)
                ),
            )

        # ---- Snapshot prior values (the revert source) ----------------------
        # Read from the tfc_var_* custom-field mirrors of deployed state, not
        # live TFC. The snapshot is what on_reject writes back; the custom
        # fields themselves are NOT touched on reject. Sensitive variables
        # have no mirror, so they are naturally absent from the snapshot and
        # from every upsert here (their workspace values ride along
        # untouched). HCL-mirrored names (tfc_hcl_variable_names, e.g.
        # tags) parse back to native dict/list so the full-set
        # upsert re-writes them as hcl:true values, not quoted strings.
        snapshot = {}
        for variable_name in variable_names:
            value = resource.get_value_for_custom_field(
                "tfc_var_{}".format(variable_name)
            )
            if value is not None:
                snapshot[variable_name] = parse_variable_mirror(
                    str(value), is_hcl=variable_name in hcl_names
                )

        # ---- Full-set upsert: snapshot overlaid with ONLY the new size ------
        # ALL allowlisted keys every time, not just the changed one -- every
        # run self-heals stale workspace variables (e.g. left by a jobengine
        # restart killing a paused day-2 job, where no cleanup code runs).
        # The non-size variables ride along unchanged from the snapshot.
        submitted = dict(snapshot)
        submitted.update(dialog_values)
        client.upsert_variables(workspace_id, submitted)
        set_progress(
            "Wrote the full variable set ({}) to TFC workspace '{}' with "
            "vm_size = '{}'.".format(
                ", ".join(sorted(submitted)), workspace_name or workspace_id,
                vm_size,
            )
        )

        def _revert_workspace_variables():
            """on_reject: restore the pre-action snapshot (full set).

            Invoked by the engine after it discards the run on cancellation.
            A failure here is logged (not raised) by the engine, and is
            non-fatal by design: the next successful day-2 run's full-set
            upsert self-heals whatever this revert missed.
            """
            if not snapshot:
                logger.warning(
                    "No prior tfc_var_* values are recorded on resource %s; "
                    "leaving the submitted variables in workspace %s (the "
                    "next run's full-set upsert overwrites them).",
                    resource.global_id, workspace_id,
                )
                return
            client.upsert_variables(workspace_id, snapshot)
            set_progress(
                "Rejected: TFC workspace variables reverted to their "
                "pre-action values."
            )

        # ---- Run with plan-approval pause ------------------------------------
        # The engine owns the reject path: on cancel it discards the run,
        # calls _revert_workspace_variables(), and re-raises
        # CancelJobException (a BaseException subclass -- the handlers below
        # never see it), so the cancellation completes and the custom fields
        # stay unchanged.
        message = build_run_message(job.id, resource.global_id, BLUEPRINT_NAME)
        try:
            result = run_with_plan_approval(
                job, client, workspace_id, message,
                on_reject=_revert_workspace_variables,
            )
        except TFCRunFailedError as exc:
            # The failure message already carries the run URL; persist the
            # URL on the resource too, then let the outer handler convert it
            # to the standard FAILURE return.
            if exc.run_url:
                resource.set_value_for_custom_field("tfc_run_url", exc.run_url)
                resource.save()
            raise

        resource.set_value_for_custom_field("tfc_run_url", result["run_url"])

        # ---- Refresh tfc_var_* mirrors on BOTH terminal-success states ------
        # A planned_and_finished (no-op) run still leaves the upserted
        # variables in the workspace, so the mirrors must follow on that
        # path too.
        for variable_name, value in submitted.items():
            # serialize_variable_mirror: dict/list values store as JSON for
            # the next round-trip; scalars store as str.
            resource.set_value_for_custom_field(
                "tfc_var_{}".format(variable_name),
                serialize_variable_mirror(value),
            )

        if result["status"] == RUN_CLASS_NO_CHANGES:
            resource.save()
            return (
                "SUCCESS",
                "TFC run {} found no changes to apply (the deployment is "
                "already size '{}'); the submitted variables remain on "
                "workspace '{}' and the tfc_var_* mirrors were refreshed to "
                "match. State outputs were not touched. Run URL: {}".format(
                    result["run_id"], vm_size, workspace_name,
                    result["run_url"],
                ),
                "",
            )

        # ---- "applied": refresh ALL discovered outputs -------------------------
        # No rename step here: vm_name rode along unchanged from the
        # snapshot, so the vm_name output cannot legitimately change on a
        # resize -- renaming stays in the generic Terraform Update. No
        # declared output set: every output in the applied state is recorded,
        # with tfc_output_* fields created on the fly for new ones.
        outputs = client.wait_for_outputs(workspace_id) or {}
        for output_name in ensure_output_custom_fields(sorted(outputs)):
            value = outputs.get(output_name)
            if value is not None:
                resource.set_value_for_custom_field(
                    "tfc_output_{}".format(output_name), str(value)
                )
        resource.save()
        set_progress("Refreshed TFC variable mirrors and state outputs.")

        return (
            "SUCCESS",
            "Resize applied on workspace '{}': VM size is now '{}' (run {}: "
            "{} to add, {} to change, {} to destroy). Run URL: {}".format(
                workspace_name, vm_size, result["run_id"],
                result.get("additions", "?"), result.get("changes", "?"),
                result.get("destructions", "?"), result["run_url"],
            ),
            "",
        )

    except TFCError as exc:
        # Every shared-module failure (config, auth, validation, timeout,
        # failed run) converts to the exemplar's failure-return convention;
        # run failures carry the run URL. CancelJobException is NOT a
        # TFCError and passes through.
        logger.exception("Resize failed")
        return "FAILURE", "", str(exc)
