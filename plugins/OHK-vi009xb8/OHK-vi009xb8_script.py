"""
CloudBolt day-2 resource action: AKS Namespace Connection Details (AKS Namespace).

Read-only. Tells a team member how to point kubectl at the namespace: the
az aks namespace get-credentials and kubelogin commands for the namespace's
cluster, the namespace's kubeconfig, its ingress and egress network policy,
which Microsoft Entra users, groups, and service principals currently hold
an AKS RBAC role covering it, and the GitHub Actions pipeline identities.
Nothing here is secret: the kubeconfig is shown only when it holds no
credential (a Microsoft Entra ID cluster's kubeconfig carries the API server
and its CA and runs kubelogin with the caller's own sign-in, which only
works for a principal that has access).
  https://learn.microsoft.com/en-us/cli/azure/aks/namespace#az-aks-namespace-get-credentials
  https://learn.microsoft.com/en-us/rest/api/aks/managed-namespaces/list-credential
  https://learn.microsoft.com/en-us/azure/aks/kubelogin-authentication

Synchronous, so the answer appears in the action's result message and an
MCP client gets it back directly. The Azure AKS Cluster extension's Connect
tab on the namespace shows the same details.

Action Inputs: none.

Entry point: run(job, resource, **kwargs) -> (status, output_msg, error_msg)
"""

from shared_modules.aks_namespace import AccessError, connection_details, ensure_custom_fields, render_access
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
        f"Namespace '{details['namespace']}' on AKS cluster '{details['cluster_name']}' "
        f"({details['location']}, resource group {details['resource_group']}). Connect with:",
    ]
    parts.extend(f"{number}. {command}" for number, command in enumerate(details["commands"], 1))
    parts.append(f"Network policy: {details['network_policy']}.")
    parts.append(f"Authentication: {details['auth_mode']}.")
    if details["access_error"]:
        parts.append(details["access_error"])
    elif details["access"]:
        parts.append("Microsoft Entra access:\n" + render_access(details["access"]))
    else:
        parts.append("No Microsoft Entra principal has access yet; grant one with Manage AKS Namespace Access.")
    for item in details["pipelines"]:
        parts.append(
            f"Pipeline identity: {item['repository']} ({item['trigger']}) signs in as managed identity "
            f"{item['identity_name']} (client ID {item.get('client_id')}) with azure/login."
        )
    if details["environment_name"]:
        parts.append(f"CloudBolt environment for container blueprints: {details['environment_name']}.")
    parts.append(f"Azure portal: {details['portal_url']}")
    if details["kubeconfig"]:
        parts.append("Kubeconfig (holds no secret; kubelogin signs you in):\n" + details["kubeconfig"].rstrip())
    else:
        parts.append("The get-credentials command above writes the kubeconfig.")
    parts.append("The Connect tab on this namespace shows the same details.")
    return "SUCCESS", "\n".join(parts), ""
