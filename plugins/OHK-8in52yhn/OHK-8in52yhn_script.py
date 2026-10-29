"""
POC Onboarding: User (MCP Tool Action plugin).

Creates a local user for the POC (a requestor, an approver, a resource
admin) and assigns roles in a group, without ever handling a password: the
user is created with no usable password and the response carries the edit
link where the admin sets one. An existing user only gets the role
assignments. Covers the test plan's role tests (2.3.1, 2.3.4) and approval
tests (2.4.1, 2.4.2). Synchronous. Admin only.

Every declared input is read from its rendered template token in
_read_inputs(); keep each one referenced that way or CloudBolt's token scan
deletes the input when the script is saved.
"""
import html
import re

GLOBAL_ID_RE = re.compile(r"^[A-Z]{2,4}-[A-Za-z0-9]{6,16}$")
USERNAME_RE = re.compile(r"^[A-Za-z0-9@.+_-]{1,150}$")


def _text(value):
    return html.unescape(value or "").strip()


def _lines(value):
    return [line.strip() for line in re.split(r"[\r\n,]+", _text(value)) if line.strip()]


def _bool(value, default):
    text = _text(value).lower()
    if not text:
        return default
    return text in ("true", "1", "yes", "y", "on")


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
        "onb_username": """{{ onb_username }}""",
        "onb_user_email": """{{ onb_user_email }}""",
        "onb_user_first_name": """{{ onb_user_first_name }}""",
        "onb_user_last_name": """{{ onb_user_last_name }}""",
        "onb_user_group": """{{ onb_user_group }}""",
        "onb_user_roles": """{{ onb_user_roles }}""",
        "onb_user_super_admin": """{{ onb_user_super_admin }}""",
    }
    return {
        "username": _text(raw["onb_username"]),
        "email": _text(raw["onb_user_email"]),
        "first_name": _text(raw["onb_user_first_name"]),
        "last_name": _text(raw["onb_user_last_name"]),
        "group": _text(raw["onb_user_group"]),
        "roles": _lines(raw["onb_user_roles"]),
        "super_admin": _text(raw["onb_user_super_admin"]),
    }


def _resolve_group(ref):
    from accounts.models import Group

    if GLOBAL_ID_RE.match(ref):
        return Group.objects.filter(global_id=ref).first()
    if ref.isdigit():
        return Group.objects.filter(id=int(ref)).first()
    return Group.objects.filter(name__iexact=ref).first()


def _resolve_role(ref):
    from accounts.models import Role

    role = Role.objects.filter(name__iexact=ref).first() or Role.objects.filter(label__iexact=ref).first()
    if role is None:
        role = Role.objects.filter(name__iexact=ref.replace(" ", "_")).first()
    return role


def run(job, *args, **kwargs):
    profile = _caller(job, kwargs)
    if profile is None:
        return _failure("No calling user profile was supplied. Run this tool through the MCP server as an authenticated user.")
    if not profile.is_cbadmin:
        return _failure("Only CloudBolt admins may manage users.")

    inputs = _read_inputs()
    if not inputs["username"]:
        return _failure("onb_username is required.")
    if not USERNAME_RE.match(inputs["username"]):
        return _failure("onb_username may contain letters, digits and @ . + - _ only.")
    group = None
    if inputs["group"]:
        group = _resolve_group(inputs["group"])
        if group is None:
            return _failure("No group matches '{}'.".format(inputs["group"]))
    if inputs["roles"] and group is None:
        return _failure("onb_user_roles needs onb_user_group: roles are assigned in a group.")
    roles = []
    for ref in inputs["roles"]:
        role = _resolve_role(ref)
        if role is None:
            from accounts.models import Role

            available = ", ".join(sorted(r.name for r in Role.objects.filter(assignable_to_users=True)))
            return _failure("No role matches '{}'. Assignable roles: {}.".format(ref, available))
        roles.append(role)

    from django.contrib.auth.models import User
    from accounts.models import GroupRoleMembership, UserProfile

    user = User.objects.filter(username__iexact=inputs["username"]).first()
    created = False
    changes = []
    if user is None:
        user = User.objects.create_user(
            username=inputs["username"],
            email=inputs["email"] or "",
            password=None,
            first_name=inputs["first_name"],
            last_name=inputs["last_name"],
        )
        created = True
        changes.append("created with no usable password")
    else:
        for field in ("email", "first_name", "last_name"):
            value = inputs[field]
            if value and getattr(user, field) != value:
                setattr(user, field, value)
                changes.append(field)
        if changes:
            user.save()
    user_profile = UserProfile.objects.filter(user=user).first()
    if user_profile is None:
        user_profile = UserProfile.objects.create(user=user)
    if inputs["super_admin"]:
        wanted = _bool(inputs["super_admin"], False)
        if bool(user_profile.super_admin) != wanted:
            user_profile.super_admin = wanted
            user_profile.save()
            changes.append("super_admin={}".format(wanted))

    memberships = []
    for role in roles:
        _membership, made = GroupRoleMembership.objects.get_or_create(profile=user_profile, group=group, role=role)
        memberships.append({"group": group.name, "role": role.name, "status": "added" if made else "already present"})
        if made:
            changes.append("role {} in {}".format(role.name, group.name))

    base = _portal_url()
    outputs = {
        "user": {
            "id": user_profile.global_id,
            "pk": user_profile.id,
            "username": user.username,
            "email": user.email,
            "name": "{} {}".format(user.first_name, user.last_name).strip(),
            "superAdmin": bool(user_profile.super_admin),
            "created": created,
            "url": base + "/users/{}/".format(user_profile.id),
            "setPasswordUrl": base + "/users/{}/".format(user_profile.id),
            "setPasswordSteps": "Click Edit, type the password in Change password and Confirm password, then Save changes.",
        },
        "memberships": memberships,
        "changes": changes,
        "next": (
            "Send the admin setPasswordUrl with setPasswordSteps to set the user's password in the UI (never in chat); "
            "the user has no usable password until then. Then have the user log in to exercise the role."
        ),
    }
    message = "User '{}' {}; {} role assignment(s).".format(user.username, "created" if created else "updated", len(memberships))
    return _success(message, outputs)
