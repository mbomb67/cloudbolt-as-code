"""
Grant Blueprint Permission (MCP Tool Action plugin).

Gives a group DEPLOY (or MANAGE) permission on a blueprint by creating the
ServiceBlueprintGroupPermissions row the Permissions tab creates. Needed
after every Source Control Repos sync of a blueprint whose metadata has
any_group_can_deploy false: the importer's add_group_permissions keeps only
the groups named in the metadata, repo exports carry none, so the sync
removes every group's deploy and manage permission.

Every declared input is read from its rendered template token in
_read_inputs(); keep each one referenced that way or CloudBolt's token scan
deletes the input when the script is saved. Output keys are camelCase so a
synchronous run and an asynchronous fetch_job read identically.
"""
import html

from accounts.models import Group
from servicecatalog.models import ServiceBlueprint, ServiceBlueprintGroupPermissions

PERMISSIONS = ("DEPLOY", "MANAGE")


def _text(value):
    return html.unescape(value or "").strip()


def _failure(message):
    return {
        "status": "FAILURE",
        "output_message": "",
        "error_message": message,
        "outputs": {"error": message},
    }


def _read_inputs():
    raw = {
        "grant_blueprint": """{{ grant_blueprint }}""",
        "grant_group": """{{ grant_group }}""",
        "grant_permission": """{{ grant_permission }}""",
    }
    return {
        "blueprint": _text(raw["grant_blueprint"]),
        "group": _text(raw["grant_group"]),
        "permission": (_text(raw["grant_permission"]) or "DEPLOY").upper(),
    }


def _find_blueprint(ref):
    blueprint = ServiceBlueprint.objects.filter(global_id=ref).first()
    if blueprint is not None:
        return blueprint, None
    matches = ServiceBlueprint.objects.filter(name__iexact=ref).exclude(status="HISTORICAL")
    if matches.count() == 1:
        return matches.first(), None
    if matches.count() > 1:
        return None, "Several active blueprints are named '{}': {}. Give the global id.".format(
            ref, ", ".join(bp.global_id for bp in matches)
        )
    return None, "No active blueprint matches '{}'. Give a BP-... global id or the exact name.".format(ref)


def _find_group(ref):
    group = Group.objects.filter(global_id=ref).first() or Group.objects.filter(name__iexact=ref).first()
    if group is not None:
        return group, None
    return None, "No group matches '{}'. Give a GRP-... global id or the exact name.".format(ref)


def run(job, *args, **kwargs):
    profile = kwargs.get("profile")
    if profile is None and job is not None:
        profile = job.owner
    if profile is None:
        return _failure(
            "No calling user profile was supplied. Run this tool through the MCP "
            "server or the API as an authenticated user."
        )
    if not profile.is_cbadmin:
        return _failure("Only CloudBolt admins may grant blueprint permissions.")

    inputs = _read_inputs()
    if not inputs["blueprint"] or not inputs["group"]:
        return _failure("grant_blueprint and grant_group are both required.")
    if inputs["permission"] not in PERMISSIONS:
        return _failure("grant_permission must be DEPLOY or MANAGE; got '{}'.".format(inputs["permission"]))

    blueprint, error = _find_blueprint(inputs["blueprint"])
    if error:
        return _failure(error)
    group, error = _find_group(inputs["group"])
    if error:
        return _failure(error)

    existing = ServiceBlueprintGroupPermissions.objects.filter(
        permission=inputs["permission"], group=group, blueprint=blueprint
    ).first()
    created = existing is None
    if created:
        ServiceBlueprintGroupPermissions.objects.create(
            permission=inputs["permission"], group=group, blueprint=blueprint
        )

    outputs = {
        "blueprintId": blueprint.global_id,
        "blueprintName": blueprint.name,
        "groupId": group.global_id,
        "groupName": group.name,
        "permission": inputs["permission"],
        "created": created,
        "anyGroupCanDeploy": bool(blueprint.any_group_can_deploy),
        "groupsThatCanDeploy": sorted(
            g.name for g in blueprint.groups_with_deploy_permission
        ),
        "groupsThatCanManage": sorted(g.name for g in blueprint.groups_that_can_manage),
    }
    message = "{} {} permission on '{}' for group '{}'.".format(
        "Granted" if created else "Confirmed existing",
        inputs["permission"],
        blueprint.name,
        group.name,
    )
    return {
        "status": "SUCCESS",
        "output_message": message,
        "error_message": "",
        "outputs": outputs,
    }
