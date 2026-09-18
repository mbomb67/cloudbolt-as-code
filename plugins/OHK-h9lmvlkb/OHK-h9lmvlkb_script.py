"""
CloudBolt discovery plugin: Azure Storage Accounts.

Inventories existing Azure Storage Accounts across ALL Azure resource handlers
and returns one dict per account for CloudBolt to create/update Resources under
the "Azure Storage Account" blueprint.

RBAC: discovery enumerates every handler; CloudBolt filters the returned
resources by each user's Environment access. See docs/agents/rbac-and-security.md.

Discovery contract:
  - Every dict MUST include a "name" key.
  - RESOURCE_IDENTIFIER names the namespaced field carrying the cloud-native
    unique ID (the Azure Resource ID) that uniquely keys a discovered resource.
  - All other keys are namespaced (azure_storage_account_*) and become custom
    fields automatically — matching the fields the build/teardown plugins use.

External API: Azure Storage Resource Provider via azure-mgmt-storage. Operation
shapes anchored to Microsoft's current docs (cited at the call site) per
docs/agents/external-apis.md.

Entry point: discover_resources(**kwargs) -> list[dict]
"""

from common.methods import set_progress
from utilities.logger import ThreadLogger

logger = ThreadLogger(__name__)

# The Azure Resource ID is the globally unique key for a storage account.
RESOURCE_IDENTIFIER = "azure_storage_account_id"


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
    """Discover Azure Storage Accounts across all Azure handlers."""
    discovered = []

    try:
        from resourcehandlers.azure_arm.models import AzureARMHandler
    except ImportError as exc:
        logger.warning("Azure handler model unavailable: %s", exc)
        return discovered

    try:
        from azure.mgmt.storage import StorageManagementClient
        from resourcehandlers.azure_arm.azure_wrapper import configure_arm_client
    except ImportError as exc:
        logger.warning("azure-mgmt-storage SDK not available: %s", exc)
        return discovered

    for rh in AzureARMHandler.objects.all():
        try:
            wrapper = rh.get_api_wrapper()
            storage_client = configure_arm_client(wrapper, StorageManagementClient)
        except Exception as exc:
            set_progress(f"Skipping Azure handler {rh.name}: {exc}")
            logger.warning("Skipping handler %s due to client error: %s", rh, exc)
            continue

        set_progress(f"Discovering storage accounts for Azure handler '{rh.name}'...")

        # Docs: https://learn.microsoft.com/en-us/python/api/azure-mgmt-storage/azure.mgmt.storage.v2023_01_01.operations.storageaccountsoperations#list
        #       StorageAccountsOperations.list() -> iterator[StorageAccount].
        #       The list response already carries id/name/location/sku/kind, so no
        #       per-account get_properties() hydration is required for these fields.
        try:
            accounts = storage_client.storage_accounts.list()
        except Exception as exc:
            set_progress(f"Error listing storage accounts for handler {rh.name}: {exc}")
            logger.warning("Error listing accounts for handler %s: %s", rh, exc)
            continue

        for account in accounts:
            if not account.id or not account.name:
                continue

            discovered.append({
                "name": account.name,  # REQUIRED
                "azure_storage_account_id": account.id,  # RESOURCE_IDENTIFIER
                "azure_storage_account_name": account.name,
                "azure_storage_account_rg": _parse_resource_group(account.id),
                "azure_storage_account_location": account.location,
                "azure_storage_account_sku": account.sku.name if account.sku else None,
                "azure_storage_account_rh_id": rh.id,
            })

    set_progress(f"Discovered {len(discovered)} Azure storage account(s).")
    return discovered
