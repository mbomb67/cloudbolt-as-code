"""
CloudBolt day-2 resource action: Extend Namespace Expiration (AWS EKS Namespace).

Pushes out the expiration of an EKS namespace landing zone. The new date is
written to the Resource's standard ``expiration_date`` parameter, so
CloudBolt's own expiration handling applies, and mirrored to the
``cloudbolt.io/lease-expires`` annotation on the namespace, where cluster
admins can see it with ``kubectl describe namespace``.

Action Inputs:
  - eks_ns_extend_days (INT, required): days to add, counted from the
    current expiration, or from today when it has passed or was never set.

Entry point: run(job, resource, **kwargs) -> (status, output_msg, error_msg)
"""

import datetime

from common.methods import set_progress
from shared_modules.eks_namespace import (
    ANNOTATION_EXPIRES,
    NamespaceClient,
    NamespaceError,
    ensure_custom_fields,
    load_cluster,
    resource_expiration,
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
    ensure_custom_fields()

    days_str = "{{ eks_ns_extend_days }}".strip()
    try:
        days = int(days_str)
    except ValueError:
        return "FAILURE", "", f"Extend By must be a whole number of days, got '{days_str}'."
    if days <= 0:
        return "FAILURE", "", "Extend By must be greater than zero."

    try:
        namespace, orchestrator, cluster_name = load_cluster(resource)
    except ValueError as exc:
        return "FAILURE", "", f"Cannot extend the expiration: {exc}."

    today = datetime.date.today()
    current = resource_expiration(resource)
    base = current if (current and current > today) else today
    new_expiry = base + datetime.timedelta(days=days)

    set_progress(
        f"Extending expiration of '{namespace}' from "
        f"{current.isoformat() if current else 'none'} to {new_expiry.isoformat()} (+{days} days)..."
    )

    try:
        client = NamespaceClient.from_orchestrator(orchestrator)
        client.patch_namespace_metadata(
            namespace, annotations={ANNOTATION_EXPIRES: new_expiry.isoformat()}
        )
    except NamespaceError as exc:
        logger.exception("Namespace annotation update failed")
        return "FAILURE", "", f"Failed to record the new expiration on '{namespace}': {exc}"
    except Exception as exc:
        logger.exception("Could not connect to the cluster API")
        return "FAILURE", "", f"Could not connect to the API of cluster '{cluster_name}': {exc}"

    set_resource_expiration(resource, new_expiry)
    resource.save()

    msg = f"Namespace '{namespace}' now expires {new_expiry.isoformat()}."
    logger.info(msg)
    return "SUCCESS", msg, ""
