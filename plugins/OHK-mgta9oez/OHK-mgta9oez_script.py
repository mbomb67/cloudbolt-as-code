"""
Manage Blueprint Permissions (MCP Tool Action plugin).

Lists, grants, or revokes a group's DEPLOY or MANAGE permission on a
blueprint, the same ServiceBlueprintGroupPermissions rows the Permissions
tab manages. The content-testing loop uses it around every blueprint sync:
list before the sync, restore the same groups after it (the importer's
add_group_permissions keeps only the groups named in the metadata, repo
exports name none, so a sync of a blueprint whose any_group_can_deploy is
false removes every group's permission), and revoke any grant the loop
itself added once the test is over.

Every declared input is read from its rendered template token in
_read_inputs(); keep each one referenced that way or CloudBolt's token scan
deletes the input when the script is saved. Output keys are camelCase so a
synchronous run and an asynchronous fetch_job read identically.
"""
import html

from accounts.models import Group
from servicecatalog.models import ServiceBlueprint, ServiceBlueprintGroupPermissions

PERMISSIONS = ("DEPLOY", "MANAGE")
ACTIONS = ("grant", "revoke", "list")


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
        "grant_action": """{{ grant_action }}""",
        "grant_blueprint": """{{ grant_blueprint }}""",
        "grant_group": """{{ grant_group }}""",
        "grant_permission": """{{ grant_permission }}""",
    }
    return {
        "action": (_text(raw["grant_action"]) or "grant").lower(),
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


def _state(blueprint):
    return {
        "blueprintId": blueprint.global_id,
        "blueprintName": blueprint.name,
        "anyGroupCanDeploy": bool(blueprint.any_group_can_deploy),
        "groupsThatCanDeploy": sorted(
            ({"id": g.global_id, "name": g.name} for g in blueprint.groups_with_deploy_permission),
            key=lambda row: row["name"],
        ),
        "groupsThatCanManage": sorted(
            ({"id": g.global_id, "name": g.name} for g in blueprint.groups_that_can_manage),
            key=lambda row: row["name"],
        ),
    }


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
        return _failure("Only CloudBolt admins may manage blueprint permissions.")

    inputs = _read_inputs()
    if inputs["action"] not in ACTIONS:
        return _failure("grant_action must be grant, revoke, or list; got '{}'.".format(inputs["action"]))
    if not inputs["blueprint"]:
        return _failure("grant_blueprint is required: a BP-... global id or the exact name.")
    if inputs["permission"] not in PERMISSIONS:
        return _failure("grant_permission must be DEPLOY or MANAGE; got '{}'.".format(inputs["permission"]))

    blueprint, error = _find_blueprint(inputs["blueprint"])
    if error:
        return _failure(error)

    if inputs["action"] == "list":
        outputs = _state(blueprint)
        outputs["action"] = "list"
        message = "'{}': any group {}; deploy {}; manage {}.".format(
            blueprint.name,
            "yes" if outputs["anyGroupCanDeploy"] else "no",
            ", ".join(g["name"] for g in outputs["groupsThatCanDeploy"]) or "none",
            ", ".join(g["name"] for g in outputs["groupsThatCanManage"]) or "none",
        )
        return {"status": "SUCCESS", "output_message": message, "error_message": "", "outputs": outputs}

    if not inputs["group"]:
        return _failure("grant_group is required for grant and revoke: a GRP-... global id or the exact name.")
    group, error = _find_group(inputs["group"])
    if error:
        return _failure(error)

    rows = ServiceBlueprintGroupPermissions.objects.filter(
        permission=inputs["permission"], group=group, blueprint=blueprint
    )
    if inputs["action"] == "grant":
        changed = not rows.exists()
        if changed:
            ServiceBlueprintGroupPermissions.objects.create(
                permission=inputs["permission"], group=group, blueprint=blueprint
            )
        verb = "Granted" if changed else "Confirmed existing"
    else:
        count = rows.count()
        changed = count > 0
        if changed:
            rows.delete()
        verb = "Revoked" if changed else "No"

    outputs = _state(blueprint)
    outputs.update(
        {
            "action": inputs["action"],
            "groupId": group.global_id,
            "groupName": group.name,
            "permission": inputs["permission"],
            "changed": changed,
        }
    )
    message = "{} {} permission on '{}' for group '{}'.".format(
        verb, inputs["permission"], blueprint.name, group.name
    )
    return {"status": "SUCCESS", "output_message": message, "error_message": "", "outputs": outputs}
