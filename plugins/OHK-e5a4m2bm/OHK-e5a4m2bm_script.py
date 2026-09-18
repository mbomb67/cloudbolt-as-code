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

External API: Azure Resource Manager via azure-mgmt-resource. Operation shapes
anchored to Microsoft's current docs (cited at the call site) per
docs/agents/external-apis.md.

Entry point: discover_resources(**kwargs) -> list[dict]
"""

from common.methods import set_progress
from utilities.logger import ThreadLogger

logger = ThreadLogger(__name__)

# The Azure Resource ID is the globally unique key for a resource group.
RESOURCE_IDENTIFIER = "azure_resource_group_id"

# Lock levels that block deletion, in reporting priority order.
_LOCK_PRIORITY = ("CanNotDelete", "ReadOnly")


def _render_tags(tags):
    """Render an Azure tags dict to the 'key=value' block stored on the Resource."""
    if not tags:
        return ""
    return "\n".join(f"{key}={value}" for key, value in sorted(tags.items()))


def _lock_levels_by_group(lock_client):
    """Map lowercased resource group name -> strongest lock level, for one subscription.

    Docs: https://learn.microsoft.com/en-us/python/api/azure-mgmt-resource/azure.mgmt.resource.locks.operations.managementlocksoperations#list-at-subscription-level
          ManagementLocksOperations.list_at_subscription_level() -> ItemPaged[ManagementLockObject]
    One subscription-wide call instead of one call per resource group. Lock IDs
    look like /subscriptions/{sub}/resourceGroups/{rg}/providers/Microsoft.Authorization/locks/{name},
    so the group name is segment 4; resource-scoped locks share that prefix and
    are deliberately rolled up to their containing group.

    Reading locks requires Microsoft.Authorization/locks/read; treat a denial as
    "no locks visible" rather than failing the whole discovery pass.
    """
    levels = {}
    if lock_client is None:
        return levels
    try:
        locks = list(lock_client.management_locks.list_at_subscription_level())
    except Exception as exc:
        logger.warning("Could not read management locks for subscription: %s", exc)
        return levels

    for lock in locks:
        rg_name = _parse_resource_group(lock.id or "")
        level = _normalize_level(lock.level)
        # NotSpecified and any unrecognized level restrict nothing worth reporting.
        if not rg_name or level is None:
            continue
        key = rg_name.lower()
        current = levels.get(key)
        # Keep the strongest level seen for the group.
        if current is None or _LOCK_PRIORITY.index(level) < _LOCK_PRIORITY.index(current):
            levels[key] = level
    return levels


def _normalize_level(level):
    """Coerce a ManagementLockObject.level (enum or str) to a known level, else None.

    Docs: https://learn.microsoft.com/en-us/rest/api/resources/management-locks/list-at-subscription-level
          LockLevel is one of NotSpecified, CanNotDelete, ReadOnly.
    """
    text = str(getattr(level, "value", level) or "")
    for known in _LOCK_PRIORITY:
        if text.lower() == known.lower():
            return known
    return None


def _parse_resource_group(resource_id):
    """Extract the resource group from an Azure Resource ID.

    Format: /subscriptions/{sub}/resourceGroups/{rg}/providers/...
    See docs/agents/common-patterns.md (Parsing Azure Resource IDs).
    """
    parts = resource_id.split("/")
    if len(parts) > 4 and parts[3].lower() == "resourcegroups":
        return parts[4]
    return None


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

    # Management locks live in a separate client in the same package. Locks are a
    # nice-to-have on discovery, so a missing client degrades to "None".
    try:
        from azure.mgmt.resource.locks import ManagementLockClient
    except ImportError as exc:
        logger.warning("Management lock client unavailable, lock state will be reported as None: %s", exc)
        ManagementLockClient = None

    for rh in AzureARMHandler.objects.all():
        try:
            wrapper = rh.get_api_wrapper()
            resource_client = configure_arm_client(wrapper, ResourceManagementClient)
        except Exception as exc:
            set_progress(f"Skipping Azure handler {rh.name}: {exc}")
            logger.warning("Skipping handler %s due to client error: %s", rh, exc)
            continue

        lock_client = None
        if ManagementLockClient is not None:
            try:
                lock_client = configure_arm_client(wrapper, ManagementLockClient)
            except Exception as exc:
                logger.warning("No lock client for handler %s: %s", rh, exc)
        lock_levels = _lock_levels_by_group(lock_client)

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
