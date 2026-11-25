"""
CloudBolt day-2 resource action: Request AKS Namespace Quota Change (AKS Namespace).

Moves an AKS namespace landing zone to a different size tier. The resource
action that wraps this plugin requires approval, so the request waits in
CloudBolt's approval queue until an approver for the group accepts it; only
then does this code run and, through ``aks_namespace.change_tier``:
  1. set the managed namespace's CPU and memory quota (enforced by AKS) and
     its cloudbolt.io/size-tier label to the new tier, through Azure
     Resource Manager,
  2. replace the namespace's storage, claim, and pod ResourceQuota,
  3. mirror the new limits into the pinned CloudBolt Environment quota, and
  4. record the new tier on the Resource.
  https://learn.microsoft.com/en-us/rest/api/aks/managed-namespaces/create-or-update

Action Inputs:
  - aks_ns_new_size_tier (STR, required): small | medium | large | xlarge
  - aks_ns_change_reason (TXT, optional): justification, echoed into the job log

Kubernetes accepts a quota below current usage: existing pods keep running
and new ones are refused until usage drops. The job log shows the usage at
the time of the change so approvers and the team can see what a downsize
will block.
https://kubernetes.io/docs/concepts/policy/resource-quotas/

Entry point: run(job, resource, **kwargs) -> (status, output_msg, error_msg)
"""

from common.methods import set_progress
from shared_modules.aks_namespace import (
    CF_PREFIX,
    AccessError,
    change_tier,
    ensure_custom_fields,
    get_tier,
    quota_usage,
    tier_options,
)
from shared_modules.azure_aks import AKSError
from utilities.logger import ThreadLogger

logger = ThreadLogger(__name__)


def _resource_from_kwargs(kwargs):
    """The Resource the action dialog was opened on, from whichever slot CloudBolt used."""
    if kwargs.get("resource") is not None:
        return kwargs["resource"]
    return next((r for r in kwargs.get("resources") or [] if r is not None), None)


def generate_options_for_aks_ns_new_size_tier(field, **kwargs):
    """Tier dropdown; preselect the namespace's current tier when known."""
    resource = _resource_from_kwargs(kwargs)
    current = ""
    if resource is not None:
        current = resource.get_value_for_custom_field(CF_PREFIX + "size_tier") or ""
    return tier_options(initial=current or "small")


def _resolve_resource(job, resource, kwargs):
    """Accept the resource from any of the slots CloudBolt may use."""
    if resource is not None:
        return resource
    resource = _resource_from_kwargs(kwargs)
    if resource is None and job is not None:
        resource = job.resource_set.first()
    return resource


def _text(value):
    """A rendered input as plain text; an input left empty may render as 'None'."""
    text = str(value or "").strip()
    return "" if text == "None" else text


def _log_usage(resource, namespace):
    """Log every quota's used and hard values in the namespace. Best effort."""
    try:
        usage = quota_usage(resource)
    except Exception as exc:
        logger.warning("Could not read current quota usage for %s: %s", namespace, exc)
        return
    for quota_name, values in sorted(usage.items()):
        if values:
            text = ", ".join(f"{key}={used}/{hard}" for key, (hard, used) in sorted(values.items()))
            set_progress(f"Current usage in '{namespace}' ({quota_name}, used/hard): {text}.")


def run(job, resource=None, **kwargs):
    """Apply the new tier to the namespace and its CloudBolt environment."""
    resource = _resolve_resource(job, resource, kwargs)
    if resource is None:
        return "FAILURE", "", "No namespace resource was supplied."
    ensure_custom_fields()

    tier_key = _text("{{ aks_ns_new_size_tier }}").lower()
    reason = _text("""{{ aks_ns_change_reason }}""")

    namespace = resource.get_value_for_custom_field(CF_PREFIX + "name")
    if not namespace:
        return "FAILURE", "", f"Cannot change the quota: the resource has no '{CF_PREFIX}name' value."
    try:
        tier = get_tier(tier_key)
    except ValueError as exc:
        return "FAILURE", "", f"Cannot change the quota: {exc}"

    current_tier = resource.get_value_for_custom_field(CF_PREFIX + "size_tier") or "unknown"
    if current_tier == tier_key:
        msg = f"Namespace '{namespace}' is already on the {tier['label']} tier; nothing to change."
        set_progress(msg)
        return "WARNING", msg, ""

    if reason:
        set_progress(f"Reason for change: {reason}")
    _log_usage(resource, namespace)

    set_progress(
        f"Resizing namespace '{namespace}' from {current_tier} to {tier['label']}: "
        f"{tier['description']}..."
    )
    try:
        messages = change_tier(resource, tier_key)
    except (AccessError, AKSError) as exc:
        logger.exception("Quota update failed")
        return "FAILURE", "", f"Failed to update the quota on '{namespace}': {exc}"
    for message in messages:
        set_progress(message)

    # change_tier reports a storage/pod quota it could not update as a warning, not an error.
    status = "WARNING" if any(m.startswith("Warning") for m in messages) else "SUCCESS"
    msg = f"Namespace '{namespace}' resized to the {tier['label']} tier. {' '.join(messages)}"
    logger.info(msg)
    return status, msg, ""
