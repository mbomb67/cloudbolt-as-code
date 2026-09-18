"""
CloudBolt "Generated Parameter Options" plugin for the `azure_nsg` parameter.

Attach this plug-in to the `azure_nsg` custom field (Parameter > Options >
Generated). When the order form renders that field, CloudBolt calls
`get_options_list(field, **kwargs)` and passes the order's current context —
including the selected `environment`. This plugin:

  - reads the selected Environment from kwargs (RBAC gate: the user only ever
    picks an Environment, never a resource handler — see
    docs/agents/rbac-and-security.md);
  - casts the Environment to its Azure resource handler and lists the Network
    Security Groups in that subscription;
  - keeps only the NSGs in the region the Environment is bound to
    (env.node_location), since a NIC can only attach an NSG in its own region;
  - returns (value, label) tuples where label is the NSG name and value is the
    NSG's full Azure Resource ID. The Resource ID is what the paired
    Post-Provision orchestration action needs to attach the VM's NIC to the NSG.

External API: Azure Network Resource Provider via the azure-mgmt-network SDK,
authenticated through CloudBolt's configure_arm_client wrapper. Operation shapes
are anchored to Microsoft's current docs (cited at the call site) per
docs/agents/external-apis.md — not extrapolated from memory.

Entry point: get_options_list(field, **kwargs) -> list[(value, label)]
"""

from infrastructure.models import Environment
from utilities.logger import ThreadLogger

logger = ThreadLogger(__name__)

# The custom field this plugin generates options for. The paired orchestration
# action reads the selected value back off the server under the same name.
PARAM_NAME = "azure_nsg"


def _resolve_environment(kwargs):
    """Return the selected Environment from order-form context, or None.

    Confirmed against platform source: cf_generated_choices_list()
    (src/common/methods.py:1535) invokes
    `get_options_list(cf, environment=..., group=..., ...)` and passes the chosen
    environment under the key "environment" as an infrastructure.models.Environment
    INSTANCE — or None before one is selected (the field can render first). We
    still tolerate a bare pk defensively.
    """
    candidate = kwargs.get("environment")
    if candidate is None:
        return None

    if isinstance(candidate, Environment):
        return candidate

    try:
        return Environment.objects.get(id=int(candidate))
    except (Environment.DoesNotExist, ValueError, TypeError):
        logger.warning("azure_nsg options: could not resolve environment from %r", candidate)
        return None


def get_options_list(field, **kwargs):
    """Generate (NSG Resource ID, NSG name) options for the selected environment."""
    env = _resolve_environment(kwargs)
    if env is None:
        return [("", "------ Select an environment first ------")]

    from resourcehandlers.azure_arm.models import AzureARMHandler

    rh = env.resource_handler.cast() if env.resource_handler else None
    if not isinstance(rh, AzureARMHandler):
        return [("", "------ Selected environment is not backed by Azure ------")]

    # Each CloudBolt Azure environment is bound to a single region; an NSG can
    # only be attached to a NIC in the same region, so scope the list to it.
    region = (getattr(env, "node_location", "") or "").strip().lower()

    try:
        from azure.mgmt.network import NetworkManagementClient
        from resourcehandlers.azure_arm.azure_wrapper import configure_arm_client
    except ImportError as exc:
        logger.exception("azure-mgmt-network SDK not available")
        return [("", f"------ Azure Network SDK not installed: {exc} ------")]

    wrapper = rh.get_api_wrapper()
    network_client = configure_arm_client(wrapper, NetworkManagementClient)

    # Docs: https://learn.microsoft.com/en-us/python/api/azure-mgmt-network/azure.mgmt.network.operations.networksecuritygroupsoperations#list-all
    #       NetworkSecurityGroupsOperations.list_all() -> Iterable[NetworkSecurityGroup]
    #       (all NSGs in the subscription; each carries .id, .name, .location).
    try:
        nsgs = network_client.network_security_groups.list_all()
        options = [
            (nsg.id, nsg.name)
            for nsg in nsgs
            if nsg.id and nsg.name
            and (not region or (nsg.location or "").strip().lower() == region)
        ]
    except Exception as exc:
        logger.warning("Failed to list NSGs for handler %s: %s", rh, exc)
        return [("", "------ Could not load Network Security Groups ------")]

    if not options:
        where = f" in region '{region}'" if region else ""
        return [("", f"------ No Network Security Groups found{where} ------")]

    options.sort(key=lambda pair: pair[1].lower())
    # Leading placeholder lets the user leave the field unset (no NSG applied).
    options.insert(0, ("", "------ None (do not attach an NSG) ------"))
    return options
