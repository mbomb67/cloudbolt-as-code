"""
CloudBolt day-2 resource action: Request Namespace Quota Change (AWS EKS Namespace).

Moves an EKS namespace landing zone to a different size tier. The resource
action that wraps this plugin requires approval, so the request waits in
CloudBolt's approval queue until an approver for the group accepts it; only
then does this code run and:
  1. replace the namespace ResourceQuota with the new tier's limits,
  2. relabel the namespace with the new tier,
  3. mirror the new limits into the pinned CloudBolt Environment quota, and
  4. record the new tier on the Resource.

Action Inputs:
  - eks_ns_new_size_tier (STR, required): small | medium | large | xlarge
  - eks_ns_change_reason (TXT, optional): justification, echoed into the job log

Kubernetes accepts a quota below current usage: existing pods keep running
and new ones are refused until usage drops. The job log shows the usage at
the time of the change so approvers and the team can see what a downsize
will block.
https://kubernetes.io/docs/concepts/policy/resource-quotas/

Entry point: run(job, resource, **kwargs) -> (status, output_msg, error_msg)
"""

from common.methods import set_progress
from shared_modules.eks_namespace import (
    CF_PREFIX,
    LABEL_TIER,
    NamespaceClient,
    NamespaceError,
    ensure_custom_fields,
    get_tier,
    load_cluster,
    load_environment,
    quota_hard_for_tier,
    set_environment_quota,
    store_tier_on_resource,
    tier_options,
)
from utilities.logger import ThreadLogger

logger = ThreadLogger(__name__)


def _resource_from_kwargs(kwargs):
    """The Resource the action dialog was opened on, from whichever slot CloudBolt used."""
    if kwargs.get("resource") is not None:
        return kwargs["resource"]
    return next((r for r in kwargs.get("resources") or [] if r is not None), None)


def generate_options_for_eks_ns_new_size_tier(field, **kwargs):
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


def run(job, resource=None, **kwargs):
    """Apply the new tier to the namespace and its CloudBolt environment."""
    resource = _resolve_resource(job, resource, kwargs)
    ensure_custom_fields()

    tier_key = "{{ eks_ns_new_size_tier }}".strip().lower()
    reason = """{{ eks_ns_change_reason }}""".strip()

    try:
        namespace, orchestrator, cluster_name = load_cluster(resource)
        tier = get_tier(tier_key)
    except ValueError as exc:
        return "FAILURE", "", f"Cannot change the quota: {exc}."

    current_tier = resource.get_value_for_custom_field(CF_PREFIX + "size_tier") or "unknown"
    if current_tier == tier_key:
        msg = f"Namespace '{namespace}' is already on the {tier['label']} tier; nothing to change."
        set_progress(msg)
        return "WARNING", msg, ""

    if reason:
        set_progress(f"Reason for change: {reason}")

    try:
        client = NamespaceClient.from_orchestrator(orchestrator)
    except Exception as exc:
        logger.exception("Could not connect to the cluster API")
        return "FAILURE", "", f"Could not connect to the API of cluster '{cluster_name}': {exc}"

    # ResourceQuota status.used reports current consumption per hard limit.
    # https://kubernetes.io/docs/reference/kubernetes-api/policy-resources/resource-quota-v1/
    try:
        quota = client.get_resource_quota(namespace) or {}
        used = (quota.get("status") or {}).get("used") or {}
        if used:
            usage = ", ".join(f"{key}={value}" for key, value in sorted(used.items()))
            set_progress(f"Current usage in '{namespace}': {usage}.")
    except NamespaceError as exc:
        logger.warning("Could not read current quota usage for %s: %s", namespace, exc)

    set_progress(
        f"Resizing namespace '{namespace}' from {current_tier} to {tier['label']}: "
        f"{tier['description']}..."
    )
    try:
        client.apply_resource_quota(namespace, quota_hard_for_tier(tier))
        client.patch_namespace_metadata(namespace, labels={LABEL_TIER: tier_key})
    except NamespaceError as exc:
        logger.exception("Quota update failed")
        return "FAILURE", "", f"Failed to update the quota on '{namespace}': {exc}"

    env = load_environment(resource)
    env_note = ""
    if env is not None:
        if set_environment_quota(env, tier):
            env_note = f" CloudBolt environment '{env.name}' quota updated to match."
    else:
        env_note = " No CloudBolt environment is recorded for this namespace, so only the cluster quota changed."

    store_tier_on_resource(resource, tier_key, tier)
    resource.save()

    msg = f"Namespace '{namespace}' resized to the {tier['label']} tier.{env_note}"
    logger.info(msg)
    return "SUCCESS", msg, ""
