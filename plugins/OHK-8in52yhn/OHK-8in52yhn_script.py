"""
POC Onboarding: User (MCP Tool Action plugin).

Assigns roles in a group to a POC user (a requestor, an approver, a
resource admin) without ever handling a password. A user that does not exist
yet is not created here: CloudBolt's edit dialog asks for a user's current
password before it changes one (Original Password Verification, on by
default and hidden from the UI), so a user created without a password could
never get one. The response instead says how the admin adds the user on the
Users list, where the password is typed; calling again then assigns the
roles. Covers the test plan's role tests (2.3.1, 2.3.4) and approval
tests (2.4.1, 2.4.2). Synchronous. Admin only.

Every declared input is read from its rendered template token in
_read_inputs(); keep each one referenced that way or CloudBolt's token scan
deletes the input when the script is saved.
"""
import html
import re

GLOBAL_ID_RE = re.compile(r"^[A-Z]{2,4}-[A-Za-z0-9]{6,16}$")
USERNAME_RE = re.compile(r"^[A-Za-z0-9@.+_-]{1,150}$")
CREATE_STEPS = "Click Add, fill in the values given, type the password in Password and Confirm password, then click Create."
PASSWORD_FIX_STEPS = (
    "CloudBolt asks for the user's current password before it changes one, and this user has none. On the user's page click Delete and confirm, then on the Users list click Add, enter the user again with Password and Confirm password, and click Create."
)


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


def _create_in_ui(inputs, base):
    values = {"Username": inputs["username"]}
    for label, key in (("Email", "email"), ("First Name", "first_name"), ("Last Name", "last_name")):
        if inputs[key]:
            values[label] = inputs[key]
    outputs = {
        "user": None,
        "createInUi": {"url": base + "/users/", "click": "Add", "values": values, "steps": CREATE_STEPS},
        "next": (
            "Give the admin this as one step: the link, Add, the values, and that the password is typed in the form "
            "(never in chat). Wait until they say it is created, then call onb_user again with the same inputs to "
            "assign the roles."
        ),
    }
    if not inputs["email"]:
        outputs["note"] = "The form requires an Email; ask the admin which address to use."
    return outputs


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

    base = _portal_url()
    user = User.objects.filter(username__iexact=inputs["username"]).first()
    if user is None:
        return _success(
            "User '{}' does not exist yet: the admin adds it in the UI, where the password is typed.".format(inputs["username"]),
            _create_in_ui(inputs, base),
        )
    changes = []
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

    password_set = user.has_usable_password()
    outputs = {
        "user": {
            "id": user_profile.global_id,
            "pk": user_profile.id,
            "username": user.username,
            "email": user.email,
            "name": "{} {}".format(user.first_name, user.last_name).strip(),
            "superAdmin": bool(user_profile.super_admin),
            "passwordSet": password_set,
            "url": base + "/users/{}/".format(user_profile.id),
        },
        "memberships": memberships,
        "changes": changes,
        "next": "Have the user log in to exercise the role.",
    }
    # Earlier versions of this tool created users without a password, which the UI cannot add later.
    if not password_set and user.last_login is None and user_profile.ldap_id is None:
        outputs["passwordFix"] = {"url": outputs["user"]["url"], "steps": PASSWORD_FIX_STEPS}
        outputs["next"] = (
            "This user has no password and CloudBolt cannot add one on the user's page. Give the admin passwordFix as one "
            "step (deleting the user also drops its roles); once they have added it again, call onb_user with the same "
            "inputs to restore the roles."
        )
    message = "User '{}' updated; {} role assignment(s).".format(user.username, len(memberships))
    return _success(message, outputs)
