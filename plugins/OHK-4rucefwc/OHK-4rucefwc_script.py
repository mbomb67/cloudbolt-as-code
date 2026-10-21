"""
CloudBolt day-2 resource action: Manage AKS Namespace Pipeline Identity (AKS Namespace).

Gives a GitHub Actions workflow kubectl access to the namespace without
stored credentials, or takes it away.

Add:
  1. creates a user-assigned managed identity in the cluster's resource
     group, tagged with this namespace (an identity of the same name that
     was not created for this namespace is refused, never changed);
  2. adds federated identity credentials that trust GitHub's OIDC issuer
     for the repository and exactly one branch or GitHub environment (both
     the owner/repo and the ID-based subject when GitHub reports the IDs);
  3. gives the identity Azure Kubernetes Service Namespace User and the
     chosen RBAC role on this managed namespace, and nothing else in Azure;
  4. records it on the namespace and returns a workflow snippet.
Running Add again for the same repository and trigger refreshes the
credentials and access level.
  https://learn.microsoft.com/en-us/entra/workload-id/workload-identity-federation-create-trust-user-assigned-managed-identity
  https://learn.microsoft.com/en-us/azure/developer/github/connect-from-azure-openid-connect
  https://docs.github.com/en/actions/reference/security/oidc

Azure federated credentials match one exact subject and take no wildcards,
so a branch or a GitHub environment is required.
  https://learn.microsoft.com/en-us/entra/workload-id/workload-identity-federation-considerations

Remove revokes the identity's namespace access and deletes the identity
(its federated credentials go with it). Teardown removes every pipeline
identity the same way.

Action Inputs:
  - aks_ns_pipeline_action (STR, required)     : add | remove
  - aks_ns_pipeline_repository (STR, required) : owner/repo, or owner@ID/repo@ID
  - aks_ns_pipeline_branch (STR, optional)     : only this branch may sign in
  - aks_ns_pipeline_environment (STR, optional): only jobs in this environment may
  - aks_ns_pipeline_access_level (STR)         : edit | view | admin (add only)

The Azure and GitHub calls and their documentation are in
shared_modules/SHM-cozv547n (add_pipeline_identity, remove_pipeline_identity).

Entry point: run(job, resource, **kwargs) -> (status, output_msg, error_msg)
"""

from common.methods import set_progress
from shared_modules.aks_namespace import (
    CF_PREFIX,
    AccessError,
    access_level_options,
    add_pipeline_identity,
    ensure_custom_fields,
    find_pipeline_identity,
    get_access_level,
    load_cluster_resource,
    parse_repository,
    pipeline_trigger,
    remove_pipeline_identity,
    workflow_snippet,
)
from shared_modules.azure_aks import AKSError, cluster_for_resource
from utilities.logger import ThreadLogger

logger = ThreadLogger(__name__)


def generate_options_for_aks_ns_pipeline_action(field, **kwargs):
    return {"options": [("add", "Add or update a pipeline identity"),
                        ("remove", "Remove a pipeline identity")],
            "initial_value": "add", "sort": False}


def generate_options_for_aks_ns_pipeline_access_level(field, **kwargs):
    return access_level_options(initial="edit")


def _refused(exc):
    """Azure's refusal, plus the permissions it takes when it is an authorization failure.

    Managed Identity Contributor covers userAssignedIdentities and their
    federatedIdentityCredentials (read, write, delete):
    https://learn.microsoft.com/en-us/azure/role-based-access-control/built-in-roles/identity#managed-identity-contributor
    """
    message = f"Azure refused the change: {exc}"
    if "AuthorizationFailed" in message:
        message += (
            " The Azure handler's service principal needs to create managed identities and federated "
            "credentials in the cluster's resource group (Managed Identity Contributor) and role "
            "assignments on the cluster (User Access Administrator or Role Based Access Control Administrator)."
        )
    return message


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


def _text(value):
    """A rendered input as plain text; an input left empty may render as 'None'."""
    text = str(value or "").strip()
    return "" if text == "None" else text


def _snippet(resource, identity):
    """The workflow snippet for the identity, or '' when the cluster cannot be resolved."""
    try:
        cluster = cluster_for_resource(load_cluster_resource(resource))
    except Exception as exc:
        logger.warning("No workflow snippet for %s: %s", resource, exc)
        return ""
    return workflow_snippet(
        identity,
        cluster.arm.tenant_id,
        cluster.arm.subscription_id,
        cluster.resource_group,
        cluster.name,
        resource.get_value_for_custom_field(CF_PREFIX + "name"),
    )


def run(job, resource=None, **kwargs):
    """Add or remove a GitHub Actions pipeline identity for the namespace."""
    resource = _resolve_resource(job, resource, kwargs)
    if resource is None:
        return "FAILURE", "", "No namespace resource was supplied."
    ensure_custom_fields()

    action = _text("{{ aks_ns_pipeline_action }}").lower()
    repository = _text("{{ aks_ns_pipeline_repository }}")
    branch = _text("{{ aks_ns_pipeline_branch }}")
    environment = _text("{{ aks_ns_pipeline_environment }}")
    level_key = _text("{{ aks_ns_pipeline_access_level }}").lower() or "edit"

    if action not in ("add", "remove"):
        return "FAILURE", "", "Action must be 'add' or 'remove'."
    try:
        parse_repository(repository)
        if branch and environment:
            raise ValueError("Give a branch or a GitHub environment, not both.")
        _, trigger_text = pipeline_trigger(branch, environment)
        if action == "add":
            get_access_level(level_key)
    except ValueError as exc:
        return "FAILURE", "", str(exc)

    if action == "remove":
        identity = find_pipeline_identity(resource, repository, branch, environment)
        if identity is None:
            return "FAILURE", "", f"No pipeline identity is recorded for {repository} ({trigger_text}) on this namespace."
        try:
            messages = remove_pipeline_identity(resource, identity)
        except AccessError as exc:
            return "FAILURE", "", str(exc)
        except AKSError as exc:
            logger.exception("Removing the pipeline identity failed")
            return "FAILURE", "", _refused(exc)
        for message in messages:
            set_progress(message)
        return "SUCCESS", " ".join(messages), ""

    set_progress(f"Setting up a pipeline identity for {repository} ({trigger_text})...")
    try:
        identity, messages = add_pipeline_identity(resource, repository, branch, environment, level_key)
    except (AccessError, ValueError) as exc:
        return "FAILURE", "", str(exc)
    except AKSError as exc:
        logger.exception("Adding the pipeline identity failed")
        return "FAILURE", "", _refused(exc)
    for message in messages:
        set_progress(message)

    output = (
        f"Managed identity {identity['identity_name']} (client ID {identity['client_id']}) can deploy to this "
        f"namespace from {identity['repository']} ({identity['trigger']})."
    )
    snippet = _snippet(resource, identity)
    if snippet:
        set_progress("GitHub Actions workflow:\n" + snippet)
        output += " Sign in with this GitHub Actions workflow (the Connect tab shows it too):\n" + snippet
    return "SUCCESS", output, ""
