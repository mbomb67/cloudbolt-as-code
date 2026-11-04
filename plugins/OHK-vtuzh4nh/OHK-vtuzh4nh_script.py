"""
CloudBolt day-2 resource action: Extend GKE Namespace Expiration (GKE Namespace).

Pushes out the expiration of a GKE namespace landing zone. The new date is
written to the Resource's standard ``expiration_date`` parameter, so
CloudBolt's own expiration handling applies, and mirrored to the namespace's
``cloudbolt.io/lease-expires`` annotation, where cluster admins can see it
with ``kubectl describe namespace``.
  https://kubernetes.io/docs/concepts/overview/working-with-objects/annotations/

Action Inputs:
  - gke_ns_extend_days (INT, required): days to add, counted from the
    current expiration, or from today when it has passed or was never set.

Entry point: run(job, resource, **kwargs) -> (status, output_msg, error_msg)
"""

import datetime

from common.methods import set_progress
from shared_modules.gke_namespace import (
    CF_PREFIX,
    NamespaceError,
    ensure_custom_fields,
    resource_expiration,
    set_expiration_annotation,
    set_resource_expiration,
)
from utilities.logger import ThreadLogger

logger = ThreadLogger(__name__)


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
    """Extend the namespace expiration."""
    resource = _resolve_resource(job, resource, kwargs)
    if resource is None:
        return "FAILURE", "", "No namespace resource was supplied."
    ensure_custom_fields()

    days_str = "{{ gke_ns_extend_days }}".strip()
    try:
        days = int(days_str)
    except ValueError:
        return "FAILURE", "", f"Days to Add must be a whole number of days, got '{days_str}'."
    if days <= 0:
        return "FAILURE", "", "Days to Add must be greater than zero."

    namespace = resource.get_value_for_custom_field(CF_PREFIX + "name")
    if not namespace:
        return "FAILURE", "", f"Cannot extend the expiration: the resource has no '{CF_PREFIX}name' value."

    today = datetime.date.today()
    current = resource_expiration(resource)
    base = current if (current and current > today) else today
    new_expiry = base + datetime.timedelta(days=days)

    set_progress(
        f"Extending expiration of '{namespace}' from "
        f"{current.isoformat() if current else 'none'} to {new_expiry.isoformat()} (+{days} days)..."
    )

    try:
        set_expiration_annotation(resource, new_expiry)
    except (ValueError, NamespaceError) as exc:
        logger.exception("Namespace annotation update failed")
        return "FAILURE", "", f"Failed to record the new expiration on '{namespace}': {exc}"

    set_resource_expiration(resource, new_expiry)
    resource.save()

    msg = f"Namespace '{namespace}' now expires {new_expiry.isoformat()}."
    logger.info(msg)
    return "SUCCESS", msg, ""
