"""
Generic Bicep teardown plugin.

Deletes the Azure deployment stack backing the resource with delete-all
semantics — one call regardless of what the template deployed. Auto-confirmed
(deletion is already an explicit user action); no approval pause.

Scope-aware: a resource-group-scoped stack is addressed via the stored
bicep_resource_group; a subscription-scoped one (bicep_target_scope ==
subscription, no RG) via the subscription. Delete-all on a subscription-scoped
stack also deletes the resource group(s) the template created
(unmanageAction.ResourceGroups=delete). Resources with no stored scope are
treated as resource-group scoped (pre-scope-support resources).

Idempotent and PROVFAILED-tolerant (R16): a missing stack name/resource group
or an already-deleted stack yields WARNING (not FAILURE) so failed or
half-provisioned resources delete cleanly. A delete already in flight is waited
out; a 409 (resources still managed / locked) gets bounded retries then FAILURE
with guidance — the stack is never force-deleted.

Returns (status, output_msg, error_msg).
"""
import time

from common.methods import set_progress
from utilities.logger import ThreadLogger

from shared_modules.bicep_engine import (
    BicepArmClient,
    BicepEngineError,
    SCOPE_RESOURCE_GROUP,
    SCOPE_SUBSCRIPTION,
)

logger = ThreadLogger(__name__)

DELETE_CONFIRM_TIMEOUT_S = 60 * 20
DELETE_POLL_INTERVAL_S = 10
DELETE_RETRY_ATTEMPTS = 3


def _cf(resource, name):
    try:
        return resource.get_value_for_custom_field(name)
    except Exception:
        return None


def _confirm_gone(client, rg, stack_name):
    deadline = time.time() + DELETE_CONFIRM_TIMEOUT_S
    while time.time() < deadline:
        try:
            if client.get_stack(rg, stack_name) is None:
                return True
        except BicepEngineError:
            # Transient ARM error during the poll window — keep waiting rather
            # than crashing teardown with a traceback.
            pass
        time.sleep(DELETE_POLL_INTERVAL_S)
    return False


def run(job, **kwargs):
    resource = kwargs.get("resource")
    if resource is None:
        return "WARNING", "No resource in job context; nothing to tear down.", ""

    stack_name = _cf(resource, "bicep_stack_name")
    rg = _cf(resource, "bicep_resource_group") or None
    scope = _cf(resource, "bicep_target_scope") or (SCOPE_RESOURCE_GROUP if rg else None)
    rh_id = _cf(resource, "bicep_rh_id")

    if not stack_name or (scope != SCOPE_SUBSCRIPTION and not rg):
        return ("WARNING",
                "No Bicep deployment stack recorded on this resource; nothing to "
                "clean up.", "")
    if not rh_id:
        return ("WARNING",
                f"No resource handler recorded; cannot reach Azure to delete "
                f"stack {stack_name}.", "")

    from resourcehandlers.azure_arm.models import AzureARMHandler
    try:
        rh = AzureARMHandler.objects.get(id=int(rh_id))
    except (AzureARMHandler.DoesNotExist, ValueError, TypeError):
        return ("WARNING",
                f"Resource handler {rh_id} no longer exists; cannot delete stack "
                f"{stack_name}.", "")

    client = BicepArmClient(rh)
    try:
        stack = client.get_stack(rg, stack_name)
    except BicepEngineError as e:
        return "FAILURE", "", f"Could not query deployment stack {stack_name}: {e}"

    if stack is None:
        return ("WARNING",
                f"Deployment stack {stack_name} is already gone; nothing to "
                "delete.", "")

    state = (stack.get("properties") or {}).get("provisioningState", "")
    if state == "deleting":
        set_progress(f"Stack {stack_name} is already deleting; waiting for it to finish...")
        if _confirm_gone(client, rg, stack_name):
            return "SUCCESS", f"Deployment stack {stack_name} deleted.", ""
        return "FAILURE", "", f"Stack {stack_name} still deleting after timeout."

    where = f"in resource group {rg}" if rg else "at subscription scope"
    set_progress(f"Deleting deployment stack {stack_name} {where} (delete-all"
                 + ("; this removes the resource group(s) it created" if not rg else "")
                 + ")...")
    for attempt in range(DELETE_RETRY_ATTEMPTS):
        try:
            client.delete_stack(rg, stack_name, mode="deleteAll")
            break
        except BicepEngineError as e:
            if attempt == DELETE_RETRY_ATTEMPTS - 1:
                return ("FAILURE", "",
                        f"Failed to delete stack {stack_name} after "
                        f"{DELETE_RETRY_ATTEMPTS} attempts: {e}. The stack was "
                        f"never force-deleted; resolve the blocking condition "
                        f"(e.g. a resource lock) and delete the resource again.")
            time.sleep(DELETE_POLL_INTERVAL_S)

    if _confirm_gone(client, rg, stack_name):
        return "SUCCESS", f"Deployment stack {stack_name} deleted.", ""
    return ("WARNING",
            f"Delete of stack {stack_name} was submitted but is not yet confirmed "
            f"complete; Azure is still processing it.", "")
