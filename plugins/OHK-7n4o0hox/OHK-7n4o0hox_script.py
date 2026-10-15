"""
CloudBolt day-2 resource action: Namespace Connection Details (AWS EKS Namespace).

Read-only. Tells a team member how to point kubectl at the namespace: the
update-kubeconfig and set-context commands for the namespace's cluster and
region, and which IAM roles and users currently hold an EKS access policy
covering the namespace. Nothing here is secret: an EKS kubeconfig carries
the cluster endpoint and CA and runs ``aws eks get-token`` with the caller's
own AWS credentials, which only work for a principal that has access.

Synchronous, so the answer appears in the action's result message and an
MCP client gets it back directly. The AWS EKS Cluster extension's Connect
tab on the namespace shows the same details with a ready-made kubeconfig.

Action Inputs: none.

Entry point: run(job, resource, **kwargs) -> (status, output_msg, error_msg)
"""

from shared_modules.eks_namespace import AccessEntryError, aws_message, connection_details
from utilities.logger import ThreadLogger

logger = ThreadLogger(__name__)

SCOPES = {"namespace": "", "pattern": " (namespace pattern)", "cluster": " (every namespace)"}


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


def run(job=None, resource=None, **kwargs):
    """Return the connection commands and current IAM access for the namespace."""
    resource = _resolve_resource(job, resource, kwargs)
    if resource is None:
        return "FAILURE", "", "No namespace resource was supplied."
    try:
        details = connection_details(resource)
    except AccessEntryError as exc:
        return "FAILURE", "", str(exc)
    except Exception as exc:
        logger.exception("Could not read connection details")
        return "FAILURE", "", f"Could not read the cluster: {aws_message(exc)}"

    parts = [
        f"Namespace '{details['namespace']}' on EKS cluster '{details['cluster_name']}' "
        f"({details['region']}). Connect with:",
    ]
    parts.extend(f"{number}. {command}" for number, command in enumerate(details["commands"], 1))
    if details["access_error"]:
        parts.append(details["access_error"])
    elif details["access"]:
        holders = "; ".join(
            f"{row['label']}: {row['principal']}{SCOPES[row['scope']]}" for row in details["access"]
        )
        parts.append(f"IAM access: {holders}.")
    else:
        parts.append("No IAM role or user has access yet; grant one with Manage Namespace Access.")
    parts.append("The Connect tab on this namespace has a ready-made kubeconfig.")
    return "SUCCESS", "\n".join(parts), ""
