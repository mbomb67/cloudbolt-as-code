"""
POC Onboarding: LDAP Mapping (MCP Tool Action plugin).

Maps an Active Directory or LDAP group (by distinguished name) to a
CloudBolt group with roles, the way the LDAP utility's Group Mappings tab
does, so members land in the right group with the right role at their
first login (test plan 2.1.2). The LDAP utility itself is created in the
UI, where its service-account password is entered. Synchronous. Admin only.

Every declared input is read from its rendered template token in
_read_inputs(); keep each one referenced that way or CloudBolt's token scan
deletes the input when the script is saved.
"""
import html
import re

GLOBAL_ID_RE = re.compile(r"^[A-Z]{2,4}-[A-Za-z0-9]{6,16}$")


def _text(value):
    return html.unescape(value or "").strip()


def _lines(value):
    return [line.strip() for line in re.split(r"[\r\n,]+", _text(value)) if line.strip()]


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
        "onb_ldap": """{{ onb_ldap }}""",
        "onb_ldap_group_dn": """{{ onb_ldap_group_dn }}""",
        "onb_ldap_cb_group": """{{ onb_ldap_cb_group }}""",
        "onb_ldap_roles": """{{ onb_ldap_roles }}""",
    }
    return {
        "ldap": _text(raw["onb_ldap"]),
        "group_dn": _text(raw["onb_ldap_group_dn"]),
        "cb_group": _text(raw["onb_ldap_cb_group"]),
        "roles": _lines(raw["onb_ldap_roles"]),
    }


def _resolve_ldap(ref):
    from utilities.models import LDAPUtility

    utilities = list(LDAPUtility.objects.all().order_by("id"))
    if not utilities:
        return None, "No LDAP utility exists yet; create one in the UI (Admin > LDAP Utilities > Add), where the service-account password is entered."
    if not ref:
        if len(utilities) == 1:
            return utilities[0], None
        return None, "Several LDAP utilities exist; pass onb_ldap: {}.".format(
            ", ".join("{} ({})".format(getattr(u, "global_id", u.id), getattr(u, "name", None) or u.ip) for u in utilities)
        )
    for utility in utilities:
        if ref in (getattr(utility, "global_id", None), str(utility.id)):
            return utility, None
        for attr in ("name", "ldap_domain", "ip"):
            if (getattr(utility, attr, None) or "").lower() == ref.lower():
                return utility, None
    return None, "No LDAP utility matches '{}'.".format(ref)


def _resolve_group(ref):
    from accounts.models import Group

    if GLOBAL_ID_RE.match(ref):
        return Group.objects.filter(global_id=ref).first()
    if ref.isdigit():
        return Group.objects.filter(id=int(ref)).first()
    return Group.objects.filter(name__iexact=ref).first()


def _resolve_role(ref):
    from accounts.models import Role

    return (
        Role.objects.filter(name__iexact=ref).first()
        or Role.objects.filter(label__iexact=ref).first()
        or Role.objects.filter(name__iexact=ref.replace(" ", "_")).first()
    )


def run(job, *args, **kwargs):
    profile = _caller(job, kwargs)
    if profile is None:
        return _failure("No calling user profile was supplied. Run this tool through the MCP server as an authenticated user.")
    if not profile.is_cbadmin:
        return _failure("Only CloudBolt admins may manage LDAP mappings.")

    inputs = _read_inputs()
    if not inputs["group_dn"]:
        return _failure("onb_ldap_group_dn is required: the distinguished name of the AD or LDAP group, for example CN=CloudBolt Users,OU=Groups,DC=example,DC=com.")
    if not inputs["cb_group"]:
        return _failure("onb_ldap_cb_group is required: the CloudBolt group (GRP- id or name) members land in.")
    utility, error = _resolve_ldap(inputs["ldap"])
    if error:
        return _failure(error)
    group = _resolve_group(inputs["cb_group"])
    if group is None:
        return _failure("No CloudBolt group matches '{}'; create it with onb_group.".format(inputs["cb_group"]))
    roles = []
    for ref in inputs["roles"] or ["requestor"]:
        role = _resolve_role(ref)
        if role is None:
            from accounts.models import Role

            return _failure("No role matches '{}'. Roles: {}.".format(ref, ", ".join(sorted(r.name for r in Role.objects.all()))))
        roles.append(role)

    from utilities.models import LDAPMapping, LDAPMappingGroup

    mapping, created = LDAPMapping.objects.get_or_create(ldap_utility=utility, ldap_group_dn=inputs["group_dn"])
    mapping_group, mg_created = LDAPMappingGroup.objects.get_or_create(mapping=mapping, group=group)
    added = []
    for role in roles:
        if not mapping_group.roles.filter(id=role.id).exists():
            mapping_group.roles.add(role)
            added.append(role.name)

    base = _portal_url()
    outputs = {
        "ldapUtility": {"id": getattr(utility, "global_id", None), "pk": utility.id, "name": getattr(utility, "name", None) or utility.ip,
                        "url": base + "/admin/ldap_settings/{}/".format(utility.id)},
        "mapping": {"id": mapping.id, "groupDn": mapping.ldap_group_dn, "created": created},
        "cloudboltGroup": {"id": group.global_id, "name": group.name, "mappingCreated": mg_created},
        "roles": sorted(r.name for r in mapping_group.roles.all()),
        "rolesAdded": added,
        "next": (
            "Have a member of that AD group log in to CloudBolt; fetch_group_details on the group then lists them with "
            "the role. If the login fails, onb_recent_errors with onb_log=authentication."
        ),
    }
    message = "'{}' -> group '{}' with roles {}.".format(inputs["group_dn"], group.name, ", ".join(outputs["roles"]))
    return _success(message, outputs)
