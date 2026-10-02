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

Pinned per blueprint (hidden defaultValue fields in the blueprint's custom
form, read here as templated inputs; a custom form does not receive BDI
parameter_defaults, so the build deployment item carries none):
  - tfc_connection_info : the 'tf-cloud'-labeled ConnectionInfo global_id
  - tfc_organization    : the HCP Terraform organization
  - tfc_project         : the HCP Terraform project
  - tfc_nocode_module_id: the nocode-* module this blueprint deploys

Order-collected:
  - env_id (STR)       : the CloudBolt Environment to deploy into, RBAC-gated
                         via generate_options_for_env_id. Its Azure handler's
                         subscription and tenant are written to the workspace
                         as ARM_SUBSCRIPTION_ID / ARM_TENANT_ID environment
                         variables (sent in the no-code create payload so the
                         auto-queued run already targets them; they override
                         the same keys from the project's non-priority
                         credentials variable set). The handler is never
                         exposed (cardinal rule 4).
  - parameters (TXT) : JSON object of the module's variable name/value pairs,
                       funneled from the form's Dynamic Panel. The reserved
                       '_sensitive' key (a hidden form field listing variable
                       names) is popped and those variables are written
                       sensitive:true with no tfc_var_* mirror. The reserved
                       'workspace_name' key (required; never sent to
                       terraform) is the orderer's name for the HCP Terraform
                       workspace AND the CloudBolt resource; the resource is
                       renamed to it before any TFC call, and outputs never
                       rename it afterwards. Uniqueness is the orderer's: a
                       name already in use in the organization fails the
                       order before anything is created.

Flow (plan U3):
  parse the funnel payload (BEFORE any TFC call, so a malformed payload fails
  fast with no workspace/run) -> pop '_sensitive' and 'workspace_name'
  (validated against TFC's name charset) -> name the resource -> collapse tag
  rows, drop blank values -> ensure custom fields -> get client ->
  get-or-adopt the workspace: the stored tfc_workspace_id first, else a
  same-named workspace ONLY if it carries this resource's cmp:resource-id tag
  (the tag is applied best-effort right after the create, which ignores
  tag-bindings -- U1); any other same-named workspace is a collision ->
  store the workspace ID + coordinates IMMEDIATELY (before any run/poll) so a
  later failure leaves a cleanable, tracked resource -> obtain the run:
    * fresh create -> adopt the auto-queued run (drive_run_with_plan_approval);
      no run appeared -> upsert vars + create one (run_with_plan_approval)
    * adopted workspace (retry) -> adopt an orphaned pending run, or fail fast
      if a live CloudBolt job owns one, else upsert vars + fresh run
  -> plan-approval pause ('Continue Job' applies; cancel rejects+discards) ->
  store the run URL, every discovered tfc_output_* field and the tfc_var_*
  mirrors (the resource keeps the orderer's Workspace Name).

Approval/reject ownership: the shared engine owns the reject path -- it catches
CancelJobException (a BaseException subclass), discards the run, and re-raises.
This plugin NEVER catches CancelJobException. A rejected provision re-raises
out of run(), leaving the resource PROVFAILED with its workspace ID already
stored -- teardown-cleanable.

Credentials stay in HCP Terraform (the project-scoped variable set holds the
client ID/secret); CloudBolt contributes only WHERE to deploy -- the chosen
environment's subscription and tenant -- as workspace environment variables.

Returns a 3-tuple: (status, output_msg, error_msg)
  status: "SUCCESS" | "WARNING" | "FAILURE" (WARNING: provisioned, but a VM
          listed in the module's cloudbolt_vm_ids output could not be
          adopted as a CloudBolt server record)
"""

import re

from common.methods import set_progress
from jobs.models import Job
from utilities.logger import ThreadLogger

from shared_modules.env_options import (
    EnvOptionsError,
    entitled_environment,
    environment_options,
    resolve_group,
    subscription_context,
)
from shared_modules.vm_adoption import (
    adopt_from_outputs,
    missing_output_note,
    outcome as adoption_outcome,
)
from shared_modules.tfc_api import (
    FIELD_VISIBILITY_ATTRIBUTE,
    FIELD_VISIBILITY_HIDDEN,
    FIELD_VISIBILITY_PARAMETER,
    RUN_CLASS_APPLIED,
    RUN_CLASS_NO_CHANGES,
    TFCError,
    TFCNotFoundError,
    TFCRunFailedError,
    WORKSPACE_NAME_MAX_LENGTH,
    build_run_message,
    collapse_key_value_rows,
    drive_run_with_plan_approval,
    ensure_custom_field,
    ensure_output_custom_fields,
    get_client,
    parse_job_id_from_run_message,
    parse_params_payload,
    pop_sensitive_marker,
    portal_url_for_job,
    run_with_plan_approval,
    serialize_variable_mirror,
    workspace_module_info,
)

logger = ThreadLogger(__name__)

BLUEPRINT_NAME = "HCP Terraform No-Code Module"

# Reserved panel keys that are NOT terraform variables: the sensitive marker is
# popped by pop_sensitive_marker; workspace_name is the orderer's name for the
# HCP Terraform workspace and the CloudBolt resource, collected without writing
# an undeclared variable to the workspace.
WORKSPACE_NAME_KEY = "workspace_name"

# TFC's documented workspace-name charset ("Workspace names can only include
# letters, numbers, -, and _"), capped at tfc_api's conservative length limit.
# Docs: https://developer.hashicorp.com/terraform/cloud-docs/api-docs/workspaces
WORKSPACE_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,%d}$" % WORKSPACE_NAME_MAX_LENGTH)

# Job.status values meaning a run's owning CloudBolt job is still LIVE (owns its
# TFC run via a paused approval gate or an active poll loop). Mirrored from the
# teardown plugin by deliberate copy -- plugins cannot import one another.
LIVE_JOB_STATUSES = ("RUNNING", "PAUSED")

# Provider environment variables written per workspace from the CloudBolt
# Environment's Azure handler. Names are the azurerm provider's:
# https://registry.terraform.io/providers/hashicorp/azurerm/latest/docs/guides/service_principal_client_secret#configuring-the-service-principal-in-terraform
ARM_ENV_VARIABLES = (("ARM_SUBSCRIPTION_ID", "subscription_id"), ("ARM_TENANT_ID", "tenant_id"))


def generate_options_for_env_id(field=None, **kwargs):
    """RBAC-aware Environment selector restricted to Azure-backed
    environments (group.get_available_environments(), narrowed by handler
    type). The resource handler itself is never offered."""
    group = resolve_group(kwargs.get("group"))
    if group is None:
        return []
    options = [
        (option["value"], option["title"])
        for option in environment_options(group, profile=kwargs.get("profile"))
    ]
    return options or [("", "------ No Azure environments available ------")]


def _ensure_custom_fields(variable_names, sensitive_names=()):
    """Pre-create the runtime custom fields this blueprint persists on its
    Resource. ensure_custom_field (tfc_api) wraps get_or_create, so this is
    idempotent, and the visibility below is a creation DEFAULT only -- an
    admin's later change on the instance wins. Sensitive names get NO
    tfc_var_* mirror -- a sensitive value must never land in a custom field.

    Visibility: ATTRIBUTE (Overview attributes panel + Parameters tab) for
    the workspace and its last run, plus every tfc_output_* created by
    ensure_output_custom_fields; PARAMETER (Parameters tab only) for the TFC
    coordinates, the module pin, the Azure subscription context and the
    tfc_var_* mirrors; HIDDEN for the bookkeeping the day-2 and teardown
    actions read (connection ref, environment id, the variable-name sets).
    """
    fields = [
        ("tfc_workspace_id", "TFC Workspace ID",
         "ID of the dedicated HCP Terraform workspace backing this deployment.",
         FIELD_VISIBILITY_PARAMETER),
        ("tfc_workspace_name", "TFC Workspace Name",
         "Name of the dedicated HCP Terraform workspace backing this deployment.",
         FIELD_VISIBILITY_ATTRIBUTE),
        ("tfc_run_url", "TFC Run URL",
         "URL of the most recent HCP Terraform run for this deployment.",
         FIELD_VISIBILITY_ATTRIBUTE),
        ("tfc_run_id", "TFC Run ID",
         "ID of the run this deployment's provision job adopted/created; the "
         "attribution source for the day-2 and teardown non-final-run guards "
         "(the no-code auto-queued run's message is TFC-authored).",
         FIELD_VISIBILITY_PARAMETER),
        ("tfc_connection_info", "TF Cloud Connection",
         "Global ID of the 'tf-cloud'-labeled ConnectionInfo this deployment "
         "provisions through; read by the day-2 and teardown actions.",
         FIELD_VISIBILITY_HIDDEN),
        ("tfc_organization", "TFC Organization",
         "HCP Terraform organization this deployment's workspace lives in.",
         FIELD_VISIBILITY_PARAMETER),
        ("tfc_project", "TFC Project",
         "HCP Terraform project this deployment's workspace lives in.",
         FIELD_VISIBILITY_PARAMETER),
        ("tfc_nocode_module_id", "TFC No-Code Module ID",
         "The nocode-* module this deployment was provisioned from.",
         FIELD_VISIBILITY_PARAMETER),
        ("tfc_nocode_module_name", "TFC No-Code Module Name",
         "Registry name of the no-code module the deployment's workspace runs "
         "(from the workspace's source-module-id); recorded at provision and "
         "refreshed by the Deploy Latest Version action.",
         FIELD_VISIBILITY_PARAMETER),
        ("tfc_nocode_module_version", "TFC No-Code Module Version",
         "Module version the deployment's workspace currently runs (from the "
         "workspace's source-module-id); recorded at provision and refreshed by "
         "the Deploy Latest Version action. HCP owns the version pin.",
         FIELD_VISIBILITY_PARAMETER),
        ("tfc_env_id", "TFC Environment ID",
         "ID of the CloudBolt Environment this deployment was ordered into; "
         "its Azure handler supplied the workspace's ARM_* variables.",
         FIELD_VISIBILITY_HIDDEN),
        ("azure_subscription_id", "Azure Subscription ID",
         "Azure subscription the deployment targets (from the environment's "
         "resource handler).",
         FIELD_VISIBILITY_PARAMETER),
        ("azure_tenant_id", "Azure Tenant ID",
         "Azure tenant the deployment targets (from the environment's "
         "resource handler).",
         FIELD_VISIBILITY_PARAMETER),
        ("tfc_variable_names", "TFC Variable Names",
         "Comma-separated set of Terraform variables this deployment manages; "
         "read by the day-2 actions.",
         FIELD_VISIBILITY_HIDDEN),
        ("tfc_sensitive_variable_names", "TFC Sensitive Variable Names",
         "Comma-separated subset of tfc_variable_names written to the TFC "
         "workspace as sensitive. These have no tfc_var_* mirror and the "
         "day-2 actions refuse to edit them.",
         FIELD_VISIBILITY_HIDDEN),
        ("tfc_hcl_variable_names", "TFC HCL Variable Names",
         "Comma-separated subset of tfc_variable_names whose values are written "
         "to TFC as HCL (object/map/list) and mirrored as JSON; read by the "
         "day-2 actions to round-trip them.",
         FIELD_VISIBILITY_HIDDEN),
    ]
    for variable_name in variable_names:
        if variable_name in sensitive_names:
            continue
        fields.append((
            "tfc_var_{}".format(variable_name),
            "TFC Variable: {}".format(variable_name),
            "Mirror of the '{}' variable on this deployment's TFC "
            "workspace.".format(variable_name),
            FIELD_VISIBILITY_PARAMETER,
        ))
    for name, label, description, visibility in fields:
        ensure_custom_field(name, label, description, visibility=visibility)


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


def _hydrate_resource(resource, outputs, variables, sensitive_keys=()):
    """Store EVERY discovered state output + tfc_var_* mirrors. Used on both
    terminal-success paths (real apply + no-op reconcile against an existing
    state). The output set is whatever wait_for_outputs discovered -- no
    declared allowlist. The resource keeps the orderer's Workspace Name;
    outputs never rename it.
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
    resource.save()


def _no_contract_note(client, workspace_id):
    """The module exposes no cloudbolt_vm_ids output: say so, naming the VM
    resources Terraform did create (HCP's workspace-resources listing carries
    addresses and types only -- no state values, no secrets)."""
    try:
        resources = client.list_workspace_resources(workspace_id)
    except TFCError:
        resources = []
    return missing_output_note(resources)


def run(job, **kwargs):
    """Provision through HCP Terraform no-code with a plan-approval pause."""
    set_progress("Starting HCP Terraform No-Code Module provisioning...")
    logger.info("HCP Terraform No-Code build plugin started for job %s", job.id)

    # Cardinal rule 3: every templated input quoted. The funnel is triple-quoted
    # so a rendered Python-repr / JSON blob survives intact for
    # parse_params_payload. Coordinates + module ID are pinned per blueprint as
    # hidden fields in the custom form (no dropdowns).
    params_json = """{{ parameters }}"""
    env_id = "{{ env_id }}".strip()
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
            "project, and no-code module ID are pinned as hidden fields in "
            "the custom form of BP-00meiwwz (forms/FRM-1dxfulvq; see "
            "docs/hcp-no-code-setup.md).".format(", ".join(missing_coords)),
        )
    if not env_id:
        return "FAILURE", "", "An Environment is required (env_id arrived blank)."

    resource = job.resource_set.first()
    if resource is None:
        return (
            "FAILURE",
            "",
            "No resource is associated with this job; the workspace-per-"
            "deployment pattern requires one. Order this plugin through the "
            "'{}' blueprint.".format(BLUEPRINT_NAME),
        )

    # ---- Re-check entitlement server-side (cardinal rule 4) ---------------
    env = entitled_environment(resource.group, env_id)
    if env is None:
        return (
            "FAILURE",
            "",
            "Environment {} is not an Azure environment available to group "
            "'{}'.".format(env_id, getattr(resource.group, "name", "?")),
        )
    try:
        azure = subscription_context(env)
    except EnvOptionsError as exc:
        return "FAILURE", "", str(exc)
    if not azure["subscription_id"]:
        return (
            "FAILURE",
            "",
            "Environment '{}' has no subscription ID on its Azure resource "
            "handler.".format(env.name),
        )
    arm_variables = {key: azure[source] for key, source in ARM_ENV_VARIABLES if azure[source]}

    try:
        # ---- Parse the funnel payload BEFORE any TFC network call ----------
        panel_variables = parse_params_payload(params_json)

        # ---- Pop reserved, non-terraform panel keys ------------------------
        sensitive_names = pop_sensitive_marker(panel_variables)
        workspace_name = str(
            panel_variables.pop(WORKSPACE_NAME_KEY, "") or ""
        ).strip()
        if not workspace_name:
            return (
                "FAILURE",
                "",
                "A Workspace Name is required (the form's '{}' field arrived "
                "blank).".format(WORKSPACE_NAME_KEY),
            )
        if not WORKSPACE_NAME_PATTERN.match(workspace_name):
            return (
                "FAILURE",
                "",
                "Workspace Name '{}' is not a valid HCP Terraform workspace name: "
                "use 1-{} letters, numbers, hyphens or underscores.".format(
                    workspace_name, WORKSPACE_NAME_MAX_LENGTH
                ),
            )

        # ---- Name the resource from the form BEFORE any TFC call -----------
        # The Workspace Name is the resource name for the deployment's whole
        # life; nothing downstream (outputs, day-2 runs) renames it.
        resource.name = workspace_name
        resource.save()
        set_progress("Resource named '{}'.".format(workspace_name))

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

        # ---- Get-or-adopt the workspace ------------------------------------
        # Ownership: the stored tfc_workspace_id (written right after the
        # create) first. With none, a workspace already carrying the orderer's
        # name is adopted ONLY when it is tagged cmp:resource-id=<this
        # resource> -- the tag is applied best-effort right after the create
        # (which ignores tag-bindings -- U1), so a crash between the create and
        # the resource save is recoverable. Any other same-named workspace is
        # a name collision the orderer resolves by choosing another name.
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
                same_name = client.get_workspace_by_name(workspace_name)
            except TFCNotFoundError:
                same_name = None
            if same_name is not None:
                if not client.workspace_has_resource_tag(same_name["id"], resource.global_id):
                    return (
                        "FAILURE",
                        "",
                        "A workspace named '{}' already exists in HCP Terraform "
                        "organization '{}' and does not belong to this resource. "
                        "Choose a different Workspace Name, or delete that "
                        "workspace in HCP Terraform, then order again.".format(
                            workspace_name, tfc_organization
                        ),
                    )
                existing = same_name

        created = False
        if existing is not None:
            workspace = existing
            set_progress("Adopting existing workspace '{}' (retry).".format(workspace_name))
        else:
            workspace = client.create_no_code_workspace(
                tfc_nocode_module_id, resource.global_id, tfc_project,
                variables, sensitive_keys=sensitive_keys,
                description="CloudBolt resource {} ({})".format(
                    resource.global_id, BLUEPRINT_NAME
                ),
                env_variables=arm_variables,
                source_url=portal_url_for_job(job),
                workspace_name=workspace_name,
            )
            created = True
            # Best-effort tag (the create ignores tag-bindings -- U1); never
            # fatal. The stored workspace ID below is the primary ownership
            # record; the tag only enables name-based re-adoption after a
            # crash before that save.
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
        # The module name and version HCP created the workspace from (its
        # source-module-id). Shown on the Terraform tab; the version is
        # compared against the module's pin by the Deploy Latest Version action.
        module_info = workspace_module_info(workspace)
        if module_info["name"]:
            resource.set_value_for_custom_field("tfc_nocode_module_name", module_info["name"])
        if module_info["version"]:
            resource.set_value_for_custom_field("tfc_nocode_module_version", module_info["version"])
        resource.set_value_for_custom_field("tfc_env_id", str(env.id))
        resource.set_value_for_custom_field("azure_subscription_id", azure["subscription_id"])
        if azure["tenant_id"]:
            resource.set_value_for_custom_field("azure_tenant_id", azure["tenant_id"])
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
                client.upsert_variables(workspace_id, arm_variables, category="env")
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
            resource, outputs or {}, variables, sensitive_keys=sensitive_keys,
        )
        set_progress("Stored TFC outputs and variable mirrors on the resource.")

        # ---- Adopt the VMs Terraform created as child Server records --------
        # Contract: the module's cloudbolt_vm_ids output (vm_adoption shared
        # module). Each id is looked up through the ordered environment's
        # handler and hydrated like a Sync VMs discovery, flagged
        # created_by_terraform. A problem here is a WARNING, never a FAILURE:
        # the infrastructure exists and its outputs are recorded.
        adoption = adopt_from_outputs(resource, env, outputs or {}, progress=set_progress)
        status, adoption_note = adoption_outcome(
            adoption, _no_contract_note(client, workspace_id) if adoption is None else ""
        )

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
        return status, output_msg + adoption_note, ""

    except TFCError as exc:
        logger.exception("HCP Terraform No-Code Module provisioning failed")
        return "FAILURE", "", str(exc)
