"""
CloudBolt discovery plugin: Azure Resource Groups.

Inventories existing Azure Resource Groups across ALL Azure resource handlers
and returns one dict per group for CloudBolt to create/update Resources under
the "Azure Resource Group" blueprint.

RBAC: discovery enumerates every handler; CloudBolt filters the returned
resources by each user's Environment access. See docs/agents/rbac-and-security.md.

Discovery contract:
  - Every dict MUST include a "name" key.
  - RESOURCE_IDENTIFIER names the namespaced field carrying the cloud-native
    unique ID (the Azure Resource ID) that uniquely keys a discovered resource.
  - All other keys are namespaced (azure_resource_group_*) and become custom
    fields automatically — matching the fields the build, teardown, and day-2
    plugins use.

External API: Azure Resource Manager via azure-mgmt-resource for the groups
themselves; management locks are read over REST (api-version 2016-09-01)
through shared_modules/azure_management_locks, because the locks SDK client is
not installed on CloudBolt appliances. Operation shapes anchored to Microsoft's
current docs (cited at the call site) per docs/agents/external-apis.md.

Entry point: discover_resources(**kwargs) -> list[dict]
"""

from common.methods import set_progress
from utilities.logger import ThreadLogger

from shared_modules.azure_management_locks import (
    ManagementLocks,
    lock_level,
    resource_group_of,
    strongest_level,
)

logger = ThreadLogger(__name__)

# The Azure Resource ID is the globally unique key for a resource group.
RESOURCE_IDENTIFIER = "azure_resource_group_id"

def _render_tags(tags):
    """Render an Azure tags dict to the 'key=value' block stored on the Resource."""
    if not tags:
        return ""
    return "\n".join(f"{key}={value}" for key, value in sorted(tags.items()))


def _lock_levels_by_group(rh):
    """Map lowercased resource group name -> most restrictive lock level, for one subscription.

    One subscription-wide REST call (list-at-subscription-level, cited in
    shared_modules/azure_management_locks) instead of one call per resource
    group. Lock IDs look like
    /subscriptions/{sub}/resourceGroups/{rg}/providers/Microsoft.Authorization/locks/{name},
    so resource-scoped locks share the group prefix and are deliberately rolled
    up to their containing group.

    Reading locks requires Microsoft.Authorization/locks/read; treat a denial as
    "no locks visible" rather than failing the whole discovery pass.
    """
    try:
        locks = ManagementLocks(rh).list_subscription_locks()
    except Exception as exc:
        logger.warning("Could not read management locks for handler %s: %s", rh, exc)
        return {}

    levels_by_group = {}
    for lock in locks:
        rg_name = resource_group_of(lock.get("id"))
        level = lock_level(lock)
        # NotSpecified and any unrecognized level restrict nothing worth reporting.
        if not rg_name or level is None:
            continue
        levels_by_group.setdefault(rg_name.lower(), []).append(level)
    return {group: strongest_level(levels) for group, levels in levels_by_group.items()}


def discover_resources(**kwargs):
    """Discover Azure Resource Groups across all Azure handlers."""
    discovered = []

    try:
        from resourcehandlers.azure_arm.models import AzureARMHandler
    except ImportError as exc:
        logger.warning("Azure handler model unavailable: %s", exc)
        return discovered

    try:
        from azure.mgmt.resource import ResourceManagementClient
        from resourcehandlers.azure_arm.azure_wrapper import configure_arm_client
    except ImportError as exc:
        logger.warning("azure-mgmt-resource SDK not available: %s", exc)
        return discovered

    for rh in AzureARMHandler.objects.all():
        try:
            wrapper = rh.get_api_wrapper()
            resource_client = configure_arm_client(wrapper, ResourceManagementClient)
        except Exception as exc:
            set_progress(f"Skipping Azure handler {rh.name}: {exc}")
            logger.warning("Skipping handler %s due to client error: %s", rh, exc)
            continue

        # Locks are a nice-to-have on discovery; an unreadable lock list degrades to "None".
        lock_levels = _lock_levels_by_group(rh)

        set_progress(f"Discovering resource groups for Azure handler '{rh.name}'...")

        # Docs: https://learn.microsoft.com/en-us/python/api/azure-mgmt-resource/azure.mgmt.resource.resources.operations.resourcegroupsoperations#list
        #       ResourceGroupsOperations.list() -> ItemPaged[ResourceGroup].
        #       The list response already carries id/name/location/tags, so no
        #       per-group get() hydration is required for these fields.
        try:
            resource_groups = resource_client.resource_groups.list()
        except Exception as exc:
            set_progress(f"Error listing resource groups for handler {rh.name}: {exc}")
            logger.warning("Error listing resource groups for handler %s: %s", rh, exc)
            continue

        for resource_group in resource_groups:
            if not resource_group.id or not resource_group.name:
                continue

            discovered.append({
                "name": resource_group.name,  # REQUIRED
                "azure_resource_group_id": resource_group.id,  # RESOURCE_IDENTIFIER
                "azure_resource_group_name": resource_group.name,
                "azure_resource_group_location": resource_group.location,
                "azure_resource_group_tags": _render_tags(resource_group.tags),
                "azure_resource_group_lock": lock_levels.get(resource_group.name.lower(), "None"),
                "azure_resource_group_rh_id": rh.id,
            })

    set_progress(f"Discovered {len(discovered)} Azure resource group(s).")
    return discovered
