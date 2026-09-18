"""
CloudBolt Day-2 resource action: Delete Blob Container.

Deletes a blob container from the Azure storage account backing this resource,
using the storage management plane. Idempotent: returns WARNING (not FAILURE)
when the container is already gone.

Action Inputs:
  - container_name (STR, required) : name of the container to delete

The storage account, resource group, and Azure handler are read from the
custom fields persisted on the resource and the handler is rehydrated by ID.
See docs/agents/rbac-and-security.md.

External API: Azure Storage Resource Provider via azure-mgmt-storage. Operation
shapes anchored to Microsoft's current docs (cited at the call site) per
docs/agents/external-apis.md.

Entry point: run(job, resource, **kwargs) -> (status, output_msg, error_msg)
"""

from common.methods import set_progress
from utilities.logger import ThreadLogger

logger = ThreadLogger(__name__)


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


def generate_options_for_container_name(field, **kwargs):
    """List the existing blob containers in this resource's storage account.

    CloudBolt passes the target resource in kwargs for resource-action option
    generation, so the dropdown is scoped to this account's containers.

    Docs: https://learn.microsoft.com/en-us/python/api/azure-mgmt-storage/azure.mgmt.storage.v2023_01_01.operations.blobcontainersoperations#list
          BlobContainersOperations.list(resource_group_name, account_name) -> iterator[ListContainerItem].
    """
    resource = kwargs.get("resource")
    if resource is None:
        return [("", "------ No resource context ------")]

    try:
        account_name, resource_group, rh = _load_account(resource)
    except Exception as exc:
        logger.warning("Could not resolve storage account for container options: %s", exc)
        return [("", "------ Storage account metadata unavailable ------")]

    try:
        storage_client = _storage_client(rh)
        containers = storage_client.blob_containers.list(resource_group, account_name)
        options = [(c.name, c.name) for c in containers if c.name]
    except Exception as exc:
        logger.warning("Failed to list containers for %s: %s", account_name, exc)
        return [("", "------ Could not load containers ------")]

    if not options:
        return [("", "------ No containers found ------")]

    options.sort(key=lambda x: x[1])
    return options


def run(job, resource, **kwargs):
    """Delete the blob container."""
    set_progress("Starting Delete Blob Container...")

    container_name = "{{ container_name }}".strip().lower()
    if not container_name:
        return "FAILURE", "", "A container name is required."

    try:
        account_name, resource_group, rh = _load_account(resource)
    except Exception as exc:
        logger.warning("Could not resolve storage account from resource: %s", exc)
        return "FAILURE", "", "Could not resolve the Azure storage account for this resource."

    try:
        from azure.core.exceptions import ResourceNotFoundError, HttpResponseError
    except ImportError as exc:
        return "FAILURE", "", f"Azure Storage SDK is not installed: {exc}"

    storage_client = _storage_client(rh)

    set_progress(
        f"Deleting container '{container_name}' from storage account '{account_name}'..."
    )

    # Docs: https://learn.microsoft.com/en-us/python/api/azure-mgmt-storage/azure.mgmt.storage.v2023_01_01.operations.blobcontainersoperations#delete
    #       BlobContainersOperations.delete(resource_group_name, account_name, container_name) -> None.
    try:
        storage_client.blob_containers.delete(resource_group, account_name, container_name)
    except ResourceNotFoundError:
        msg = f"Container '{container_name}' not found; assuming already deleted."
        logger.warning(msg)
        set_progress(msg)
        return "WARNING", msg, ""
    except HttpResponseError as exc:
        if exc.status_code == 404:
            msg = f"Container '{container_name}' not found; assuming already deleted."
            logger.warning(msg)
            set_progress(msg)
            return "WARNING", msg, ""
        if exc.status_code == 403:
            return "FAILURE", "", "Insufficient permissions to delete the container."
        logger.exception("Failed to delete blob container")
        return "FAILURE", "", f"Failed to delete container: {exc.message}"

    msg = f"Container '{container_name}' deleted from storage account '{account_name}'."
    logger.info(msg)
    set_progress(msg)
    return "SUCCESS", msg, ""
