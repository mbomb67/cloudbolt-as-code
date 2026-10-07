"""
CloudBolt orchestration plug-in (Post Group Creation): give a newly created
group explicit Deploy permission on every blueprint that one of its ancestor
groups is explicitly permitted to deploy.

CloudBolt fires the Post Group Creation hook point from Group.save() right
after the row is inserted, passing the new group as the ``group`` kwarg and no
job. This plug-in:

  - walks the parent chain to the root (parent, grandparent, ...) with
    Group.get_ancestor_list();
  - collects the blueprints on which any ancestor holds an explicit
    ServiceBlueprintGroupPermissions row (MANAGE or DEPLOY, which is exactly
    what ServiceBlueprint.groups_with_deploy_permission counts as "can deploy");
  - ignores blueprints flagged any_group_can_deploy, since the new group can
    already order those and an explicit row would be redundant;
  - adds a DEPLOY permission row for the new group on each remaining blueprint,
    idempotently (get_or_create), and reports what was added versus already
    present.

Only Deploy is granted. Manage permission is never copied, because a subgroup
inheriting the right to edit a blueprint is not what "can order it" means.

Entry point: run(job, *args, **kwargs) -> (status, output_msg, error_msg)
"""

from accounts.models import Group
from common.methods import set_progress
from servicecatalog.models import ServiceBlueprint, ServiceBlueprintGroupPermissions
from utilities.logger import ThreadLogger

logger = ThreadLogger(__name__)

# Rows that make a group "able to deploy" a blueprint in CloudBolt's own
# ServiceBlueprint.groups_with_deploy_permission query.
DEPLOY_CAPABLE_PERMISSIONS = ("MANAGE", "DEPLOY")

# The one permission this plug-in ever writes.
GRANTED_PERMISSION = "DEPLOY"


def _resolve_group(group):
    """
    Return the Group object for the hook's ``group`` kwarg.

    The hook point passes the model instance, but accept a name as well so the
    plug-in can be exercised by hand from a shell.
    """
    if isinstance(group, Group):
        return group
    if isinstance(group, str) and group:
        return Group.objects.get(name=group)
    return None


def _blueprints_deployable_by(groups):
    """
    Return the blueprints that at least one of ``groups`` is explicitly permitted
    to deploy, excluding blueprints that any group can deploy.
    """
    blueprint_ids = (
        ServiceBlueprintGroupPermissions.objects.filter(
            group__in=groups, permission__in=DEPLOY_CAPABLE_PERMISSIONS
        )
        .values_list("blueprint_id", flat=True)
        .distinct()
    )
    return (
        ServiceBlueprint.objects.filter(id__in=blueprint_ids)
        .exclude(any_group_can_deploy=True)
        .order_by("name")
    )


def _grant_deploy(group, blueprints):
    """
    Add a DEPLOY row for ``group`` on each blueprint. Returns the names that were
    newly granted and the names that already had the permission.
    """
    granted, already_present = [], []
    for blueprint in blueprints:
        _, created = ServiceBlueprintGroupPermissions.objects.get_or_create(
            permission=GRANTED_PERMISSION, group=group, blueprint=blueprint
        )
        if created:
            granted.append(blueprint.name)
            set_progress(
                f"Granted '{group.name}' {GRANTED_PERMISSION.lower()} permission on "
                f"blueprint '{blueprint.name}'."
            )
        else:
            already_present.append(blueprint.name)
    return granted, already_present


def run(job, *args, **kwargs):
    group = _resolve_group(kwargs.get("group"))
    if group is None:
        return (
            "WARNING",
            "No group was passed to the Post Group Creation hook; nothing to do.",
            "",
        )

    ancestors = group.get_ancestor_list()
    if not ancestors:
        return (
            "SUCCESS",
            f"Group '{group.name}' has no parent group; no blueprint permissions "
            "to inherit.",
            "",
        )

    ancestor_names = ", ".join(f"'{ancestor.name}'" for ancestor in ancestors)
    set_progress(
        f"Looking for blueprints explicitly deployable by the ancestors of "
        f"'{group.name}': {ancestor_names}."
    )
    logger.info(
        "Post Group Creation: group %s has ancestors %s", group.name, ancestor_names
    )

    blueprints = list(_blueprints_deployable_by(ancestors))
    if not blueprints:
        return (
            "SUCCESS",
            f"No blueprint grants explicit deploy permission to an ancestor of "
            f"'{group.name}'; nothing to add.",
            "",
        )

    granted, already_present = _grant_deploy(group, blueprints)

    parts = []
    if granted:
        parts.append(
            f"granted deploy permission on {len(granted)} blueprint(s): "
            + ", ".join(granted)
        )
    if already_present:
        parts.append(
            f"{len(already_present)} already permitted: " + ", ".join(already_present)
        )
    return "SUCCESS", f"Group '{group.name}': " + "; ".join(parts) + ".", ""
