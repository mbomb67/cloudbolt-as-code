"""
CloudBolt Day-2 resource action: Change SKU.

Updates the replication SKU of the Azure storage account backing this resource
and records the new SKU on the resource. Azure rejects unsupported conversions
(e.g. Standard <-> Premium); those surface as a FAILURE with Azure's message.

Action Inputs:
  - sku (STR, required) : e.g. "Standard_LRS", "Standard_GRS", "Premium_LRS"

External API: Azure Storage Resource Provider via azure-mgmt-storage. Operation
shapes anchored to Microsoft's current docs (cited at the call site) per
docs/agents/external-apis.md.

Entry point: run(job, resource, **kwargs) -> (status, output_msg, error_msg)
"""

from common.methods import set_progress
from utilities.logger import ThreadLogger

logger = ThreadLogger(__name__)

_VALID_SKUS = {
    "Standard_LRS",
    "Standard_ZRS",
    "Standard_GRS",
    "Standard_RAGRS",
    "Standard_GZRS",
    "Standard_RAGZRS",
    "Premium_LRS",
    "Premium_ZRS",
}


def _load_account(resource):
    """Return (account_name, resource_group, handler) from the resource's CFVs."""
    name = resource.get_value_for_custom_field("azure_storage_account_name")
    rg = resource.get_value_for_custom_field("azure_storage_account_rg")
    rh_id = resource.get_value_for_custom_field("azure_storage_account_rh_id")
    if not name or not rg or not rh_id:
        raise ValueError("Resource is missing Azure storage account metadata.")

    from resourcehandlers.azure_arm.models import AzureARMHandler

    return name, rg, AzureARMHandler.objects.get(id=rh_id)


def _storage_client(rh):
    from azure.mgmt.storage import StorageManagementClient
    from resourcehandlers.azure_arm.azure_wrapper import configure_arm_client

    wrapper = rh.get_api_wrapper()
    return configure_arm_client(wrapper, StorageManagementClient)


def generate_options_for_sku(field, **kwargs):
    """Common Azure Storage SKUs.

    Docs: https://learn.microsoft.com/en-us/rest/api/storagerp/srp_sku_types
    """
    return {
        "options": [
            ("Standard_LRS", "Standard LRS (locally redundant)"),
            ("Standard_ZRS", "Standard ZRS (zone redundant)"),
            ("Standard_GRS", "Standard GRS (geo redundant)"),
            ("Standard_RAGRS", "Standard RA-GRS (read-access geo redundant)"),
            ("Standard_GZRS", "Standard GZRS (geo-zone redundant)"),
            ("Standard_RAGZRS", "Standard RA-GZRS (read-access geo-zone redundant)"),
            ("Premium_LRS", "Premium LRS (locally redundant)"),
            ("Premium_ZRS", "Premium ZRS (zone redundant)"),
        ],
        "sort": False,
    }


def run(job, resource, **kwargs):
    """Change the storage account's replication SKU."""
    set_progress("Starting Change SKU...")

    sku = "{{ sku }}".strip()
    if sku not in _VALID_SKUS:
        return (
            "FAILURE",
            "",
            f"Invalid SKU '{sku}'. Expected one of: {', '.join(sorted(_VALID_SKUS))}.",
        )

    try:
        account_name, resource_group, rh = _load_account(resource)
    except Exception as exc:
        logger.warning("Could not resolve storage account from resource: %s", exc)
        return "FAILURE", "", "Could not resolve the Azure storage account for this resource."

    try:
        from azure.mgmt.storage.models import StorageAccountUpdateParameters, Sku
        from azure.core.exceptions import HttpResponseError
    except ImportError as exc:
        return "FAILURE", "", f"Azure Storage SDK is not installed: {exc}"

    storage_client = _storage_client(rh)

    set_progress(f"Changing SKU of storage account '{account_name}' to '{sku}'...")

    # Docs: https://learn.microsoft.com/en-us/python/api/azure-mgmt-storage/azure.mgmt.storage.v2023_01_01.operations.storageaccountsoperations#update
    #       StorageAccountsOperations.update(resource_group_name, account_name, parameters) -> StorageAccount.
    #       Azure rejects unsupported SKU conversions with an HttpResponseError.
    try:
        storage_client.storage_accounts.update(
            resource_group,
            account_name,
            StorageAccountUpdateParameters(sku=Sku(name=sku)),
        )
    except HttpResponseError as exc:
        logger.exception("Failed to change SKU")
        return "FAILURE", "", f"Failed to change SKU: {exc.message}"

    resource.set_value_for_custom_field("azure_storage_account_sku", sku)

    msg = f"SKU of storage account '{account_name}' changed to '{sku}'."
    logger.info(msg)
    set_progress(msg)
    return "SUCCESS", msg, ""
