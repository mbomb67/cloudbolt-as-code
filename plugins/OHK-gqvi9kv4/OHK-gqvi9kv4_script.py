"""
Generic Bicep build plugin.

Provisions infrastructure from a GitHub-hosted Bicep template by fetching the
repo archive, compiling it, assembling+validating parameters against the
compiled schema, previewing with what-if behind an approval gate, and deploying
an Azure deployment stack. No template-specific code lives here — the same
plugin backs the generic exemplar blueprint and every scaffold-generated
blueprint (scaffold-bicep wires typed inputs to this plugin via blueprint
parameters that land as resource custom fields).

Action inputs (OHK-gqvi9kv4_metadata.json):
  env_id (INT)         : Azure environment, gates RBAC (handler derived in run)
  resource_group (STR) : OPTIONAL target RG. Required by resource-group-scoped
                         templates; must be blank/ignored for subscription-
                         scoped ones (e.g. a template that creates resource
                         groups). The scope is detected from the compiled
                         template's $schema, never from the inputs.
  repo (STR)           : owner/repo of the template source
  template_path (STR)  : path to the .bicep (or .json) template in the repo
  ref (STR, optional)  : branch/tag/SHA; default branch if blank
  parameters (TXT)     : optional JSON of param values for direct (non-scaffold) use

RBAC: only env_id is exposed; the resource handler is cast inside run() and its
id stored on the resource (cardinal rule 4). The stack name derives from the
immutable resource global id and is stored BEFORE any Azure submission so every
failure mode leaves a tracked, teardown-cleanable resource (R2/R7).

Returns (status, output_msg, error_msg).
"""
import json
import shutil
import tempfile

from django.db.models import Q

from accounts.models import Group
from common.methods import set_progress
from infrastructure.models import CustomField, Environment
from utilities.logger import ThreadLogger

from shared_modules.github import GitHubConnection, CONNECTION_INFO_NAME
from shared_modules.bicep_engine import (
    BicepArmClient,
    BicepEngineError,
    assemble_parameters,
    compile_template,
    detect_target_scope,
    extract_parameter_schema,
    fetch_template_checkout,
    humanize_label,
    parse_params_payload,
    resolve_stack_location,
    run_with_approval,
    OUTPUT_ALLOWLIST,
    OUTPUT_CF_PREFIX,
    RESOURCE_NAME_OUTPUT_CANDIDATES,
    RESOURCE_NAME_PARAM_CANDIDATES,
    SCOPE_RESOURCE_GROUP,
    SCOPE_SUBSCRIPTION,
    SUPPORTED_SCOPES,
    VAR_CF_PREFIX,
)

logger = ThreadLogger(__name__)

STACK_NAME_PREFIX = "cb-bicep-"


def _ensure_custom_fields():
    """Idempotently pre-create the fixed bicep_* fields this plugin persists.

    `show_on_servers` / `show_as_attribute` here are **creation-time defaults
    only** — `get_or_create` applies `defaults` solely when the field is first
    created, so an operator who later retunes a field's visibility is never
    overridden on subsequent orders.
    """
    # (name, label, type, desc, show_on_servers, show_as_attribute)
    fields = [
        ("bicep_stack_name", "Bicep Stack Name", "STR",
         "Azure deployment stack name (derived from the resource global id).",
         False, True),
        ("bicep_stack_id", "Bicep Stack Resource ID", "STR",
         "Full ARM resource ID of the deployment stack.", False, True),
        ("bicep_pinned_params", "Bicep Pinned Parameters", "TXT",
         "JSON map of scaffold-pinned parameter values.", False, False),
        ("bicep_repo", "Bicep Repo", "STR", "owner/repo of the template source.",
         False, False),
        ("bicep_resource_group", "Bicep Resource Group", "STR",
         "Resource group containing the deployment stack (resource-group-scoped "
         "templates only; blank for subscription-scoped ones).",
         True, False),
        ("bicep_target_scope", "Bicep Target Scope", "STR",
         "Deployment scope of the compiled template: resourceGroup or "
         "subscription. Teardown/day-2 use it to address the stack.",
         False, True),
        ("bicep_template_path", "Bicep Template Path", "STR",
         "Template path within the repo.", False, False),
        ("bicep_ref", "Bicep Ref", "STR", "Git ref deployed.", False, False),
        ("bicep_rh_id", "Bicep Resource Handler ID", "INT",
         "ID of the Azure resource handler that owns this deployment.", True, False),
        ("bicep_env_id", "Bicep Environment ID", "INT",
         "ID of the CloudBolt Environment the order was provisioned into (used by "
         "follow-on build steps such as Add Resource Group to Environment).",
         False, False),
    ]
    for name, label, cf_type, desc, on_servers, as_attr in fields:
        CustomField.objects.get_or_create(
            name=name,
            defaults=dict(label=label, description=desc, type=cf_type,
                          show_on_servers=on_servers, show_as_attribute=as_attr),
        )


def _ensure_dynamic_cf(name, label, show_on_servers=False, show_as_attribute=False):
    """Create a per-parameter/output STR mirror field on demand (POC: STR keeps
    the global CF namespace type-uniform; see the plan's namespacing note).

    Visibility flags are creation-time defaults only — `get_or_create` never
    overrides an operator's later retuning of an existing field."""
    CustomField.objects.get_or_create(
        name=name,
        defaults=dict(label=label, description="Bicep-managed field.",
                      type="STR", show_on_servers=show_on_servers,
                      show_as_attribute=show_as_attribute),
    )


def _cf(resource, name):
    try:
        return resource.get_value_for_custom_field(name)
    except Exception:
        return None


def _resolve_group(group):
    """The group kwarg arrives as a Group or as its name depending on caller."""
    if group is None or isinstance(group, Group):
        return group
    return Group.objects.filter(name=str(group)).first()


def generate_options_for_env_id(field, **kwargs):
    """
    RBAC-aware Environment selector, restricted to Azure-backed environments.
    Offers every environment the ordering group is explicitly entitled to PLUS
    every *unconstrained* environment (one with no groups assigned at all) —
    both are usable by the group's members. `group__isnull=True` on the M2M
    matches environments with zero related groups.
    """
    group = _resolve_group(kwargs.get("group"))
    if not group:
        return []
    envs = Environment.objects.filter(
        Q(group__in=[group]) | Q(group__isnull=True),
        resource_handler__azurearmhandler__isnull=False,
    ).distinct()
    if not envs.exists():
        return [("", "------ No Azure environments available ------")]
    options = [(env.id, env.name) for env in envs]
    options.sort(key=lambda x: x[1])
    return options


def generate_options_for_resource_group(field, control_value=None, **kwargs):
    """List resource groups in the selected environment's subscription. The
    input is optional: a subscription-scoped template (one that creates
    resource groups) needs none, and the build ignores any selection then."""
    if not control_value:
        return [("", "------ Select an environment first ------")]
    try:
        env = Environment.objects.get(id=control_value)
    except (Environment.DoesNotExist, ValueError):
        return [("", "------ Invalid environment ------")]
    from resourcehandlers.azure_arm.models import AzureARMHandler
    rh = env.resource_handler.cast()
    if not isinstance(rh, AzureARMHandler):
        return [("", "------ Environment has no Azure handler ------")]
    try:
        from azure.mgmt.resource import ResourceManagementClient
        from resourcehandlers.azure_arm.azure_wrapper import configure_arm_client
        wrapper = rh.get_api_wrapper()
        client = configure_arm_client(wrapper, ResourceManagementClient)
        options = [(rg.name, rg.name) for rg in client.resource_groups.list()]
        options.sort(key=lambda x: x[1])
        return options or [("", "------ No resource groups ------")]
    except Exception as e:  # noqa: BLE001 — option generators degrade gracefully
        logger.debug(f"resource_group options error: {e}")
        return [("", "------ Could not list resource groups ------")]


def _supplied_parameters(resource, params_json, schema):
    """
    Assemble orderer-supplied parameter values from two sources, JSON input wins:
      (1) scaffold-typed inputs, which land as resource custom fields named
          exactly after the template parameter (read per schema param name);
      (2) the generic Parameters (JSON) order input (direct, non-scaffold use).
    """
    supplied = {}
    for pname in schema:
        val = _cf(resource, pname)
        if val is not None:
            supplied[pname] = val
    # parse_params_payload accepts a JSON or Python-literal string AND strips a
    # SurveyJS Dynamic Panel's single-item list[dict] down to the dict.
    supplied.update(parse_params_payload(params_json))
    return supplied


def run(job, **kwargs):
    resource = kwargs.get("resource")
    if resource is None:
        return "FAILURE", "", "No resource in job context."

    _ensure_custom_fields()

    # Quote/cast every templated input (cardinal rule 3).
    env_id = int("{{ env_id }}")
    # Optional input: an unset value may render as "" or as a literal None.
    resource_group = "{{ resource_group }}".strip()
    if resource_group.lower() in ("none", "null"):
        resource_group = ""
    repo = "{{ repo }}".strip()
    template_path = "{{ template_path }}".strip()
    ref = "{{ ref }}".strip() or None
    # JSON blob: triple-quote render + json.loads is the sanctioned dict pattern.
    params_json = """{{ parameters }}"""

    env = Environment.objects.get(id=env_id)
    from resourcehandlers.azure_arm.models import AzureARMHandler
    rh = env.resource_handler.cast()
    if not isinstance(rh, AzureARMHandler):
        return "FAILURE", "", "Selected environment is not backed by an Azure handler."

    # Store the resource-handler id and coordinates, then the deterministic stack
    # name — BEFORE anything else can fail — so teardown can always find it.
    resource.set_value_for_custom_field("bicep_rh_id", rh.id)
    resource.set_value_for_custom_field("bicep_env_id", env.id)
    resource.set_value_for_custom_field("bicep_repo", repo)
    resource.set_value_for_custom_field("bicep_resource_group", resource_group)
    resource.set_value_for_custom_field("bicep_template_path", template_path)
    if ref:
        resource.set_value_for_custom_field("bicep_ref", ref)
    stack_name = f"{STACK_NAME_PREFIX}{resource.global_id}"
    resource.set_value_for_custom_field("bicep_stack_name", stack_name)
    resource.save()

    workdir = tempfile.mkdtemp(prefix="bicep-build-")
    try:
        set_progress(f"Fetching {repo}@{ref or 'default-branch'}...")
        with GitHubConnection.from_connection_name(CONNECTION_INFO_NAME) as gh:
            checkout = fetch_template_checkout(gh, repo, ref, template_path, workdir)
        arm_template = compile_template(checkout, template_path)

        # Scope routing — one plugin for both kinds of template. The compiled
        # $schema says whether the template deploys INTO a resource group or
        # AT the subscription (e.g. it creates resource groups); the RG input
        # is required for the former and unused for the latter.
        scope = detect_target_scope(arm_template)
        if scope not in SUPPORTED_SCOPES:
            raise BicepEngineError(
                f"The template targets the '{scope}' scope; this engine deploys "
                f"resource-group- and subscription-scoped templates only.")
        if scope == SCOPE_RESOURCE_GROUP and not resource_group:
            raise BicepEngineError(
                "The template is resource-group scoped but no Resource Group was "
                "selected. Re-order and choose a Resource Group.")
        if scope == SCOPE_SUBSCRIPTION and resource_group:
            set_progress(f"Template is subscription-scoped; the selected resource "
                         f"group '{resource_group}' is not used.")
            resource_group = ""
            resource.set_value_for_custom_field("bicep_resource_group", "")
        resource.set_value_for_custom_field("bicep_target_scope", scope)
        resource.save()
        target_rg = resource_group or None
        set_progress(f"Template scope: {scope}"
                     + (f" (resource group {target_rg})" if target_rg else ""))

        schema = extract_parameter_schema(arm_template)

        pinned_raw = _cf(resource, "bicep_pinned_params")
        pinned = json.loads(pinned_raw) if pinned_raw else {}
        supplied = _supplied_parameters(resource, params_json, schema)
        arm_params = assemble_parameters(schema, pinned, supplied)
        # Subscription-scoped stacks/what-ifs need a deployment-metadata location.
        location = resolve_stack_location(arm_params) if target_rg is None else None

        client = BicepArmClient(rh)
        tags = {"cmp:resource-id": resource.global_id, "source-name": "CloudBolt"}
        result = run_with_approval(
            job, client, target_rg, stack_name, arm_template, arm_params,
            schema, tags=tags, description=f"CloudBolt resource {resource.global_id}",
            location=location,
        )
    except BicepEngineError as e:
        return "FAILURE", "", str(e)
    except Exception as e:  # noqa: BLE001 — surface a clean FAILURE, not a traceback
        logger.exception("Unexpected error during Bicep build")
        return "FAILURE", "", "Unexpected error during deployment; see the job log."
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    status = result.get("status")
    if status == "rejected":
        return ("FAILURE",
                "Deployment rejected at the approval gate; nothing was submitted.",
                "")
    if status == "invalid":
        return ("FAILURE", "",
                f"The requested deployment cannot be applied: {result.get('error')} "
                f"Nothing was submitted to Azure.")
    if status != "succeeded":
        err = result.get("error") or f"provisioning state: {result.get('state')}"
        return ("FAILURE", "",
                f"Deployment failed: {err} The resource is retained and can be "
                f"deleted to tear down any partial infrastructure.")

    try:
        _capture_outputs(resource, client, target_rg, stack_name, schema,
                         arm_params)
    except BicepEngineError as e:
        # Apply succeeded; output capture is best-effort.
        logger.warning(f"Output capture incomplete: {e}")
    where = f"resource group {target_rg}" if target_rg else "subscription scope"
    return ("SUCCESS",
            f"Deployed Bicep template as deployment stack {stack_name} at {where}.",
            "")


def _capture_outputs(resource, client, rg, stack_name, schema, arm_params):
    """Store the stack id, non-secure parameter mirrors, allowlisted outputs, and
    set resource.name from the designated output (R8) — or, when the template
    exposes no such output, from a designated parameter (e.g. the resource
    group name a subscription-scoped template was asked to create).
    `rg` is None for a subscription-scoped stack."""
    stack = client.get_stack(rg, stack_name) or {}
    props = stack.get("properties") or {}
    resource.set_value_for_custom_field("bicep_stack_id", stack.get("id", "") or "")

    # Non-secure parameter mirrors for day-2 pre-fill.
    for pname, wrapped in arm_params.items():
        if schema.get(pname, {}).get("secure"):
            continue
        cf = f"{VAR_CF_PREFIX}{pname}"
        _ensure_dynamic_cf(cf, humanize_label(pname), show_on_servers=True)
        val = wrapped.get("value")
        resource.set_value_for_custom_field(
            cf, val if isinstance(val, str) else json.dumps(val))

    # Outputs (secure outputs come back null and are skipped).
    name_candidates = [c.lower() for c in RESOURCE_NAME_OUTPUT_CANDIDATES]
    name_set = False
    for oname, odata in (props.get("outputs") or {}).items():
        if OUTPUT_ALLOWLIST is not None and oname not in OUTPUT_ALLOWLIST:
            logger.debug(f"Ignoring non-allowlisted output '{oname}'.")
            continue
        oval = odata.get("value") if isinstance(odata, dict) else odata
        if oval is None:
            continue
        cf = f"{OUTPUT_CF_PREFIX}{oname}"
        _ensure_dynamic_cf(cf, humanize_label(oname), show_as_attribute=True)
        resource.set_value_for_custom_field(
            cf, oval if isinstance(oval, str) else json.dumps(oval))
        if (not name_set and oname.lower() in name_candidates
                and isinstance(oval, str) and oval):
            resource.name = oval
            name_set = True
    if not name_set:
        # No naming output: fall back to a non-secure string parameter, in
        # RESOURCE_NAME_PARAM_CANDIDATES priority order.
        by_lower = {p.lower(): p for p in arm_params}
        for cand in RESOURCE_NAME_PARAM_CANDIDATES:
            pname = by_lower.get(cand.lower())
            if not pname or schema.get(pname, {}).get("secure"):
                continue
            pval = arm_params[pname].get("value")
            if isinstance(pval, str) and pval:
                resource.name = pval
                name_set = True
                break
    resource.save()
