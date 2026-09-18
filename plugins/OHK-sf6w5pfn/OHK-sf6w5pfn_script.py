"""
CloudBolt Day-2 resource action: Manage Delete Lock.

Applies or releases an Azure management lock on the resource group backing this
resource. A CanNotDelete lock is the standard guardrail against accidental
deletion of a whole resource group; ReadOnly additionally blocks modification.

The blueprint's teardown plugin refuses to delete a locked group, so this action
is the supported way to unlock before decommissioning.

Action Inputs:
  - rg_lock_state (STR, required): "CanNotDelete" | "ReadOnly" | "None"

Only locks created by CloudBolt (lock name "cloudbolt-lock") are managed here;
locks created elsewhere are reported but left untouched.

Required Azure permissions: Microsoft.Authorization/locks/* — of the built-in
roles, only Owner and User Access Administrator grant these.

External API: Azure Resource Manager management locks via azure-mgmt-resource.
Operation shapes anchored to Microsoft's current docs (cited at each call site)
per docs/agents/external-apis.md.

Entry point: run(job, resource, **kwargs) -> (status, output_msg, error_msg)
"""

from common.methods import set_progress
from utilities.logger import ThreadLogger

logger = ThreadLogger(__name__)

# The lock this action owns. Locks with any other name were created outside
# CloudBolt and are left alone.
LOCK_NAME = "cloudbolt-lock"

_LOCK_LEVELS = ("CanNotDelete", "ReadOnly")


def _resolve_resource(job, resource, kwargs):
    """Return the Resource this action targets.

    CloudBolt hands plugins their context as keyword arguments, and which key
    carries the resource depends on how the action was invoked: a single-resource
    run supplies `resource`, the bulk/list path supplies `resources`, and any
    job-backed run has it on the job. Accept all three rather than trusting one
    slot — the same defensive read other day-2 plugins in this repo use.
    """
    if resource is not None:
        return resource
    if kwargs.get("resource") is not None:
        return kwargs["resource"]
    for candidate in kwargs.get("resources") or []:
        if candidate is not None:
            return candidate
    if job is not None:
        return job.resource_set.first()
    return None


def _load_group(resource):
    """Return (rg_name, handler) from the resource's custom field values.

    Raises ValueError naming the specific missing piece, so a failure message
    identifies the cause instead of collapsing every cause into one string.
    """
    if resource is None:
        raise ValueError("no Resource was supplied to this action")

    rg_name = resource.get_value_for_custom_field("azure_resource_group_name")
    if not rg_name:
        raise ValueError("resource has no 'azure_resource_group_name' value")

    rh_id = resource.get_value_for_custom_field("azure_resource_group_rh_id")
    if not rh_id:
        raise ValueError("resource has no 'azure_resource_group_rh_id' value")

    from resourcehandlers.azure_arm.models import AzureARMHandler

    try:
        return rg_name, AzureARMHandler.objects.get(id=rh_id)
    except AzureARMHandler.DoesNotExist:
        raise ValueError(f"no Azure resource handler with id={rh_id}")


def _lock_client(rh):
    from azure.mgmt.resource.locks import ManagementLockClient
    from resourcehandlers.azure_arm.azure_wrapper import configure_arm_client

    return configure_arm_client(rh.get_api_wrapper(), ManagementLockClient)


def _level_of(lock):
    """Read a ManagementLockObject.level as a plain string (it may be an enum)."""
    return str(getattr(lock.level, "value", lock.level) or "")


def generate_options_for_rg_lock_state(field, **kwargs):
    """Target lock state for the resource group.

    Docs: https://learn.microsoft.com/en-us/azure/azure-resource-manager/management/lock-resources
    """
    return {
        "options": [
            ("CanNotDelete", "CanNotDelete - block deletion, allow changes"),
            ("ReadOnly", "ReadOnly - block deletion and changes"),
            ("None", "None - remove the CloudBolt lock"),
        ],
        "initial_value": "CanNotDelete",
        "sort": False,
    }


def run(job=None, resource=None, **kwargs):
    """Apply or release the CloudBolt management lock on the resource group."""
    set_progress("Starting Manage Delete Lock...")

    resource = _resolve_resource(job, resource, kwargs)
    requested = "{{ rg_lock_state }}".strip()
    valid = set(_LOCK_LEVELS) | {"None"}
    if requested not in valid:
        return "FAILURE", "", f"Lock state must be one of {sorted(valid)}, got '{requested}'."

    try:
        rg_name, rh = _load_group(resource)
    except Exception as exc:
        logger.warning("Could not resolve resource group from resource: %s", exc)
        return "FAILURE", "", f"Could not resolve the Azure resource group: {exc}"

    try:
        from azure.core.exceptions import HttpResponseError, ResourceNotFoundError
        from azure.mgmt.resource.locks.models import ManagementLockObject
    except ImportError as exc:
        return "FAILURE", "", f"Azure management lock SDK is not installed: {exc}"

    lock_client = _lock_client(rh)

    if requested == "None":
        set_progress(f"Removing the CloudBolt lock from resource group '{rg_name}'...")

        # Docs: https://learn.microsoft.com/en-us/python/api/azure-mgmt-resource/azure.mgmt.resource.locks.operations.managementlocksoperations#delete-at-resource-group-level
        #       ManagementLocksOperations.delete_at_resource_group_level(resource_group_name, lock_name) -> None
        try:
            lock_client.management_locks.delete_at_resource_group_level(rg_name, LOCK_NAME)
        except ResourceNotFoundError:
            resource.set_value_for_custom_field("azure_resource_group_lock", "None")
            msg = f"Resource group '{rg_name}' had no CloudBolt lock; nothing to remove."
            logger.info(msg)
            set_progress(msg)
            return "WARNING", msg, ""
        except HttpResponseError as exc:
            if exc.status_code == 404:
                resource.set_value_for_custom_field("azure_resource_group_lock", "None")
                msg = f"Resource group '{rg_name}' had no CloudBolt lock; nothing to remove."
                logger.info(msg)
                set_progress(msg)
                return "WARNING", msg, ""
            logger.exception("Failed to remove management lock")
            return "FAILURE", "", f"Failed to remove the lock: {exc.message}"

        new_state = _remaining_lock_state(lock_client, rg_name)
        resource.set_value_for_custom_field("azure_resource_group_lock", new_state)

        msg = f"CloudBolt lock removed from resource group '{rg_name}'."
        if new_state != "None":
            msg += f" A lock created outside CloudBolt is still in place ({new_state})."
            logger.info(msg)
            set_progress(msg)
            return "WARNING", msg, ""

        logger.info(msg)
        set_progress(msg)
        return "SUCCESS", msg, ""

    set_progress(f"Applying a {requested} lock to resource group '{rg_name}'...")

    # Docs: https://learn.microsoft.com/en-us/python/api/azure-mgmt-resource/azure.mgmt.resource.locks.operations.managementlocksoperations#create-or-update-at-resource-group-level
    #       ManagementLocksOperations.create_or_update_at_resource_group_level(
    #           resource_group_name, lock_name, parameters: ManagementLockObject) -> ManagementLockObject
    try:
        lock_client.management_locks.create_or_update_at_resource_group_level(
            rg_name,
            LOCK_NAME,
            ManagementLockObject(
                level=requested,
                notes=f"Managed by CloudBolt resource {resource.name}.",
            ),
        )
    except HttpResponseError as exc:
        if exc.status_code == 403:
            logger.error("Permission denied managing locks on '%s'", rg_name)
            return (
                "FAILURE",
                "",
                "Insufficient permissions to manage locks. The service principal needs "
                "Microsoft.Authorization/locks/* (Owner or User Access Administrator).",
            )
        logger.exception("Failed to apply management lock")
        return "FAILURE", "", f"Failed to apply the lock: {exc.message}"

    resource.set_value_for_custom_field("azure_resource_group_lock", requested)

    msg = f"Resource group '{rg_name}' is now locked with level '{requested}'."
    logger.info(msg)
    set_progress(msg)
    return "SUCCESS", msg, ""


def _remaining_lock_state(lock_client, rg_name):
    """Report the strongest lock still on the group after the CloudBolt one is gone.

    Docs: https://learn.microsoft.com/en-us/python/api/azure-mgmt-resource/azure.mgmt.resource.locks.operations.managementlocksoperations#list-at-resource-group-level
          ManagementLocksOperations.list_at_resource_group_level(resource_group_name) -> ItemPaged[ManagementLockObject]
    """
    try:
        levels = {
            _level_of(lock)
            for lock in lock_client.management_locks.list_at_resource_group_level(rg_name)
        }
    except Exception as exc:
        logger.warning("Could not re-read locks on %s: %s", rg_name, exc)
        return "None"

    for level in _LOCK_LEVELS:
        if level in levels:
            return level
    return "None"
