"""
CloudBolt discovery plugin: HCP Terraform No-Code Module workspaces.

Syncs the "HCP Terraform No-Code Module" blueprint's (BP-00meiwwz)
deployments with HCP Terraform. CloudBolt's Sync Resources job (the
blueprint's "Sync Resources" button, or the built-in Sync Resources
recurring job) calls discover_resources(blueprint=<ServiceBlueprint>) and
reconciles the returned dictionaries against the blueprint's Resources by
RESOURCE_IDENTIFIER (tfc_workspace_id): a matching Resource is updated, an
unmatched dictionary creates one, and a Resource whose workspace is gone is
marked Historical.

Scope: ONLY workspaces created from the no-code module the blueprint is
pinned to. The module's registry identity (namespace / name / provider) is
resolved from the pinned nocode-* ID, and a workspace is in scope when its
source-module-id names that module -- whatever version it runs, whichever
project it sits in, and whether CloudBolt or someone in the HCP UI created
it. Workspaces of other modules (including the VCS-based HCP Terraform VM
blueprint's cb-vm-* workspaces) are never touched.

Coordinates (connection, organization, project, module ID) are the
blueprint's single-option parameters (env_options.blueprint_pinned_inputs).
A blueprint that has not pinned all four fails the sync before anything is
read.

Per workspace the dictionary refreshes the facts the build and Deploy Latest
Version plugins record: workspace name, module name / running version and
HCP's update-available flag, the latest FINAL run (ID, URL, status), the
current state outputs (tfc_output_*), the terraform variables (tfc_var_*
mirrors of the non-sensitive ones plus the variable-name sets), the Azure
subscription / tenant the workspace targets and the CloudBolt Environment
that supplies them. Sensitive variable values are never read into CloudBolt.

A workspace with no CloudBolt Resource is onboarded. Its group and owner
come from the Resource its cmp:resource-id tag or cb-nc-<global_id> name
points at when that Resource still exists (a deployment deleted in CloudBolt
but not in HCP), else CloudBolt's Unassigned group. Its name is the state's
name / vm_name output, else the workspace name. Existing Resources keep
their name, group and owner.

A Resource whose stored workspace HCP answers 404 for is returned with
lifecycle HISTORICAL. The blueprint keeps auto_historical_resources false on
purpose: that flag retires EVERY active Resource the plugin did not return,
including a deployment mid-provision that has no workspace yet, whereas
this plugin retires only on a confirmed 404.

Failure model: an error while establishing scope (missing coordinates, auth,
unknown module, workspace listing) raises, so the sync job fails and no
Resource changes. A per-workspace detail error (variables, outputs, runs,
tags) is logged and that workspace's dictionary carries what was read.

Entry point: discover_resources(blueprint=None, **kwargs) -> list[dict]
"""

from common.methods import set_progress
from infrastructure.models import Environment
from resources.models import Resource
from utilities.logger import ThreadLogger

from shared_modules.env_options import (
    EnvOptionsError,
    blueprint_pinned_inputs,
    subscription_context,
)
from shared_modules.tfc_api import (
    FIELD_VISIBILITY_ATTRIBUTE,
    FIELD_VISIBILITY_HIDDEN,
    FIELD_VISIBILITY_PARAMETER,
    NO_CODE_WORKSPACE_NAME_PREFIX,
    POLL_INTERVAL_SECONDS,
    RESOURCE_ID_TAG_KEY,
    TERMINAL_ABANDONED_STATUSES,
    TFCConfigError,
    TFCError,
    TFCNotFoundError,
    TFCTimeoutError,
    ensure_custom_field,
    ensure_output_custom_fields,
    get_client,
    workspace_from_module,
    workspace_module_info,
)

logger = ThreadLogger(__name__)

# CloudBolt matches discovered dictionaries to this blueprint's Resources on
# this custom field -- the one the build plugin stores first, before any run.
RESOURCE_IDENTIFIER = "tfc_workspace_id"

# The blueprint parameters that pin the blueprint to its module.
COORDINATE_INPUTS = (
    "tfc_connection_info",
    "tfc_organization",
    "tfc_project",
    "tfc_nocode_module_id",
)

# Run statuses after which no CloudBolt job can still own the run. Only a
# final run updates tfc_run_id / tfc_run_url: a non-final run belongs to a
# live provision / day-2 job and its attribution must not be disturbed.
FINAL_RUN_STATUSES = frozenset({"applied", "planned_and_finished", "errored"}) | set(
    TERMINAL_ABANDONED_STATUSES
)

# Workspace env-category variables the build writes from the Environment's
# Azure handler (azurerm provider names) and the resource fields they mirror.
ARM_SUBSCRIPTION_VARIABLE = "ARM_SUBSCRIPTION_ID"
ARM_TENANT_VARIABLE = "ARM_TENANT_ID"

# Custom fields this plugin writes, with the same creation defaults the build
# plugin (OHK-axtt0yqq) and Deploy Latest Version (OHK-4y8f1vff) use, so a
# first sync on an instance that never provisioned creates them identically.
# Deliberate copy -- plugins cannot import one another. ensure_custom_field is
# get_or_create: an existing field (and any admin edit to it) is untouched.
FIELDS = (
    ("tfc_workspace_id", "TFC Workspace ID",
     "ID of the dedicated HCP Terraform workspace backing this deployment.",
     FIELD_VISIBILITY_PARAMETER, "STR"),
    ("tfc_workspace_name", "TFC Workspace Name",
     "Name of the dedicated HCP Terraform workspace backing this deployment.",
     FIELD_VISIBILITY_ATTRIBUTE, "STR"),
    ("tfc_run_url", "TFC Run URL",
     "URL of the most recent HCP Terraform run for this deployment.",
     FIELD_VISIBILITY_ATTRIBUTE, "STR"),
    ("tfc_run_id", "TFC Run ID",
     "ID of the run this deployment's provision job adopted/created; the "
     "attribution source for the day-2 and teardown non-final-run guards "
     "(the no-code auto-queued run's message is TFC-authored).",
     FIELD_VISIBILITY_PARAMETER, "STR"),
    ("tfc_run_status", "TFC Run Status",
     "HCP Terraform status of the workspace's most recent run as of the last "
     "provision, day-2 action or resource sync (applied, errored, "
     "discarded, planning, ...).",
     FIELD_VISIBILITY_PARAMETER, "STR"),
    ("tfc_connection_info", "TF Cloud Connection",
     "Global ID of the 'tf-cloud'-labeled ConnectionInfo this deployment "
     "provisions through; read by the day-2 and teardown actions.",
     FIELD_VISIBILITY_HIDDEN, "STR"),
    ("tfc_organization", "TFC Organization",
     "HCP Terraform organization this deployment's workspace lives in.",
     FIELD_VISIBILITY_PARAMETER, "STR"),
    ("tfc_project", "TFC Project",
     "HCP Terraform project this deployment's workspace lives in.",
     FIELD_VISIBILITY_PARAMETER, "STR"),
    ("tfc_nocode_module_id", "TFC No-Code Module ID",
     "The nocode-* module this deployment was provisioned from.",
     FIELD_VISIBILITY_PARAMETER, "STR"),
    ("tfc_nocode_module_name", "TFC No-Code Module Name",
     "Registry name of the no-code module the deployment's workspace runs "
     "(from the workspace's source-module-id); recorded at provision and "
     "refreshed by the Deploy Latest Version action.",
     FIELD_VISIBILITY_PARAMETER, "STR"),
    ("tfc_nocode_module_version", "TFC No-Code Module Version",
     "Module version the deployment's workspace currently runs (from the "
     "workspace's source-module-id); recorded at provision and refreshed by "
     "the Deploy Latest Version action. HCP owns the version pin.",
     FIELD_VISIBILITY_PARAMETER, "STR"),
    ("tfc_nocode_upgrade_available", "TFC Module Update Available",
     "True when HCP Terraform reports that the no-code module's pinned "
     "version is newer than the one this deployment's workspace runs "
     "(the workspace's no-code-upgrade-available flag, as of the last "
     "resource sync). Deploy Latest Version clears it.",
     FIELD_VISIBILITY_PARAMETER, "BOOL"),
    ("tfc_env_id", "TFC Environment ID",
     "ID of the CloudBolt Environment this deployment was ordered into; "
     "its Azure handler supplied the workspace's ARM_* variables.",
     FIELD_VISIBILITY_HIDDEN, "STR"),
    ("azure_subscription_id", "Azure Subscription ID",
     "Azure subscription the deployment targets (from the environment's "
     "resource handler).",
     FIELD_VISIBILITY_PARAMETER, "STR"),
    ("azure_tenant_id", "Azure Tenant ID",
     "Azure tenant the deployment targets (from the environment's "
     "resource handler).",
     FIELD_VISIBILITY_PARAMETER, "STR"),
    ("tfc_variable_names", "TFC Variable Names",
     "Comma-separated set of Terraform variables this deployment manages; "
     "read by the day-2 actions.",
     FIELD_VISIBILITY_HIDDEN, "STR"),
    ("tfc_sensitive_variable_names", "TFC Sensitive Variable Names",
     "Comma-separated subset of tfc_variable_names written to the TFC "
     "workspace as sensitive. These have no tfc_var_* mirror and the "
     "day-2 actions refuse to edit them.",
     FIELD_VISIBILITY_HIDDEN, "STR"),
    ("tfc_hcl_variable_names", "TFC HCL Variable Names",
     "Comma-separated subset of tfc_variable_names whose values are written "
     "to TFC as HCL (object/map/list) and mirrored as JSON; read by the "
     "day-2 actions to round-trip them.",
     FIELD_VISIBILITY_HIDDEN, "STR"),
)


def _ensure_fields(variable_names):
    for name, label, description, visibility, field_type in FIELDS:
        ensure_custom_field(name, label, description, visibility=visibility, field_type=field_type)
    for variable_name in variable_names:
        ensure_custom_field(
            "tfc_var_{}".format(variable_name),
            "TFC Variable: {}".format(variable_name),
            "Mirror of the '{}' variable on this deployment's TFC workspace.".format(variable_name),
            visibility=FIELD_VISIBILITY_PARAMETER,
        )


def _coordinates(blueprint):
    """The blueprint's pinned TFC coordinates, or a TFCConfigError naming what
    is missing (the sync must fail loudly rather than scan the wrong org)."""
    pins = blueprint_pinned_inputs(blueprint, COORDINATE_INPUTS)
    coordinates = {}
    missing = []
    for name in COORDINATE_INPUTS:
        value = str(pins.get(name) or "").strip()
        if not value or "FILL-ME" in value:
            missing.append(name)
        coordinates[name] = value
    if missing:
        raise TFCConfigError(
            "Blueprint '{}' does not pin {}. Pin each as a blueprint parameter "
            "with exactly one option (Parameters tab, destination Resource; "
            "docs/hcp-no-code-setup.md section 6).".format(
                blueprint.name, ", ".join(missing)
            )
        )
    return coordinates


def _existing_resources(blueprint):
    """{workspace id: [Resource, ...]} for this blueprint's non-historical
    Resources that record a workspace. The build stores tfc_workspace_id
    before any run, so a mid-provision deployment is included (and must only
    be refreshed, never renamed, regrouped or retired)."""
    by_workspace = {}
    for resource in blueprint.resource_set.exclude(lifecycle="HISTORICAL"):
        workspace_id = str(resource.get_value_for_custom_field(RESOURCE_IDENTIFIER) or "").strip()
        if workspace_id:
            by_workspace.setdefault(workspace_id, []).append(resource)
    return by_workspace


def _primary(resources):
    """The Resource CloudBolt will match a dictionary to: the single ACTIVE
    one when several share a workspace, else the first."""
    active = [resource for resource in resources if resource.lifecycle == "ACTIVE"]
    if len(active) == 1:
        return active[0]
    return resources[0] if resources else None


def _azure_environments_by_subscription():
    """{subscription id (lowercased): [Environment, ...]} over every Azure-
    backed Environment. Same handler-type narrowing as
    env_options.available_environments; entitlement is not a concern here
    (the sync runs as the system, and the Environment is recorded for the
    day-2 actions, which re-check the resource's group against it)."""
    index = {}
    environments = Environment.objects.filter(resource_handler__azurearmhandler__isnull=False)
    for environment in environments:
        try:
            subscription_id = subscription_context(environment)["subscription_id"].lower()
        except EnvOptionsError:
            continue
        if subscription_id:
            index.setdefault(subscription_id, []).append(environment)
    return index


def _environment_id_for(existing, subscription_id, environment_index):
    """The CloudBolt Environment id to record for a workspace targeting
    ``subscription_id``: the existing resource's Environment when it still
    matches that subscription (it was the orderer's choice), else the ONE
    Azure Environment on that subscription, else None (ambiguous: left to
    the operator)."""
    if not subscription_id:
        return None
    candidates = environment_index.get(subscription_id.lower(), [])
    if existing is not None:
        recorded = str(existing.get_value_for_custom_field("tfc_env_id") or "").strip()
        if recorded and any(str(environment.id) == recorded for environment in candidates):
            return recorded
    if len(candidates) == 1:
        return str(candidates[0].id)
    return None


def _variables(client, workspace_id):
    """(terraform values by key, env values by key, sensitive keys, hcl keys)
    from the workspace's own variables. Sensitive values are never returned
    for either category; their keys are kept so the name sets stay honest."""
    terraform, env_values = {}, {}
    sensitive, hcl = set(), set()
    for record in client.list_variable_records(workspace_id):
        attributes = record.get("attributes", {}) or {}
        key = attributes.get("key")
        if not key:
            continue
        if attributes.get("category") == "env":
            if not attributes.get("sensitive"):
                env_values[key] = attributes.get("value")
            continue
        if attributes.get("sensitive"):
            sensitive.add(key)
            terraform[key] = None
            continue
        if attributes.get("hcl"):
            hcl.add(key)
        terraform[key] = attributes.get("value")
    return terraform, env_values, sensitive, hcl


def _name_set(existing, field_name, names):
    """Comma-joined ``names`` in the order the resource already stores them
    when the SET is unchanged (the build writes form order; re-sorting it
    would log a spurious change on every sync), else sorted."""
    new_names = sorted(names)
    if existing is not None:
        current = str(existing.get_value_for_custom_field(field_name) or "")
        current_names = [name for name in current.split(",") if name]
        if set(current_names) == set(new_names):
            return current
    return ",".join(new_names)


def _outputs(client, workspace_id):
    """Current state outputs, {} when the workspace has no state, or None
    when HCP is still processing a new state version (the sync does not
    wait for an apply that is in flight; the next sync picks it up)."""
    try:
        outputs = client.wait_for_outputs(workspace_id, timeout_seconds=POLL_INTERVAL_SECONDS)
    except TFCTimeoutError:
        logger.info(
            "Workspace %s: state outputs still being processed; skipping outputs this sync.",
            workspace_id,
        )
        return None
    return outputs if outputs is not None else {}


def _run_facts(client, workspace_id, workspace_name):
    """tfc_run_status for the newest run, plus tfc_run_id / tfc_run_url when
    that run is final (see FINAL_RUN_STATUSES); {} for a never-run
    workspace."""
    run = client.latest_run(workspace_id)
    if run is None:
        return {}
    status = str((run.get("attributes", {}) or {}).get("status") or "")
    facts = {"tfc_run_status": status}
    if status in FINAL_RUN_STATUSES and run.get("id"):
        facts["tfc_run_id"] = run["id"]
        facts["tfc_run_url"] = client.run_app_url(run["id"], workspace_name)
    return facts


def _prior_resource(client, workspace_id, workspace_name):
    """The Resource an unmanaged workspace once belonged to, if CloudBolt
    still has it: by the cmp:resource-id tag the build applies, else by the
    global ID embedded in the deterministic cb-nc-<global_id> name. None for
    a workspace created outside CloudBolt."""
    global_ids = []
    try:
        for binding in client.get_workspace_tag_bindings(workspace_id):
            if str(binding.get("key", "")).lower() == RESOURCE_ID_TAG_KEY.lower():
                global_ids.append(str(binding.get("value", "")))
    except TFCError as exc:
        logger.warning("Workspace %s: could not read tag bindings (%s).", workspace_id, exc)
    lowered = str(workspace_name or "").lower()
    if lowered.startswith(NO_CODE_WORKSPACE_NAME_PREFIX):
        global_ids.append(lowered[len(NO_CODE_WORKSPACE_NAME_PREFIX):])
    for global_id in global_ids:
        global_id = global_id.strip()
        if not global_id:
            continue
        resource = Resource.objects.filter(global_id__iexact=global_id).first()
        if resource is not None:
            return resource
    return None


def _workspace_record(client, workspace, coordinates, project_names, existing, environment_index):
    """One discovery dictionary for one in-scope workspace document."""
    attributes = workspace.get("attributes", {}) or {}
    workspace_id = workspace["id"]
    workspace_name = attributes.get("name") or workspace_id
    info = workspace_module_info(workspace)
    project_id = (
        (((workspace.get("relationships") or {}).get("project") or {}).get("data") or {}).get("id")
    )

    record = {
        RESOURCE_IDENTIFIER: workspace_id,
        "tfc_workspace_name": workspace_name,
        "tfc_connection_info": coordinates["tfc_connection_info"],
        "tfc_organization": coordinates["tfc_organization"],
        "tfc_nocode_module_id": coordinates["tfc_nocode_module_id"],
    }
    project_name = project_names.get(project_id)
    if project_name:
        record["tfc_project"] = project_name
    elif existing is None:
        # Unresolvable project (the token cannot read projects): the pinned
        # project is the best statement for a new resource; an existing one
        # keeps what provisioning recorded.
        record["tfc_project"] = coordinates["tfc_project"]
    if info["name"]:
        record["tfc_nocode_module_name"] = info["name"]
    if info["version"]:
        record["tfc_nocode_module_version"] = info["version"]
    if info["upgrade_available"] is not None:
        record["tfc_nocode_upgrade_available"] = bool(info["upgrade_available"])

    # ---- Variables: mirrors + name sets + the ARM_* environment context ----
    variable_names = []
    try:
        terraform, env_values, sensitive, hcl = _variables(client, workspace_id)
    except TFCError as exc:
        logger.warning("Workspace %s: could not read variables (%s).", workspace_id, exc)
    else:
        variable_names = sorted(terraform)
        _ensure_fields([name for name in variable_names if name not in sensitive])
        for key, value in terraform.items():
            if key in sensitive or value is None:
                continue
            record["tfc_var_{}".format(key)] = str(value)
        record["tfc_variable_names"] = _name_set(existing, "tfc_variable_names", variable_names)
        record["tfc_sensitive_variable_names"] = _name_set(
            existing, "tfc_sensitive_variable_names", sensitive
        )
        record["tfc_hcl_variable_names"] = _name_set(existing, "tfc_hcl_variable_names", hcl)
        subscription_id = str(env_values.get(ARM_SUBSCRIPTION_VARIABLE) or "").strip()
        tenant_id = str(env_values.get(ARM_TENANT_VARIABLE) or "").strip()
        if subscription_id:
            record["azure_subscription_id"] = subscription_id
        if tenant_id:
            record["azure_tenant_id"] = tenant_id
        environment_id = _environment_id_for(existing, subscription_id, environment_index)
        if environment_id:
            record["tfc_env_id"] = environment_id
    if not variable_names:
        _ensure_fields([])

    # ---- Latest run -------------------------------------------------------
    try:
        record.update(_run_facts(client, workspace_id, workspace_name))
    except TFCError as exc:
        logger.warning("Workspace %s: could not read runs (%s).", workspace_id, exc)

    # ---- State outputs ----------------------------------------------------
    outputs = None
    try:
        outputs = _outputs(client, workspace_id)
    except TFCError as exc:
        logger.warning("Workspace %s: could not read state outputs (%s).", workspace_id, exc)
    if outputs:
        for output_name in ensure_output_custom_fields(sorted(outputs)):
            value = outputs.get(output_name)
            if value is not None:
                record["tfc_output_{}".format(output_name)] = str(value)

    # ---- Identity for a workspace CloudBolt does not track yet -----------
    if existing is None:
        prior = _prior_resource(client, workspace_id, workspace_name)
        if prior is not None:
            record["group"] = prior.group
            if prior.owner is not None:
                record["owner"] = prior.owner
        chosen_name = None
        if outputs:
            chosen_name = outputs.get("name") or outputs.get("vm_name")
        record["name"] = str(chosen_name or workspace_name)

    return record


def discover_resources(blueprint=None, **kwargs):
    """Return one dictionary per workspace created from the blueprint's
    pinned no-code module, plus a HISTORICAL marker for each tracked
    workspace HCP Terraform no longer has."""
    if blueprint is None:
        raise ValueError("discover_resources requires the blueprint kwarg CloudBolt passes.")

    coordinates = _coordinates(blueprint)
    organization = coordinates["tfc_organization"]
    module_id = coordinates["tfc_nocode_module_id"]
    client = get_client(organization, coordinates["tfc_connection_info"])

    identity = client.no_code_module_identity(module_id)
    module_label = "/".join(
        part for part in (identity["namespace"], identity["name"], identity["provider"]) if part
    ) or module_id
    set_progress(
        "Discovering workspaces created from no-code module {} ({}) in organization '{}'...".format(
            module_label, module_id, organization
        )
    )

    workspaces = [
        workspace for workspace in client.list_workspaces()
        if workspace_from_module(workspace, identity)
    ]
    workspaces.sort(key=lambda workspace: str((workspace.get("attributes") or {}).get("name") or ""))
    set_progress("{} workspace(s) match the module.".format(len(workspaces)))

    try:
        project_names = client.project_names_by_id()
    except TFCError as exc:
        logger.warning("Could not list projects in '%s' (%s); project names unresolved.", organization, exc)
        project_names = {}

    existing_by_workspace = _existing_resources(blueprint)
    environment_index = _azure_environments_by_subscription()

    records = []
    seen = set()
    for workspace in workspaces:
        workspace_id = workspace["id"]
        seen.add(workspace_id)
        existing = _primary(existing_by_workspace.get(workspace_id, []))
        workspace_name = (workspace.get("attributes") or {}).get("name") or workspace_id
        set_progress(
            "Reading workspace '{}' ({}){}...".format(
                workspace_name, workspace_id,
                "" if existing is None else " for resource '{}'".format(existing.name),
            )
        )
        records.append(
            _workspace_record(
                client, workspace, coordinates, project_names, existing, environment_index
            )
        )

    # ---- Tracked workspaces HCP no longer has: retire on a confirmed 404 --
    for workspace_id, resources in existing_by_workspace.items():
        if workspace_id in seen:
            continue
        active = [resource for resource in resources if resource.lifecycle == "ACTIVE"]
        if not active:
            continue
        try:
            client.get_workspace(workspace_id)
        except TFCNotFoundError:
            set_progress(
                "Workspace {} of resource '{}' no longer exists in HCP Terraform; marking "
                "it Historical.".format(workspace_id, active[0].name)
            )
            records.append({RESOURCE_IDENTIFIER: workspace_id, "lifecycle": "HISTORICAL"})
        except TFCError as exc:
            logger.warning(
                "Workspace %s of resource '%s' could not be checked (%s); left unchanged.",
                workspace_id, active[0].name, exc,
            )
        else:
            set_progress(
                "Workspace {} of resource '{}' exists but was not created from module {}; "
                "left unchanged.".format(workspace_id, active[0].name, module_label)
            )

    set_progress("Discovered {} workspace record(s) for '{}'.".format(len(records), blueprint.name))
    return records
