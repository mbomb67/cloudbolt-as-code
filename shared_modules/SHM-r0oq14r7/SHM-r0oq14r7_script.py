"""
env_options -- RBAC-gated option sources derived from a CloudBolt Environment.

Generic, blueprint-agnostic helpers for order forms that must not expose a
resource handler (AGENTS.md cardinal rule 4): the user picks an Environment,
and everything Azure-specific -- subscription, tenant, location, resource
groups, subnets, images, sizes -- is derived from it server-side.

Two consumers:
  - the 'form-options' inbound webhook (webhooks/IWH-yj93is5z), which SurveyJS
    custom forms call through choicesByUrl for dropdowns inside a Dynamic
    Panel (where fields are not plugin inputs, so generate_options_for_* is
    unavailable);
  - build plugins, which call resolve_group / entitled_environment /
    subscription_context in run() to re-check entitlement and pull the
    subscription coordinates.

Every option source returns a list of {"value": ..., "title": ...} dicts.
Sources never raise for a caller-facing condition: an empty or placeholder
list is returned instead, so a hiccup cannot break form rendering.

Entitlement is ALWAYS group.get_available_environments() -- the platform's own
query (explicit group entitlement + ancestors + unconstrained environments).
Never reimplement it with Environment.objects.filter(group__in=[group]).
It returns a LIST sorted by name (not a QuerySet), and the unconstrained set is
tenant-filtered unless the caller passes a cb_admin / global-viewer profile --
so always pass the requesting profile and its tenant when you have them.
"""

import re

from accounts.models import Group
from infrastructure.models import Environment
from utilities.logger import ThreadLogger

logger = ThreadLogger(__name__)

# Azure custom-field names CloudBolt's Azure handler populates on Environments
# (AZURE_PARAMETER_FIELDS / AZURE_SIZE_FIELD in resourcehandlers/azure_arm).
AZURE_RESOURCE_GROUP_CF = "resource_group_arm"
AZURE_SIZE_CF = "node_size"

GROUP_GLOBAL_ID_RE = re.compile(r"(GRP-[a-z0-9]{8})")
# BDI input-mapping names carry CloudBolt's per-hook suffix: <input>_a<hookid>.
HOOK_INPUT_SUFFIX_RE = re.compile(r"_a\d+$")

SOURCES = ("environment", "resource_group", "subnet", "os_image", "vm_size", "location")


class EnvOptionsError(Exception):
    """Caller-facing configuration/authorization error."""


# -----------------------------------------------------------------------------
# Resolution + entitlement
# -----------------------------------------------------------------------------

def resolve_group(group_ref):
    """Resolve a Group from whatever a caller has: a Group, its name (what
    generate_options_for_* receives), its global_id, its numeric pk, or the
    relative API href a SurveyJS form's standard group dropdown submits
    ('/api/v3/cmp/groups/GRP-xxxxxxxx/' -- the last path segment is a global
    ID or a pk). Returns None when unresolvable."""
    if group_ref is None or group_ref == "":
        return None
    if isinstance(group_ref, Group):
        return group_ref
    text = str(group_ref).strip()
    match = GROUP_GLOBAL_ID_RE.search(text)
    if match:
        return Group.objects.filter(global_id=match.group(1)).first()
    if "/" in text:
        text = text.rstrip("/").rsplit("/", 1)[-1]
    if text.isdigit():
        return Group.objects.filter(id=int(text)).first()
    return Group.objects.filter(name=text).first()


def _is_cbadmin(profile):
    flag = getattr(profile, "is_cbadmin", False)
    return bool(flag() if callable(flag) else flag)


def profile_may_act_for_group(profile, group):
    """True if the profile is a CloudBolt admin or a (possibly inherited)
    member of the group. The inbound webhook uses this so a user cannot
    enumerate another group's environments by editing the group query
    parameter; build plugins get the same guarantee from the order itself."""
    if profile is None or group is None:
        return False
    if _is_cbadmin(profile):
        return True
    return profile.get_groups(include_inherited=True).filter(id=group.id).exists()


def available_environments(group, profile=None, azure_only=True):
    """Environments the group may order into, optionally narrowed to
    Azure-backed ones. Narrowing is done with id__in so the platform's
    entitlement query stays the single source of truth."""
    if profile is not None:
        envs = group.get_available_environments(
            tenant=getattr(profile, "tenant", None), profile=profile
        )
    else:
        envs = group.get_available_environments()
    queryset = Environment.objects.filter(id__in=[env.id for env in envs])
    if azure_only:
        queryset = queryset.filter(resource_handler__azurearmhandler__isnull=False)
    return queryset.order_by("name")


def entitled_environment(group, env_id, profile=None, azure_only=True):
    """The Environment with this id IF the group is entitled to it, else None.
    Used by both the webhook (before listing anything about the env) and the
    build plugins (before provisioning into it)."""
    try:
        env_id = int(str(env_id).strip())
    except (TypeError, ValueError):
        return None
    return available_environments(group, profile=profile, azure_only=azure_only).filter(id=env_id).first()


def azure_handler(env):
    """The env's AzureARMHandler, or None if the env is not Azure-backed."""
    from resourcehandlers.azure_arm.models import AzureARMHandler
    rh = env.resource_handler.cast() if env.resource_handler_id else None
    return rh if isinstance(rh, AzureARMHandler) else None


def subscription_context(env):
    """Subscription coordinates for an Azure environment:
    {"subscription_id", "tenant_id", "location"}. The resource handler is
    consulted only here, server-side; it never reaches the order form.
    CloudBolt stores the Azure subscription ID in the handler's serviceaccount
    field, the tenant in azure_tenant_id, and the env's location in its
    node_location parameter (read via get_env_location)."""
    rh = azure_handler(env)
    if rh is None:
        raise EnvOptionsError(
            "Environment '{}' is not backed by an Azure resource handler.".format(env.name)
        )
    return {
        "subscription_id": (rh.serviceaccount or "").strip(),
        "tenant_id": (rh.azure_tenant_id or "").strip(),
        "location": (rh.get_env_location(env) or "").strip(),
    }


# -----------------------------------------------------------------------------
# Form support
# -----------------------------------------------------------------------------

def service_item_defaults(bdi_global_id):
    """The pinned parameter_defaults of a blueprint deployment item, keyed by
    bare input name (the '_a<hookid>' suffix stripped). Lets a webhook read
    per-blueprint coordinates (connection, module ID, ...) from the single
    place they are pinned instead of duplicating them as hidden form fields.
    Storage: ServiceItem.input_mappings -> RunHookInputMapping(hook_input,
    default_value CFV)."""
    from servicecatalog.models import ServiceItem
    service_item = ServiceItem.objects.filter(global_id=str(bdi_global_id).strip()).first()
    if service_item is None:
        raise EnvOptionsError("No deployment item '{}' exists.".format(bdi_global_id))
    service_item = service_item.cast()
    mappings = getattr(service_item, "input_mappings", None)
    if mappings is None:
        raise EnvOptionsError(
            "Deployment item '{}' is not a plugin item and has no input defaults.".format(bdi_global_id)
        )
    defaults = {}
    for mapping in mappings.select_related("hook_input", "default_value"):
        if mapping.default_value is None:
            continue
        name = HOOK_INPUT_SUFFIX_RE.sub("", mapping.hook_input.name)
        defaults[name] = mapping.default_value.value
    return defaults


# -----------------------------------------------------------------------------
# Option sources
# -----------------------------------------------------------------------------

def _option(value, title=None):
    return {"value": value, "title": title if title is not None else str(value)}


def _placeholder(text):
    return [_option("", text)]


def environment_options(group, profile=None):
    """Azure environments the group can order into."""
    return [_option(env.id, env.name) for env in available_environments(group, profile=profile)]


def cf_options(env, cf_name):
    """Values of a custom field's options on the Environment (the generic
    source: any parameter an admin has scoped to the env, e.g. a customer's
    own 'cost_center' or 'app_tier'). Value/title come from the CFV."""
    options = []
    for cfv in env.custom_field_options.filter(field__name=cf_name).order_by("display_seq", "id"):
        value = cfv.value
        if value is None or str(value).strip() == "":
            continue
        options.append(_option(str(value), str(cfv.display_value or value)))
    return options


def resource_group_options(env):
    """Resource groups for the env: the env-scoped resource_group_arm options
    (what admins curated on the environment) first; if none are configured,
    fall back to listing the subscription live so the dropdown is never empty
    for a freshly created environment."""
    options = cf_options(env, AZURE_RESOURCE_GROUP_CF)
    if options:
        return options
    rh = azure_handler(env)
    if rh is None:
        return _placeholder("------ Environment has no Azure handler ------")
    try:
        from azure.mgmt.resource import ResourceManagementClient
        from resourcehandlers.azure_arm.azure_wrapper import configure_arm_client
        client = configure_arm_client(rh.get_api_wrapper(), ResourceManagementClient)
        # Docs: https://learn.microsoft.com/en-us/python/api/azure-mgmt-resource/azure.mgmt.resource.resources.operations.resourcegroupsoperations#azure-mgmt-resource-resources-operations-resourcegroupsoperations-list
        names = sorted(rg.name for rg in client.resource_groups.list())
        return [_option(name) for name in names] or _placeholder("------ No resource groups ------")
    except Exception as exc:  # noqa: BLE001 -- option sources degrade gracefully
        logger.debug("resource_group options error for env %s: %s", env.id, exc)
        return _placeholder("------ Could not list resource groups ------")


def subnet_options(env):
    """Subnets imported on the env, as full ARM resource IDs (what an
    azurerm_network_interface.ip_configuration.subnet_id wants). CloudBolt
    stores no ARM ID on AzureARMSubnet: name/network are '<vnet>/<subnet>',
    parent_network is the VNet and resource_group its group, so the ID is
    assembled per the ARM resource ID format:
    https://learn.microsoft.com/en-us/azure/azure-resource-manager/management/resource-name-rules#microsoftnetwork
    """
    rh = azure_handler(env)
    if rh is None:
        return _placeholder("------ Environment has no Azure handler ------")
    subscription_id = (rh.serviceaccount or "").strip()
    options = []
    try:
        networks = env.get_possible_networks()
    except Exception as exc:  # noqa: BLE001
        logger.debug("subnet options error for env %s: %s", env.id, exc)
        return _placeholder("------ Could not list subnets ------")
    for network in networks:
        subnet = network.cast() if hasattr(network, "cast") else network
        resource_group = getattr(subnet, "resource_group", None)
        vnet = getattr(subnet, "parent_network", None)
        raw_name = getattr(subnet, "network", None) or subnet.name or ""
        subnet_name = raw_name.split("/", 1)[1] if "/" in raw_name else raw_name
        if not (subscription_id and resource_group and vnet and subnet_name):
            continue
        arm_id = (
            "/subscriptions/{}/resourceGroups/{}/providers/Microsoft.Network/"
            "virtualNetworks/{}/subnets/{}".format(
                subscription_id, resource_group, vnet, subnet_name
            )
        )
        cidr = getattr(subnet, "cidr_block", "") or ""
        title = "{} / {}".format(vnet, subnet_name) + (" ({})".format(cidr) if cidr else "")
        options.append(_option(arm_id, title))
    options.sort(key=lambda option: option["title"])
    return options or _placeholder("------ No subnets imported on this environment ------")


def os_image_options(env):
    """Marketplace image URNs (publisher:offer:sku:version) for the OS builds
    orderable in the env, resolved through the handler's per-region image
    lookup so the URN is valid for the env's location. azure_image_name
    yields publisher:offer:sku[:version]; a missing version becomes 'latest'."""
    rh = azure_handler(env)
    if rh is None:
        return _placeholder("------ Environment has no Azure handler ------")
    options = []
    for os_build in env.get_orderable_os_builds():
        try:
            image = rh.get_osba_for_osb_and_env(os_build, env, raise_on_none=False)
        except Exception as exc:  # noqa: BLE001 -- e.g. several images match the region
            logger.debug("os_image lookup failed for %s: %s", os_build, exc)
            continue
        if image is None:
            continue
        urn = str(getattr(image, "azure_image_name", "") or "")
        if urn.count(":") == 2:
            urn += ":latest"
        if urn.count(":") != 3:
            continue
        options.append(_option(urn, "{} ({})".format(os_build.name, urn)))
    options.sort(key=lambda option: option["title"])
    return options or _placeholder("------ No images on this environment ------")


def vm_size_options(env):
    """VM sizes admins made available on the env (node_size options)."""
    options = cf_options(env, AZURE_SIZE_CF)
    return options or _placeholder("------ No VM sizes on this environment ------")


def location_options(env):
    """The env's single Azure location, as a one-item list."""
    location = subscription_context(env)["location"]
    return [_option(location)] if location else _placeholder("------ Environment has no location ------")


def options_for(source, env, cf_name=None):
    """Dispatch an env-scoped source name to its options. 'cf:<name>' (or
    source='cf' with cf_name) reads any custom field's env options."""
    source = (source or "").strip()
    if source.startswith("cf:"):
        source, cf_name = "cf", source[3:]
    if source == "cf":
        if not cf_name:
            raise EnvOptionsError("cf_name is required for source 'cf'.")
        return cf_options(env, cf_name)
    dispatch = {
        "resource_group": resource_group_options,
        "subnet": subnet_options,
        "os_image": os_image_options,
        "vm_size": vm_size_options,
        "location": location_options,
    }
    if source not in dispatch:
        raise EnvOptionsError(
            "Unknown source '{}'. Valid: {}, cf:<custom_field_name>.".format(
                source, ", ".join(SOURCES)
            )
        )
    return dispatch[source](env)
