"""
CloudBolt day-2 resource action: Manage GKE Namespace Pipeline Identity (GKE Namespace).

Gives a GitHub Actions workflow kubectl access to the namespace without a
stored key, or takes it away.

Add sets up Workload Identity Federation for the repository and exactly one
branch or GitHub environment in the cluster's Google Cloud project, creates
an IAM service account the workflow impersonates, gives that service account
a RoleBinding in this namespace to the chosen built-in ClusterRole, records
it on the namespace, and returns a workflow snippet. Running Add again for the same repository and trigger
refreshes the trust and the access level.
  https://cloud.google.com/iam/docs/workload-identity-federation-with-deployment-pipelines
  https://github.com/google-github-actions/auth
  https://docs.github.com/en/actions/reference/security/oidc

The identity trusts one exact subject, so a branch or a GitHub environment is
required.

Remove revokes the namespace access and deletes what Add created. Teardown
removes every pipeline identity the same way.

Action Inputs:
  - gke_ns_pipeline_action (STR, required)     : add | remove
  - gke_ns_pipeline_repository (STR, required) : owner/repo, or owner@ID/repo@ID
  - gke_ns_pipeline_branch (STR, optional)     : only this branch gets access
  - gke_ns_pipeline_environment (STR, optional): only jobs in this environment do
  - gke_ns_pipeline_access_level (STR)         : edit | view | admin (add only)

The Google Cloud, Kubernetes, and GitHub calls and their documentation are in
shared_modules/SHM-kephq8iq (add_pipeline_identity, remove_pipeline_identity).

Entry point: run(job, resource, **kwargs) -> (status, output_msg, error_msg)
"""

from common.methods import set_progress
from shared_modules.gcp_gke import GKEError
from shared_modules.gke_namespace import (
    CF_PREFIX,
    AccessError,
    NamespaceError,
    access_level_options,
    add_pipeline_identity,
    ensure_custom_fields,
    find_pipeline_identity,
    get_access_level,
    load_cluster_resource,
    parse_repository,
    remove_pipeline_identity,
    workflow_snippet,
)
from utilities.logger import ThreadLogger

logger = ThreadLogger(__name__)


def generate_options_for_gke_ns_pipeline_action(field, **kwargs):
    return {"options": [("add", "Add or update a pipeline identity"),
                        ("remove", "Remove a pipeline identity")],
            "initial_value": "add", "sort": False}


def generate_options_for_gke_ns_pipeline_access_level(field, **kwargs):
    return access_level_options(initial="edit")


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


def _trigger_text(branch, environment):
    """'branch main' or 'environment production'; one of the two is required."""
    if branch and environment:
        raise ValueError("Give a branch or a GitHub environment, not both.")
    if environment:
        return f"environment {environment}"
    if branch:
        return f"branch {branch}"
    raise ValueError("Give a branch or a GitHub environment: the pipeline identity trusts one exact branch or "
                     "environment, never a whole repository.")


def _snippet(resource, identity):
    """The workflow snippet for the identity, or '' when the cluster is not recorded."""
    cluster = load_cluster_resource(resource)
    if cluster is None:
        return ""
    values = cluster.get_cf_values_as_dict()
    try:
        return workflow_snippet(
            identity,
            values.get("gke_project_id", ""),
            values.get("gke_location", ""),
            values.get("gke_cluster_name") or cluster.name,
            resource.get_value_for_custom_field(CF_PREFIX + "name"),
        )
    except Exception as exc:
        logger.warning("No workflow snippet for %s: %s", resource, exc)
        return ""


def run(job, resource=None, **kwargs):
    """Add or remove a GitHub Actions pipeline identity for the namespace."""
    resource = _resolve_resource(job, resource, kwargs)
    if resource is None:
        return "FAILURE", "", "No namespace resource was supplied."
    ensure_custom_fields()

    action = _text("{{ gke_ns_pipeline_action }}").lower()
    repository = _text("{{ gke_ns_pipeline_repository }}")
    branch = _text("{{ gke_ns_pipeline_branch }}")
    environment = _text("{{ gke_ns_pipeline_environment }}")
    level_key = _text("{{ gke_ns_pipeline_access_level }}").lower() or "edit"

    if action not in ("add", "remove"):
        return "FAILURE", "", "Action must be 'add' or 'remove'."
    try:
        parse_repository(repository)
        trigger_text = _trigger_text(branch, environment)
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
        except (AccessError, NamespaceError, GKEError, ValueError) as exc:
            logger.exception("Removing the pipeline identity failed")
            return "FAILURE", "", f"The pipeline identity could not be removed: {exc}"
        for message in messages:
            set_progress(message)
        return "SUCCESS", " ".join(messages), ""

    set_progress(f"Setting up a pipeline identity for {repository} ({trigger_text})...")
    try:
        identity, messages = add_pipeline_identity(resource, repository, branch, environment, level_key)
    except (AccessError, NamespaceError, GKEError, ValueError) as exc:
        logger.exception("Adding the pipeline identity failed")
        return "FAILURE", "", f"The pipeline identity could not be set up: {exc}"
    for message in messages:
        set_progress(message)

    output = (
        f"GitHub Actions workflows of {identity.get('repository')} ({identity.get('trigger')}) can deploy to this "
        f"namespace as {identity.get('principal')}."
    )
    snippet = _snippet(resource, identity)
    if snippet:
        set_progress("GitHub Actions workflow:\n" + snippet)
        output += " Use this GitHub Actions workflow (the Connect tab shows it too):\n" + snippet
    return "SUCCESS", output, ""
