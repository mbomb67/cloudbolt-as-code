"""
Run Azure PowerShell Script -- the single enabled build step of the
"Run an Azure PowerShell Script" blueprint (BP-jvvjm3te).

The blueprint carries a catalog of DISABLED Remote Script build items. The
orderer picks an Environment and one catalog script; this plugin then

1. resolves the Environment to its Azure resource handler (RBAC-gated through
   group.get_available_environments(), never exposed to the orderer),
2. mints a short-lived ARM access token for the handler's app registration,
3. renders the chosen Remote Script exactly as CloudBolt would (same template
   engine, same action-input context, same stripped-secret bookkeeping),
4. prepends a Connect-AzAccount block that signs the Az PowerShell session in
   with that token, and
5. runs the result on the Remote Script's own "Run on Server" over WinRM with
   the Remote Script's credentials, timeout and cleanup settings.

The author's script is byte-identical to what a native Remote Script run would
execute, so existing scripts need no changes: inputs are declared as Remote
Script action inputs and referenced with the usual template variables, and the
Az context is already established when the script's first line runs. Scripts
must NOT call Connect-AzAccount themselves (they hold no credentials); use
Set-AzContext to switch subscriptions within the tenant.

Action inputs (all three come from the order form):
- env_id (INT, required): CloudBolt Environment backed by an Azure handler.
- script (STR, required): global_id of a Remote Script that is a DISABLED
  build item of this blueprint. Options come from generate_options_for_script.
- script_parameters (TXT, optional): JSON object of the chosen script's
  action-input values keyed by input name, e.g. {"name_filter": "rg-prod-*"}.
  Missing keys fall back to the build item's pinned defaults.

Extra template variables this runner adds to the Remote Script's context:
cb_azure_subscription_id, cb_azure_tenant_id, cb_azure_location and
cb_target_environment (the selected environment's name). The standard
server/environment/group variables resolve to the jump host, as they do for
any Remote Script that uses "Run on Server".

Azure access tokens: default lifetime is a random 60-90 minutes
(https://learn.microsoft.com/en-us/entra/identity-platform/configurable-token-lifetimes).
The remaining lifetime is logged; scripts expected to run longer than that
should be split or moved to the client-secret sign-in path.

Internal CloudBolt APIs used (verified against CloudBolt 2026.1 source; recheck
on upgrade): RemoteScriptHook.render_module_file, .get_script_args,
.cannot_run_script, .get_runas_credentials, .validate_action_input_values,
cbhooks.services.parsing.get_hook_action_input_dict, Server.execute_script.
"""

import ast
import json
import time

from azure.identity import ClientSecretCredential
from django.core.exceptions import ValidationError

from cbhooks.services.parsing import get_hook_action_input_dict
from common.methods import set_progress
from orders.models import BlueprintOrderItem
from servicecatalog.models import (
    RunCloudBoltHookServiceItem,
    RunRemoteScriptHookServiceItem,
    ServiceBlueprint,
    ServiceItem,
)
from utilities.events import add_server_event
from utilities.logger import ThreadLogger

from shared_modules.env_options import (
    EnvOptionsError,
    azure_handler,
    entitled_environment,
    environment_options,
    resolve_group,
    subscription_context,
)

logger = ThreadLogger(__name__)

# Must match this plugin's "name" in its metadata; used only as the last-resort
# way to find the blueprint(s) this runner is a build item of when the option
# generator is called without a blueprint in context.
ACTION_NAME = "Run Azure PowerShell Script"
ARM_SCOPE = "https://management.azure.com/.default"
OUTPUT_TAIL_CHARS = 4000


# -----------------------------------------------------------------------------
# Option generators
# -----------------------------------------------------------------------------

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


def generate_options_for_script(field=None, **kwargs):
    """The blueprint's script catalog: every DISABLED Remote Script build
    item, labelled by its item name. The option value is the Remote Script's
    global_id, which run() re-validates against the same catalog so an edited
    request cannot run an arbitrary action."""
    blueprints = _blueprints_from_kwargs(kwargs)
    if not blueprints:
        return [("", "------ Open this item from its blueprint to list scripts ------")]
    options = []
    seen = set()
    for blueprint in blueprints:
        for item in _script_catalog(blueprint):
            if item.hook.global_id in seen:
                continue
            seen.add(item.hook.global_id)
            options.append((item.hook.global_id, item.name or item.hook.name))
    return options or [("", "------ No disabled Remote Script items on this blueprint ------")]


def _blueprints_from_kwargs(kwargs):
    """Blueprint(s) to list scripts for. CloudBolt passes `blueprint` on most
    option-generation paths; fall back to the service item, then to every
    blueprint that carries this runner as a build item."""
    blueprint = kwargs.get("blueprint")
    if isinstance(blueprint, ServiceBlueprint):
        return [blueprint]
    if blueprint:
        text = str(blueprint).strip().rstrip("/").rsplit("/", 1)[-1]
        found = ServiceBlueprint.objects.filter(global_id=text).first()
        if found is None and text.isdigit():
            found = ServiceBlueprint.objects.filter(id=int(text)).first()
        if found is not None:
            return [found]
    service_item = kwargs.get("service_item")
    if service_item is not None and getattr(service_item, "blueprint_id", None):
        return [service_item.blueprint]
    runner_items = RunCloudBoltHookServiceItem.objects.filter(
        hook__name=ACTION_NAME, enabled=True
    ).select_related("blueprint")
    return list({item.blueprint for item in runner_items})


def _script_catalog(blueprint):
    return (
        RunRemoteScriptHookServiceItem.objects.filter(blueprint=blueprint, enabled=False)
        .select_related("hook")
        .order_by("deploy_seq", "id")
    )


# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------

def _find_order(job):
    """A build-item job's own parameters are HookParameters, which carry no
    order; the parent deploy job's parameters are the blueprint order item,
    which does. Walk up until an order is found."""
    current = job
    while current is not None:
        order = current.get_order()
        if order is not None:
            return order
        current = current.parent_job
    return None


def _resolve_order_context(job, kwargs):
    """(blueprint, group) for this deployment. Build-item jobs carry the
    blueprint order item id and the service item id in their context (kwargs
    here); the job's order chain is the fallback."""
    blueprint = None
    order = None
    boi_id = kwargs.get("blueprint_order_item")
    if boi_id:
        boi = BlueprintOrderItem.objects.filter(id=boi_id).select_related("blueprint", "order").first()
        if boi is not None:
            blueprint, order = boi.blueprint, boi.order
    if blueprint is None:
        service_item_id = kwargs.get("service_item_id")
        if service_item_id:
            item = ServiceItem.objects.filter(id=service_item_id).select_related("blueprint").first()
            if item is not None:
                blueprint = item.blueprint
    if order is None:
        order = _find_order(job)
    if blueprint is None and order is not None:
        order_item = order.top_level_items.first()
        if order_item is not None:
            blueprint = getattr(order_item.cast(), "blueprint", None)
    group = getattr(order, "group", None) if order is not None else None
    return blueprint, group


def _parse_parameters(raw):
    """script_parameters arrives rendered through the template engine: a JSON
    string typed on the form, or the str() of a dict/list when it came from a
    Dynamic Panel. Accept both; a single-item list collapses to its dict."""
    text = (raw or "").strip()
    if not text:
        return {}
    try:
        value = ast.literal_eval(text)
    except (ValueError, SyntaxError):
        value = json.loads(text)
    if isinstance(value, list) and len(value) == 1 and isinstance(value[0], dict):
        value = value[0]
    if not isinstance(value, dict):
        raise ValueError("script_parameters must be a JSON object of input name/value pairs.")
    return value


def _apply_item_defaults(item, hook, values):
    """Fill inputs the orderer left out from the build item's pinned defaults,
    the way the native order form treats a hidden default."""
    values = dict(values)
    for mapping in item.input_mappings.select_related("hook_input", "default_value"):
        if mapping.default_value is None:
            continue
        name = mapping.hook_input.name.replace("_a{}".format(hook.id), "")
        if values.get(name) in (None, ""):
            values[name] = mapping.default_value.value
    return values


def _validate_inputs(hook, values, **context):
    """Run CloudBolt's own required/option checks. Keys must be the raw input
    names (with the _a<hook id> suffix) for these validators."""
    raw = {}
    for hook_input in hook.input_fields.all():
        clean = hook_input.name.replace("_a{}".format(hook.id), "")
        if clean in values:
            raw[hook_input.name] = values[clean]
    unknown = sorted(set(values) - {k.replace("_a{}".format(hook.id), "") for k in raw})
    if unknown:
        return "Unknown input(s) for '{}': {}".format(hook.name, ", ".join(unknown))
    try:
        hook.validate_action_input_values(raw, **context)
    except ValidationError as exc:
        return "; ".join(exc.messages)
    return ""


def _mint_token(rh, tenant_id):
    # azure-identity ClientSecretCredential, the same class CloudBolt's own
    # Azure wrapper uses: https://learn.microsoft.com/en-us/python/api/azure-identity/azure.identity.clientsecretcredential
    credential = ClientSecretCredential(
        tenant_id=tenant_id, client_id=rh.client_id, client_secret=rh.secret
    )
    token = credential.get_token(ARM_SCOPE)
    minutes_left = int((token.expires_on - time.time()) // 60)
    logger.info("ARM access token minted; expires in about %s minutes.", minutes_left)
    set_progress("Azure access token acquired ({} minutes of lifetime left).".format(minutes_left))
    return token.token


def _ps_literal(value):
    """Single-quoted PowerShell string literal; the only escape is '' for '."""
    return "'" + str(value).replace("'", "''") + "'"


def _wrap(body, token, client_id, tenant_id, subscription_id, script_name):
    # Connect-AzAccount, AccessTokenWithSubscriptionId parameter set:
    # https://learn.microsoft.com/en-us/powershell/module/az.accounts/connect-azaccount
    # -AccessToken <String> and -AccountId <String> (the app registration's
    # client id) are mandatory; -Tenant/-Subscription take ids; -Scope Process
    # keeps the context in this process only so nothing persists on the host.
    header = "\r\n".join([
        "# ---- CloudBolt: Azure PowerShell session (generated by '{}'; not part of the Remote Script) ----".format(ACTION_NAME),
        "$cbPrevEap = $ErrorActionPreference",
        "$ErrorActionPreference = 'Stop'",
        "$cbAzToken = {}".format(_ps_literal(token)),
        "Connect-AzAccount -AccessToken $cbAzToken -AccountId {} -Tenant {} -Subscription {} -Scope Process -SkipContextPopulation | Out-Null".format(
            _ps_literal(client_id), _ps_literal(tenant_id), _ps_literal(subscription_id)
        ),
        "Remove-Variable -Name cbAzToken",
        "$ErrorActionPreference = $cbPrevEap",
        "Remove-Variable -Name cbPrevEap",
        "# ---- Remote Script '{}' ----".format(script_name.replace("\r", " ").replace("\n", " ")),
        "",
    ])
    return header + body


# -----------------------------------------------------------------------------
# Entry point
# -----------------------------------------------------------------------------

def run(job, **kwargs):
    env_id = "{{ env_id }}".strip()
    script_ref = "{{ script }}".strip()
    params_raw = """{{ script_parameters }}"""

    if not env_id:
        return "FAILURE", "", "An Environment is required (env_id arrived blank)."
    if not script_ref:
        return "FAILURE", "", "A script is required (script arrived blank)."

    blueprint, group = _resolve_order_context(job, kwargs)
    if blueprint is None:
        return "FAILURE", "", "Could not determine the blueprint this job belongs to; order this plugin through its blueprint."
    if group is None:
        return "FAILURE", "", "Could not determine the ordering group; cannot evaluate environment entitlement."

    env = entitled_environment(group, env_id, profile=job.owner)
    if env is None:
        return "FAILURE", "", "Group '{}' is not entitled to environment id {} or it is not Azure-backed.".format(group.name, env_id)
    try:
        azure = subscription_context(env)
    except EnvOptionsError as exc:
        return "FAILURE", "", str(exc)
    rh = azure_handler(env)
    if not azure["subscription_id"] or not azure["tenant_id"] or not rh.client_id:
        return "FAILURE", "", "Resource handler '{}' is missing its subscription, tenant or application id.".format(rh.name)

    item = _script_catalog(blueprint).filter(hook__global_id=script_ref).first()
    if item is None:
        return "FAILURE", "", "'{}' is not a disabled Remote Script build item of blueprint '{}'.".format(script_ref, blueprint.name)
    hook = item.hook.cast()
    target = hook.run_on_server
    if target is None:
        return "FAILURE", "", "Remote Script '{}' has no 'Run on Server'. Set it to the Windows host that has Az PowerShell installed.".format(hook.name)

    try:
        values = _parse_parameters(params_raw)
    except (ValueError, SyntaxError) as exc:
        return "FAILURE", "", "script_parameters is not a JSON object: {}".format(exc)
    values = _apply_item_defaults(item, hook, values)
    problem = _validate_inputs(hook, values, group=group, environment=env)
    if problem:
        return "FAILURE", "", problem

    cannot, with_failure = hook.cannot_run_script(target, job, logger=logger)
    if cannot and with_failure:
        return "FAILURE", "", cannot
    if cannot:
        return "WARNING", "", cannot

    # Same rendering path as RemoteScriptHook.run_hook_on_server: PWD/ETXT
    # inputs are tracked in __sensitive_keys and nothing below logs the body.
    context = get_hook_action_input_dict(hook, values, input_names_clean=True)
    context.update({
        "job": job,
        "cb_azure_subscription_id": azure["subscription_id"],
        "cb_azure_tenant_id": azure["tenant_id"],
        "cb_azure_location": azure["location"],
        "cb_target_environment": env.name,
    })
    body, _stripped = hook.render_module_file(target, context)
    script_args = hook.get_script_args(target, context) if hook.commandline_args else ""

    token = _mint_token(rh, azure["tenant_id"])
    full_script = _wrap(body, token, rh.client_id, azure["tenant_id"], azure["subscription_id"], hook.name)
    runas_username, runas_password, runas_key = hook.get_runas_credentials(job)

    set_progress("Running Remote Script '{}' on {} against environment '{}' (subscription {}).".format(
        hook.name, target.hostname, env.name, azure["subscription_id"]
    ))
    try:
        output = target.execute_script(
            script_contents=full_script,
            script_args=script_args,
            runas_username=runas_username,
            runas_password=runas_password,
            runas_key=runas_key,
            timeout=hook.execution_timeout,
            file_extension="ps1",
            run_with_sudo=hook.run_with_sudo,
            remove_after_run=hook.remove_after_run,
        )
    except Exception as exc:  # noqa: BLE001 -- surface the script's own failure text
        add_server_event(
            "MODIFICATION", target,
            "Remote script '{}' failed when run via '{}'".format(hook.name, ACTION_NAME),
            job=job,
        )
        detail = getattr(exc, "output", None) or str(exc)
        return "FAILURE", "", "Script '{}' failed on {}:\n{}".format(hook.name, target.hostname, detail)

    add_server_event(
        "MODIFICATION", target,
        "Remote script '{}' successfully run via '{}'".format(hook.name, ACTION_NAME),
        job=job,
    )
    tail = (output or "").strip()
    if len(tail) > OUTPUT_TAIL_CHARS:
        tail = "...\n" + tail[-OUTPUT_TAIL_CHARS:]
    if tail:
        set_progress("Script output:\n{}".format(tail))
    return "SUCCESS", "Ran '{}' on {} against '{}'.".format(hook.name, target.hostname, env.name), ""
