"""
CloudBolt day-2 resource action: Manage GKE Namespace Access (GKE Namespace).

Grants or revokes access to a GKE namespace landing zone on two planes:

  * CloudBolt entitlement -- add or remove a CloudBolt Group on the
    namespace's pinned Environment, so its members can (or can no longer)
    order container blueprints into this namespace. The owning group always
    keeps its entitlement.

  * Kubernetes access through RBAC -- give a Google account, Google Group,
    or Google service account a RoleBinding in the namespace to the built-in
    edit, view, or admin ClusterRole; or delete the RoleBindings CloudBolt
    made for it (RoleBindings made outside CloudBolt are reported, never
    deleted). A Google Group works only when the cluster has Google Groups
    for RBAC. This is what makes ``kubectl`` and pipelines work for the team.
      https://cloud.google.com/kubernetes-engine/docs/how-to/role-based-access-control
      https://cloud.google.com/kubernetes-engine/docs/how-to/google-groups-rbac
      https://kubernetes.io/docs/reference/access-authn-authz/rbac/#user-facing-roles

Fill in either plane or both.

Action Inputs:
  - gke_ns_access_action (STR, required)  : grant | revoke
  - gke_ns_principal (STR, optional)      : user:EMAIL, group:EMAIL, serviceAccount:EMAIL, or an email
  - gke_ns_access_level (STR, required)   : edit | view | admin (grant only)
  - gke_ns_cloudbolt_group (INT, optional): CloudBolt Group to entitle or revoke

The Kubernetes calls are in shared_modules/SHM-kephq8iq
(grant_namespace_access, revoke_namespace_access).

Entry point: run(job, resource, **kwargs) -> (status, output_msg, error_msg)
"""

from accounts.models import Group
from common.methods import set_progress
from shared_modules.gke_namespace import (
    CF_PREFIX,
    AccessError,
    NamespaceError,
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
    revoke_group,
    revoke_namespace_access,
)
from utilities.logger import ThreadLogger

logger = ThreadLogger(__name__)


def generate_options_for_gke_ns_access_action(field, **kwargs):
    return {"options": [("grant", "Grant access"), ("revoke", "Revoke access")],
            "initial_value": "grant", "sort": False}


def generate_options_for_gke_ns_access_level(field, **kwargs):
    return access_level_options(initial="edit")


def generate_options_for_gke_ns_cloudbolt_group(field, **kwargs):
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
    """Grant or revoke CloudBolt entitlement and/or Kubernetes RBAC access to the namespace."""
    resource = _resolve_resource(job, resource, kwargs)
    if resource is None:
        return "FAILURE", "", "No namespace resource was supplied."
    ensure_custom_fields()

    action = _text("{{ gke_ns_access_action }}").lower()
    principal_text = _text("{{ gke_ns_principal }}")
    level_key = _text("{{ gke_ns_access_level }}").lower() or "edit"
    group_id_str = _text("{{ gke_ns_cloudbolt_group }}")

    if action not in ("grant", "revoke"):
        return "FAILURE", "", "Action must be 'grant' or 'revoke'."
    if not group_id_str and not principal_text:
        return "FAILURE", "", "Give a Google principal, a CloudBolt group, or both."
    principal = ""
    if principal_text:
        principals, errors = parse_principals(principal_text)
        if errors:
            return "FAILURE", "", " ".join(errors)
        if len(principals) != 1:
            return "FAILURE", "", "Give one Google principal per run."
        principal = principals[0]
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

    # ---- Kubernetes access through RBAC ----------------------------------
    if principal:
        try:
            namespace, client = access_context(resource)
            if action == "grant":
                message = grant_namespace_access(client, principal, namespace, level_key)
            else:
                message = revoke_namespace_access(client, principal, namespace)
        except (AccessError, NamespaceError, ValueError) as exc:
            logger.exception("RoleBinding change failed")
            resource.save()
            return "FAILURE", " ".join(results), f"The Kubernetes access change failed: {exc}"
        results.append(message)
        set_progress(message)
        refresh_access_attribute(resource, client, namespace)

    resource.save()
    msg = " ".join(results)
    logger.info(msg)
    return "SUCCESS", msg, ""
