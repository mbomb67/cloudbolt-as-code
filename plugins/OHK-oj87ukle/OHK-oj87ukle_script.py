"""
CloudBolt Day-2 resource action: Update Variables (generic JSON).

Schema-free variable-update action for "HCP Terraform No-Code Module"
deployments (BP-00meiwwz): re-presents the deployment's CURRENT Terraform
variable values as one editable JSON object and applies any combination of
value changes through a new HCP Terraform (TFC) run with the same human
plan-approval pause the build uses. Template-agnostic -- it edits the VALUES of
the variable set the deployment already manages (tfc_variable_names, seeded at
provision), never the schema. All TFC REST access goes through the tfc_api
shared module (shared_modules/SHM-jlguerjr).

This is the sibling of the VCS blueprint's "Terraform Update" (OHK-lvy5tj0y);
the logic is identical. A plain run on a no-code workspace re-plans against the
module's attached configuration version, so no upgrade-cycle machinery is
needed here (module-version upgrades are a separate, deferred action).

Expected Action Inputs (declared in OHK-oj87ukle_metadata.json; the RSA shares
this CF- ID under its kebab-case twin -- one CustomField per name platform-wide):
  - parameters (TXT, optional) : JSON OBJECT of variable name/value pairs,
                                 pre-filled from the tfc_var_* mirrors. HCL
                                 variables round-trip as nested JSON. Sensitive
                                 variables have no mirror, never pre-fill, and
                                 are REJECTED if submitted (edit them in the TFC
                                 workspace UI instead).

Flow (plan U6):
  read tfc_workspace_id (missing -> FAILURE) -> STRICT parse (require_dict) ->
  REJECT unknown keys (not in tfc_variable_names) -> REJECT sensitive keys ->
  get client -> CONCURRENCY GUARD: fail fast on ANY non-final run -> SNAPSHOT
  prior values from tfc_var_* -> FULL-SET upsert (snapshot overlaid with the
  submit, blanks dropped) -> run_with_plan_approval with on_reject reverting the
  workspace variables to the snapshot -> refresh tfc_var_* mirrors on both
  terminal-success states; on "applied" also refresh every discovered
  tfc_output_* and rename if a name output changed.

Approval/reject ownership: the shared engine owns the reject path -- it catches
CancelJobException, discards the run, invokes on_reject (reverts the workspace
variables), and re-raises. This plugin NEVER catches CancelJobException.

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

BLUEPRINT_NAME = "HCP Terraform No-Code Module"


def _resource_csv(resource, field_name):
    """Read a comma-separated custom field into a list of names."""
    raw = resource.get_value_for_custom_field(field_name) or ""
    return [name.strip() for name in str(raw).split(",") if name.strip()]


def _safe_keys(names):
    """Join key names for a returned message, neutralizing braces so a stray {
    or } can't crash CloudBolt's synchronous str.format message path."""
    return ", ".join(names).replace("{", "(").replace("}", ")")


def _current_variable_values(resource, variable_names, hcl_names=()):
    """Current tfc_var_<name> mirror values for the known variable set (sensitive
    variables have no mirror, so they are naturally excluded). HCL-mirrored names
    parse back to native dict/list so they present as nested JSON."""
    current = {}
    if resource is None:
        return current
    for variable_name in variable_names:
        value = resource.get_value_for_custom_field("tfc_var_{}".format(variable_name))
        if value is not None:
            current[variable_name] = parse_variable_mirror(
                str(value), is_hcl=variable_name in hcl_names
            )
    return current


def _is_blank_value(value):
    """None, a blank/whitespace string, or an empty dict/list -- 'no change for
    this run' (the key is left out of the write set)."""
    if value is None:
        return True
    if isinstance(value, (dict, list)):
        return not value
    return str(value).strip() == ""


def generate_options_for_parameters(field, resource=None, **kwargs):
    """Pre-fill the dialog with the deployment's current variable values as a
    pretty-printed JSON object (from the tfc_var_* mirrors, not a live TFC
    read). Sensitive variables have no mirror, so they never pre-fill."""
    if resource is None:
        return {"initial_value": "{}", "options": []}
    variable_names = _resource_csv(resource, "tfc_variable_names")
    hcl_names = set(_resource_csv(resource, "tfc_hcl_variable_names"))
    current = _current_variable_values(resource, variable_names, hcl_names)
    return {"initial_value": json.dumps(current, indent=2), "options": []}


def run(job, resource=None, **kwargs):
    """Apply variable value changes through TFC with a plan-approval pause."""
    set_progress("Starting Update Variables...")
    logger.info("Update Variables day-2 plugin started for job %s", job.id)

    if resource is None:
        return (
            "FAILURE",
            "",
            "No resource is associated with this action run. Launch 'Update "
            "Variables' from a deployed '{}' resource.".format(BLUEPRINT_NAME),
        )

    # Cardinal rule 3: the funnel payload is triple-quoted for parse_params_payload.
    params_json = """{{ parameters }}"""

    workspace_id = (
        resource.get_value_for_custom_field("tfc_workspace_id") or ""
    ).strip()
    if not workspace_id:
        return (
            "FAILURE",
            "",
            "Resource '{}' has no tfc_workspace_id value, so there is no HCP "
            "Terraform workspace to update -- provisioning likely did not "
            "complete. See docs/hcp-no-code-setup.md.".format(resource.name),
        )

    tfc_organization = (
        resource.get_value_for_custom_field("tfc_organization") or ""
    ).strip()
    tfc_connection_info = (
        resource.get_value_for_custom_field("tfc_connection_info") or ""
    ).strip()
    variable_names = _resource_csv(resource, "tfc_variable_names")
    sensitive_names = set(_resource_csv(resource, "tfc_sensitive_variable_names"))
    hcl_names = set(_resource_csv(resource, "tfc_hcl_variable_names"))

    try:
        # ---- STRICT parse of the JSON object, BEFORE any TFC call ----------
        submitted_values = parse_params_payload(params_json, require_dict=True)

        # ---- Reject keys the deployment does not already manage -------------
        known = set(variable_names)
        unknown = [key for key in submitted_values if key not in known]
        if unknown:
            return (
                "FAILURE",
                "",
                "The parameters object contains variable(s) this deployment does "
                "not manage: {}. This deployment manages only: {}. Remove the "
                "unknown key(s) and retry (extending a deployment's variable set "
                "from day-2 is not supported -- there is no variable-delete path "
                "in TFC; evolve the module and its blueprint/form together "
                "instead). Nothing was written to the workspace.".format(
                    _safe_keys(sorted(unknown)),
                    _safe_keys(sorted(known)) if known else "(none recorded)",
                ),
            )

        # ---- Reject edits to sensitive variables ----------------------------
        sensitive_edits = [key for key in submitted_values if key in sensitive_names]
        if sensitive_edits:
            return (
                "FAILURE",
                "",
                "The parameters object edits sensitive variable(s): {}. Sensitive "
                "values cannot be updated through this plaintext action; update "
                "them directly on the deployment's TFC workspace (Variables tab) "
                "instead. Nothing was written to the workspace.".format(
                    _safe_keys(sorted(sensitive_edits))
                ),
            )

        client = get_client(tfc_organization, tfc_connection_info)

        workspace_name = (
            resource.get_value_for_custom_field("tfc_workspace_name") or ""
        ).strip()
        if not workspace_name:
            workspace = client.get_workspace(workspace_id)
            workspace_name = (workspace.get("attributes", {}) or {}).get("name", "")

        # ---- Concurrency guard: fail fast on non-final runs ----------------
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
                "A change is already pending on this deployment's TFC workspace "
                "'{}': {}. Wait for it to complete (or be approved/rejected), then "
                "retry. If its CloudBolt job is gone -- e.g. a jobengine restart "
                "killed a paused approval -- see the recovery section of "
                "docs/hcp-no-code-setup.md.".format(
                    workspace_name or workspace_id, "; ".join(descriptions)
                ),
            )

        # ---- Snapshot prior values (the revert source) ----------------------
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
            """on_reject: restore the pre-action snapshot (full set)."""
            if not snapshot:
                logger.warning(
                    "No prior tfc_var_* values are recorded on resource %s; "
                    "leaving the submitted variables in workspace %s (the next "
                    "run's full-set upsert overwrites them).",
                    resource.global_id, workspace_id,
                )
                return
            client.upsert_variables(workspace_id, snapshot)
            set_progress(
                "Rejected: TFC workspace variables reverted to their pre-action "
                "values."
            )

        # ---- Run with plan-approval pause ------------------------------------
        message = build_run_message(job.id, resource.global_id, BLUEPRINT_NAME)
        try:
            result = run_with_plan_approval(
                job, client, workspace_id, message,
                on_reject=_revert_workspace_variables,
            )
        except TFCRunFailedError as exc:
            if exc.run_url:
                resource.set_value_for_custom_field("tfc_run_url", exc.run_url)
                resource.save()
            raise

        resource.set_value_for_custom_field("tfc_run_url", result["run_url"])

        # ---- Refresh tfc_var_* mirrors on BOTH terminal-success states ------
        for variable_name, value in submitted.items():
            resource.set_value_for_custom_field(
                "tfc_var_{}".format(variable_name),
                serialize_variable_mirror(value),
            )

        if result["status"] == RUN_CLASS_NO_CHANGES:
            resource.save()
            return (
                "SUCCESS",
                "TFC run {} found no changes to apply; the submitted variables "
                "remain on workspace '{}' and the tfc_var_* mirrors were "
                "refreshed to match. State outputs were not touched. Run URL: "
                "{}".format(result["run_id"], workspace_name, result["run_url"]),
                "",
            )

        # ---- "applied": refresh ALL discovered outputs; rename on name change
        outputs = client.wait_for_outputs(workspace_id) or {}
        for output_name in ensure_output_custom_fields(sorted(outputs)):
            value = outputs.get(output_name)
            if value is not None:
                resource.set_value_for_custom_field(
                    "tfc_output_{}".format(output_name), str(value)
                )
        rename_note = ""
        output_name_value = outputs.get("name") or outputs.get("vm_name")
        if output_name_value is not None and str(output_name_value) != resource.name:
            rename_note = " Resource renamed '{}' -> '{}'.".format(
                resource.name, output_name_value
            )
            resource.name = str(output_name_value)
        resource.save()
        set_progress("Refreshed TFC variable mirrors and state outputs.")

        return (
            "SUCCESS",
            "Update Variables applied on workspace '{}' (run {}: {} to add, {} to "
            "change, {} to destroy).{} Run URL: {}".format(
                workspace_name, result["run_id"], result.get("additions", "?"),
                result.get("changes", "?"), result.get("destructions", "?"),
                rename_note, result["run_url"],
            ),
            "",
        )

    except TFCError as exc:
        logger.exception("Update Variables failed")
        return "FAILURE", "", str(exc)
