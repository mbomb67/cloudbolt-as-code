"""
CloudBolt Day-2 resource action: Change Access Tier.

Updates the default blob access tier (Hot/Cool) of the Azure storage account
backing this resource. Valid only for StorageV2 and BlobStorage accounts.

Action Inputs:
  - access_tier (STR, required) : "Hot" | "Cool"

External API: Azure Storage Resource Provider via azure-mgmt-storage. Operation
shapes anchored to Microsoft's current docs (cited at the call site) per
docs/agents/external-apis.md.

Entry point: run(job, resource, **kwargs) -> (status, output_msg, error_msg)
"""

from common.methods import set_progress
from utilities.logger import ThreadLogger

logger = ThreadLogger(__name__)

_VALID_TIERS = {"Hot", "Cool"}


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


def generate_options_for_access_tier(field, **kwargs):
    """Blob access tiers."""
    return [("Hot", "Hot"), ("Cool", "Cool")]


def run(job, resource, **kwargs):
    """Change the storage account's default access tier."""
    set_progress("Starting Change Access Tier...")

    access_tier = "{{ access_tier }}".strip().capitalize()
    if access_tier not in _VALID_TIERS:
        return "FAILURE", "", "Access tier must be 'Hot' or 'Cool'."

    try:
        account_name, resource_group, rh = _load_account(resource)
    except Exception as exc:
        logger.warning("Could not resolve storage account from resource: %s", exc)
        return "FAILURE", "", "Could not resolve the Azure storage account for this resource."

    try:
        from azure.mgmt.storage.models import StorageAccountUpdateParameters
        from azure.core.exceptions import HttpResponseError
    except ImportError as exc:
        return "FAILURE", "", f"Azure Storage SDK is not installed: {exc}"

    storage_client = _storage_client(rh)

    set_progress(
        f"Setting access tier of storage account '{account_name}' to '{access_tier}'..."
    )

    # Docs: https://learn.microsoft.com/en-us/python/api/azure-mgmt-storage/azure.mgmt.storage.v2023_01_01.operations.storageaccountsoperations#update
    #       StorageAccountsOperations.update(resource_group_name, account_name, parameters) -> StorageAccount.
    #       access_tier is only honored on StorageV2 / BlobStorage accounts.
    try:
        storage_client.storage_accounts.update(
            resource_group,
            account_name,
            StorageAccountUpdateParameters(access_tier=access_tier),
        )
    except HttpResponseError as exc:
        logger.exception("Failed to change access tier")
        return "FAILURE", "", f"Failed to change access tier: {exc.message}"

    msg = f"Access tier of storage account '{account_name}' set to '{access_tier}'."
    logger.info(msg)
    set_progress(msg)
    return "SUCCESS", msg, ""
