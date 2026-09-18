"""
CloudBolt Day-2 resource action: Manage Team Access.

Grants or revokes access to an OpenShift landing-zone project on two planes:

  * CloudBolt entitlement -- add or remove a CloudBolt Group on the project's
    pinned Environment. Members can (or can no longer) order the VM and
    container blueprints into this project. This is the governance layer
    OpenShift itself does not have: business-unit level, cross-cluster,
    audited through CloudBolt's job history.

  * OpenShift RBAC -- create or delete a RoleBinding that grants an
    identity-provider Group, a User, or a ServiceAccount one of the project
    roles (admin / edit / view) for direct oc and console access.

Fill in either plane or both. At least one must be supplied.

Action Inputs:
  - access_action (STR, required)          : grant | revoke
  - cloudbolt_group_id (INT, optional)     : CloudBolt Group to entitle/revoke
  - openshift_subject_kind (STR, optional) : Group | User | ServiceAccount
  - openshift_subject_name (STR, optional) : subject, listed from the cluster for the chosen kind
  - access_role (STR, required)            : admin | edit | view (OpenShift plane only)

Entry point: run(job, resource, **kwargs) -> (status, output_msg, error_msg)
"""

from accounts.models import Group
from common.methods import set_progress
from shared_modules.openshift_landing_zone import (
    CF_PREFIX,
    LandingZoneError,
    OpenShiftLandingZoneClient,
    ensure_custom_fields,
    entitle_group,
    entitled_group_names,
    load_environment,
    load_handler,
    render_bindings,
    revoke_group,
)
from utilities.logger import ThreadLogger

logger = ThreadLogger(__name__)


def generate_options_for_access_action(field, **kwargs):
    return {"options": [("grant", "Grant access"), ("revoke", "Revoke access")],
            "initial_value": "grant", "sort": False}


def generate_options_for_cloudbolt_group_id(field, **kwargs):
    groups = Group.objects.exclude(name__startswith="Unassigned").order_by("name")
    options = [("", "------ No CloudBolt group change ------")]
    options.extend((g.id, g.name) for g in groups)
    return {"options": options, "sort": False}


def generate_options_for_openshift_subject_kind(field, **kwargs):
    return {"options": [("Group", "Group (identity provider group)"), ("User", "User"),
                        ("ServiceAccount", "ServiceAccount (in this project)")],
            "initial_value": "Group", "sort": False}


def _resource_from_kwargs(kwargs):
    """The Resource the action dialog was opened on, from whichever slot CloudBolt used."""
    resource = kwargs.get("resource")
    if resource is not None:
        return resource
    for candidate in kwargs.get("resources") or []:
        if candidate is not None:
            return candidate
    return None


def generate_options_for_openshift_subject_name(field, control_value=None, **kwargs):
    """Subjects of the chosen kind, read live from the project's cluster.

    control_value is the selected openshift_subject_kind (REGENOPTIONS
    dependency): Group and User list the cluster's user.openshift.io objects,
    ServiceAccount lists the service accounts in this project's namespace.
    Subjects that already hold a CloudBolt-managed binding are included even if
    they no longer exist on the cluster, so a revoke can still target them.
    """
    none_option = ("", "------ None (CloudBolt group change only) ------")
    kind = (control_value or "Group").strip()

    resource = _resource_from_kwargs(kwargs)
    try:
        namespace, rh = load_handler(resource)
    except ValueError as exc:
        logger.warning("Cannot list OpenShift subjects: %s", exc)
        return [none_option]

    try:
        client = OpenShiftLandingZoneClient.from_handler(rh)
        names = set(client.list_subjects(kind, namespace))
        names.update(
            name for role, bound_kind, name in client.list_managed_role_bindings(namespace)
            if bound_kind == kind
        )
    except Exception as exc:
        logger.warning("Could not list OpenShift %ss for %s: %s", kind, namespace, exc)
        return [none_option, ("", f"------ Could not list {kind}s: {exc} ------")]

    if not names:
        return [none_option, ("", f"------ No {kind}s found ------")]

    options = [none_option]
    options.extend((name, name) for name in sorted(names))
    return {"options": options, "sort": False}


def generate_options_for_access_role(field, **kwargs):
    # OpenShift's default project roles.
    return {"options": [("admin", "admin - manage the project and its members"),
                        ("edit", "edit - create and change workloads"),
                        ("view", "view - read only")],
            "initial_value": "edit", "sort": False}


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
    """Grant or revoke CloudBolt entitlement and/or OpenShift role access."""
    resource = _resolve_resource(job, resource, kwargs)
    ensure_custom_fields()

    action = "{{ access_action }}".strip().lower()
    group_id_str = "{{ cloudbolt_group_id }}".strip()
    subject_kind = "{{ openshift_subject_kind }}".strip() or "Group"
    subject_name = "{{ openshift_subject_name }}".strip()
    role = "{{ access_role }}".strip().lower()

    if action not in ("grant", "revoke"):
        return "FAILURE", "", "Action must be 'grant' or 'revoke'."
    if not group_id_str and not subject_name:
        return "FAILURE", "", "Choose a CloudBolt group, an OpenShift subject, or both."

    try:
        namespace, rh = load_handler(resource)
    except ValueError as exc:
        return "FAILURE", "", f"Cannot manage access: {exc}."

    results = []

    # ---- CloudBolt entitlement plane ------------------------------------
    if group_id_str:
        group = Group.objects.filter(id=int(group_id_str)).first()
        if group is None:
            return "FAILURE", "", f"CloudBolt group id={group_id_str} not found."
        env = load_environment(resource)
        if env is None:
            return (
                "FAILURE", "",
                f"No CloudBolt environment is recorded for project '{namespace}'; "
                "cannot change group entitlement.",
            )
        if action == "grant":
            changed = entitle_group(env, group)
            results.append(
                f"CloudBolt group '{group.name}' {'entitled to' if changed else 'was already entitled to'} "
                f"environment '{env.name}'."
            )
        else:
            changed = revoke_group(env, group)
            results.append(
                f"CloudBolt group '{group.name}' {'removed from' if changed else 'was not entitled to'} "
                f"environment '{env.name}'."
            )
        set_progress(results[-1])
        resource.set_value_for_custom_field(
            CF_PREFIX + "entitled_groups", "\n".join(entitled_group_names(env))
        )

    # ---- OpenShift RBAC plane -------------------------------------------
    if subject_name:
        try:
            client = OpenShiftLandingZoneClient.from_handler(rh)
        except Exception as exc:
            logger.exception("Could not connect to OpenShift API")
            return "FAILURE", "", f"Could not connect to the OpenShift API for '{rh.name}': {exc}"

        try:
            if action == "grant":
                client.apply_role_binding(namespace, role, subject_kind, subject_name)
                results.append(
                    f"OpenShift {subject_kind} '{subject_name}' granted the '{role}' role on '{namespace}'."
                )
            else:
                removed = client.delete_role_binding(namespace, role, subject_kind, subject_name)
                results.append(
                    f"OpenShift {subject_kind} '{subject_name}' "
                    f"{'no longer has' if removed else 'did not have a CloudBolt-managed'} "
                    f"'{role}' role binding on '{namespace}'."
                )
            set_progress(results[-1])
            bindings = client.list_managed_role_bindings(namespace)
            resource.set_value_for_custom_field(CF_PREFIX + "team_bindings", render_bindings(bindings))
        except (LandingZoneError, ValueError) as exc:
            logger.exception("RoleBinding change failed")
            return "FAILURE", "", f"Failed to update OpenShift access on '{namespace}': {exc}"

    resource.save()
    msg = " ".join(results)
    logger.info(msg)
    return "SUCCESS", msg, ""
