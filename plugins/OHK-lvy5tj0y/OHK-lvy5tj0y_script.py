"""
CloudBolt Day-2 resource action: Terraform Update (generic JSON).

Schema-free update action for "HCP Terraform VM" deployments (BP-b0qm83lh):
re-presents the deployment's CURRENT Terraform variable values as one editable
JSON object and applies any combination of value changes through a new HCP
Terraform (TFC) run with the same human plan-approval pause the build uses.
The action is template-agnostic -- it edits the VALUES of the variable set the
deployment already manages (tfc_variable_names, seeded at provision), never the
schema -- so onboarding a new Terraform template needs no change here (plan R5,
R7). All TFC REST access goes through the tfc_api shared module
(shared_modules/SHM-jlguerjr) -- no vendor API call is made directly here.

This replaces the retired typed "Terraform Update" (which declared
vm_name/vm_size/location/resource_group_name as fixed inputs). Resize
(OHK-9xffkz53) stays as the typed, validated narrow shortcut for vm_size.

Expected Action Inputs (declared in OHK-lvy5tj0y_metadata.json; the RSA shares
this CF- ID under its kebab-case twin -- one CustomField per name platform-wide):
  - parameters (TXT, optional) : JSON OBJECT of variable name/value pairs.
                                 Pre-filled by generate_options_for_parameters
                                 with the deployment's current tfc_var_* mirror
                                 values (NOT a live TFC read, so the dialog
                                 stays fast). vm_size remains editable here;
                                 Resize is the guided shortcut for it.
                                 HCL-mirrored variables (tfc_hcl_variable_names,
                                 e.g. tags) pre-fill and re-submit as
                                 nested JSON and are re-written hcl:true.
                                 Sensitive variables (tfc_sensitive_variable_names)
                                 have no mirror, never pre-fill, and are
                                 REJECTED if submitted -- this field is
                                 plaintext; sensitive values are edited in the
                                 TFC workspace UI instead.

Flow (plan U5):
  read tfc_workspace_id (missing -> FAILURE: provision incomplete) ->
  STRICT parse of the funneled JSON (require_dict=True: a pasted array/scalar or
  invalid JSON FAILS fast with operator guidance, BEFORE any TFC network call) ->
  REJECT unknown keys: any submitted key not in the deployment's
  tfc_variable_names FAILS fast naming the known set (no variable-delete path
  exists in TFC, so a typo'd key would permanently pollute the workspace) ->
  REJECT sensitive keys: any submitted key in tfc_sensitive_variable_names
  FAILS fast (this field is plaintext; sensitive values live only in TFC) ->
  get client ->
  CONCURRENCY GUARD: fail fast if the workspace has ANY non-final run (the
  asymmetry with teardown, which discards them, is deliberate -- deletion is
  the designated recovery path for orphaned runs, day-2 is not) ->
  SNAPSHOT the prior values of the known variable set from the tfc_var_*
  custom fields -> build the submitted overlay = snapshot updated with the
  parsed dict, then DROP blank/None values (a removed key is absent from the
  submit, so its snapshot value rides along = documented no-op; a blank value
  is dropped, so its current workspace value is left untouched this run) ->
  FULL-SET upsert (every retained key every time -- self-heals stale workspace
  variables) -> run_with_plan_approval with on_reject reverting the workspace
  variables to the snapshot -> on "applied": refresh tfc_var_* mirrors AND
  tfc_output_* for EVERY output discovered in the applied state
  (wait_for_outputs with no filter; fields created on the fly for outputs the
  template grew since provision), rename the resource if the vm_name output
  changed, store tfc_run_url; on "planned_and_finished" (no-op submit): refresh
  tfc_var_* mirrors ONLY (the upserted variables stayed in the workspace, so
  the mirrors must follow) and report success-with-no-changes -- tfc_output_*
  is NOT touched.

Approval/reject ownership: the shared engine owns the whole reject path -- it
catches CancelJobException (a BaseException subclass), discards the TFC run,
invokes on_reject (which reverts the workspace variables here), and re-raises.
This plugin NEVER catches CancelJobException and never wraps the engine call in
a handler that could swallow BaseException. On reject the custom fields stay
UNCHANGED -- only the workspace is reverted.

No environment / resource-handler coupling here: the build plugin already
wrote the deployment's ARM_SUBSCRIPTION_ID / ARM_TENANT_ID onto the workspace
from the CloudBolt environment it was ordered into, and TFC holds the
credentials, so this action only edits terraform-category variables and never
touches a resource handler (AGENTS.md cardinal rule 4).

Returns a 3-tuple: (status, output_msg, error_msg)
  status: "SUCCESS" | "FAILURE"
"""

import json

from common.methods import set_progress
from utilities.logger import ThreadLogger

from shared_modules.tfc_api import (
    RUN_CLASS_NO_CHANGES,
    TFCError,
    TFCRunFailedError,
    build_run_message,
    ensure_output_custom_fields,
    get_client,
    parse_params_payload,
    parse_variable_mirror,
    run_with_plan_approval,
    serialize_variable_mirror,
)

logger = ThreadLogger(__name__)

# Blueprint-specific constant. tfc_api is blueprint-agnostic, so this lives with
# the plugins. This generic Update carries NO static variable-name list and NO
# vm_size choices: the managed variable set is read from the resource's
# tfc_variable_names (seeded by the build plugin), and values are edited as free
# JSON -- Resize (OHK-9xffkz53) is the typed, validated shortcut for vm_size.
BLUEPRINT_NAME = "HCP Terraform VM"


def _resource_csv(resource, field_name):
    """Read a comma-separated custom field (tfc_variable_names, seeded by the
    build plugin) into a list of names."""
    raw = resource.get_value_for_custom_field(field_name) or ""
    return [name.strip() for name in str(raw).split(",") if name.strip()]


def _safe_keys(names):
    """Join operator-controlled key names for a returned message, neutralizing
    braces so a stray { or } in a pasted JSON key can't crash CloudBolt's
    synchronous str.format/format_html message path (see SHM-5hjzm9e4)."""
    return ", ".join(names).replace("{", "(").replace("}", ")")


def _current_variable_values(resource, variable_names, hcl_names=()):
    """Current tfc_var_<name> mirror values for the known variable set.

    The mirrors are written by the build plugin and refreshed by every
    successful day-2 run, so they reflect deployed state without a live TFC
    read (dialogs stay fast). Only names with a non-None mirror are included
    -- which also naturally excludes sensitive variables: they never get a
    mirror at all. Names in ``hcl_names`` (the deployment's
    tfc_hcl_variable_names, e.g. tags) store their mirror as JSON
    and are parsed back to native dict/list here so they present as real
    nested JSON in the dialog and re-upsert as hcl:true values.
    """
    current = {}
    if resource is None:
        return current
    for variable_name in variable_names:
        value = resource.get_value_for_custom_field(
            "tfc_var_{}".format(variable_name)
        )
        if value is not None:
            current[variable_name] = parse_variable_mirror(
                str(value), is_hcl=variable_name in hcl_names
            )
    return current


def _is_blank_value(value):
    """None, a blank/whitespace string, or an empty dict/list -- all treated
    as 'no change for this run' (the key is left out of the write set)."""
    if value is None:
        return True
    if isinstance(value, (dict, list)):
        return not value
    return str(value).strip() == ""


def generate_options_for_parameters(field, resource=None, **kwargs):
    """Pre-fill the dialog with the deployment's current variable values as a
    pretty-printed JSON object (from the tfc_var_* mirrors, not a live TFC
    read). Keyed by the deployment's tfc_variable_names so the operator edits
    exactly the set this deployment manages; sensitive variables have no
    mirror, so they are never pre-filled here."""
    if resource is None:
        return {"initial_value": "{}", "options": []}
    variable_names = _resource_csv(resource, "tfc_variable_names")
    hcl_names = set(_resource_csv(resource, "tfc_hcl_variable_names"))
    current = _current_variable_values(resource, variable_names, hcl_names)
    return {"initial_value": json.dumps(current, indent=2), "options": []}


def run(job, resource=None, **kwargs):
    """Apply variable value changes through TFC with a plan-approval pause."""
    set_progress("Starting Terraform Update...")
    logger.info("Terraform Update day-2 plugin started for job %s", job.id)

    if resource is None:
        return (
            "FAILURE",
            "",
            "No resource is associated with this action run. Launch "
            "'Terraform Update' from a deployed '{}' resource.".format(
                BLUEPRINT_NAME
            ),
        )

    # Cardinal rule 3: the funnel payload is triple-quoted for parse_params_payload.
    # KNOWN LIMITATION (shared with the build plugin, deferred to a form-side /
    # encoding fix per the plan): a submitted value whose rendered repr contains
    # triple double-quotes could terminate this literal early. Both this site and
    # the build's must be fixed together when that hardening lands.
    params_json = """{{ parameters }}"""

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
    sensitive_names = set(
        _resource_csv(resource, "tfc_sensitive_variable_names")
    )
    hcl_names = set(_resource_csv(resource, "tfc_hcl_variable_names"))

    try:
        # ---- STRICT parse of the JSON object, BEFORE any TFC call ----------
        # require_dict=True: the operator edits a JSON OBJECT of the
        # deployment's current variable values, so a pasted array/scalar or
        # invalid JSON is a mistake -- parse_params_payload raises a clean,
        # operator-readable TFCError (converted to FAILURE by the handler
        # below) with NO workspace read or run created. An empty input parses
        # to {} (no value changes; the current set is re-applied).
        submitted_values = parse_params_payload(params_json, require_dict=True)

        # ---- Reject keys the deployment does not already manage -------------
        # There is NO variable-delete path in the TFC client, so a typo'd key
        # written here would permanently pollute the workspace. Fail fast,
        # naming the deployment's known variable set, BEFORE any TFC call.
        # (Extending the variable SET from day-2 is out of scope -- the recipe
        # is: evolve the template + blueprint/form together.)
        known = set(variable_names)
        unknown = [key for key in submitted_values if key not in known]
        if unknown:
            return (
                "FAILURE",
                "",
                "The parameters object contains variable(s) this deployment "
                "does not manage: {}. This deployment manages only: {}. "
                "Remove the unknown key(s) and retry (extending a deployment's "
                "variable set from day-2 is not supported -- there is no "
                "variable-delete path in TFC; evolve the Terraform template "
                "and its blueprint/form together instead). Nothing was written "
                "to the workspace.".format(
                    _safe_keys(sorted(unknown)),
                    _safe_keys(sorted(known)) if known else "(none recorded)",
                ),
            )

        # ---- Reject edits to sensitive variables ----------------------------
        # Sensitive variables (tfc_sensitive_variable_names, seeded at build)
        # have no tfc_var_* mirror to snapshot/revert from, and this action's
        # parameters field is PLAINTEXT (visible in job parameters and logs).
        # Fail fast BEFORE any TFC call; the values already in the workspace
        # are untouched by the full-set upsert below because they are never
        # in its write set (which also avoids TFC's one-way sensitive flag:
        # an unmarked write to a sensitive variable would be rejected).
        sensitive_edits = [
            key for key in submitted_values if key in sensitive_names
        ]
        if sensitive_edits:
            return (
                "FAILURE",
                "",
                "The parameters object edits sensitive variable(s): {}. "
                "Sensitive values cannot be updated through this plaintext "
                "action; update them directly on the deployment's TFC "
                "workspace (Variables tab) instead. Nothing was written to "
                "the workspace.".format(_safe_keys(sorted(sensitive_edits))),
            )

        client = get_client(tfc_organization, tfc_connection_info)

        workspace_name = (
            resource.get_value_for_custom_field("tfc_workspace_name") or ""
        ).strip()
        if not workspace_name:
            workspace = client.get_workspace(workspace_id)
            workspace_name = (workspace.get("attributes", {}) or {}).get("name", "")

        # ---- Concurrency guard: fail fast on non-final runs ----------------
        # Deliberate asymmetry with teardown (which DISCARDS them): deletion
        # is the designated recovery path for runs orphaned by a killed pause;
        # a day-2 action must never silently throw away a change that is still
        # awaiting someone's approval.
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
        # have no mirror, so they are naturally absent from the snapshot --
        # and therefore from every upsert this action performs. HCL-mirrored
        # names (tags) parse back to native dict/list so the
        # full-set upsert re-writes them as hcl:true values, not quoted
        # strings.
        snapshot = {}
        for variable_name in variable_names:
            value = resource.get_value_for_custom_field(
                "tfc_var_{}".format(variable_name)
            )
            if value is not None:
                snapshot[variable_name] = parse_variable_mirror(
                    str(value), is_hcl=variable_name in hcl_names
                )

        # ---- Full-set upsert: snapshot overlaid with the submitted values ---
        # Start from the full snapshot, overlay whatever the operator edited,
        # then DROP blank values. Consequences (documented in the dialog):
        #   * a REMOVED key is simply absent from submitted_values, so the
        #     snapshot value rides along unchanged (no-op);
        #   * a BLANK value (None, empty/whitespace string, or empty
        #     object/array) is dropped from the overlay, so that key is left
        #     out of this run's write set -- its current workspace value is
        #     left untouched (not overwritten with "").
        # Every retained key is written every run, which self-heals stale
        # workspace variables (e.g. left by a jobengine restart killing a
        # paused day-2 job, where no cleanup code runs).
        submitted = dict(snapshot)
        submitted.update(submitted_values)
        submitted = {
            key: value
            for key, value in submitted.items()
            if not _is_blank_value(value)
        }
        client.upsert_variables(workspace_id, submitted)
        set_progress(
            "Wrote the full variable set ({}) to TFC workspace '{}'.".format(
                ", ".join(sorted(submitted)), workspace_name or workspace_id
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
        # calls _revert_workspace_variables(), and re-raises CancelJobException
        # (a BaseException subclass -- the handlers below never see it), so the
        # cancellation completes and the custom fields stay unchanged.
        message = build_run_message(job.id, resource.global_id, BLUEPRINT_NAME)
        try:
            result = run_with_plan_approval(
                job, client, workspace_id, message,
                on_reject=_revert_workspace_variables,
            )
        except TFCRunFailedError as exc:
            # The failure message already carries the run URL; persist the URL
            # on the resource too, then let the outer handler convert it to the
            # standard FAILURE return.
            if exc.run_url:
                resource.set_value_for_custom_field("tfc_run_url", exc.run_url)
                resource.save()
            raise

        resource.set_value_for_custom_field("tfc_run_url", result["run_url"])

        # ---- Refresh tfc_var_* mirrors on BOTH terminal-success states ------
        # A planned_and_finished (no-op) run still leaves the upserted variables
        # in the workspace, so the mirrors must follow on that path too.
        for variable_name, value in submitted.items():
            # serialize_variable_mirror: dict/list values store as JSON for
            # the next round-trip; scalars store as str to match the STR
            # custom-field type.
            resource.set_value_for_custom_field(
                "tfc_var_{}".format(variable_name),
                serialize_variable_mirror(value),
            )

        if result["status"] == RUN_CLASS_NO_CHANGES:
            resource.save()
            return (
                "SUCCESS",
                "TFC run {} found no changes to apply; the submitted "
                "variables remain on workspace '{}' and the tfc_var_* "
                "mirrors were refreshed to match. State outputs were not "
                "touched. Run URL: {}".format(
                    result["run_id"], workspace_name, result["run_url"]
                ),
                "",
            )

        # ---- "applied": refresh ALL discovered outputs and rename if the
        #      vm_name output changed. No declared output set: every output in
        #      the applied state is recorded, with tfc_output_* fields created
        #      on the fly for outputs the template grew since provision.
        outputs = client.wait_for_outputs(workspace_id) or {}
        for output_name in ensure_output_custom_fields(sorted(outputs)):
            value = outputs.get(output_name)
            if value is not None:
                resource.set_value_for_custom_field(
                    "tfc_output_{}".format(output_name), str(value)
                )
        rename_note = ""
        output_vm_name = outputs.get("vm_name")
        if output_vm_name is not None and str(output_vm_name) != resource.name:
            rename_note = " Resource renamed '{}' -> '{}'.".format(
                resource.name, output_vm_name
            )
            resource.name = str(output_vm_name)
        resource.save()
        set_progress("Refreshed TFC variable mirrors and state outputs.")

        return (
            "SUCCESS",
            "Terraform Update applied on workspace '{}' (run {}: {} to add, "
            "{} to change, {} to destroy).{} Run URL: {}".format(
                workspace_name, result["run_id"],
                result.get("additions", "?"), result.get("changes", "?"),
                result.get("destructions", "?"), rename_note,
                result["run_url"],
            ),
            "",
        )

    except TFCError as exc:
        # Every shared-module failure (config, auth, validation, timeout,
        # failed run) -- including a malformed/non-object funnel payload from
        # parse_params_payload -- converts to the exemplar's failure-return
        # convention; run failures carry the run URL. CancelJobException is NOT
        # a TFCError and passes through.
        logger.exception("Terraform Update failed")
        return "FAILURE", "", str(exc)
