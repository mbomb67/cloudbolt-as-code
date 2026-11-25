"""
CloudBolt day-2 resource action: Manage AKS Namespace Access (AKS Namespace).

Grants or revokes access to an AKS namespace landing zone on two planes:

  * CloudBolt entitlement -- add or remove a CloudBolt Group on the
    namespace's pinned Environment, so its members can (or can no longer)
    order container blueprints into this namespace. The owning group always
    keeps its entitlement.

  * Kubernetes access through Azure RBAC -- give a Microsoft Entra user,
    group, or service principal the Azure Kubernetes Service Namespace User
    role and an RBAC Writer, Reader, or Admin role, both scoped to the
    managed namespace; or remove the role assignments CloudBolt made for it
    there (assignments made outside CloudBolt are reported, never deleted).
    This is what makes ``kubectl`` and pipelines work for the team. Granting
    needs a cluster that uses Azure RBAC for Kubernetes authorization.
      https://learn.microsoft.com/en-us/azure/aks/managed-namespaces#assign-role
      https://learn.microsoft.com/en-us/azure/aks/manage-azure-rbac

Fill in either plane or both.

Action Inputs:
  - aks_ns_access_action (STR, required)     : grant | revoke
  - aks_ns_principal (STR, optional)         : Microsoft Entra object ID, optionally
                                               prefixed Group:, User:, or ServicePrincipal:
  - aks_ns_access_level (STR, required)      : edit | view | admin (grant only)
  - aks_ns_cloudbolt_group_id (INT, optional): CloudBolt Group to entitle or revoke

The Azure RBAC calls and their documentation are in shared_modules/SHM-cozv547n
(grant_namespace_access, revoke_namespace_access).

Entry point: run(job, resource, **kwargs) -> (status, output_msg, error_msg)
"""

from accounts.models import Group
from common.methods import set_progress
from shared_modules.aks_namespace import (
    CF_PREFIX,
    AccessError,
    access_context,
    access_level_options,
    ensure_custom_fields,
    entitle_group,
    entitled_group_names,
    get_access_level,
    grant_namespace_access,
    load_environment,
    parse_principals,
    refresh_access_attribute,
    require_azure_rbac,
    revoke_group,
    revoke_namespace_access,
)
from shared_modules.azure_aks import AKSError
from utilities.logger import ThreadLogger

logger = ThreadLogger(__name__)


def generate_options_for_aks_ns_access_action(field, **kwargs):
    return {"options": [("grant", "Grant access"), ("revoke", "Revoke access")],
            "initial_value": "grant", "sort": False}


def generate_options_for_aks_ns_access_level(field, **kwargs):
    return access_level_options(initial="edit")


def generate_options_for_aks_ns_cloudbolt_group_id(field, **kwargs):
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


def _text(value):
    """A rendered input as plain text; an input left empty may render as 'None'."""
    text = str(value or "").strip()
    return "" if text == "None" else text


def _refused(exc):
    """Azure's refusal, plus the permission it takes when it is an authorization failure.

    Creating role assignments needs Microsoft.Authorization/roleAssignments/write:
    https://learn.microsoft.com/en-us/azure/role-based-access-control/role-assignments-portal#prerequisites
    """
    message = f"Azure refused the access change: {exc}"
    if "AuthorizationFailed" in message:
        message += (
            " The Azure handler's service principal needs permission to create role assignments "
            "(User Access Administrator or Role Based Access Control Administrator) on the cluster."
        )
    return message


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
    """Grant or revoke CloudBolt entitlement and/or Azure RBAC access to the namespace."""
    resource = _resolve_resource(job, resource, kwargs)
    if resource is None:
        return "FAILURE", "", "No namespace resource was supplied."
    ensure_custom_fields()

    action = _text("{{ aks_ns_access_action }}").lower()
    principal_text = _text("{{ aks_ns_principal }}")
    level_key = _text("{{ aks_ns_access_level }}").lower() or "edit"
    group_id_str = _text("{{ aks_ns_cloudbolt_group_id }}")

    if action not in ("grant", "revoke"):
        return "FAILURE", "", "Action must be 'grant' or 'revoke'."
    if not group_id_str and not principal_text:
        return "FAILURE", "", "Give a Microsoft Entra principal, a CloudBolt group, or both."
    principal_id, principal_type = "", ""
    if principal_text:
        principals, errors = parse_principals(principal_text)
        if errors:
            return "FAILURE", "", " ".join(errors)
        if len(principals) != 1:
            return "FAILURE", "", "Give one Microsoft Entra principal per run."
        principal_id, principal_type = principals[0]
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

    # ---- Kubernetes access through Azure RBAC -----------------------------
    if principal_id:
        try:
            namespace, cluster, cluster_json = access_context(resource)
            if action == "grant":
                require_azure_rbac(cluster_json)
                message = grant_namespace_access(cluster, principal_id, namespace, level_key, principal_type)
            else:
                message = revoke_namespace_access(cluster, principal_id, namespace)
        except AccessError as exc:
            resource.save()
            return "FAILURE", " ".join(results), str(exc)
        except AKSError as exc:
            logger.exception("Role assignment change failed")
            resource.save()
            return "FAILURE", " ".join(results), _refused(exc)
        results.append(message)
        set_progress(message)
        refresh_access_attribute(resource, cluster, namespace)

    resource.save()
    msg = " ".join(results)
    logger.info(msg)
    return "SUCCESS", msg, ""
