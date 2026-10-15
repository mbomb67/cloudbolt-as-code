"""
CloudBolt day-2 resource action: Manage Pipeline Identity (AWS EKS Namespace).

Gives a GitHub Actions workflow kubectl access to the namespace without
stored credentials, or takes it away.

Add:
  1. finds the account's GitHub Actions OIDC provider, creating it when the
     account has none (never changing or deleting an existing one);
  2. creates an IAM role, owned by CloudBolt and tagged with this namespace,
     that only workflows from the repository (and optional branch or
     environment) can assume, with eks:DescribeCluster on this cluster and
     nothing else in AWS;
  3. grants the role the chosen access level on this namespace through an
     EKS access entry; and
  4. records it on the namespace and returns a workflow snippet.
Running Add again for the same repository and trigger refreshes the trust
policy and access level.

Remove revokes the role's namespace access, deletes its access entry, and
deletes the role. Teardown removes every pipeline identity the same way.

Action Inputs:
  - eks_ns_pipeline_action (STR, required)     : add | remove
  - eks_ns_pipeline_repository (STR, required) : owner/repo, or owner@ID/repo@ID
  - eks_ns_pipeline_branch (STR, optional)     : only this branch may assume the role
  - eks_ns_pipeline_environment (STR, optional): only jobs in this environment may
  - eks_ns_pipeline_access_level (STR)         : edit | view | admin (add only)
With neither a branch nor an environment, any workflow in the repository
may assume the role.

The IAM, EKS and GitHub calls and their documentation are in
shared_modules/SHM-sa5yqm9j (add_pipeline_identity, remove_pipeline_identity).

Entry point: run(job, resource, **kwargs) -> (status, output_msg, error_msg)
"""

from common.methods import set_progress
from shared_modules.eks_namespace import (
    AccessEntryError,
    access_level_options,
    add_pipeline_identity,
    aws_message,
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


def generate_options_for_eks_ns_pipeline_action(field, **kwargs):
    return {"options": [("add", "Add or update a pipeline identity"),
                        ("remove", "Remove a pipeline identity")],
            "initial_value": "add", "sort": False}


def generate_options_for_eks_ns_pipeline_access_level(field, **kwargs):
    return access_level_options(initial="edit")


def _refused(exc):
    """AWS's refusal, plus where to find the permissions when it is an access denial."""
    message = f"AWS refused the change: {aws_message(exc)}"
    if "AccessDenied" in message:
        message += (
            " The AWS handler's credentials need the IAM policy in the AWS EKS Namespace "
            "blueprint README (Pipeline identities)."
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


def run(job, resource=None, **kwargs):
    """Add or remove a GitHub Actions pipeline identity for the namespace."""
    resource = _resolve_resource(job, resource, kwargs)
    ensure_custom_fields()

    action = "{{ eks_ns_pipeline_action }}".strip().lower()
    repository = "{{ eks_ns_pipeline_repository }}".strip()
    branch = "{{ eks_ns_pipeline_branch }}".strip()
    environment = "{{ eks_ns_pipeline_environment }}".strip()
    level_key = "{{ eks_ns_pipeline_access_level }}".strip().lower() or "edit"

    if action not in ("add", "remove"):
        return "FAILURE", "", "Action must be 'add' or 'remove'."
    try:
        parse_repository(repository)
        if action == "add":
            get_access_level(level_key)
    except ValueError as exc:
        return "FAILURE", "", str(exc)

    if action == "remove":
        identity = find_pipeline_identity(resource, repository, branch, environment)
        if identity is None:
            trigger = f"branch {branch}" if branch else (f"environment {environment}" if environment else "any trigger")
            return "FAILURE", "", f"No pipeline identity is recorded for {repository} ({trigger}) on this namespace."
        try:
            messages = remove_pipeline_identity(resource, identity)
        except AccessEntryError as exc:
            return "FAILURE", "", str(exc)
        except Exception as exc:
            logger.exception("Removing the pipeline identity failed")
            return "FAILURE", "", _refused(exc)
        for message in messages:
            set_progress(message)
        return "SUCCESS", " ".join(messages), ""

    set_progress(f"Setting up a pipeline identity for {repository}...")
    try:
        identity, messages = add_pipeline_identity(resource, repository, branch, environment, level_key)
    except (AccessEntryError, ValueError) as exc:
        return "FAILURE", "", str(exc)
    except Exception as exc:
        logger.exception("Adding the pipeline identity failed")
        return "FAILURE", "", _refused(exc)
    for message in messages:
        set_progress(message)

    cluster = load_cluster_resource(resource)
    values = cluster.get_cf_values_as_dict() if cluster is not None else {}
    snippet = workflow_snippet(
        identity["role_arn"],
        values.get("aws_region") or "<region>",
        values.get("eks_cluster_name") or getattr(cluster, "name", "<cluster>"),
        resource.get_value_for_custom_field("eks_namespace_name"),
        identity.get("environment", ""),
    )
    set_progress("GitHub Actions workflow:\n" + snippet)
    return (
        "SUCCESS",
        f"Role {identity['role_arn']} can deploy to this namespace from {identity['repository']} "
        f"({identity['trigger']}). The job log and the Connect tab show the workflow snippet.",
        "",
    )
