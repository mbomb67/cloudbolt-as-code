"""
CloudBolt day-2 resource action: Manage Namespace Access (AWS EKS Namespace).

Grants or revokes access to an EKS namespace landing zone on two planes:

  * CloudBolt entitlement -- add or remove a CloudBolt Group on the
    namespace's pinned Environment, so its members can (or can no longer)
    order container blueprints into this namespace. The owning group always
    keeps its entitlement.

  * Kubernetes access through IAM -- give an IAM role or user an EKS access
    policy (Edit, View, or Admin) scoped to this namespace, creating the
    principal's access entry on the cluster when it has none; or take the
    namespace back out of the principal's access policies, deleting an
    access entry CloudBolt created once it grants nothing else. This is what
    makes ``kubectl`` and pipelines work for the team.

Fill in either plane or both.

Action Inputs:
  - eks_ns_access_action (STR, required)     : grant | revoke
  - eks_ns_iam_principal_arn (STR, optional) : IAM role or user ARN
  - eks_ns_access_level (STR, required)      : edit | view | admin (grant only)
  - eks_ns_cloudbolt_group_id (INT, optional): CloudBolt Group to entitle or revoke

The EKS calls and their documentation are in shared_modules/SHM-sa5yqm9j
(grant_namespace_access, revoke_namespace_access).

Entry point: run(job, resource, **kwargs) -> (status, output_msg, error_msg)
"""

from accounts.models import Group
from common.methods import set_progress
from shared_modules.eks_namespace import (
    CF_PREFIX,
    AccessEntryError,
    access_context,
    access_level_options,
    aws_message,
    ensure_custom_fields,
    entitle_group,
    entitled_group_names,
    get_access_level,
    grant_namespace_access,
    load_environment,
    refresh_access_attribute,
    revoke_group,
    revoke_namespace_access,
    validate_principal_arn,
)
from utilities.logger import ThreadLogger

logger = ThreadLogger(__name__)


def generate_options_for_eks_ns_access_action(field, **kwargs):
    return {"options": [("grant", "Grant access"), ("revoke", "Revoke access")],
            "initial_value": "grant", "sort": False}


def generate_options_for_eks_ns_access_level(field, **kwargs):
    return access_level_options(initial="edit")


def generate_options_for_eks_ns_cloudbolt_group_id(field, **kwargs):
    groups = Group.objects.exclude(name__startswith="Unassigned").order_by("name")
    options = [("", "------ No CloudBolt group change ------")]
    options.extend((g.id, g.name) for g in groups)
    return {"options": options, "sort": False}


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


def _change_group(resource, action, group_id_str):
    """CloudBolt entitlement plane. Returns (message, error)."""
    try:
        group = Group.objects.filter(id=int(group_id_str)).first()
    except ValueError:
        group = None
    if group is None:
        return "", f"CloudBolt group id={group_id_str} not found."
    env = load_environment(resource)
    if env is None:
        return "", "No CloudBolt environment is recorded for this namespace; cannot change group entitlement."
    if action == "grant":
        changed = entitle_group(env, group)
        message = (
            f"CloudBolt group '{group.name}' "
            f"{'entitled to' if changed else 'was already entitled to'} environment '{env.name}'."
        )
    else:
        if getattr(resource, "group_id", None) == group.id:
            return "", (
                f"'{group.name}' owns this namespace and keeps its entitlement; "
                "transfer the resource to another group first."
            )
        changed = revoke_group(env, group)
        message = (
            f"CloudBolt group '{group.name}' "
            f"{'removed from' if changed else 'was not entitled to'} environment '{env.name}'."
        )
    resource.set_value_for_custom_field(
        CF_PREFIX + "entitled_groups", "\n".join(entitled_group_names(env))
    )
    return message, ""


def run(job, resource=None, **kwargs):
    """Grant or revoke CloudBolt entitlement and/or IAM access to the namespace."""
    resource = _resolve_resource(job, resource, kwargs)
    ensure_custom_fields()

    action = "{{ eks_ns_access_action }}".strip().lower()
    principal = "{{ eks_ns_iam_principal_arn }}".strip()
    level_key = "{{ eks_ns_access_level }}".strip().lower() or "edit"
    group_id_str = "{{ eks_ns_cloudbolt_group_id }}".strip()

    if action not in ("grant", "revoke"):
        return "FAILURE", "", "Action must be 'grant' or 'revoke'."
    if not group_id_str and not principal:
        return "FAILURE", "", "Give an IAM principal ARN, a CloudBolt group, or both."
    if principal:
        error = validate_principal_arn(principal)
        if error:
            return "FAILURE", "", error
    if action == "grant":
        try:
            get_access_level(level_key)
        except ValueError as exc:
            return "FAILURE", "", str(exc)

    results = []

    # ---- CloudBolt entitlement plane ------------------------------------
    if group_id_str:
        message, error = _change_group(resource, action, group_id_str)
        if error:
            return "FAILURE", "", error
        results.append(message)
        set_progress(message)

    # ---- Kubernetes access through EKS access entries -------------------
    if principal:
        try:
            namespace, eks, cluster_info = access_context(resource)
            if action == "grant":
                message = grant_namespace_access(eks, cluster_info, principal, namespace, level_key)
            else:
                message = revoke_namespace_access(eks, cluster_info, principal, namespace)
        except AccessEntryError as exc:
            resource.save()
            return "FAILURE", " ".join(results), str(exc)
        except Exception as exc:
            logger.exception("Access entry change failed")
            resource.save()
            return "FAILURE", " ".join(results), f"EKS refused the access change: {aws_message(exc)}"
        results.append(message)
        set_progress(message)
        refresh_access_attribute(resource, eks, cluster_info, namespace)

    resource.save()
    msg = " ".join(results)
    logger.info(msg)
    return "SUCCESS", msg, ""
