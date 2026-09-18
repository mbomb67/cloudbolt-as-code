"""
CloudBolt build plugin: HCP Terraform No-Code Module.

Provisions infrastructure for the "HCP Terraform No-Code Module" blueprint
(BP-00meiwwz) through HCP Terraform's NO-CODE provisioning workflow: one
dedicated workspace per deployment, created FROM a pinned no-code registry
module (POST /no-code-modules/:id/workspaces), the form-collected variables
written as workspace variables, and the run HCP auto-queues adopted into a
human plan-approval pause. All TFC REST access goes through the tfc_api shared
module (shared_modules/SHM-jlguerjr) -- no vendor API call is made directly
here.

This is the sibling of the VCS-based "HCP Terraform VM" build plugin
(OHK-pvo05e24). It shares that blueprint's "form is the manifest; terraform
validates" model verbatim: the static custom form's Dynamic Panel collects the
module's input variables (field names == the module's variable names), funneled
into ONE generic `parameters` input; whatever the panel submits is written to
the workspace and terraform is the authority on what is declared/required. The
ONLY material difference from OHK-pvo05e24 is the workspace-creation API path
(no-code create + auto-queued-run adoption instead of VCS create + config-
version wait).

Pinned per blueprint (parameter_defaults on the build deployment item, read
here as templated inputs -- there are NO order-form dropdowns or option
generators, unlike the VCS blueprint):
  - tfc_connection_info : the 'tf-cloud'-labeled ConnectionInfo global_id
  - tfc_organization    : the HCP Terraform organization
  - tfc_project         : the HCP Terraform project
  - tfc_nocode_module_id: the nocode-* module this blueprint deploys

Order-collected:
  - parameters (TXT) : JSON object of the module's variable name/value pairs,
                       funneled from the form's Dynamic Panel. The reserved
                       '_sensitive' key (a hidden form field listing variable
                       names) is popped and those variables are written
                       sensitive:true with no tfc_var_* mirror. The reserved
                       'deployment_name' key (naming only, never sent to
                       terraform) names the CloudBolt resource when no naming
                       output is present.

Flow (plan U3):
  parse the funnel payload (BEFORE any TFC call, so a malformed payload fails
  fast with no workspace/run) -> pop '_sensitive' and 'deployment_name' ->
  collapse tag rows, drop blank values -> ensure custom fields -> get client ->
  get-or-adopt the workspace by its deterministic cb-nc-<global_id> name
  (name-primary ownership; the no-code create ignores tag-bindings -- U1) ->
  store the workspace ID + coordinates IMMEDIATELY (before any run/poll) so a
  later failure leaves a cleanable, tracked resource -> obtain the run:
    * fresh create -> adopt the auto-queued run (drive_run_with_plan_approval);
      no run appeared -> upsert vars + create one (run_with_plan_approval)
    * adopted workspace (retry) -> adopt an orphaned pending run, or fail fast
      if a live CloudBolt job owns one, else upsert vars + fresh run
  -> plan-approval pause ('Continue Job' applies; cancel rejects+discards) ->
  store the run URL, every discovered tfc_output_* field, tfc_var_* mirrors,
  and name the resource.

Approval/reject ownership: the shared engine owns the reject path -- it catches
CancelJobException (a BaseException subclass), discards the run, and re-raises.
This plugin NEVER catches CancelJobException. A rejected provision re-raises
out of run(), leaving the resource PROVFAILED with its workspace ID already
stored -- teardown-cleanable.

No environment / resource-handler coupling: TFC holds the cloud credentials
(project-scoped variable set), so no env_id gating applies and no resource
handler is ever exposed here (AGENTS.md cardinal rule 4 is structurally
inapplicable to this blueprint).

Returns a 3-tuple: (status, output_msg, error_msg)
  status: "SUCCESS" | "FAILURE"
"""

from common.methods import set_progress
from infrastructure.models import CustomField
from jobs.models import Job
from utilities.logger import ThreadLogger

from shared_modules.tfc_api import (
    RUN_CLASS_APPLIED,
    RUN_CLASS_NO_CHANGES,
    TFCError,
    TFCNotFoundError,
    TFCRunFailedError,
    build_run_message,
    collapse_key_value_rows,
    drive_run_with_plan_approval,
    ensure_output_custom_fields,
    get_client,
    no_code_workspace_name_for_resource,
    parse_job_id_from_run_message,
    parse_params_payload,
    pop_sensitive_marker,
    run_with_plan_approval,
    serialize_variable_mirror,
)

logger = ThreadLogger(__name__)

BLUEPRINT_NAME = "HCP Terraform No-Code Module"

# Reserved panel keys that are NOT terraform variables: the sensitive marker is
# popped by pop_sensitive_marker; deployment_name is a naming-only field so a
# friendly resource name can be collected without writing an undeclared
# variable to the workspace.
DEPLOYMENT_NAME_KEY = "deployment_name"

# Job.status values meaning a run's owning CloudBolt job is still LIVE (owns its
# TFC run via a paused approval gate or an active poll loop). Mirrored from the
# teardown plugin by deliberate copy -- plugins cannot import one another.
LIVE_JOB_STATUSES = ("RUNNING", "PAUSED")


def _ensure_custom_fields(variable_names, sensitive_names=()):
    """Pre-create the runtime custom fields this blueprint persists on its
    Resource (get_or_create -> idempotent). Sensitive names get NO tfc_var_*
    mirror -- a sensitive value must never land in a custom field.
    """
    fields = [
        ("tfc_workspace_id", "TFC Workspace ID",
         "ID of the dedicated HCP Terraform workspace backing this deployment."),
        ("tfc_workspace_name", "TFC Workspace Name",
         "Name of the dedicated HCP Terraform workspace backing this deployment."),
        ("tfc_run_url", "TFC Run URL",
         "URL of the most recent HCP Terraform run for this deployment."),
        ("tfc_run_id", "TFC Run ID",
         "ID of the run this deployment's provision job adopted/created; the "
         "attribution source for the day-2 and teardown non-final-run guards "
         "(the no-code auto-queued run's message is TFC-authored)."),
        ("tfc_connection_info", "TF Cloud Connection",
         "Global ID of the 'tf-cloud'-labeled ConnectionInfo this deployment "
         "provisions through; read by the day-2 and teardown actions."),
        ("tfc_organization", "TFC Organization",
         "HCP Terraform organization this deployment's workspace lives in."),
        ("tfc_project", "TFC Project",
         "HCP Terraform project this deployment's workspace lives in."),
        ("tfc_nocode_module_id", "TFC No-Code Module ID",
         "The nocode-* module this deployment was provisioned from."),
        ("tfc_nocode_module_version", "TFC No-Code Module Version",
         "Module version this deployment was provisioned from; reserved for the "
         "deferred module-version Upgrade action (HCP owns the version pin)."),
        ("tfc_variable_names", "TFC Variable Names",
         "Comma-separated set of Terraform variables this deployment manages; "
         "read by the day-2 actions."),
        ("tfc_sensitive_variable_names", "TFC Sensitive Variable Names",
         "Comma-separated subset of tfc_variable_names written to the TFC "
         "workspace as sensitive. These have no tfc_var_* mirror and the "
         "day-2 actions refuse to edit them."),
        ("tfc_hcl_variable_names", "TFC HCL Variable Names",
         "Comma-separated subset of tfc_variable_names whose values are written "
         "to TFC as HCL (object/map/list) and mirrored as JSON; read by the "
         "day-2 actions to round-trip them."),
    ]
    for variable_name in variable_names:
        if variable_name in sensitive_names:
            continue
        fields.append((
            "tfc_var_{}".format(variable_name),
            "TFC Variable: {}".format(variable_name),
            "Mirror of the '{}' variable on this deployment's TFC "
            "workspace.".format(variable_name),
        ))
    for name, label, description in fields:
        CustomField.objects.get_or_create(
            name=name,
            defaults=dict(
                label=label, description=description, type="STR",
                show_on_servers=False,
            ),
        )


def _is_blank_value(value):
    """None, a blank/whitespace string, or an empty dict/list -- all mean
    'unset', so the variable falls back to the module's own default."""
    if value is None:
        return True
    if isinstance(value, (dict, list)):
        return not value
    return str(value).strip() == ""


def _initial_run_progress(workspace_id):
    def _callback(elapsed_seconds):
        set_progress(
            "Waiting for HCP Terraform to auto-queue the initial run for "
            "workspace {} ({}s elapsed)...".format(workspace_id, int(elapsed_seconds))
        )
    return _callback


def _find_owning_job(current_job, run_message):
    """Resolve the CloudBolt Job that created a TFC run from its message, or
    None when unattributed (TFC-authored no-code run messages are not parseable)
    or when the message names the current job. Mirrors the teardown plugin."""
    owning_job_id = parse_job_id_from_run_message(run_message)
    if owning_job_id is None or owning_job_id == current_job.id:
        return None
    return Job.objects.filter(id=owning_job_id).first()


def _hydrate_resource(resource, outputs, variables, deployment_name, sensitive_keys=()):
    """Store EVERY discovered state output + tfc_var_* mirrors and name the
    resource. Used on both terminal-success paths (real apply + no-op reconcile
    against an existing state). The output set is whatever wait_for_outputs
    discovered -- no declared allowlist.

    Naming precedence: a 'name'/'vm_name' OUTPUT, then the form's
    deployment_name, then a same-named variable value, else the workspace name
    is left as the resource name (unchanged).
    """
    for output_name in ensure_output_custom_fields(sorted(outputs)):
        value = outputs.get(output_name)
        if value is not None:
            resource.set_value_for_custom_field(
                "tfc_output_{}".format(output_name), str(value)
            )
    for variable_name, value in variables.items():
        if variable_name in sensitive_keys:
            continue
        resource.set_value_for_custom_field(
            "tfc_var_{}".format(variable_name),
            serialize_variable_mirror(value),
        )
    chosen_name = (
        outputs.get("name")
        or outputs.get("vm_name")
        or deployment_name
        or variables.get("name")
        or variables.get("vm_name")
    )
    if chosen_name:
        resource.name = str(chosen_name)
    resource.save()


def run(job, **kwargs):
    """Provision through HCP Terraform no-code with a plan-approval pause."""
    set_progress("Starting HCP Terraform No-Code Module provisioning...")
    logger.info("HCP Terraform No-Code build plugin started for job %s", job.id)

    # Cardinal rule 3: every templated input quoted. The funnel is triple-quoted
    # so a rendered Python-repr / JSON blob survives intact for
    # parse_params_payload. Coordinates + module ID are pinned per blueprint via
    # parameter_defaults (no dropdowns).
    params_json = """{{ parameters }}"""
    tfc_connection_info = "{{ tfc_connection_info }}".strip()
    tfc_organization = "{{ tfc_organization }}".strip()
    tfc_project = "{{ tfc_project }}".strip()
    tfc_nocode_module_id = "{{ tfc_nocode_module_id }}".strip()

    missing_coords = [
        label
        for label, value in (
            ("tfc_connection_info", tfc_connection_info),
            ("tfc_organization", tfc_organization),
            ("tfc_project", tfc_project),
            ("tfc_nocode_module_id", tfc_nocode_module_id),
        )
        if not value or "FILL-ME" in value
    ]
    if missing_coords:
        return (
            "FAILURE",
            "",
            "TFC coordinates are missing: {}. The connection, organization, "
            "project, and no-code module ID are pinned via parameter_defaults "
            "on the build deployment item of BP-00meiwwz (see "
            "docs/hcp-no-code-setup.md).".format(", ".join(missing_coords)),
        )

    resource = job.resource_set.first()
    if resource is None:
        return (
            "FAILURE",
            "",
            "No resource is associated with this job; the workspace-per-"
            "deployment pattern requires one. Order this plugin through the "
            "'{}' blueprint.".format(BLUEPRINT_NAME),
        )

    try:
        # ---- Parse the funnel payload BEFORE any TFC network call ----------
        panel_variables = parse_params_payload(params_json)

        # ---- Pop reserved, non-terraform panel keys ------------------------
        sensitive_names = pop_sensitive_marker(panel_variables)
        deployment_name = str(
            panel_variables.pop(DEPLOYMENT_NAME_KEY, "") or ""
        ).strip()

        # ---- Compose the workspace variable set (form is the manifest) -----
        variables = {}
        for key, raw_value in panel_variables.items():
            value = collapse_key_value_rows(raw_value)
            if isinstance(value, dict):
                value = {
                    entry_key: entry_value
                    for entry_key, entry_value in value.items()
                    if entry_value is not None and str(entry_value).strip() != ""
                }
            variables[key] = value
        variables = {
            key: value
            for key, value in variables.items()
            if not _is_blank_value(value)
        }
        variable_names = list(variables.keys())
        sensitive_keys = sorted(set(sensitive_names) & set(variables))
        hcl_variable_names = sorted(
            key for key, value in variables.items()
            if isinstance(value, (dict, list))
        )

        _ensure_custom_fields(variable_names, sensitive_keys)

        client = get_client(tfc_organization, tfc_connection_info)

        # ---- Get-or-adopt the workspace (name-primary ownership) -----------
        # The no-code create ignores tag-bindings (U1), so the deterministic
        # cb-nc-<global_id> name -- which embeds this resource's unique,
        # immutable global_id -- is the ownership proof. A stored ID or a
        # name match is adopted unless it carries a DIFFERENT resource-id tag.
        workspace_name = no_code_workspace_name_for_resource(resource.global_id)
        stored_workspace_id = (
            resource.get_value_for_custom_field("tfc_workspace_id") or ""
        ).strip()
        existing = None
        if stored_workspace_id:
            try:
                existing = client.get_workspace(stored_workspace_id)
            except TFCNotFoundError:
                existing = None
        if existing is None:
            try:
                existing = client.get_workspace_by_name(workspace_name)
            except TFCNotFoundError:
                existing = None

        created = False
        if existing is not None:
            if client.workspace_resource_tag_conflicts(existing["id"], resource.global_id):
                return (
                    "FAILURE",
                    "",
                    "No-code workspace '{}' already exists but is tagged for a "
                    "different resource; refusing to adopt a foreign workspace. "
                    "Resolve the collision in TFC, then retry.".format(workspace_name),
                )
            workspace = existing
            set_progress("Adopting existing workspace '{}' (retry).".format(workspace_name))
        else:
            workspace = client.create_no_code_workspace(
                tfc_nocode_module_id, resource.global_id, tfc_project,
                variables, sensitive_keys=sensitive_keys,
                description="CloudBolt deployment '{}'".format(
                    deployment_name or resource.global_id
                ),
            )
            created = True
            # Best-effort tag (the create ignores tag-bindings -- U1); never
            # fatal, the name is the load-bearing ownership signal.
            client.apply_resource_tag(workspace["id"], resource.global_id)

        workspace_id = workspace["id"]
        workspace_name = (workspace.get("attributes", {}) or {}).get("name", workspace_name)

        # ---- Store the workspace ID + coordinates IMMEDIATELY --------------
        # Before any run/poll: any later failure leaves a tracked,
        # teardown-cleanable resource.
        resource.set_value_for_custom_field("tfc_workspace_id", workspace_id)
        resource.set_value_for_custom_field("tfc_workspace_name", workspace_name)
        resource.set_value_for_custom_field("tfc_connection_info", tfc_connection_info)
        resource.set_value_for_custom_field("tfc_organization", tfc_organization)
        resource.set_value_for_custom_field("tfc_project", tfc_project)
        resource.set_value_for_custom_field("tfc_nocode_module_id", tfc_nocode_module_id)
        resource.set_value_for_custom_field("tfc_variable_names", ",".join(variable_names))
        resource.set_value_for_custom_field(
            "tfc_sensitive_variable_names", ",".join(sensitive_keys)
        )
        resource.set_value_for_custom_field(
            "tfc_hcl_variable_names", ",".join(hcl_variable_names)
        )
        resource.save()
        set_progress(
            "No-code workspace '{}' ({}) recorded on the resource.".format(
                workspace_name, workspace_id
            )
        )

        # ---- Obtain the run to drive ---------------------------------------
        message = build_run_message(job.id, resource.global_id, BLUEPRINT_NAME)
        adopted_run_id = None

        if created:
            # Fresh create: HCP auto-queues the run (U1). Adopt it. Its plan
            # reflects the vars sent in the create payload.
            # U3 LIVE-VERIFY: confirm the auto-queued run plans against the
            # submitted variables (vars-in-create honored, like the docs say
            # and unlike tag-bindings). If not, the fallback branch below
            # (upsert + fresh run) is the correct path to switch to.
            initial_run = client.wait_for_initial_run(
                workspace_id, progress_callback=_initial_run_progress(workspace_id)
            )
            if initial_run is not None:
                adopted_run_id = initial_run["id"]
        else:
            # Retry against an adopted workspace: fail fast if a LIVE job owns a
            # pending run; otherwise adopt an orphaned pending run (its message
            # is TFC-authored for the auto-queued run, or names a dead job).
            pending_runs = client.list_non_final_runs(workspace_id)
            for pending in pending_runs:
                owning_job = _find_owning_job(job, pending["message"])
                if owning_job is not None and owning_job.status in LIVE_JOB_STATUSES:
                    return (
                        "FAILURE",
                        "",
                        "A run on this deployment's workspace '{}' belongs to "
                        "CloudBolt job {} which is still {}. Wait for it (or "
                        "cancel it), then retry.".format(
                            workspace_name, owning_job.id, owning_job.status
                        ),
                    )
            if pending_runs:
                adopted_run_id = pending_runs[-1]["id"]

        try:
            if adopted_run_id is not None:
                resource.set_value_for_custom_field("tfc_run_id", adopted_run_id)
                resource.save()
                result = drive_run_with_plan_approval(
                    job, client, workspace_id, adopted_run_id
                )
            else:
                # No adoptable run (no auto-queued run appeared, or an adopted
                # workspace had none pending): set the variables and create our
                # own run through the unchanged existing engine.
                client.upsert_variables(
                    workspace_id, variables, sensitive_keys=sensitive_keys
                )
                result = run_with_plan_approval(job, client, workspace_id, message)
                resource.set_value_for_custom_field("tfc_run_id", result["run_id"])
                resource.save()
        except TFCRunFailedError as exc:
            if exc.run_url:
                resource.set_value_for_custom_field("tfc_run_url", exc.run_url)
                resource.save()
            raise

        resource.set_value_for_custom_field("tfc_run_url", result["run_url"])
        resource.save()

        # ---- Terminal success: hydrate the resource ------------------------
        outputs = client.wait_for_outputs(workspace_id)
        if result["status"] == RUN_CLASS_NO_CHANGES and outputs is None:
            return (
                "SUCCESS",
                "No-code run {} finished with no changes and workspace '{}' has "
                "no Terraform state yet; no outputs to record. Run URL: "
                "{}".format(result["run_id"], workspace_name, result["run_url"]),
                "",
            )

        _hydrate_resource(
            resource, outputs or {}, variables, deployment_name,
            sensitive_keys=sensitive_keys,
        )
        set_progress("Stored TFC outputs and variable mirrors on the resource.")

        if result["status"] == RUN_CLASS_APPLIED:
            output_msg = (
                "Provisioned '{}' via no-code module {} on TFC workspace '{}' "
                "(run {}: {} to add, {} to change, {} to destroy). Run URL: "
                "{}".format(
                    resource.name, tfc_nocode_module_id, workspace_name,
                    result["run_id"], result.get("additions", "?"),
                    result.get("changes", "?"), result.get("destructions", "?"),
                    result["run_url"],
                )
            )
        else:
            output_msg = (
                "No-code run {} found no changes to apply; resource reconciled "
                "from the current state of workspace '{}'. Run URL: {}".format(
                    result["run_id"], workspace_name, result["run_url"]
                )
            )
        return "SUCCESS", output_msg, ""

    except TFCError as exc:
        logger.exception("HCP Terraform No-Code Module provisioning failed")
        return "FAILURE", "", str(exc)
