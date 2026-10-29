"""
POC Onboarding: Group (MCP Tool Action plugin).

Creates or updates a group for the POC: parent, entitled environments,
auto-approval and a server-count quota. Covers the test plan's group
entitlements (2.3.2, 2.3.3), approvals (2.4.1) and quota (2.5.2) without
the UI. Synchronous. Admin only.

Every declared input is read from its rendered template token in
_read_inputs(); keep each one referenced that way or CloudBolt's token scan
deletes the input when the script is saved.
"""
import html
import re
from decimal import Decimal

GLOBAL_ID_RE = re.compile(r"^[A-Z]{2,4}-[A-Za-z0-9]{6,16}$")


def _text(value):
    return html.unescape(value or "").strip()


def _lines(value):
    return [line.strip() for line in re.split(r"[\r\n,]+", _text(value)) if line.strip()]


def _int(value):
    text = _text(value)
    if not text:
        return None
    try:
        return int(float(text))
    except ValueError:
        return None


def _failure(message):
    return {"status": "FAILURE", "output_message": "", "error_message": message, "outputs": {"error": message}}


def _success(message, outputs):
    return {"status": "SUCCESS", "output_message": message, "error_message": "", "outputs": outputs}


def _caller(job, kwargs):
    profile = kwargs.get("profile")
    if profile is None and job is not None:
        profile = job.owner
    return profile


def _portal_url():
    try:
        from portals.models import PortalConfig

        portal = PortalConfig.get_current_portal()
        url = portal.site_url if portal else None
    except Exception:
        url = None
    return (url or "").rstrip("/")


def _read_inputs():
    raw = {
        "onb_group_name": """{{ onb_group_name }}""",
        "onb_group_parent": """{{ onb_group_parent }}""",
        "onb_group_environments": """{{ onb_group_environments }}""",
        "onb_group_auto_approval": """{{ onb_group_auto_approval }}""",
        "onb_group_quota_servers": """{{ onb_group_quota_servers }}""",
        "onb_group_description": """{{ onb_group_description }}""",
    }
    return {
        "name": _text(raw["onb_group_name"]),
        "parent": _text(raw["onb_group_parent"]),
        "environments": _lines(raw["onb_group_environments"]),
        "auto_approval": _text(raw["onb_group_auto_approval"]).lower(),
        "quota_servers": _text(raw["onb_group_quota_servers"]),
        "description": _text(raw["onb_group_description"]),
    }


def _resolve_group(ref):
    from accounts.models import Group

    if GLOBAL_ID_RE.match(ref):
        return Group.objects.filter(global_id=ref).first()
    if ref.isdigit():
        return Group.objects.filter(id=int(ref)).first()
    return Group.objects.filter(name__iexact=ref).first()


def _resolve_env(ref):
    from infrastructure.models import Environment

    if GLOBAL_ID_RE.match(ref):
        return Environment.objects.filter(global_id=ref).first()
    if ref.isdigit():
        return Environment.objects.filter(id=int(ref)).first()
    return Environment.objects.filter(name__iexact=ref).first()


def _default_group_type(parent):
    from accounts.models import Group, GroupType

    if parent is not None and parent.type_id:
        return parent.type
    counts = {}
    for group in Group.objects.exclude(name="Unassigned"):
        counts[group.type_id] = counts.get(group.type_id, 0) + 1
    if counts:
        best = max(counts, key=counts.get)
        gtype = GroupType.objects.filter(id=best).first()
        if gtype is not None:
            return gtype
    gtype, _ = GroupType.objects.get_or_create(group_type="Organization")
    return gtype


def _set_quota(group, servers):
    from quota.models import NO_LIMIT

    item = group.quota_set.vm_cnt
    if item is None:
        return "no server-count quota item on this group"
    new_limit = NO_LIMIT if servers < 0 else Decimal(servers)
    item.change_limit(new_limit)
    item.save()
    group.quota_set.save()
    return "unlimited" if servers < 0 else str(servers)


def run(job, *args, **kwargs):
    profile = _caller(job, kwargs)
    if profile is None:
        return _failure("No calling user profile was supplied. Run this tool through the MCP server as an authenticated user.")
    if not profile.is_cbadmin:
        return _failure("Only CloudBolt admins may manage groups.")

    inputs = _read_inputs()
    if not inputs["name"]:
        return _failure("onb_group_name is required.")
    parent = None
    if inputs["parent"]:
        parent = _resolve_group(inputs["parent"])
        if parent is None:
            return _failure("No group matches the parent '{}'.".format(inputs["parent"]))
    envs = []
    for ref in inputs["environments"]:
        env = _resolve_env(ref)
        if env is None:
            return _failure("No environment matches '{}'.".format(ref))
        envs.append(env)
    quota_servers = _int(inputs["quota_servers"]) if inputs["quota_servers"] else None
    if inputs["quota_servers"] and quota_servers is None:
        return _failure("onb_group_quota_servers must be a whole number (or -1 for unlimited).")

    from accounts.models import Group

    group = _resolve_group(inputs["name"]) if not GLOBAL_ID_RE.match(inputs["name"]) else _resolve_group(inputs["name"])
    created = False
    changes = []
    if group is None:
        group = Group(name=inputs["name"], type=_default_group_type(parent), parent=parent, description=inputs["description"])
        group.save()
        created = True
        changes.append("created")
    else:
        if parent is not None and group.parent_id != parent.id:
            group.parent = parent
            changes.append("parent")
        if inputs["description"] and group.description != inputs["description"]:
            group.description = inputs["description"]
            changes.append("description")
    if inputs["auto_approval"] in ("true", "false", "1", "0", "yes", "no"):
        wanted = inputs["auto_approval"] in ("true", "1", "yes")
        if bool(group.allow_auto_approval) != wanted:
            group.allow_auto_approval = wanted
            changes.append("allow_auto_approval={}".format(wanted))
    if changes and not created:
        group.save()
    elif changes and created and len(changes) > 1:
        group.save()

    env_rows = []
    for env in envs:
        if not group.environments.filter(id=env.id).exists():
            group.environments.add(env)
            env_rows.append({"id": env.global_id, "name": env.name, "status": "entitled"})
            changes.append("environment '{}' entitled".format(env.name))
        else:
            env_rows.append({"id": env.global_id, "name": env.name, "status": "already entitled"})

    quota_note = None
    if quota_servers is not None:
        try:
            quota_note = _set_quota(group, quota_servers)
            changes.append("server quota {}".format(quota_note))
        except Exception as exc:
            quota_note = "failed: {}: {}".format(type(exc).__name__, str(exc)[:300])

    base = _portal_url()
    outputs = {
        "group": {
            "id": group.global_id,
            "pk": group.id,
            "name": group.name,
            "type": group.type.group_type if group.type_id else None,
            "parent": group.parent.name if group.parent_id else None,
            "allowAutoApproval": bool(group.allow_auto_approval),
            "created": created,
            "url": base + "/groups/{}/".format(group.id),
            "quotasUrl": base + "/groups/{}/#tab-quotas".format(group.id),
            "quotasClick": "Edit on the Quotas tab",
        },
        "environments": env_rows,
        "serverQuota": quota_note,
        "changes": changes,
        "next": (
            "Add users and roles with onb_user. Entitle the group to blueprints with manage_blueprint_permissions "
            "when the blueprint is not open to every group. Orders from members without the approver role pause "
            "for approval unless allowAutoApproval is true."
        ),
    }
    message = "Group '{}' {}; {} change(s).".format(group.name, "created" if created else "updated", len(changes))
    return _success(message, outputs)
