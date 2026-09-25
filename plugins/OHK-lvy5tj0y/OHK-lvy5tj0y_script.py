"""
CloudBolt Day-2 resource action: Terraform Update (shared, template-agnostic).

ONE update plugin for every HCP Terraform (TFC) workspace-per-deployment
blueprint in this repo -- the VCS-backed "HCP Terraform VM" (BP-b0qm83lh) and
the "HCP Terraform No-Code Module" (BP-00meiwwz) both point their day-2
resource action at it. It edits the VALUES of the variable set a deployment
already manages (tfc_variable_names, seeded by its build plugin), never the
schema, and applies the change through a new TFC run with the same human
plan-approval pause the builds use. Nothing here is blueprint-specific:
onboarding a new Terraform template or module needs a blueprint, an order
form, and a resource action pointing at this plugin -- no day-2 code. All TFC
REST access goes through the tfc_api shared module (shared_modules/SHM-jlguerjr).

Expected Action Inputs (declared in OHK-lvy5tj0y_metadata.json; every
resource action that uses this plugin shares the CF- ID under its kebab-case
twin -- one CustomField per name platform-wide):
  - parameters (TXT, optional) : the deployment's variable name/value pairs
                                 to apply, in ANY of the shapes CloudBolt
                                 delivers:
                                   * built-in dialog / API / MCP: a JSON
                                     object string, pre-filled by
                                     generate_options_for_parameters with
                                     the current tfc_var_* mirror values;
                                   * custom action form: the variables
                                     Dynamic Panel named
                                     action-input.parameters, which arrives
                                     as a native list[dict] (one panel) --
                                     unwrapped here.
                                 HCL-mirrored variables
                                 (tfc_hcl_variable_names, e.g. tags)
                                 pre-fill and re-submit as nested JSON (or
                                 as key/value rows from a matrixdynamic,
                                 collapsed here) and are re-written
                                 hcl:true. Sensitive variables
                                 (tfc_sensitive_variable_names) have no
                                 mirror, never pre-fill, and a NON-BLANK
                                 value for one is REJECTED -- this input is
                                 plaintext (job parameters, logs, and the
                                 custom_form_data kwarg); sensitive values
                                 are edited in the TFC workspace UI instead.

Validation is this plugin's job. A custom action form is submitted straight
to run_action -- CloudBolt evaluates no required/type/allowed-value rules
on that path -- so every check below runs BEFORE any TFC network call:
  parse (malformed payload -> FAILURE with operator guidance) ->
  drop form-internal marker keys (the '_sensitive' marker a cloned order-form
  panel carries, and any other '_'-prefixed key the deployment does not
  manage) -> collapse key/value rows and drop blank values ->
  REJECT unknown keys (not in tfc_variable_names: no variable-delete path
  exists in TFC, so a typo'd key would permanently pollute the workspace) ->
  REJECT non-blank sensitive keys.
Template-level rules (name formats, allowed sizes, ...) are Terraform's:
variable validation blocks fail the plan, and the plan-approval gate shows
the failure before anything is applied.

Flow after validation (plan U5):
  read tfc_workspace_id (missing -> FAILURE: provision incomplete) ->
  get client -> CONCURRENCY GUARD: fail fast if the workspace has ANY
  non-final run (the asymmetry with teardown, which discards them, is
  deliberate -- deletion is the designated recovery path for orphaned runs,
  day-2 is not) -> SNAPSHOT the prior values of the known variable set from
  the tfc_var_* custom fields -> overlay the submitted values (a key absent
  from the submit rides along unchanged; a blank value is dropped so its
  current workspace value is left untouched this run) -> FULL-SET upsert
  (every retained key every time -- self-heals stale workspace variables) ->
  run_with_plan_approval with on_reject reverting the workspace variables to
  the snapshot -> on "applied": refresh tfc_var_* mirrors AND tfc_output_*
  for EVERY output discovered in the applied state (fields created on the
  fly for outputs the template grew since provision), rename the resource if
  the name/vm_name output changed, store tfc_run_url; on
  "planned_and_finished" (no-op submit): refresh tfc_var_* mirrors ONLY and
  report success-with-no-changes -- tfc_output_* is NOT touched.

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
    collapse_key_value_rows,
    ensure_output_custom_fields,
    get_client,
    parse_params_payload,
    parse_variable_mirror,
    pop_sensitive_marker,
    run_with_plan_approval,
    serialize_variable_mirror,
)

logger = ThreadLogger(__name__)

# Outputs a template may use to name its deployment; the first one present
# wins. The no-code blueprint's modules report "name", the VM template
# reports "vm_name". A changed value renames the CloudBolt resource on apply.
NAME_OUTPUTS = ("name", "vm_name")


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
    tfc_hcl_variable_names, e.g. tags) store their mirror as JSON and are
    parsed back to native dict/list here so they present as real nested JSON
    in the dialog and re-upsert as hcl:true values.
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


def _normalize_submitted(submitted_values, known):
    """Turn a parsed payload into the value overlay, in place of a form's
    own validation.

    * Form-internal markers are dropped: the '_sensitive' marker (a cloned
      order-form panel plants it; the build already seeded
      tfc_sensitive_variable_names, so it carries nothing new here) and any
      other '_'-prefixed key the deployment does not manage.
    * A matrixdynamic's [{"key":..,"value":..}] rows collapse to a dict
      (collapse_key_value_rows); blank entries inside any dict are dropped so
      an untouched object field does not send empty strings.
    * Blank values are dropped: the key is left out of this run's write set,
      so its current workspace value is left untouched (never overwritten
      with "").
    Returns (overlay, dropped_marker_keys).
    """
    pop_sensitive_marker(submitted_values)
    dropped = [
        key for key in submitted_values
        if str(key).startswith("_") and key not in known
    ]
    for key in dropped:
        submitted_values.pop(key, None)

    overlay = {}
    for key, raw_value in submitted_values.items():
        value = collapse_key_value_rows(raw_value)
        if isinstance(value, dict):
            value = {
                entry_key: entry_value
                for entry_key, entry_value in value.items()
                if entry_value is not None and str(entry_value).strip() != ""
            }
        if _is_blank_value(value):
            continue
        overlay[key] = value
    return overlay, dropped


def generate_options_for_parameters(field, resource=None, **kwargs):
    """Pre-fill the built-in dialog (and the API/MCP parameter-options call)
    with the deployment's current variable values as a pretty-printed JSON
    object (from the tfc_var_* mirrors, not a live TFC read). Keyed by the
    deployment's tfc_variable_names so the operator edits exactly the set
    this deployment manages; sensitive variables have no mirror, so they are
    never pre-filled here. A custom action form ignores this: CloudBolt does
    not apply initial values to text or panel questions, and a submitted
    value always wins over a generated one on the action path."""
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
            "No resource is associated with this action run. Launch this "
            "action from a deployed HCP Terraform resource.",
        )

    # ---- The payload, in whichever shape CloudBolt delivered it ------------
    # A custom action form hands the Dynamic Panel over as a native
    # list[dict] in kwargs (no template rendering involved). The built-in
    # dialog, the API and MCP deliver the JSON object string, which the
    # template engine renders below. Cardinal rule 3: the rendered form is
    # triple-quoted for parse_params_payload.
    # KNOWN LIMITATION (shared with the build plugins, deferred to a form-side
    # / encoding fix per the plan): a rendered value whose repr contains
    # triple double-quotes could terminate the literal early. The native
    # kwarg path is immune, which is one more reason custom forms prefer it.
    params_payload = kwargs.get("parameters")
    if params_payload is None or isinstance(params_payload, str):
        params_payload = """{{ parameters }}"""

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
            "complete. Re-order its blueprint, or delete this resource and "
            "start over. See the blueprint's setup runbook under "
            "docs/.".format(resource.name),
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
    known = set(variable_names)

    try:
        # ---- Parse and normalize, BEFORE any TFC call ----------------------
        # parse_params_payload: a dict passes through; a Dynamic Panel's
        # single-item list[dict] unwraps (a multi-item list merges, later
        # panels winning); a scalar or unparseable payload raises a clean,
        # operator-readable TFCError (converted to FAILURE by the handler
        # below) with NO workspace read or run created. An empty input
        # parses to {} (no value changes; the current set is re-applied).
        submitted_values = parse_params_payload(params_payload)
        overlay, dropped_markers = _normalize_submitted(submitted_values, known)
        if dropped_markers:
            logger.info(
                "Ignoring form-internal key(s) in the parameters payload: %s",
                ", ".join(sorted(dropped_markers)),
            )

        # ---- Reject keys the deployment does not already manage -------------
        # There is NO variable-delete path in the TFC client, so a typo'd key
        # written here would permanently pollute the workspace. Fail fast,
        # naming the deployment's known variable set, BEFORE any TFC call.
        # (Extending the variable SET from day-2 is out of scope -- the recipe
        # is: evolve the template + blueprint/form together.)
        unknown = [key for key in submitted_values if key not in known]
        if unknown:
            return (
                "FAILURE",
                "",
                "The parameters payload contains variable(s) this deployment "
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
        # parameters input is PLAINTEXT (visible in job parameters, logs and
        # the custom_form_data kwarg). A NON-BLANK value fails fast BEFORE any
        # TFC call; a blank one was already dropped as 'no change' (a cloned
        # form panel may submit an untouched password field as ""). The
        # values already in the workspace are untouched by the full-set
        # upsert below because they are never in its write set (which also
        # avoids TFC's one-way sensitive flag: an unmarked write to a
        # sensitive variable would be rejected).
        sensitive_edits = [key for key in overlay if key in sensitive_names]
        if sensitive_edits:
            return (
                "FAILURE",
                "",
                "The parameters payload edits sensitive variable(s): {}. "
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
                "recovery section of the blueprint's setup runbook under "
                "docs/.".format(
                    workspace_name or workspace_id, "; ".join(descriptions)
                ),
            )

        # ---- Snapshot prior values (the revert source) ----------------------
        # Read from the tfc_var_* custom-field mirrors of deployed state, not
        # live TFC. The snapshot is what on_reject writes back; the custom
        # fields themselves are NOT touched on reject. Sensitive variables
        # have no mirror, so they are naturally absent from the snapshot --
        # and therefore from every upsert this action performs. HCL-mirrored
        # names (tags) parse back to native dict/list so the full-set upsert
        # re-writes them as hcl:true values, not quoted strings.
        snapshot = _current_variable_values(resource, variable_names, hcl_names)

        # ---- Full-set upsert: snapshot overlaid with the submitted values ---
        # Start from the full snapshot and overlay whatever the operator
        # edited (blanks were already dropped by _normalize_submitted, so a
        # blank or absent key means its snapshot value rides along unchanged).
        # Every retained key is written every run, which self-heals stale
        # workspace variables (e.g. left by a jobengine restart killing a
        # paused day-2 job, where no cleanup code runs).
        submitted = dict(snapshot)
        submitted.update(overlay)
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
        blueprint = getattr(resource, "blueprint", None)
        message = build_run_message(
            job.id, resource.global_id,
            getattr(blueprint, "name", "") or "HCP Terraform",
        )
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
        #      name output changed. No declared output set: every output in
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
        output_name_value = next(
            (outputs[key] for key in NAME_OUTPUTS if outputs.get(key) is not None),
            None,
        )
        if output_name_value is not None and str(output_name_value) != resource.name:
            rename_note = " Resource renamed '{}' -> '{}'.".format(
                resource.name, output_name_value
            )
            resource.name = str(output_name_value)
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
        # failed run) -- including a malformed funnel payload from
        # parse_params_payload -- converts to the exemplar's failure-return
        # convention; run failures carry the run URL. CancelJobException is NOT
        # a TFCError and passes through.
        logger.exception("Terraform Update failed")
        return "FAILURE", "", str(exc)
