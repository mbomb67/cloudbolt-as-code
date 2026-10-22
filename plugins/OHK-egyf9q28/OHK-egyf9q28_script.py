"""
CloudBolt day-2 resource action: GKE Namespace Connection Details (GKE Namespace).

Read-only. Tells a team member how to point kubectl at the namespace: the
gcloud container clusters get-credentials command for the namespace's
cluster, a kubeconfig for the namespace, its ingress and egress network
policy, which Google accounts, groups, and service accounts hold a
RoleBinding in it, and the GitHub Actions pipeline identities. Nothing here
is secret: the kubeconfig carries the API server and its CA and runs
gke-gcloud-auth-plugin with the caller's own gcloud sign-in, which only works
for a principal that has access.
  https://cloud.google.com/sdk/gcloud/reference/container/clusters/get-credentials
  https://cloud.google.com/kubernetes-engine/docs/how-to/cluster-access-for-kubectl

Synchronous, so the answer appears in the action's result message and an
MCP client gets it back directly. The Google GKE Cluster extension's Connect
tab on the namespace shows the same details.

Action Inputs: none.

Entry point: run(job, resource, **kwargs) -> (status, output_msg, error_msg)
"""

from shared_modules.gke_namespace import AccessError, connection_details, ensure_custom_fields, render_access
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


def run(job=None, resource=None, **kwargs):
    """Return the connection commands, kubeconfig, network policy, and current access for the namespace."""
    resource = _resolve_resource(job, resource, kwargs)
    if resource is None:
        return "FAILURE", "", "No namespace resource was supplied."
    ensure_custom_fields()
    try:
        details = connection_details(resource)
    except AccessError as exc:
        return "FAILURE", "", str(exc)
    except Exception as exc:
        logger.exception("Could not read connection details")
        return "FAILURE", "", f"Could not read the cluster: {exc}"

    parts = [
        f"Namespace '{details['namespace']}' on GKE cluster '{details['cluster_name']}' "
        f"({details['location']}, project {details['project']}). Connect with:",
    ]
    parts.extend(f"{number}. {command}" for number, command in enumerate(details["commands"], 1))
    parts.append("get-credentials needs container.clusters.get on the project (Kubernetes Engine Cluster "
                 "Viewer); without it, use the kubeconfig below.")
    if details["network_policy"]:
        parts.append(f"Network policy: {details['network_policy']}.")
    parts.append(f"Authentication: {details['auth_mode']}.")
    if details["google_groups"]:
        parts.append(f"Google Groups for RBAC: Google Groups that are members of {details['google_groups']} "
                     "can be granted access.")
    if details["access_error"]:
        parts.append(details["access_error"])
    elif details["access"]:
        parts.append("Kubernetes RBAC access:\n" + render_access(details["access"]))
    else:
        parts.append("No Google principal has access yet; grant one with Manage GKE Namespace Access.")
    for item in details["pipelines"]:
        parts.append(
            f"Pipeline identity: {item.get('repository')} ({item.get('trigger')}) deploys as "
            f"{item.get('principal')} through Workload Identity Federation."
        )
    if details["environment_name"]:
        parts.append(f"CloudBolt environment for container blueprints: {details['environment_name']}.")
    parts.append(f"Google Cloud console: {details['console_url']}")
    if details["kubeconfig"]:
        parts.append("Kubeconfig (holds no secret; gke-gcloud-auth-plugin signs you in):\n"
                     + details["kubeconfig"].rstrip())
    parts.append("The Connect tab on this namespace shows the same details.")
    return "SUCCESS", "\n".join(parts), ""
