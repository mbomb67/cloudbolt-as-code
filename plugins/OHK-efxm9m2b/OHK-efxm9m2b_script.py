"""
Onboarding: Group (MCP Tool Action plugin).

Creates or updates a group for the POC: parent, entitled environments,
auto-approval and a server-count quota. Covers the test plan's group
entitlements (2.3.2, 2.3.3), approvals (2.4.1) and quota (2.5.2) without
the UI. Synchronous. Admin only.

Inputs left empty change nothing: environments are only added, and the
parent, description, auto-approval and quota keep their values. Called with
only the name of an existing group it changes nothing and reports the group.
Every result reports the group as it now stands: its environment
entitlements, its members with their roles, its server quota and
auto-approval, because fetch_group_details shows neither environments nor
members.

Every declared input is read from its rendered template token in
_read_inputs(); keep each one referenced that way or CloudBolt's token scan
deletes the input when the script is saved.
"""
import html
import re
from decimal import Decimal

GLOBAL_ID_RE = re.compile(r"^[A-Z]{2,4}-[A-Za-z0-9]{6,16}$")
BOOL_WORDS = {"true": True, "1": True, "yes": True, "false": False, "0": False, "no": False}
MAX_MEMBERS = 100


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


def _quota_report(group):
    """The group's server-count quota as it stands: limit (None when unlimited) and servers counted against it."""
    try:
        item = group.quota_set.vm_cnt
    except Exception:
        item = None
    if item is None:
        return None
    limit = item.limit
    unlimited = limit is None or Decimal(limit).is_infinite()
    return {"limit": None if unlimited else int(limit), "unlimited": unlimited, "used": int(item.used or 0)}


def _env_rows(queryset):
    return [{"id": env.global_id, "name": env.name} for env in queryset.order_by("name")]


def _members(group):
    """Each member of the group with the roles they hold in it; inherited roles name the group they come from."""
    from accounts.models import GroupRoleMembership

    grms = (
        GroupRoleMembership.objects.filter(group=group)
        .select_related("profile__user", "role", "group_inherited_from")
        .order_by("profile__user__username", "role__name")
    )
    members = {}
    for grm in grms:
        row = members.get(grm.profile_id)
        if row is None:
            user = grm.profile.user
            row = members[grm.profile_id] = {"username": user.username, "email": user.email or None, "roles": []}
        role = grm.role.name
        if grm.group_inherited_from_id:
            role = "{} (inherited from {})".format(role, grm.group_inherited_from.name)
        row["roles"].append(role)
    return list(members.values())


def _report(group, base):
    """The group's current environments, members, quota and auto-approval."""
    from infrastructure.models import Environment

    members = _members(group)
    report = {
        "environments": _env_rows(group.environments.all()),
        "memberCount": len(members),
        "members": members[:MAX_MEMBERS],
        "serverQuota": _quota_report(group),
    }
    inherited = _env_rows(group.inherited_environments.all())
    if inherited:
        report["inheritedEnvironments"] = inherited
    unconstrained = _env_rows(Environment.without_unassigned.filter(group=None))
    if unconstrained:
        report["unconstrainedEnvironments"] = unconstrained
    if len(members) > MAX_MEMBERS:
        report["membersNote"] = "Showing {} of {} members; the group page lists them all: {}".format(
            MAX_MEMBERS, len(members), base + "/groups/{}/".format(group.id))
    return report


def run(job, *args, **kwargs):
    profile = _caller(job, kwargs)
    if profile is None:
        return _failure("No calling user profile was supplied. Run this tool through the MCP server as an authenticated user.")
    if not profile.is_cbadmin:
        return _failure("Only CloudBolt admins may manage groups.")

    inputs = _read_inputs()
    if not inputs["name"]:
        return _failure("onb_group_name is required.")
    if inputs["auto_approval"] and inputs["auto_approval"] not in BOOL_WORDS:
        return _failure("onb_group_auto_approval must be true or false, or empty to leave it unchanged.")
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

    group = _resolve_group(inputs["name"])
    report_only = not any(inputs[key] for key in ("parent", "environments", "auto_approval", "quota_servers", "description"))
    if group is None and GLOBAL_ID_RE.match(inputs["name"]):
        return _failure("No group has the id '{}'. Nothing was changed.".format(inputs["name"]))
    if group is None and report_only:
        return _failure(
            "No group is named '{}'. Nothing was created: to create it, call again with at least one other input, "
            "such as onb_group_environments.".format(inputs["name"])
        )
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
    if inputs["auto_approval"]:
        wanted = BOOL_WORDS[inputs["auto_approval"]]
        if bool(group.allow_auto_approval) != wanted:
            group.allow_auto_approval = wanted
            changes.append("allow_auto_approval={}".format(wanted))
    if changes and not created:
        group.save()
    elif changes and created and len(changes) > 1:
        group.save()

    for env in envs:
        if not group.environments.filter(id=env.id).exists():
            group.environments.add(env)
            changes.append("environment '{}' entitled".format(env.name))

    quota_error = None
    if quota_servers is not None:
        current = _quota_report(group)
        unchanged = current is not None and (
            current["unlimited"] if quota_servers < 0 else current["limit"] == quota_servers
        )
        if not unchanged:
            try:
                changes.append("server quota {}".format(_set_quota(group, quota_servers)))
            except Exception as exc:
                quota_error = "{}: {}".format(type(exc).__name__, str(exc)[:300])

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
    }
    outputs.update(_report(group, base))
    if quota_error:
        outputs["serverQuotaError"] = quota_error
    outputs["changes"] = changes
    outputs["next"] = (
        "environments are the group's own entitlements; inheritedEnvironments come from its parent groups, and "
        "unconstrainedEnvironments have no group assigned, so every group may order into them. Add users and roles "
        "with onb_user. Entitle the group to blueprints with manage_blueprint_permissions when the blueprint is not "
        "open to every group. Orders from members without the approver role pause for approval unless "
        "allowAutoApproval is true."
    )
    if created:
        verb = "created"
    elif changes:
        verb = "updated"
    else:
        verb = "unchanged"
    message = "Group '{}' {}; {} change(s).".format(group.name, verb, len(changes))
    return _success(message, outputs)
