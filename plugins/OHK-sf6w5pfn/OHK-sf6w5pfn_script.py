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

External API: Azure Resource Manager management locks over REST
(api-version 2016-09-01) through shared_modules/azure_management_locks. The
locks SDK client (azure.mgmt.resource.locks) is not installed on CloudBolt
appliances, so this plugin imports no Azure SDK. Operation shapes are anchored
to Microsoft's current docs, cited in the shared module, per
docs/agents/external-apis.md.

Entry point: run(job, resource, **kwargs) -> (status, output_msg, error_msg)
"""

from common.methods import set_progress
from utilities.logger import ThreadLogger

from shared_modules.azure_management_locks import (
    LOCK_LEVELS,
    AzureLockError,
    ManagementLocks,
    lock_level,
    strongest_level,
)

logger = ThreadLogger(__name__)

# The lock this action owns. Locks with any other name were created outside
# CloudBolt and are left alone.
LOCK_NAME = "cloudbolt-lock"

_PERMISSION_HINT = (
    "Insufficient permissions to manage locks. The service principal needs "
    "Microsoft.Authorization/locks/* (Owner or User Access Administrator)."
)


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


def _failure(exc, verb):
    """Map an AzureLockError to this action's FAILURE tuple."""
    if exc.status_code == 403:
        logger.error("Permission denied managing locks: %s", exc)
        return "FAILURE", "", _PERMISSION_HINT
    logger.error("Failed to %s the lock: %s", verb, exc)
    return "FAILURE", "", f"Failed to {verb} the lock: {exc}"


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
    valid = set(LOCK_LEVELS) | {"None"}
    if requested not in valid:
        return "FAILURE", "", f"Lock state must be one of {sorted(valid)}, got '{requested}'."

    try:
        rg_name, rh = _load_group(resource)
        locks = ManagementLocks(rh)
    except Exception as exc:
        logger.warning("Could not resolve resource group from resource: %s", exc)
        return "FAILURE", "", f"Could not resolve the Azure resource group: {exc}"

    if requested == "None":
        set_progress(f"Removing the CloudBolt lock from resource group '{rg_name}'...")
        try:
            removed = locks.delete_resource_group_lock(rg_name, LOCK_NAME)
        except AzureLockError as exc:
            return _failure(exc, "remove")

        if not removed:
            resource.set_value_for_custom_field("azure_resource_group_lock", "None")
            msg = f"Resource group '{rg_name}' had no CloudBolt lock; nothing to remove."
            logger.info(msg)
            set_progress(msg)
            return "WARNING", msg, ""

        new_state = _remaining_lock_state(locks, rg_name)
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
    try:
        locks.set_resource_group_lock(
            rg_name,
            LOCK_NAME,
            requested,
            notes=f"Managed by CloudBolt resource {resource.name}.",
        )
    except AzureLockError as exc:
        return _failure(exc, "apply")

    resource.set_value_for_custom_field("azure_resource_group_lock", requested)

    msg = f"Resource group '{rg_name}' is now locked with level '{requested}'."
    logger.info(msg)
    set_progress(msg)
    return "SUCCESS", msg, ""


def _remaining_lock_state(locks, rg_name):
    """Report the most restrictive lock still on the group after the CloudBolt one is gone.

    Uses list-at-resource-group-level, cited in shared_modules/azure_management_locks.
    """
    try:
        levels = [lock_level(lock) for lock in locks.list_resource_group_locks(rg_name)]
    except AzureLockError as exc:
        logger.warning("Could not re-read locks on %s: %s", rg_name, exc)
        return "None"
    return strongest_level(levels)
