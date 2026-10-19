"""
POC Onboarding: SMTP Test (MCP Tool Action plugin).

Sends a test email through the saved SMTP settings, the same as the Email
Settings page's test button, and returns the relay's real error when it
fails. Runs as a job because it talks to the mail server. Admin only.

Every declared input is read from its rendered template token in
_read_inputs(); keep each one referenced that way or CloudBolt's token scan
deletes the input when the script is saved.
"""
import html
import re
from datetime import datetime, timezone

HINTS = [
    (re.compile(r"authentication failed|535|Username and Password not accepted", re.I), "The relay rejected the SMTP user or password; re-enter the password on the Email Settings page."),
    (re.compile(r"Connection refused|timed out|Network is unreachable|getaddrinfo", re.I), "The appliance cannot reach the relay on that port; check the host, port and outbound firewall rule."),
    (re.compile(r"STARTTLS|SSL|certificate", re.I), "TLS negotiation failed; toggle Use TLS or check the relay's certificate."),
    (re.compile(r"sender address rejected|553|550", re.I), "The relay refused the from address; use an address the relay is allowed to send as."),
    (re.compile(r"No .*Email specified|cbadmin_email", re.I), "Set the CloudBolt admin email (onb_smtp with onb_smtp_admin_email) or pass onb_smtp_to."),
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
    raw = {"onb_smtp_to": """{{ onb_smtp_to }}"""}
    return {"to": _text(raw["onb_smtp_to"])}


def run(job, *args, **kwargs):
    profile = _caller(job, kwargs)
    if profile is None:
        return _failure("No calling user profile was supplied. Run this tool through the MCP server as an authenticated user.")
    if not profile.is_cbadmin:
        return _failure("Only CloudBolt admins may send test emails.")

    from utilities.models import GlobalPreferences

    gp = GlobalPreferences.get()
    recipient = _read_inputs()["to"] or gp.cbadmin_email or getattr(getattr(profile, "user", None), "email", "")
    base = _portal_url()
    outputs = {
        "recipient": recipient or None,
        "settings": {"host": gp.smtp_host or None, "port": gp.smtp_port or None, "user": gp.smtp_user or None, "useTls": bool(gp.smtp_use_tls), "passwordSet": bool(gp.smtp_password)},
        "editUrl": base + "/admin/email_settings/edit/",
    }
    if not gp.smtp_host:
        return _failure("No SMTP host is configured; set it with onb_smtp (and the password on the Email Settings page) first.")
    if not recipient:
        return _failure("No recipient: pass onb_smtp_to or set the CloudBolt admin email with onb_smtp.")

    try:
        from utilities.mail import email

        when = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        email(
            recipients=recipient,
            context={"subject": "CloudBolt email test", "message": "This test email was sent from CloudBolt on {} by the POC onboarding toolkit.".format(when)},
        )
        outputs["sent"] = True
        outputs["next"] = "Ask the user to confirm the message arrived; then record prerequisite P7 as done."
        return _success("Test email sent to {}.".format(recipient), outputs)
    except Exception as exc:
        error_text = "{}: {}".format(type(exc).__name__, str(exc))[:2000]
        hints = [hint for pattern, hint in HINTS if pattern.search(error_text)]
        outputs.update({"sent": False, "error": error_text, "hints": hints or ["Check host, port, TLS and credentials on the Email Settings page, then onb_recent_errors with onb_contains=smtp."]})
        return _failure("Test email to {} failed: {}".format(recipient, error_text[:300]))
