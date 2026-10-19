"""
POC Onboarding: SMTP Settings (MCP Tool Action plugin).

Sets the non-secret email settings (host, port, user, TLS, from address,
CloudBolt admin email, email on order completion) and returns the link to
the Email Settings page where the SMTP password is entered. Never accepts
a password. With no inputs it reports the current settings. Synchronous.
Admin only.

Every declared input is read from its rendered template token in
_read_inputs(); keep each one referenced that way or CloudBolt's token scan
deletes the input when the script is saved.
"""
import html


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
    raw = {
        "onb_smtp_host": """{{ onb_smtp_host }}""",
        "onb_smtp_port": """{{ onb_smtp_port }}""",
        "onb_smtp_user": """{{ onb_smtp_user }}""",
        "onb_smtp_tls": """{{ onb_smtp_tls }}""",
        "onb_smtp_from": """{{ onb_smtp_from }}""",
        "onb_smtp_admin_email": """{{ onb_smtp_admin_email }}""",
    }
    return {
        "host": _text(raw["onb_smtp_host"]),
        "port": _text(raw["onb_smtp_port"]),
        "user": _text(raw["onb_smtp_user"]),
        "tls": _text(raw["onb_smtp_tls"]).lower(),
        "from_address": _text(raw["onb_smtp_from"]),
        "admin_email": _text(raw["onb_smtp_admin_email"]),
    }


def run(job, *args, **kwargs):
    profile = _caller(job, kwargs)
    if profile is None:
        return _failure("No calling user profile was supplied. Run this tool through the MCP server as an authenticated user.")
    if not profile.is_cbadmin:
        return _failure("Only CloudBolt admins may change email settings.")

    inputs = _read_inputs()
    from utilities.models import GlobalPreferences

    gp = GlobalPreferences.get()
    changes = []
    if inputs["host"]:
        if gp.smtp_host != inputs["host"]:
            gp.smtp_host = inputs["host"]
            changes.append("smtp_host")
    if inputs["port"]:
        try:
            port = int(inputs["port"])
        except ValueError:
            return _failure("onb_smtp_port must be a number (25, 465 or 587 usually).")
        if gp.smtp_port != port:
            gp.smtp_port = port
            changes.append("smtp_port")
    if inputs["user"]:
        if gp.smtp_user != inputs["user"]:
            gp.smtp_user = inputs["user"]
            changes.append("smtp_user")
    if inputs["tls"] in ("true", "false", "1", "0", "yes", "no"):
        wanted = inputs["tls"] in ("true", "1", "yes")
        if bool(gp.smtp_use_tls) != wanted:
            gp.smtp_use_tls = wanted
            changes.append("smtp_use_tls={}".format(wanted))
    if inputs["from_address"]:
        if gp.email_default_from_address != inputs["from_address"]:
            gp.email_default_from_address = inputs["from_address"]
            changes.append("email_default_from_address")
    if inputs["admin_email"]:
        if gp.cbadmin_email != inputs["admin_email"]:
            gp.cbadmin_email = inputs["admin_email"]
            changes.append("cbadmin_email")
    if changes:
        gp.save()

    base = _portal_url()
    password_set = bool(gp.smtp_password)
    outputs = {
        "settings": {
            "host": gp.smtp_host or None,
            "port": gp.smtp_port or None,
            "user": gp.smtp_user or None,
            "useTls": bool(gp.smtp_use_tls),
            "fromAddress": gp.email_default_from_address or None,
            "adminEmail": gp.cbadmin_email or None,
            "emailOnOrderCompletion": bool(getattr(gp, "email_on_order_completion", False)),
            "passwordSet": password_set,
        },
        "changes": changes,
        "editUrl": base + "/admin/email_settings/edit/",
        "testUrl": base + "/admin/email_test/",
        "next": (
            "If the relay needs a password, send the user editUrl to enter it (never in chat). Then onb_smtp_test "
            "sends a test message with the saved settings."
            if not password_set else "Run onb_smtp_test to send a test message with the saved settings."
        ),
    }
    if not gp.smtp_host:
        outputs["note"] = "No SMTP host is set. Without a relay, record prerequisite P7 as not applicable; approval and completion emails will not be sent."
    message = "SMTP {}; {} change(s).".format("configured for {}".format(gp.smtp_host) if gp.smtp_host else "not configured", len(changes))
    return _success(message, outputs)
