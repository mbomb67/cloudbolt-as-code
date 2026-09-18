"""
CloudBolt teardown plugin: Azure Storage Account.

Deletes the Azure Storage Account recorded on the CloudBolt Resource by the
build (or discovery) plugin for the "Azure Storage Account" blueprint.

Idempotency requirement: this plugin returns WARNING (not FAILURE) when the
account, its metadata, or its handler can no longer be found, so re-runs and
already-deleted resources tear down cleanly. See docs/agents/plugin-templates.md.

RBAC: the Azure handler is rehydrated from the stored resource-handler ID, which
was itself derived from an RBAC-gated Environment selection at build time.

External API: Azure Storage Resource Provider via azure-mgmt-storage. Operation
shapes anchored to Microsoft's current docs (cited at the call site) per
docs/agents/external-apis.md.

Returns a 3-tuple: (status, output_msg, error_msg)
  status: "SUCCESS" | "WARNING" | "FAILURE"
"""

from common.methods import set_progress
from utilities.logger import ThreadLogger

logger = ThreadLogger(__name__)


def run(job, **kwargs):
    """Delete the Azure Storage Account."""
    set_progress("Starting Azure Storage Account teardown...")
    logger.info("Azure Storage Account teardown plugin started for job %s", job.id)

    resource = job.resource_set.first()
    if resource is None:
        msg = "No resource associated with this job; assuming already deleted."
        logger.warning(msg)
        return "WARNING", msg, ""

    account_name = resource.get_value_for_custom_field("azure_storage_account_name")
    resource_group = resource.get_value_for_custom_field("azure_storage_account_rg")
    rh_id = resource.get_value_for_custom_field("azure_storage_account_rh_id")

    if not account_name or not resource_group:
        msg = "Resource is missing storage account metadata; assuming already deleted."
        logger.warning(msg)
        set_progress(msg)
        return "WARNING", msg, ""

    if not rh_id:
        msg = "Resource is missing the Azure handler ID; cannot rehydrate handler. Assuming already deleted."
        logger.warning(msg)
        set_progress(msg)
        return "WARNING", msg, ""

    try:
        from resourcehandlers.azure_arm.models import AzureARMHandler

        rh = AzureARMHandler.objects.get(id=rh_id)
    except Exception as exc:
        msg = f"Failed to load Azure handler (id={rh_id}); assuming already deleted."
        logger.warning("%s (%s)", msg, exc)
        return "WARNING", msg, ""

    set_progress(
        f"Deleting storage account '{account_name}' from resource group '{resource_group}'..."
    )

    # Docs: https://learn.microsoft.com/en-us/python/api/azure-mgmt-storage/azure.mgmt.storage.v2023_01_01.operations.storageaccountsoperations#delete
    #       StorageAccountsOperations.delete(resource_group_name, account_name) -> None.
    #       Returns 200/204 whether or not the account existed; a missing resource
    #       group surfaces as ResourceNotFoundError / ResourceGroupNotFound.
    try:
        from azure.mgmt.storage import StorageManagementClient
        from azure.core.exceptions import ResourceNotFoundError, HttpResponseError
        from resourcehandlers.azure_arm.azure_wrapper import configure_arm_client
    except ImportError as exc:
        logger.exception("azure-mgmt-storage SDK not available")
        return "FAILURE", "", f"Azure Storage SDK is not installed: {exc}"

    try:
        wrapper = rh.get_api_wrapper()
        storage_client = configure_arm_client(wrapper, StorageManagementClient)
    except Exception as exc:
        msg = f"Failed to create Azure storage client for handler {rh}."
        logger.exception(msg)
        return "WARNING", f"{msg} Assuming already deleted.", ""

    try:
        storage_client.storage_accounts.delete(resource_group, account_name)
    except ResourceNotFoundError:
        msg = f"Storage account '{account_name}' not found; assuming already deleted."
        logger.warning(msg)
        set_progress(msg)
        return "WARNING", msg, ""
    except HttpResponseError as exc:
        if exc.status_code == 404:
            msg = f"Storage account '{account_name}' not found; assuming already deleted."
            logger.warning(msg)
            set_progress(msg)
            return "WARNING", msg, ""
        if exc.status_code == 403:
            logger.error("Permission denied deleting storage account '%s'", account_name)
            return "FAILURE", "", "Insufficient permissions to delete the storage account."
        logger.exception("Azure storage account deletion failed")
        return "FAILURE", "", f"Failed to delete storage account: {exc.message}"

    msg = f"Storage account '{account_name}' deleted successfully."
    logger.info(msg)
    set_progress(msg)
    return "SUCCESS", msg, ""
