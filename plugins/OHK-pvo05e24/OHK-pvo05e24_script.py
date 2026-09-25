"""
CloudBolt build plugin: HCP Terraform VM.

Provisions infrastructure for the "HCP Terraform VM" blueprint (BP-b0qm83lh)
through HCP Terraform (TFC): one dedicated TFC workspace per deployment, the
form-collected template variables upserted as workspace variables, a run with
a human plan-approval pause, and the state outputs stored back on the
CloudBolt resource. All TFC REST access goes through the tfc_api shared module
(shared_modules/SHM-jlguerjr) -- no vendor API call is made directly here.

The build plugin is template-agnostic (the form is the manifest): the order
form collects the Terraform template's variables -- INCLUDING vm_name -- in a
Dynamic Panel funneled into ONE generic `parameters` input; whatever the panel
submits is written to the workspace and terraform is the authority on what is
declared/required. No per-template code lives here.

Expected Action Inputs (declared in OHK-pvo05e24_metadata.json):
  - parameters (TXT, optional) : JSON object of the template's variable
                                 name/value pairs, funneled from the order
                                 form's Dynamic Panel (field names match the
                                 Terraform variables; vm_name rides in the
                                 panel and is regex-validated here as
                                 defense-in-depth). Empty parses to {} so the
                                 template defaults apply. dict/list values
                                 (tags) are written to TFC as hcl:true
                                 variables. The reserved '_sensitive'
                                 key (a hidden form field listing variable
                                 names, e.g. ["admin_password"]) is popped
                                 before the payload becomes the variable set;
                                 the named variables are written sensitive:true
                                 and NEVER mirrored to custom fields.
  - env_id (STR, required)      : the CloudBolt Environment the user orders
                                 into, RBAC-gated via generate_options_for_
                                 env_id (group.get_available_environments()).
                                 The env's Azure resource handler supplies
                                 the subscription and tenant, written to the
                                 workspace as ARM_SUBSCRIPTION_ID /
                                 ARM_TENANT_ID environment variables (which
                                 override the same keys inherited from the
                                 project's credentials variable set), and
                                 the env drives the form's resource-group /
                                 subnet / image / size dropdowns through the
                                 'form-options' inbound webhook. The handler
                                 itself is never exposed (cardinal rule 4).
  - six pinned per-blueprint inputs (tfc_connection_info, tfc_organization,
    tfc_project, tfc_repo_identifier, tfc_branch, tfc_working_directory),
    pinned as hidden defaultValue fields in the blueprint's custom form
    (a custom form does not receive BDI parameter_defaults, so the build
    deployment item carries none).

State outputs are NOT declared anywhere: after apply, EVERY output in the
workspace's current Terraform state is discovered and recorded on the
resource as a tfc_output_<name> custom field.

Flow (the plan's provision diagram):
  parse the funneled parameters (BEFORE any TFC call, so a malformed payload
  fails fast with no workspace/run) -> pop the '_sensitive' marker ->
  validate the panel's vm_name, collapse tag rows, and drop blank values ->
  re-check the ordering group's entitlement to env_id and read the
  subscription context from its handler -> ensure custom fields -> get
  client (from the pinned 'tf-cloud' ConnectionInfo) -> ensure workspace
  (ID-first get-or-adopt keyed on the resource's immutable global_id) ->
  store tfc_workspace_id / tfc_workspace_name IMMEDIATELY, before variables,
  runs, or polling, so any later failure leaves a cleanable, tracked resource
  -> upsert the workspace variables (terraform) and ARM_* (env) -> wait for
  VCS config-version ingestion -> run_with_plan_approval
  (writes the plan summary to job output and pauses; 'Continue Job' approves
  and applies, canceling rejects) -> store the run URL, every discovered
  tfc_output_* field, tfc_var_* mirrors, and name the resource after the
  provisioned VM.

Approval/reject ownership: the shared engine owns the whole reject path --
it catches CancelJobException (a BaseException subclass), discards the TFC
run, and re-raises. This plugin NEVER catches CancelJobException and never
wraps the engine call in a handler that could swallow BaseException; a
rejected provision re-raises out of run(), leaving the resource PROVFAILED
with its workspace ID already stored -- teardown-cleanable.

Credentials stay in HCP Terraform (the project-scoped variable set holds the
client ID/secret); CloudBolt contributes only WHERE to deploy -- the
subscription and tenant of the chosen environment's handler -- as workspace
environment variables. The variable set must not be flagged "priority" in
HCP, or its ARM_SUBSCRIPTION_ID would win over the workspace's.

Returns a 3-tuple: (status, output_msg, error_msg)
  status: "SUCCESS" | "FAILURE"
"""

import re

from common.methods import set_progress
from infrastructure.models import CustomField
from utilities.logger import ThreadLogger

from shared_modules.env_options import (
    EnvOptionsError,
    entitled_environment,
    environment_options,
    resolve_group,
    subscription_context,
)
from shared_modules.tfc_api import (
    RUN_CLASS_APPLIED,
    RUN_CLASS_NO_CHANGES,
    TFCError,
    TFCRunFailedError,
    build_run_message,
    collapse_key_value_rows,
    ensure_output_custom_fields,
    get_client,
    parse_params_payload,
    pop_sensitive_marker,
    portal_url_for_job,
    run_with_plan_approval,
    serialize_variable_mirror,
)

logger = ThreadLogger(__name__)

# Blueprint-specific constants. tfc_api is blueprint-agnostic, so these live
# with the blueprint's own plugins. The day-2 plugins carry their own
# deliberate copy of VM_NAME_RE (plugins cannot import one another; the only
# shared-code surface is tfc_api, which must stay generic).
BLUEPRINT_NAME = "HCP Terraform VM"

# NOTE: this plugin no longer carries a static variable-name list. The set of
# Terraform variables a deployment manages is DERIVED at order time from the
# funneled `parameters` panel dict (plus the fixed vm_name) and seeded to the
# resource as tfc_variable_names, so the day-2 actions read the actual
# submitted set. Onboarding a new template needs no change here (R7).

# Azure VM name rules, mirrored from the custom form's regex validator as
# defense-in-depth on top of cardinal-rule-3 quoting: 1-64 chars, letters,
# numbers and hyphens, starting and ending alphanumeric (Windows images are
# further limited to 15 chars by Azure). Keep in lockstep with the vm_name
# validator in FRM-t3v8zpb7's Dynamic Panel and the day-2 actions.
# Docs: https://learn.microsoft.com/en-us/azure/azure-resource-manager/management/resource-name-rules#microsoftcompute
VM_NAME_RE = re.compile(r"^[a-zA-Z0-9]([a-zA-Z0-9-]{0,62}[a-zA-Z0-9])?$")


# Provider environment variables written per workspace from the CloudBolt
# Environment's Azure handler. Names are the azurerm provider's:
# https://registry.terraform.io/providers/hashicorp/azurerm/latest/docs/guides/service_principal_client_secret#configuring-the-service-principal-in-terraform
ARM_ENV_VARIABLES = (("ARM_SUBSCRIPTION_ID", "subscription_id"), ("ARM_TENANT_ID", "tenant_id"))


# -----------------------------------------------------------------------------
# Order-form option generator. ONLY env_id has one: a generate_options_for_*
# function on an input makes CloudBolt ignore the value a custom form submits
# for it, so the pinned coordinates (connection, org, project, repo, branch)
# must stay plain inputs with no generator and no field dependencies.
# -----------------------------------------------------------------------------

def generate_options_for_env_id(field=None, **kwargs):
    """RBAC-aware Environment selector restricted to Azure-backed
    environments: everything group.get_available_environments() returns
    (entitled + ancestor-entitled + unconstrained), narrowed by handler type.
    The resource handler itself is never offered."""
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
    Resource. get_or_create makes this idempotent.

    The coordinate fields (tfc_connection_info / tfc_organization /
    tfc_project / tfc_repo_identifier / tfc_branch / tfc_working_directory)
    are also declared as the build plugin's action inputs, so they already
    exist from the metadata import; recreating them here is a harmless no-op
    that also covers a hand-cleared instance. tfc_variable_names,
    tfc_sensitive_variable_names, and tfc_hcl_variable_names are
    build-derived (the submitted variable set and its sensitive/HCL subsets,
    for the day-2 actions to read) and exist only here. The tfc_var_* mirror
    fields are created per submitted variable key -- EXCEPT for names in
    ``sensitive_names``: a sensitive value must never land in a custom field,
    so no mirror field exists for it at all. tfc_output_* fields are NOT
    created here: the output set is discovered from the applied Terraform
    state, so ensure_output_custom_fields (tfc_api) runs at hydration time
    instead.
    """
    fields = [
        ("tfc_workspace_id", "TFC Workspace ID",
         "ID of the dedicated HCP Terraform workspace backing this deployment."),
        ("tfc_workspace_name", "TFC Workspace Name",
         "Name of the dedicated HCP Terraform workspace backing this deployment."),
        ("tfc_run_url", "TFC Run URL",
         "URL of the most recent HCP Terraform run for this deployment."),
        ("tfc_connection_info", "TF Cloud Connection",
         "Global ID of the 'tf-cloud'-labeled ConnectionInfo this deployment "
         "provisions through; read by the day-2 and teardown actions."),
        ("tfc_organization", "TFC Organization",
         "HCP Terraform organization this deployment's workspace lives in."),
        ("tfc_project", "TFC Project",
         "HCP Terraform project this deployment's workspace lives in."),
        ("tfc_repo_identifier", "TFC VCS Repo",
         "VCS repo (org/name) the deployment's workspace tracks."),
        ("tfc_branch", "TFC VCS Branch",
         "VCS branch the deployment's workspace tracks."),
        ("tfc_working_directory", "TFC Working Directory",
         "Terraform working directory within the VCS repo."),
        ("tfc_env_id", "TFC Environment ID",
         "ID of the CloudBolt Environment this deployment was ordered into; "
         "its Azure handler supplied the workspace's ARM_* variables."),
        ("azure_subscription_id", "Azure Subscription ID",
         "Azure subscription the deployment targets (from the environment's "
         "resource handler)."),
        ("azure_tenant_id", "Azure Tenant ID",
         "Azure tenant the deployment targets (from the environment's "
         "resource handler)."),
        ("tfc_variable_names", "TFC Variable Names",
         "Comma-separated set of Terraform variables this deployment "
         "manages; read by the day-2 actions."),
        ("tfc_sensitive_variable_names", "TFC Sensitive Variable Names",
         "Comma-separated subset of tfc_variable_names written to the TFC "
         "workspace as sensitive. These have no tfc_var_* mirror and the "
         "day-2 actions refuse to edit them."),
        ("tfc_hcl_variable_names", "TFC HCL Variable Names",
         "Comma-separated subset of tfc_variable_names whose values are "
         "written to TFC as HCL (object/map/list) and mirrored as JSON; "
         "read by the day-2 actions to round-trip them."),
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
                label=label,
                description=description,
                type="STR",
                show_on_servers=False,
            ),
        )


def _config_version_progress(workspace_id):
    """progress_callback for TFCClient.wait_for_config_version."""
    def _callback(status, elapsed_seconds):
        set_progress(
            "Waiting for TFC to ingest the Terraform configuration for "
            "workspace {} (status: {}, {}s elapsed)...".format(
                workspace_id, status, int(elapsed_seconds)
            )
        )
    return _callback


def _is_blank_value(value):
    """None, a blank/whitespace string, or an empty dict/list -- all mean
    'unset', so the variable falls back to the template's own default."""
    if value is None:
        return True
    if isinstance(value, (dict, list)):
        return not value
    return str(value).strip() == ""


def _hydrate_resource(resource, outputs, variables, vm_name, sensitive_keys=()):
    """Store EVERY discovered state output + tfc_var_* mirrors and name the
    resource.

    Used on both terminal-success paths: after a real apply, and on the
    planned_and_finished reconcile path when the workspace already has a
    state version (no-op provision retry must not skip storage).

    The output set is whatever wait_for_outputs discovered in the applied
    state -- no declared allowlist. Each output gets a tfc_output_<name>
    custom field (created on the fly for names never seen before).

    Naming: prefer the template's authoritative vm_name OUTPUT, falling back
    to the order input, so the resource list maps 1:1 to the real VM
    (docs/agents/plugin-templates.md naming rule); the name is ALSO kept in
    custom fields for later units to look up.
    """
    for output_name in ensure_output_custom_fields(sorted(outputs)):
        value = outputs.get(output_name)
        if value is not None:
            resource.set_value_for_custom_field(
                "tfc_output_{}".format(output_name), str(value)
            )
    for variable_name, value in variables.items():
        if variable_name in sensitive_keys:
            # A sensitive value must never land in a custom field; TFC holds
            # the only copy (its list endpoint returns it as null too).
            continue
        # serialize_variable_mirror: dict/list values store as JSON so the
        # day-2 actions round-trip them back to hcl:true writes; scalars
        # store as str to match the STR custom-field type.
        resource.set_value_for_custom_field(
            "tfc_var_{}".format(variable_name),
            serialize_variable_mirror(value),
        )
    resource.name = str(outputs.get("vm_name") or vm_name)
    resource.save()


def run(job, **kwargs):
    """Provision through HCP Terraform with a plan-approval pause."""
    set_progress("Starting HCP Terraform VM provisioning...")
    logger.info("HCP Terraform VM build plugin started for job %s", job.id)

    # Cardinal rule 3: every templated input quoted. The funnel payload is
    # triple-quoted so a rendered Python-repr or JSON blob survives intact for
    # parse_params_payload to handle.
    # Funnel of the template's variables, vm_name included (Dynamic Panel ->
    # one generic input).
    params_json = """{{ parameters }}"""
    # -- the user's one placement choice; everything Azure is derived from it --
    env_id = "{{ env_id }}".strip()
    # -- TFC coordinates, all pinned per blueprint as hidden fields in the
    #    custom form --
    tfc_connection_info = "{{ tfc_connection_info }}".strip()
    tfc_organization = "{{ tfc_organization }}".strip()
    tfc_project = "{{ tfc_project }}".strip()
    tfc_repo_identifier = "{{ tfc_repo_identifier }}".strip()
    tfc_branch = "{{ tfc_branch }}".strip()
    tfc_working_directory = "{{ tfc_working_directory }}".strip()

    # ---- Validate the pinned coordinates --------------------------------
    missing_coords = [
        label
        for label, value in (
            ("tfc_connection_info", tfc_connection_info),
            ("tfc_organization", tfc_organization),
            ("tfc_project", tfc_project),
            ("tfc_repo_identifier", tfc_repo_identifier),
            ("tfc_branch", tfc_branch),
        )
        if not value or "FILL-ME" in value
    ]
    if missing_coords:
        return (
            "FAILURE",
            "",
            "TFC coordinates are missing: {}. They are pinned as hidden "
            "fields in the custom form of BP-b0qm83lh (forms/FRM-t3v8zpb7; "
            "see docs/hcp-terraform-setup.md).".format(", ".join(missing_coords)),
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
    # The dropdown was RBAC-filtered at order time; an env_id is still a
    # plain form value, so the ordering group's entitlement is re-verified
    # here and the handler is only ever touched inside subscription_context.
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
        # parse_params_payload (default/unwrapping mode) ast.literal_eval's the
        # CloudBolt-rendered Python repr, falls back to json.loads, and unwraps
        # the Dynamic Panel's single-item list[dict] down to a plain dict.
        # Parsing here -- before get_client() -- means a malformed payload
        # FAILS fast (the TFCError below converts it to a clean FAILURE) with
        # no workspace or run created.
        panel_variables = parse_params_payload(params_json)

        # ---- Pop the reserved '_sensitive' marker ---------------------------
        # Planted by the form as a hidden panel field listing the variable
        # names to write as sensitive TFC variables. Popped BEFORE the payload
        # becomes the variable set so the marker itself never reaches the
        # workspace as a Terraform variable.
        sensitive_names = pop_sensitive_marker(panel_variables)

        # ---- Validate the panel's vm_name (defense-in-depth on the form's
        #      regex validator) -- vm_name is captured INSIDE the Dynamic
        #      Panel with the rest of the template's variables.
        vm_name = str(panel_variables.get("vm_name") or "").strip()
        if not vm_name:
            return (
                "FAILURE",
                "",
                "A VM name is required. The custom form captures vm_name "
                "inside its Template Variables panel; it arrived blank.",
            )
        if not VM_NAME_RE.match(vm_name):
            return (
                "FAILURE",
                "",
                "VM name '{}' is invalid: 1-64 characters, letters, numbers "
                "and hyphens only, starting and ending with a letter or "
                "number.".format(vm_name),
            )

        # ---- Compose the workspace variable set ----------------------------
        # The form is the manifest: whatever the panel collected becomes the
        # workspace variables, with the normalized (stripped, regex-validated)
        # vm_name written back last so the stored value matches what was
        # validated. Then DROP any key whose value is blank (None, empty/
        # whitespace string, or empty dict/list): "left blank" must mean
        # "unset" so the variable falls back to the template's own default --
        # writing "" would fail number/bool conversion at plan time.
        #
        # Bool/number round-trip is a documented string-coercion dependency:
        # SurveyJS emits native JSON types, so ast.literal_eval yields Python
        # bool/int here; upsert_variables writes str(value) ("True"/"3") and
        # terraform's type conversion accepts those forms for bool/number
        # variables. dict/list values (tags) stay native too --
        # upsert_variables writes them as hcl:true JSON expressions. The
        # str()/HCL serialization happens at the upsert boundary.
        variables = {}
        for key, raw_value in panel_variables.items():
            # The tags matrixdynamic submits [{"key": ..., "value": ...}]
            # rows; collapse them to the map(string) shape terraform wants.
            # Other lists and all dicts pass through unchanged.
            value = collapse_key_value_rows(raw_value)
            if isinstance(value, dict):
                # Drop blank object entries (an untouched object-typed form
                # field submits "") so a fully-blank object falls back to the
                # template default instead of sending empty strings.
                value = {
                    entry_key: entry_value
                    for entry_key, entry_value in value.items()
                    if entry_value is not None
                    and str(entry_value).strip() != ""
                }
            variables[key] = value
        variables["vm_name"] = vm_name
        variables = {
            key: value
            for key, value in variables.items()
            if not _is_blank_value(value)
        }
        variable_names = list(variables.keys())
        # Only names actually submitted matter downstream; a marker entry for
        # a variable the template later drops from the form is harmless.
        sensitive_keys = sorted(set(sensitive_names) & set(variables))
        hcl_variable_names = sorted(
            key for key, value in variables.items()
            if isinstance(value, (dict, list))
        )

        _ensure_custom_fields(variable_names, sensitive_keys)

        client = get_client(tfc_organization, tfc_connection_info)

        # ---- Ensure the deployment's workspace (ID-first get-or-adopt) ----
        # The deterministic workspace name (cb-vm-<global_id>) is derived
        # inside ensure_workspace via workspace_name_for_resource() from the
        # resource's immutable global_id; the human-facing VM name belongs in
        # the workspace DESCRIPTION. The project, VCS repo/branch, and working
        # directory are pinned per blueprint and passed in here.
        stored_workspace_id = resource.get_value_for_custom_field("tfc_workspace_id")
        workspace = client.ensure_workspace(
            resource.global_id,
            project_name=tfc_project,
            repo_identifier=tfc_repo_identifier,
            branch=tfc_branch,
            working_directory=tfc_working_directory,
            description="CloudBolt deployment of VM '{}'".format(vm_name),
            stored_workspace_id=stored_workspace_id,
            source_url=portal_url_for_job(job),
        )
        workspace_id = workspace["id"]
        workspace_name = (workspace.get("attributes", {}) or {}).get("name", "")

        # ---- Store the workspace ID + coordinates IMMEDIATELY (R2) ---------
        # Before variables, runs, or polling: any later failure -- including
        # a rejected plan -- leaves a tracked, teardown-cleanable resource.
        # The coordinates are seeded onto the resource so the day-2 and
        # teardown plugins read them (they pass the connection + org to
        # get_client, and the day-2 actions read tfc_variable_names) without
        # re-pinning the values themselves.
        resource.set_value_for_custom_field("tfc_workspace_id", workspace_id)
        resource.set_value_for_custom_field("tfc_workspace_name", workspace_name)
        resource.set_value_for_custom_field("tfc_connection_info", tfc_connection_info)
        resource.set_value_for_custom_field("tfc_organization", tfc_organization)
        resource.set_value_for_custom_field("tfc_project", tfc_project)
        resource.set_value_for_custom_field("tfc_repo_identifier", tfc_repo_identifier)
        resource.set_value_for_custom_field("tfc_branch", tfc_branch)
        resource.set_value_for_custom_field("tfc_working_directory", tfc_working_directory)
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
            "TFC workspace '{}' ({}) recorded on the resource.".format(
                workspace_name, workspace_id
            )
        )

        # ---- Concurrency guard: fail fast on a pre-existing non-final run ---
        # A provision RETRY adopts the existing workspace (ID-first); if a
        # prior attempt left a non-final run on it (e.g. a jobengine restart
        # killed a paused approval, or a transient failure orphaned a run),
        # creating a second run would queue behind the orphan and burn the
        # full plan-phase timeout with no actionable error. Fail fast instead,
        # mirroring the day-2 guard. (Workspace ID is already stored above, so
        # the resource stays teardown-cleanable.)
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
                "A run is already pending on this deployment's TFC workspace "
                "'{}': {}. A prior provision attempt likely left it (e.g. a "
                "jobengine restart killed a paused approval). Discard the run "
                "in TFC, or delete this resource to let teardown clean it up, "
                "then retry -- see the recovery section of "
                "docs/hcp-terraform-setup.md.".format(
                    workspace_name or workspace_id, "; ".join(descriptions)
                ),
            )

        # ---- Variables + configuration version -----------------------------
        client.upsert_variables(
            workspace_id, variables, sensitive_keys=sensitive_keys
        )
        # Where to deploy: the environment's subscription/tenant as provider
        # env vars. Workspace variables override the same keys inherited from
        # the project's (non-priority) credentials variable set.
        client.upsert_variables(workspace_id, arm_variables, category="env")
        set_progress(
            "Workspace variables set from the order inputs; targeting Azure "
            "subscription {} via environment '{}'.".format(
                azure["subscription_id"], env.name
            )
        )
        client.wait_for_config_version(
            workspace_id,
            progress_callback=_config_version_progress(workspace_id),
            repo_identifier=tfc_repo_identifier,
            branch=tfc_branch,
        )

        # ---- Run with plan-approval pause -----------------------------------
        # No on_reject: the engine's discard suffices for provisioning. On
        # cancel, CancelJobException re-raises out of run() untouched (it is
        # a BaseException subclass and NOT a TFCError, so the handlers below
        # never see it), leaving the resource PROVFAILED with its workspace
        # ID stored -- teardown cleans it up.
        message = build_run_message(job.id, resource.global_id, BLUEPRINT_NAME)
        try:
            result = run_with_plan_approval(job, client, workspace_id, message)
        except TFCRunFailedError as exc:
            # The failure message already carries the run URL; persist the
            # URL on the resource too, then let the outer handler convert it
            # to the standard FAILURE return.
            if exc.run_url:
                resource.set_value_for_custom_field("tfc_run_url", exc.run_url)
                resource.save()
            raise

        resource.set_value_for_custom_field("tfc_run_url", result["run_url"])
        resource.save()

        # ---- Terminal success: hydrate the resource --------------------------
        # No output_names filter: EVERY output in the applied state is
        # discovered and recorded as a tfc_output_<name> custom field.
        outputs = client.wait_for_outputs(workspace_id)
        if result["status"] == RUN_CLASS_NO_CHANGES and outputs is None:
            # No-op provision against a workspace that has never applied
            # anything (no current state version): nothing to reconcile yet.
            return (
                "SUCCESS",
                "TFC run {} finished with no changes and workspace '{}' has "
                "no Terraform state yet; no outputs to record. Run URL: "
                "{}".format(result["run_id"], workspace_name, result["run_url"]),
                "",
            )

        # "applied" -- or "planned_and_finished" with existing state (no-op
        # provision retry): reconcile outputs, variable mirrors, and the
        # resource name from the workspace's current state (R3 / R11).
        _hydrate_resource(
            resource, outputs or {}, variables, vm_name,
            sensitive_keys=sensitive_keys,
        )
        set_progress("Stored TFC outputs and variable mirrors on the resource.")

        if result["status"] == RUN_CLASS_APPLIED:
            output_msg = (
                "VM '{}' provisioned via TFC workspace '{}' (run {}: {} to "
                "add, {} to change, {} to destroy). Run URL: {}".format(
                    resource.name, workspace_name, result["run_id"],
                    result.get("additions", "?"), result.get("changes", "?"),
                    result.get("destructions", "?"), result["run_url"],
                )
            )
        else:
            output_msg = (
                "TFC run {} found no changes to apply; resource reconciled "
                "from the current state of workspace '{}'. Run URL: "
                "{}".format(result["run_id"], workspace_name, result["run_url"])
            )
        return "SUCCESS", output_msg, ""

    except TFCError as exc:
        # Every shared-module failure (config, auth, validation, timeout,
        # failed run) -- including a malformed funnel payload from
        # parse_params_payload -- converts to the exemplar's failure-return
        # convention; the messages are operator-actionable and run failures
        # carry the run URL. CancelJobException is NOT a TFCError and passes
        # through.
        logger.exception("HCP Terraform VM provisioning failed")
        return "FAILURE", "", str(exc)
