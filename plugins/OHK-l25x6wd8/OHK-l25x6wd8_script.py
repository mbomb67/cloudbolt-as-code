"""
CloudBolt Day-2 resource action: Create Blob Container.

Creates a blob container in the Azure storage account backing this resource,
using the storage management plane (no account keys required).

Action Inputs:
  - container_name (STR, required) : 3-63 chars, lowercase alphanumeric + single hyphens
  - public_access (STR, optional)  : "none" (default) | "blob" | "container"

The storage account, resource group, and Azure handler are read from the
custom fields the build/discovery plugins persisted on the resource; the
handler is rehydrated by ID (RBAC was enforced at provision time via the
environment selection). See docs/agents/rbac-and-security.md.

External API: Azure Storage Resource Provider via azure-mgmt-storage. Operation
shapes anchored to Microsoft's current docs (cited at the call site) per
docs/agents/external-apis.md.

Entry point: run(job, resource, **kwargs) -> (status, output_msg, error_msg)
"""

import re

from common.methods import set_progress
from utilities.logger import ThreadLogger

logger = ThreadLogger(__name__)

# Azure blob container naming rules:
# https://learn.microsoft.com/en-us/rest/api/storageservices/naming-and-referencing-containers--blobs--and-metadata
_CONTAINER_RE = re.compile(r"^[a-z0-9](?:[a-z0-9]|-(?!-)){1,61}[a-z0-9]$")

# Map the friendly input to the SDK PublicAccess values.
_PUBLIC_ACCESS = {"none": "None", "blob": "Blob", "container": "Container"}


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


def generate_options_for_public_access(field, **kwargs):
    """Anonymous public access levels for the container."""
    return {
        "options": [
            ("none", "Private (no anonymous access)"),
            ("blob", "Blob (anonymous read for blobs only)"),
            ("container", "Container (anonymous read for container + blobs)"),
        ],
        "initial_value": "none",
        "sort": False,
    }


def run(job, resource, **kwargs):
    """Create the blob container."""
    set_progress("Starting Create Blob Container...")

    container_name = "{{ container_name }}".strip().lower()
    public_access = ("{{ public_access }}".strip().lower() or "none")

    if not _CONTAINER_RE.match(container_name):
        return (
            "FAILURE",
            "",
            "Container name must be 3-63 characters: lowercase letters, numbers and "
            "single hyphens, starting and ending with a letter or number.",
        )
    if public_access not in _PUBLIC_ACCESS:
        return "FAILURE", "", f"Invalid public access level '{public_access}'."

    try:
        account_name, resource_group, rh = _load_account(resource)
    except Exception as exc:
        logger.warning("Could not resolve storage account from resource: %s", exc)
        return "FAILURE", "", "Could not resolve the Azure storage account for this resource."

    try:
        from azure.mgmt.storage.models import BlobContainer
        from azure.core.exceptions import HttpResponseError
    except ImportError as exc:
        return "FAILURE", "", f"Azure Storage SDK is not installed: {exc}"

    storage_client = _storage_client(rh)

    set_progress(
        f"Creating container '{container_name}' (public access: {public_access}) "
        f"in storage account '{account_name}'..."
    )

    # Docs: https://learn.microsoft.com/en-us/python/api/azure-mgmt-storage/azure.mgmt.storage.v2023_01_01.operations.blobcontainersoperations#create
    #       BlobContainersOperations.create(resource_group_name, account_name, container_name, blob_container) -> BlobContainer.
    #       PUT semantics: succeeds whether or not the container already exists.
    try:
        storage_client.blob_containers.create(
            resource_group,
            account_name,
            container_name,
            BlobContainer(public_access=_PUBLIC_ACCESS[public_access]),
        )
    except HttpResponseError as exc:
        if exc.status_code == 409:
            msg = f"Container '{container_name}' already exists."
            logger.warning(msg)
            set_progress(msg)
            return "WARNING", msg, ""
        logger.exception("Failed to create blob container")
        return "FAILURE", "", f"Failed to create container: {exc.message}"

    msg = f"Container '{container_name}' created in storage account '{account_name}'."
    logger.info(msg)
    set_progress(msg)
    return "SUCCESS", msg, ""
