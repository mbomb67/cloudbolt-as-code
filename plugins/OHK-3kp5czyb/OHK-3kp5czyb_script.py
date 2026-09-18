"""
CloudBolt Day-2 resource action: List Blob Containers.

Lists the blob containers in the Azure storage account backing this resource
and returns them in the action output. Read-only; takes no inputs.

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


def run(job, resource, **kwargs):
    """List the blob containers in the storage account."""
    set_progress("Listing blob containers...")

    try:
        account_name, resource_group, rh = _load_account(resource)
    except Exception as exc:
        logger.warning("Could not resolve storage account from resource: %s", exc)
        return "FAILURE", "", "Could not resolve the Azure storage account for this resource."

    try:
        from azure.core.exceptions import HttpResponseError
    except ImportError as exc:
        return "FAILURE", "", f"Azure Storage SDK is not installed: {exc}"

    storage_client = _storage_client(rh)

    # Docs: https://learn.microsoft.com/en-us/python/api/azure-mgmt-storage/azure.mgmt.storage.v2023_01_01.operations.blobcontainersoperations#list
    #       BlobContainersOperations.list(resource_group_name, account_name) -> iterator[ListContainerItem].
    #       Each item carries name, public_access, last_modified_time, lease_state.
    try:
        containers = list(storage_client.blob_containers.list(resource_group, account_name))
    except HttpResponseError as exc:
        logger.exception("Failed to list blob containers")
        return "FAILURE", "", f"Failed to list containers: {exc.message}"

    if not containers:
        msg = f"Storage account '{account_name}' has no blob containers."
        set_progress(msg)
        return "SUCCESS", msg, ""

    lines = [f"Blob containers in storage account '{account_name}' ({len(containers)}):"]
    for c in sorted(containers, key=lambda x: x.name or ""):
        public_access = c.public_access or "None"
        modified = getattr(c, "last_modified_time", None)
        modified_str = modified.isoformat() if modified else "unknown"
        lines.append(f"  - {c.name} (public access: {public_access}, last modified: {modified_str})")

    output = "\n".join(lines)
    for line in lines:
        set_progress(line)

    return "SUCCESS", output, ""
