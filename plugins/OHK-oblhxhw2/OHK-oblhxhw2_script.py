"""
Run Azure PowerShell Script (Deployment Script) -- the single enabled build
step of the "Run an Azure PowerShell Script (Deployment Script)" blueprint
(BP-qo1stxre).

Same catalog model as the "Run an Azure PowerShell Script" blueprint
(BP-jvvjm3te, plugins/OHK-pmyb1car): the blueprint carries DISABLED Remote
Script build items, the orderer picks an Environment and one script, and this
plugin renders the script and signs the Az PowerShell session in with the
environment's app registration. The difference is WHERE the script runs:

  BP-jvvjm3te  on a customer-managed Windows host (the Remote Script's
               "Run on Server") over WinRM / Azure Run Command.
  this one     in a Microsoft-managed container that Azure starts for the
               run, through an ARM deployment script resource
               (Microsoft.Resources/deploymentScripts, kind AzurePowerShell).
               No server, no WinRM, no Az module install: the container image
               ships the Az modules. Everything is ARM REST from the appliance.

Per run the plugin
1. resolves the Environment to its Azure resource handler (RBAC-gated through
   group.get_available_environments(), never exposed to the orderer),
2. mints a short-lived ARM access token for the handler's app registration
   (used both for the ARM calls below and for the script's own sign-in),
3. renders the chosen Remote Script through CloudBolt's own template engine
   with the same action-input context a native Remote Script run gets,
4. makes sure the host resource group exists in the environment's subscription
   (created in the environment's location when missing),
5. PUTs a deployment script resource whose scriptContent is a sign-in wrapper
   plus the rendered script, with the token in a secure environment variable,
6. polls it to a terminal state, fetches the container's stdout/stderr through
   the resource's logs endpoint, writes it to the job, and
7. deletes the deployment script resource (Azure removes the temporary
   storage account and container instance itself; cleanupPreference Always).

Scripts need no changes between the two blueprints as long as they are pure
Az PowerShell: here they run under PowerShell 7 on Linux, so Windows-only
cmdlets are unavailable. Scripts must NOT call Connect-AzAccount (they hold no
credentials); use Set-AzContext to switch subscriptions within the tenant.

Action inputs (all come from the order form):
- env_id (INT, required): CloudBolt Environment backed by an Azure handler.
- script (STR, required): global_id of a Remote Script that is a DISABLED
  build item of this blueprint. Options come from generate_options_for_script.
- script_parameters (TXT, optional): JSON object of the chosen script's
  action-input values keyed by input name. Missing keys fall back to the build
  item's pinned defaults.
- script_resource_group (STR, required): resource group, in the environment's
  subscription, that hosts the deployment script resource and its temporary
  storage account and container instance. Pinned on the custom form as a
  hidden question; created in the environment's location when missing.

Extra template variables this runner adds to the Remote Script's context:
cb_azure_subscription_id, cb_azure_tenant_id, cb_azure_location,
cb_target_environment (the selected environment's name) and
cb_script_resource_group. There is no server in the context (no "Run on
Server"), so server-derived variables are not available.

Azure access tokens: default lifetime is a random 60-90 minutes
(https://learn.microsoft.com/en-us/entra/identity-platform/configurable-token-lifetimes).
The Remote Script's execution timeout bounds the container run, so keep it
below the token lifetime; longer scripts need a user-assigned managed identity
on the deployment script resource instead (not implemented here).

Azure REST used (api-versions as documented; recheck per cardinal rule 5):
- Deployment Scripts Create / Get / Get Logs / Delete, api-version 2020-10-01
  https://learn.microsoft.com/en-us/rest/api/resources/deployment-scripts/create
  https://learn.microsoft.com/en-us/rest/api/resources/deployment-scripts/get-logs
  https://learn.microsoft.com/en-us/rest/api/resources/deployment-scripts/delete
  Concepts (two principals, identity optional, secure env vars, cleanup):
  https://learn.microsoft.com/en-us/azure/azure-resource-manager/templates/deployment-script-template
- Resource Groups Get / Create Or Update, api-version 2021-04-01
  https://learn.microsoft.com/en-us/rest/api/resources/resource-groups/create-or-update

Internal CloudBolt APIs used (verified against CloudBolt 2026.1 source; recheck
on upgrade): ExternalSourceCodeMixin.file_content (str), common.methods
.generate_string_from_template, .dos2unix, CodeActionMixin
.validate_action_input_values, cbhooks.services.parsing
.get_hook_action_input_dict, utilities.helpers.get_ssl_verification.
"""

import ast
import json
import random
import re
import string
import time

import requests
from azure.identity import ClientSecretCredential
from django.core.exceptions import ValidationError

from cbhooks.services.parsing import get_hook_action_input_dict
from common.methods import dos2unix, generate_string_from_template, set_progress
from orders.models import BlueprintOrderItem
from servicecatalog.models import (
    RunCloudBoltHookServiceItem,
    RunRemoteScriptHookServiceItem,
    ServiceBlueprint,
    ServiceItem,
)
from utilities.helpers import get_ssl_verification
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
ACTION_NAME = "Run Azure PowerShell Script (Deployment Script)"

ARM_BASE = "https://management.azure.com"
ARM_SCOPE = "https://management.azure.com/.default"
DEPLOYMENT_SCRIPTS_API_VERSION = "2020-10-01"
RESOURCE_GROUPS_API_VERSION = "2021-04-01"
HTTP_TIMEOUT_S = 60

# Az PowerShell module version the container image ships. The supported values
# are the tags of mcr.microsoft.com/azuredeploymentscripts-powershell (az16.0 is
# the tag for "16.0"); an unsupported value fails with a message listing the
# supported ones: https://mcr.microsoft.com/v2/azuredeploymentscripts-powershell/tags/list
AZ_POWERSHELL_VERSION = "16.0"

# The deployment script's own timeout comes from the Remote Script's execution
# timeout; the container also needs a few minutes to start, so the job waits
# this much longer than the script timeout before giving up on Azure.
PROVISIONING_GRACE_S = 15 * 60
MAX_SCRIPT_TIMEOUT_S = 24 * 60 * 60     # the resource's limit is P1D
DEFAULT_SCRIPT_TIMEOUT_S = 60 * 60
POLL_INTERVAL_S = 10
TERMINAL_STATES = ("Succeeded", "Failed", "Canceled")
STATE_HINTS = {
    "Creating": "Azure is creating the deployment script resource...",
    "ProvisioningResources": "Azure is provisioning the temporary storage account and container (usually 1 to 3 minutes)...",
    "Running": "The script is running in the container...",
}
TOKEN_ENV_VAR = "CB_AZ_TOKEN"
OUTPUT_TAIL_CHARS = 4000
RESOURCE_GROUP_NAME_RE = re.compile(r"^[-\w\.\(\)]+$")


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
# Order context, inputs, rendering
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
    """(blueprint, group, order) for this deployment. Build-item jobs carry
    the blueprint order item id and the service item id in their context
    (kwargs here); the job's order chain is the fallback."""
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
    return blueprint, group, order


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


def _render(hook, template, group, env, context):
    """Render a Remote Script body (or its command-line arguments) the way
    RemoteScriptHook.render_module_file does, minus the server: the same
    Django template engine, the same input context (PWD/ETXT inputs tracked as
    sensitive), with group/environment resolved from the order instead of a
    jump host. Newlines are normalised for the Linux container."""
    if not template:
        return ""
    return dos2unix(generate_string_from_template(template, group, env, None, context=context))


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


def _wrap(body, client_id, tenant_id, subscription_id, script_name):
    # Connect-AzAccount, AccessTokenWithSubscriptionId parameter set:
    # https://learn.microsoft.com/en-us/powershell/module/az.accounts/connect-azaccount
    # -AccessToken <String> and -AccountId <String> (the app registration's
    # client id) are mandatory; -Tenant/-Subscription take ids; -Scope Process
    # keeps the context in this process only.
    #
    # The token is NOT in the script body: Azure injects it as the secure
    # environment variable CB_AZ_TOKEN (never echoed by the resource's GET),
    # and the wrapper removes it from the environment right after sign-in so
    # the author's script and anything it spawns cannot read it.
    #
    # Error reporting: CloudBolt only sees the container's stdout/stderr plus
    # the deployment script's terminal state. The trap below catches any
    # terminating error (the sign-in and the author's script alike), reports
    # it on stdout as a CB_ERROR line and exits 1, which Azure reports as a
    # Failed deployment script; the footer lists non-terminating errors the
    # script left in $Error. The author's $ErrorActionPreference is untouched.
    # (Built by concatenation: a brace pair would read as a template variable.)
    safe_name = script_name.replace("\r", " ").replace("\n", " ")
    header = [
        "# ---- CloudBolt: Azure PowerShell session (generated by '" + ACTION_NAME + "'; not part of the Remote Script) ----",
        "trap {",
        "    Write-Output ('CB_ERROR: ' + $_.Exception.GetType().Name + ': ' + $_.Exception.Message)",
        "    if ($_.InvocationInfo -and $_.InvocationInfo.PositionMessage) { Write-Output $_.InvocationInfo.PositionMessage }",
        "    if ($_.ScriptStackTrace) { Write-Output $_.ScriptStackTrace }",
        "    exit 1",
        "}",
        "$cbPrevEap = $ErrorActionPreference",
        "$ErrorActionPreference = 'Stop'",
        "Import-Module Az.Accounts",
        "$cbAzToken = $env:" + TOKEN_ENV_VAR,
        "if (-not $cbAzToken) { throw 'The " + TOKEN_ENV_VAR + " environment variable is empty: CloudBolt did not pass the access token to the deployment script.' }",
        "Connect-AzAccount -AccessToken $cbAzToken -AccountId " + _ps_literal(client_id)
        + " -Tenant " + _ps_literal(tenant_id) + " -Subscription " + _ps_literal(subscription_id)
        + " -Scope Process -SkipContextPopulation | Out-Null",
        "Remove-Variable -Name cbAzToken",
        "Remove-Item -Path Env:" + TOKEN_ENV_VAR + " -ErrorAction SilentlyContinue",
        "$cbCtx = Get-AzContext",
        "Write-Output ('CB_INFO: Az.Accounts ' + (Get-Module Az.Accounts).Version + ' on PowerShell ' + $PSVersionTable.PSVersion + ' connected to subscription ' + $cbCtx.Subscription.Id + ' as ' + $cbCtx.Account.Id)",
        "$ErrorActionPreference = $cbPrevEap",
        "Remove-Variable -Name cbPrevEap, cbCtx",
        "$Error.Clear()",
        "# ---- Remote Script '" + safe_name + "' ----",
        "",
    ]
    footer = [
        "",
        "# ---- CloudBolt: end of Remote Script '" + safe_name + "' ----",
        "if ($Error.Count -gt 0) {",
        "    Write-Output ('CB_WARNING: ' + $Error.Count + ' non-terminating error(s) were recorded while the script ran:')",
        "    $Error | ForEach-Object { Write-Output ('  ' + $_.ToString()) }",
        "}",
        "",
    ]
    return "\n".join(header) + body + "\n".join(footer)


# -----------------------------------------------------------------------------
# ARM REST
# -----------------------------------------------------------------------------

class ArmError(Exception):
    def __init__(self, message, status_code=None):
        super().__init__(message)
        self.status_code = status_code


def _arm_error_text(resp):
    """Flatten an ARM error body {error: {code, message, details[]}} into one
    line. ARM error text names the failure (a validation error, a missing
    permission, an unsupported Az version) and carries no token material."""
    try:
        err = resp.json().get("error") or {}
    except ValueError:
        return (resp.text or "").strip()[:500]
    parts = []
    if err.get("code"):
        parts.append(str(err["code"]))
    if err.get("message"):
        parts.append(str(err["message"]))
    for detail in err.get("details") or []:
        if isinstance(detail, dict) and detail.get("message"):
            parts.append("{}: {}".format(detail.get("code", "detail"), detail["message"]))
    return "; ".join(parts) or (resp.text or "").strip()[:500]


class ArmClient:
    """Minimal ARM client over one bearer token. The token lives only in the
    Authorization header and is never logged or placed in a URL/exception."""

    def __init__(self, token, subscription_id):
        self._token = token
        self.subscription_id = subscription_id

    def _request(self, method, path, params=None, body=None, ok=(200,)):
        url = ARM_BASE + path
        headers = {"Authorization": "Bearer " + self._token, "Accept": "application/json"}
        kwargs = {
            "params": params or {},
            "timeout": HTTP_TIMEOUT_S,
            "verify": get_ssl_verification(),   # honours Admin > SSL Certificates
        }
        if body is not None:
            kwargs["json"] = body
        resp = requests.request(method, url, headers=headers, **kwargs)
        if resp.status_code == 429:
            # Honour Retry-After once before failing on ARM throttling.
            try:
                delay = min(int(resp.headers.get("Retry-After", "10")), 60)
            except ValueError:
                delay = 10
            time.sleep(delay)
            resp = requests.request(method, url, headers=headers, **kwargs)
        if resp.status_code not in ok:
            logger.debug("ARM %s %s -> %s", method, path, resp.status_code)
            raise ArmError(
                "Azure request failed ({}): {}".format(resp.status_code, _arm_error_text(resp) or "no detail"),
                status_code=resp.status_code,
            )
        return resp

    def _rg_path(self, rg):
        return "/subscriptions/{}/resourcegroups/{}".format(self.subscription_id, rg)

    def _script_path(self, rg, name):
        return self._rg_path(rg) + "/providers/Microsoft.Resources/deploymentScripts/" + name

    def ensure_resource_group(self, rg, location, tags):
        """True when the group was created now, False when it already existed.
        Resource Groups - Get / Create Or Update, api-version 2021-04-01."""
        params = {"api-version": RESOURCE_GROUPS_API_VERSION}
        resp = self._request("GET", self._rg_path(rg), params=params, ok=(200, 404))
        if resp.status_code == 200:
            return False
        self._request("PUT", self._rg_path(rg), params=params,
                      body={"location": location, "tags": tags}, ok=(200, 201))
        return True

    def create_script(self, rg, name, body):
        # Deployment Scripts - Create: PUT returns 201 with provisioningState
        # Creating and the run continues asynchronously (200 on an update).
        params = {"api-version": DEPLOYMENT_SCRIPTS_API_VERSION}
        return self._request("PUT", self._script_path(rg, name), params=params, body=body, ok=(200, 201)).json()

    def get_script(self, rg, name):
        params = {"api-version": DEPLOYMENT_SCRIPTS_API_VERSION}
        resp = self._request("GET", self._script_path(rg, name), params=params, ok=(200, 404))
        return resp.json() if resp.status_code == 200 else None

    def get_logs(self, rg, name):
        # Deployment Scripts - Get Logs: {"value": [{"properties": {"log": "..."}}]}
        params = {"api-version": DEPLOYMENT_SCRIPTS_API_VERSION}
        resp = self._request("GET", self._script_path(rg, name) + "/logs", params=params, ok=(200, 404))
        if resp.status_code != 200:
            return ""
        entries = resp.json().get("value") or []
        return "\n".join((entry.get("properties") or {}).get("log") or "" for entry in entries).strip()

    def delete_script(self, rg, name):
        # Deployment Scripts - Delete: 200 deleted, 204 did not exist.
        params = {"api-version": DEPLOYMENT_SCRIPTS_API_VERSION}
        self._request("DELETE", self._script_path(rg, name), params=params, ok=(200, 202, 204))


def _poll_to_terminal(arm, rg, name, deadline):
    """Poll the deployment script's provisioningState until Succeeded, Failed
    or Canceled, reporting state changes to the job. A freshly created
    resource may 404 for a moment, so a few consecutive misses are tolerated."""
    last_state = None
    misses = 0
    while time.time() < deadline:
        script = arm.get_script(rg, name)
        if script is None:
            misses += 1
            if misses >= 6:
                raise ArmError("Deployment script '{}' disappeared while CloudBolt was waiting for it.".format(name))
            time.sleep(POLL_INTERVAL_S)
            continue
        misses = 0
        props = script.get("properties") or {}
        state = props.get("provisioningState") or ""
        if state != last_state:
            set_progress("Deployment script {}: {}".format(name, STATE_HINTS.get(state, state)))
            last_state = state
        if state in TERMINAL_STATES:
            return state, props
        time.sleep(POLL_INTERVAL_S)
    raise ArmError(
        "Deployment script '{}' did not finish within the wait window; its last state was {}. "
        "It is left in place for inspection in the Azure portal and expires on its own.".format(name, last_state)
    )


def _tail(text):
    text = (text or "").strip()
    if len(text) > OUTPUT_TAIL_CHARS:
        return "...\n" + text[-OUTPUT_TAIL_CHARS:]
    return text


def _iso_seconds(seconds):
    return "PT{}S".format(int(seconds))


def _script_name(job):
    suffix = "".join(random.choices(string.ascii_lowercase + string.digits, k=6))
    return "cb-azps-job{}-{}".format(job.id, suffix)


# -----------------------------------------------------------------------------
# Entry point
# -----------------------------------------------------------------------------

def run(job, **kwargs):
    env_id = "{{ env_id }}".strip()
    script_ref = "{{ script }}".strip()
    params_raw = """{{ script_parameters }}"""
    resource_group = "{{ script_resource_group }}".strip()

    if not env_id:
        return "FAILURE", "", "An Environment is required (env_id arrived blank)."
    if not script_ref:
        return "FAILURE", "", "A script is required (script arrived blank)."
    if not resource_group or "FILL-ME" in resource_group:
        return "FAILURE", "", "script_resource_group is required: the resource group that hosts the deployment script runs."
    if len(resource_group) > 90 or resource_group.endswith(".") or not RESOURCE_GROUP_NAME_RE.match(resource_group):
        return "FAILURE", "", "'{}' is not a valid Azure resource group name.".format(resource_group)

    blueprint, group, order = _resolve_order_context(job, kwargs)
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
    if not azure["location"]:
        return "FAILURE", "", (
            "Environment '{}' has no Azure location. Set one on the environment: it is the region "
            "where the deployment script's container runs (must be a region where Azure Container "
            "Instances is available)."
        ).format(env.name)

    item = _script_catalog(blueprint).filter(hook__global_id=script_ref).first()
    if item is None:
        return "FAILURE", "", "'{}' is not a disabled Remote Script build item of blueprint '{}'.".format(script_ref, blueprint.name)
    hook = item.hook.cast()

    try:
        values = _parse_parameters(params_raw)
    except (ValueError, SyntaxError) as exc:
        return "FAILURE", "", "script_parameters is not a JSON object: {}".format(exc)
    values = _apply_item_defaults(item, hook, values)
    problem = _validate_inputs(hook, values, group=group, environment=env)
    if problem:
        return "FAILURE", "", problem

    # Same input context as RemoteScriptHook.run_hook_on_server builds: PWD/ETXT
    # inputs are tracked as sensitive and nothing below logs the body.
    context = get_hook_action_input_dict(hook, values, input_names_clean=True)
    context.update({
        "job": job,
        "profile": job.owner,
        "order": order,
        "blueprint": blueprint,
        "cb_azure_subscription_id": azure["subscription_id"],
        "cb_azure_tenant_id": azure["tenant_id"],
        "cb_azure_location": azure["location"],
        "cb_target_environment": env.name,
        "cb_script_resource_group": resource_group,
    })
    body = _render(hook, hook.file_content(), group, env, context)
    arguments = _render(hook, hook.commandline_args, group, env, context).strip() if hook.commandline_args else ""

    timeout_s = int(hook.execution_timeout or DEFAULT_SCRIPT_TIMEOUT_S)
    timeout_s = max(60, min(timeout_s, MAX_SCRIPT_TIMEOUT_S))

    token = _mint_token(rh, azure["tenant_id"])
    arm = ArmClient(token, azure["subscription_id"])
    tags = {
        "cloudbolt_job": str(job.id),
        "cloudbolt_blueprint": blueprint.global_id,
        "cloudbolt_script": hook.global_id,
    }

    try:
        created = arm.ensure_resource_group(resource_group, azure["location"], {
            "created_by": "CloudBolt",
            "purpose": "Azure PowerShell deployment scripts",
        })
    except ArmError as exc:
        return "FAILURE", "", (
            "Could not read or create resource group '{}' in subscription {}: {}\n"
            "Create it by hand in a region where Azure Container Instances is available and give the "
            "environment's app registration the deployment-script permissions on it (see the blueprint README)."
        ).format(resource_group, azure["subscription_id"], exc)
    if created:
        set_progress("Created resource group '{}' in {} for deployment script runs.".format(resource_group, azure["location"]))

    name = _script_name(job)
    resource = {
        "kind": "AzurePowerShell",
        "location": azure["location"],
        "tags": tags,
        "properties": {
            "azPowerShellVersion": AZ_POWERSHELL_VERSION,
            "scriptContent": _wrap(body, rh.client_id, azure["tenant_id"], azure["subscription_id"], hook.name),
            "environmentVariables": [{"name": TOKEN_ENV_VAR, "secureValue": token}],
            "timeout": _iso_seconds(timeout_s),
            "retentionInterval": "PT1H",
            "cleanupPreference": "Always",
        },
    }
    if arguments:
        resource["properties"]["arguments"] = arguments
    del token  # only the request body carries it from here on

    set_progress("Submitting '{}' to Azure as deployment script {} in {}/{} (Az {} image, timeout {}s) against environment '{}' (subscription {}).".format(
        hook.name, name, resource_group, azure["location"], AZ_POWERSHELL_VERSION, timeout_s, env.name, azure["subscription_id"]
    ))
    started = time.time()
    try:
        arm.create_script(resource_group, name, resource)
    except ArmError as exc:
        return "FAILURE", "", "Azure rejected the deployment script: {}".format(exc)

    try:
        state, props = _poll_to_terminal(arm, resource_group, name, started + timeout_s + PROVISIONING_GRACE_S)
    except ArmError as exc:
        return "FAILURE", "", str(exc)

    logs = ""
    try:
        logs = arm.get_logs(resource_group, name)
    except ArmError as exc:
        logger.warning("Could not fetch logs for deployment script %s: %s", name, exc)
    try:
        arm.delete_script(resource_group, name)
    except ArmError as exc:
        logger.warning("Could not delete deployment script %s (it expires on its own): %s", name, exc)

    elapsed = int(time.time() - started)
    tail = _tail(logs)
    if tail:
        set_progress("Script output:\n{}".format(tail))
    else:
        set_progress("The deployment script produced no output.")

    if state != "Succeeded":
        error = ((props.get("status") or {}).get("error") or {})
        detail = error.get("message") or error.get("code") or ""
        head = "Script '{}' {} in Azure deployment script {} after {}s{}.".format(
            hook.name, "failed" if state == "Failed" else "was canceled", name, elapsed,
            " ({})".format(detail) if detail else "",
        )
        if tail:
            return "FAILURE", "", head + "\n" + tail
        return "FAILURE", "", head + (
            "\nThe container produced no output. The wrapper reports terminating errors as CB_ERROR lines, "
            "so an empty log usually means the container never started the script; check the error above."
        )
    return "SUCCESS", "Ran '{}' in an Azure deployment script against '{}' in {}s.".format(hook.name, env.name, elapsed), ""
