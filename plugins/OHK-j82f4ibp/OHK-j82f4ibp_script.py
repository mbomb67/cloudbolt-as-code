"""
POC Onboarding: Verify LDAP (MCP Tool Action plugin).

Runs an LDAP utility's Verify Connection (bind and search) and returns the
real error text with a hint, the way onb_verify_resource_handler does for
a cloud handler. Runs as a job because it contacts the directory. Admin
only.

Every declared input is read from its rendered template token in
_read_inputs(); keep each one referenced that way or CloudBolt's token scan
deletes the input when the script is saved.
"""
import html
import re

HINTS = [
    (re.compile(r"INVALID_CREDENTIALS|invalidCredentials|49", re.I), "The directory rejected the bind: check the service account DN or UPN and re-enter its password on the LDAP utility's edit page."),
    (re.compile(r"SERVER_DOWN|Can't contact LDAP server|timed out|Connection refused", re.I), "The appliance cannot reach the directory server on the chosen port: check the host, port (389 for LDAP, 636 for LDAPS) and firewall."),
    (re.compile(r"CERTIFICATE|certificate verify failed|SSL", re.I), "LDAPS certificate verification failed: add the domain CA under Admin > SSL Certificates or use LDAP on 389 for the POC."),
    (re.compile(r"NO_SUCH_OBJECT|noSuchObject", re.I), "The base DN does not exist; copy it from the directory (for example DC=example,DC=com)."),
    (re.compile(r"Name or service not known|getaddrinfo", re.I), "DNS on the appliance cannot resolve the directory hostname."),
    (re.compile(r"STRONG_AUTH_REQUIRED|confidentiality required", re.I), "The directory requires LDAPS or signing; switch the utility to ldaps on 636."),
]


def _text(value):
    return html.unescape(value or "").strip()


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
    raw = {"onb_ldap": """{{ onb_ldap }}"""}
    return {"ldap": _text(raw["onb_ldap"])}


def _resolve_ldap(ref):
    from utilities.models import LDAPUtility

    utilities = list(LDAPUtility.objects.all().order_by("id"))
    if not utilities:
        return None, "No LDAP utility exists yet; create one in the UI (Admin > LDAP Utilities > Add)."
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


def run(job, *args, **kwargs):
    profile = _caller(job, kwargs)
    if profile is None:
        return _failure("No calling user profile was supplied. Run this tool through the MCP server as an authenticated user.")
    if not profile.is_cbadmin:
        return _failure("Only CloudBolt admins may verify LDAP utilities.")

    utility, error = _resolve_ldap(_read_inputs()["ldap"])
    if error:
        return _failure(error)
    base = _portal_url()
    outputs = {
        "ldapUtility": {
            "id": getattr(utility, "global_id", None),
            "pk": utility.id,
            "name": getattr(utility, "name", None) or utility.ip,
            "server": utility.ip,
            "port": utility.port,
            "protocol": utility.protocol,
            "serviceAccount": getattr(utility, "serviceaccount", None),
        },
        "links": {
            "detail": base + "/admin/ldap_settings/{}/".format(utility.id),
            "edit": base + "/admin/ldap_settings/{}/edit/".format(utility.id),
            "verifyDialog": base + "/admin/ldap_settings/{}/verify_connection/".format(utility.id),
            "mappings": base + "/admin/ldap_settings/{}/group_mappings/add/".format(utility.id),
        },
    }
    try:
        utility.verify_connection()
        outputs["verified"] = True
        outputs["next"] = "Map an AD group to a CloudBolt group with onb_ldap_mapping, then have a directory user log in."
        return _success("LDAP utility '{}' verified.".format(outputs["ldapUtility"]["name"]), outputs)
    except Exception as exc:
        error_text = "{}: {}".format(type(exc).__name__, str(exc))[:3000]
        hints = [hint for pattern, hint in HINTS if pattern.search(error_text)]
        outputs.update({"verified": False, "error": error_text, "hints": hints or ["Compare the server, port, protocol, base DN and service account on the edit page with the directory's values."]})
        outputs["next"] = "Explain the error and the first hint; credentials are fixed on links.edit (never in chat). Then run this tool again."
        return _success("LDAP utility '{}' failed: {}".format(outputs["ldapUtility"]["name"], error_text[:300]), outputs)
