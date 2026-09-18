"""
CloudBolt teardown plugin: Azure Resource Group.

Deletes the Azure Resource Group recorded on the CloudBolt Resource by the build
(or discovery) plugin for the "Azure Resource Group" blueprint.

Deleting a resource group deletes EVERYTHING inside it. This plugin therefore:
  1. refuses to run while a CanNotDelete / ReadOnly management lock is in place,
     pointing the user at the "Manage Delete Lock" day-2 action; and
  2. logs an inventory of what the group contains before issuing the delete, so
     the job history records what was removed.

Idempotency requirement: this plugin returns WARNING (not FAILURE) when the
group, its metadata, or its handler can no longer be found, so re-runs and
already-deleted resources tear down cleanly. See docs/agents/plugin-templates.md.

RBAC: the Azure handler is rehydrated from the stored resource-handler ID, which
was itself derived from an RBAC-gated Environment selection at build time.

External API: Azure Resource Manager via azure-mgmt-resource. Operation shapes
anchored to Microsoft's current docs (cited at each call site) per
docs/agents/external-apis.md.

Returns a 3-tuple: (status, output_msg, error_msg)
  status: "SUCCESS" | "WARNING" | "FAILURE"
"""

from common.methods import set_progress
from utilities.logger import ThreadLogger

logger = ThreadLogger(__name__)

_BLOCKING_LOCK_LEVELS = {"cannotdelete", "readonly"}

# Cap the pre-delete inventory so a huge resource group cannot flood the job log.
_INVENTORY_LIMIT = 25


def _blocking_locks(wrapper, rg_name):
    """Return the names of management locks that would block deletion.

    Docs: https://learn.microsoft.com/en-us/python/api/azure-mgmt-resource/azure.mgmt.resource.locks.operations.managementlocksoperations#list-at-resource-group-level
          ManagementLocksOperations.list_at_resource_group_level(resource_group_name) -> ItemPaged[ManagementLockObject]
    A missing lock client or a locks/read denial returns [] — the delete call
    itself remains the authoritative check.
    """
    try:
        from azure.mgmt.resource.locks import ManagementLockClient
        from resourcehandlers.azure_arm.azure_wrapper import configure_arm_client

        lock_client = configure_arm_client(wrapper, ManagementLockClient)
        locks = lock_client.management_locks.list_at_resource_group_level(rg_name)
        return [
            lock.name
            for lock in locks
            if str(getattr(lock.level, "value", lock.level) or "").lower() in _BLOCKING_LOCK_LEVELS
        ]
    except Exception as exc:
        logger.warning("Could not read locks on resource group %s: %s", rg_name, exc)
        return []


def _log_contents(resource_client, rg_name):
    """Record what the resource group contains before it is deleted.

    Docs: https://learn.microsoft.com/en-us/python/api/azure-mgmt-resource/azure.mgmt.resource.resources.operations.resourcesoperations#list-by-resource-group
          ResourcesOperations.list_by_resource_group(resource_group_name) -> ItemPaged[GenericResourceExpanded]
    """
    try:
        contents = list(resource_client.resources.list_by_resource_group(rg_name))
    except Exception as exc:
        logger.warning("Could not inventory resource group %s before delete: %s", rg_name, exc)
        return

    if not contents:
        set_progress(f"Resource group '{rg_name}' is empty.")
        return

    set_progress(
        f"Resource group '{rg_name}' contains {len(contents)} resource(s); "
        "all of them will be deleted:"
    )
    for item in contents[:_INVENTORY_LIMIT]:
        set_progress(f"  - {item.type} / {item.name}")
    if len(contents) > _INVENTORY_LIMIT:
        set_progress(f"  ... and {len(contents) - _INVENTORY_LIMIT} more.")


def run(job, **kwargs):
    """Delete the Azure Resource Group."""
    set_progress("Starting Azure Resource Group teardown...")
    logger.info("Azure Resource Group teardown plugin started for job %s", job.id)

    resource = job.resource_set.first()
    if resource is None:
        msg = "No resource associated with this job; assuming already deleted."
        logger.warning(msg)
        return "WARNING", msg, ""

    rg_name = resource.get_value_for_custom_field("azure_resource_group_name")
    rh_id = resource.get_value_for_custom_field("azure_resource_group_rh_id")

    if not rg_name:
        msg = "Resource is missing the resource group name; assuming already deleted."
        logger.warning(msg)
        set_progress(msg)
        return "WARNING", msg, ""

    if not rh_id:
        msg = (
            "Resource is missing the Azure handler ID; cannot rehydrate handler. "
            "Assuming already deleted."
        )
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

    try:
        from azure.core.exceptions import HttpResponseError, ResourceNotFoundError
        from azure.mgmt.resource import ResourceManagementClient
        from resourcehandlers.azure_arm.azure_wrapper import configure_arm_client
    except ImportError as exc:
        logger.exception("azure-mgmt-resource SDK not available")
        return "FAILURE", "", f"Azure Resource Management SDK is not installed: {exc}"

    try:
        wrapper = rh.get_api_wrapper()
        resource_client = configure_arm_client(wrapper, ResourceManagementClient)
    except Exception as exc:
        msg = f"Failed to create Azure resource client for handler {rh}."
        logger.exception(msg)
        return "WARNING", f"{msg} Assuming already deleted.", ""

    # ---- Already gone? --------------------------------------------------
    # Docs: https://learn.microsoft.com/en-us/python/api/azure-mgmt-resource/azure.mgmt.resource.resources.operations.resourcegroupsoperations#check-existence
    try:
        if not resource_client.resource_groups.check_existence(rg_name):
            msg = f"Resource group '{rg_name}' not found; assuming already deleted."
            logger.warning(msg)
            set_progress(msg)
            return "WARNING", msg, ""
    except HttpResponseError as exc:
        logger.warning("Existence check for '%s' failed (continuing): %s", rg_name, exc)

    # ---- Refuse to fight a management lock ------------------------------
    locks = _blocking_locks(wrapper, rg_name)
    if locks:
        lock_list = ", ".join(sorted(locks))
        return (
            "FAILURE",
            "",
            f"Resource group '{rg_name}' is protected by management lock(s): {lock_list}. "
            "Run the 'Manage Delete Lock' action and choose 'None' to release it, then retry the delete.",
        )

    _log_contents(resource_client, rg_name)
    set_progress(f"Deleting resource group '{rg_name}'...")

    # Docs: https://learn.microsoft.com/en-us/python/api/azure-mgmt-resource/azure.mgmt.resource.resources.operations.resourcegroupsoperations#begin-delete
    #       ResourceGroupsOperations.begin_delete(resource_group_name) -> LROPoller[None].
    #       Long-running: deleting the group deletes every resource inside it.
    try:
        poller = resource_client.resource_groups.begin_delete(rg_name)
        poller.result()  # blocks until the group and its contents are gone
    except ResourceNotFoundError:
        msg = f"Resource group '{rg_name}' not found; assuming already deleted."
        logger.warning(msg)
        set_progress(msg)
        return "WARNING", msg, ""
    except HttpResponseError as exc:
        if exc.status_code == 404:
            msg = f"Resource group '{rg_name}' not found; assuming already deleted."
            logger.warning(msg)
            set_progress(msg)
            return "WARNING", msg, ""
        if exc.status_code == 403:
            logger.error("Permission denied deleting resource group '%s'", rg_name)
            return "FAILURE", "", "Insufficient permissions to delete the resource group."
        logger.exception("Azure resource group deletion failed")
        return "FAILURE", "", f"Failed to delete resource group: {exc.message}"

    msg = f"Resource group '{rg_name}' deleted successfully."
    logger.info(msg)
    set_progress(msg)
    return "SUCCESS", msg, ""
