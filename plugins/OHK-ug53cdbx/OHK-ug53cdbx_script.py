"""
CloudBolt Day-2 resource action: Request Quota Change.

Moves an OpenShift landing-zone project to a different size tier. The
resource action that wraps this plugin requires approval, so the request sits
in CloudBolt's approval queue until an approver in the group accepts it; only
then does this code run and:
  1. replace the namespace ResourceQuota with the new tier's limits,
  2. relabel the namespace with the new tier,
  3. mirror the new limits into the pinned CloudBolt Environment quota, and
  4. record the new tier on the Resource.

Action Inputs:
  - new_size_tier (STR, required): small | medium | large | xlarge
  - change_reason (TXT, optional): justification, echoed into the job log

Kubernetes accepts a quota below current usage (new workloads are simply
blocked until usage drops), so downsizing never fails on the cluster side;
CloudBolt's environment quota refuses to drop below what is already in use and
reports a warning instead.

Entry point: run(job, resource, **kwargs) -> (status, output_msg, error_msg)
"""

from common.methods import set_progress
from shared_modules.openshift_landing_zone import (
    CF_PREFIX,
    LABEL_TIER,
    LandingZoneError,
    OpenShiftLandingZoneClient,
    ensure_custom_fields,
    get_tier,
    load_environment,
    load_handler,
    quota_hard_for_tier,
    set_environment_quota,
    store_tier_on_resource,
    tier_options,
)
from utilities.logger import ThreadLogger

logger = ThreadLogger(__name__)


def generate_options_for_new_size_tier(field, **kwargs):
    """Tier dropdown; preselect the resource's current tier when known."""
    resource = kwargs.get("resource") or next(iter(kwargs.get("resources") or []), None)
    current = ""
    if resource is not None:
        current = resource.get_value_for_custom_field(CF_PREFIX + "size_tier") or ""
    return tier_options(initial=current or "small")


def _resolve_resource(job, resource, kwargs):
    """Accept the resource from any of the slots CloudBolt may use."""
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


def run(job, resource=None, **kwargs):
    """Apply the new tier to the project and its CloudBolt environment."""
    resource = _resolve_resource(job, resource, kwargs)
    ensure_custom_fields()

    tier_key = "{{ new_size_tier }}".strip().lower()
    reason = """{{ change_reason }}""".strip()

    try:
        namespace, rh = load_handler(resource)
        tier = get_tier(tier_key)
    except ValueError as exc:
        return "FAILURE", "", f"Cannot change quota: {exc}."

    current_tier = resource.get_value_for_custom_field(CF_PREFIX + "size_tier") or "unknown"
    if current_tier == tier_key:
        msg = f"Project '{namespace}' is already on the {tier['label']} tier; nothing to change."
        set_progress(msg)
        return "WARNING", msg, ""

    if reason:
        set_progress(f"Reason for change: {reason}")

    try:
        client = OpenShiftLandingZoneClient.from_handler(rh)
    except Exception as exc:
        logger.exception("Could not connect to OpenShift API")
        return "FAILURE", "", f"Could not connect to the OpenShift API for '{rh.name}': {exc}"

    set_progress(
        f"Resizing project '{namespace}' from {current_tier} to {tier['label']}: {tier['description']}..."
    )
    try:
        client.apply_resource_quota(namespace, quota_hard_for_tier(tier))
        client.patch_namespace_metadata(namespace, labels={LABEL_TIER: tier_key})
    except LandingZoneError as exc:
        logger.exception("Quota update failed")
        return "FAILURE", "", f"Failed to update the quota on '{namespace}': {exc}"

    env = load_environment(resource)
    env_note = ""
    if env is not None:
        if set_environment_quota(env, tier):
            env_note = f" CloudBolt environment '{env.name}' quota updated to match."
    else:
        env_note = " No CloudBolt environment is recorded for this project, so only the cluster quota changed."

    store_tier_on_resource(resource, tier_key, tier)
    resource.save()

    msg = f"Project '{namespace}' resized to the {tier['label']} tier.{env_note}"
    logger.info(msg)
    return "SUCCESS", msg, ""
