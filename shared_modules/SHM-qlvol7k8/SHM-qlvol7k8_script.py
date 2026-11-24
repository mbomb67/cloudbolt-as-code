"""Escalate a failed automatic remediation: open a ServiceNow incident and post to Microsoft Teams.

An alert remediation action calls escalate() when its fix did not work. Both halves are best effort
and independent: the incident is opened first so the Teams card can carry its number, and neither one
failing, or not being configured, stops the other or raises.

    from shared_modules import alert_escalation

    result = alert_escalation.escalate(server, alert, detail, job=job)
    # {"incident": {"number", "sys_id", "link"} or None, "teams": True or False, "notes": [str, ...]}

ServiceNow uses the connection the servicenow_requests module resolves (a ConnectionInfo or ITSM
integration named "ServiceNow"). Teams uses the ConnectionInfo named "Microsoft Teams Alerts", whose
password field holds the Workflows webhook URL.

absolute_url() turns a CloudBolt path into a link with the default portal's domain.

ServiceNow Table API (POST /api/now/table/incident):
https://www.servicenow.com/docs/bundle/yokohama-api-reference/page/integrate/inbound-rest/concept/c_TableAPI.html

Teams Workflows webhook ("When a Teams webhook request is received"; the Office 365 connector webhooks
are retired) and the Adaptive Card message it accepts:
https://learn.microsoft.com/en-us/microsoftteams/platform/webhooks-and-connectors/how-to/add-incoming-webhook
"""

import requests

from common.methods import set_progress
from shared_modules import servicenow_requests as snr
from utilities.helpers import get_ssl_verification
from utilities.logger import ThreadLogger
from utilities.models import ConnectionInfo

logger = ThreadLogger(__name__)

SERVICENOW_CONNECTION = snr.DEFAULT_CONNECTION_NAME
TEAMS_CONNECTION = "Microsoft Teams Alerts"
HTTP_TIMEOUT_S = 30
DEFAULT_ACTION = "Restart Apache"

# Teams limits a message to about 28 KB; the detail is the only unbounded part of the card.
MAX_DETAIL_CHARS = 1500


def absolute_url(path):
    """A CloudBolt path with the default portal's domain in front; the path alone when the portal has none."""
    path = path or ""
    if path.startswith(("http://", "https://")):
        return path
    try:
        from portals.models import PortalConfig

        # With no request this is the default portal (portals/models.py get_current_portal).
        portal = PortalConfig.get_current_portal()
        base = ((portal.site_url if portal else None) or "").rstrip("/")
    except Exception:
        base = ""
    return base + path


def _object_url(obj):
    try:
        return absolute_url(obj.get_absolute_url())
    except Exception:
        return ""


def _tail(text, limit=MAX_DETAIL_CHARS):
    """The last limit characters of text: the status and journal lines a diagnostic ends with."""
    text = (text or "").strip()
    return text if len(text) <= limit else "..." + text[-(limit - 3):].lstrip()


def _alert_name(alert):
    return str(alert.get("alertname") or (alert.get("labels") or {}).get("alertname") or "Alert")


def _severity(alert):
    return str(alert.get("severity") or (alert.get("labels") or {}).get("severity") or "")


def _hostname(server):
    return getattr(server, "hostname", "") or str(server)


def _note(notes, message, job):
    notes.append(message)
    if job is not None:
        try:
            set_progress(message, job)
        except Exception:
            logger.info(message)
    else:
        logger.info(message)


def _incident_description(server, alert, detail, job, action):
    lines = []
    if alert.get("summary"):
        lines.append(str(alert["summary"]))
    if alert.get("description"):
        lines.append(str(alert["description"]))
    lines.append("")
    lines.append(
        f"CloudBolt received this alert for {_hostname(server)} and ran '{action}', "
        "but the server was still unhealthy afterwards."
    )
    if alert.get("startsAt"):
        lines.append(f"Alert started: {alert['startsAt']}")
    if alert.get("generatorURL"):
        lines.append(f"Alert source: {alert['generatorURL']}")
    if detail:
        lines += ["", "Diagnostics from the server:", _tail(detail, 4000)]
    links = [("CloudBolt server", _object_url(server)), ("CloudBolt job", _object_url(job) if job else "")]
    links = [f"{label}: {url}" for label, url in links if url]
    if links:
        lines += [""] + links
    return "\n".join(lines).strip()


def _open_incident(server, alert, detail, job, action):
    """Insert the incident; returns {"number", "sys_id", "link"}. Raises on any failure."""
    severity = _severity(alert).lower()
    level = "1" if severity == "critical" else "2"
    fields = {
        "short_description": f"{_alert_name(alert)} on {_hostname(server)}: automatic remediation failed",
        "description": _incident_description(server, alert, detail, job, action),
        "urgency": level,
        "impact": level,
        "correlation_id": str(alert.get("fingerprint") or getattr(job, "global_id", "") or ""),
    }
    fingerprint = str(alert.get("fingerprint") or "").replace("^", "")
    with snr.ServiceNowRequestClient(SERVICENOW_CONNECTION) as snow:
        if fingerprint:
            # query(table, encoded_query, fields, limit) in servicenow_requests; an open incident for this
            # alert is reused so a repeating alert does not open one incident per attempt.
            open_ones = snow.query(
                "incident", f"correlation_id={fingerprint}^active=true", fields=("number", "sys_id"), limit=1
            )
            if open_ones:
                sys_id = open_ones[0].get("sys_id", "")
                job_url = _object_url(job) if job else ""
                note = (
                    f"The alert is still firing and CloudBolt's automatic remediation ({action}) failed again on "
                    f"{_hostname(server)}." + (f" CloudBolt job: {job_url}" if job_url else "")
                )
                try:
                    snow.update("incident", sys_id, {"work_notes": note})
                except Exception as err:
                    logger.info(f"The work note was not added to incident {open_ones[0].get('number')}: {err}")
                link = f"{snow.base_url}/nav_to.do?uri=incident.do?sys_id={sys_id}"
                return {"number": open_ones[0].get("number", ""), "sys_id": sys_id, "link": link, "existing": True}
        try:
            ci = snow.find_server_ci(server)
        except Exception as err:
            logger.info(f"No ServiceNow CI lookup for {_hostname(server)}: {err}")
            ci = None
        if ci:
            fields["cmdb_ci"] = ci
        record = snow.insert("incident", fields)
        sys_id = record.get("sys_id", "")
        # record_link() builds RITM and REQ links only, so the incident form link is built here.
        link = f"{snow.base_url}/nav_to.do?uri=incident.do?sys_id={sys_id}" if sys_id else ""
    return {"number": record.get("number", ""), "sys_id": sys_id, "link": link}


def _incident_text(incident):
    if not incident:
        return "none opened"
    number = incident.get("number") or "opened"
    return f"{number} (already open)" if incident.get("existing") else number


def _teams_card(server, alert, detail, incident, job, action):
    facts = [
        {"title": "Alert", "value": _alert_name(alert)},
        {"title": "Server", "value": _hostname(server)},
        {"title": "Severity", "value": _severity(alert) or "unknown"},
        {"title": "Action tried", "value": action},
        {"title": "Incident", "value": _incident_text(incident)},
    ]
    body = [
        {
            "type": "TextBlock",
            "text": f"Automatic remediation failed on {_hostname(server)}",
            "weight": "Bolder",
            "size": "Medium",
            "wrap": True,
        },
        {"type": "FactSet", "facts": facts},
    ]
    if detail:
        body.append(
            {
                "type": "TextBlock",
                "text": _tail(detail).replace("\r", "").replace("\n", "\n\n"),
                "fontType": "Monospace",
                "wrap": True,
            }
        )
    buttons = [
        ("Open server in CloudBolt", _object_url(server)),
        ("Open job in CloudBolt", _object_url(job) if job else ""),
        ("Open incident", (incident or {}).get("link", "")),
    ]
    actions = [
        {"type": "Action.OpenUrl", "title": title, "url": url}
        for title, url in buttons
        if url.startswith(("http://", "https://"))
    ]
    content = {
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "type": "AdaptiveCard",
        "version": "1.2",
        "body": body,
    }
    if actions:
        content["actions"] = actions
    return {
        "type": "message",
        "attachments": [
            {"contentType": "application/vnd.microsoft.card.adaptive", "contentUrl": None, "content": content}
        ],
    }


def _post_teams(server, alert, detail, incident, job, action):
    """Post the card. Returns None on success, otherwise the note that says why it was not posted."""
    conn = ConnectionInfo.objects.filter(name=TEAMS_CONNECTION).first()
    if conn is None:
        return f"Teams is not configured (no connection named '{TEAMS_CONNECTION}')"
    url = (conn.password or "").strip()
    if not url.startswith("https://"):
        return f"Teams is not configured (the '{TEAMS_CONNECTION}' connection has no https workflow URL in its password field)"
    card = _teams_card(server, alert, detail, incident, job, action)
    try:
        response = requests.post(
            url,
            json=card,
            headers={"Content-Type": "application/json"},
            timeout=HTTP_TIMEOUT_S,
            verify=get_ssl_verification(),
        )
    except requests.RequestException as err:
        # The exception text carries the URL, which is a secret, so only its type is reported.
        return f"Teams post failed: {type(err).__name__}"
    if not 200 <= response.status_code < 300:
        return f"Teams post failed: HTTP {response.status_code}"
    return None


def escalate(server, alert, detail, job=None, action=DEFAULT_ACTION):
    """Open a ServiceNow incident, then post to Teams. Best effort: never raises.

    Returns {"incident": {"number", "sys_id", "link"} or None, "teams": True or False, "notes": [str, ...]}.
    """
    notes = []
    incident = None
    posted = False
    alert = alert if isinstance(alert, dict) else {}
    detail = detail or ""

    try:
        incident = _open_incident(server, alert, detail, job, action)
        number = incident["number"] or incident["sys_id"]
        if incident.get("existing"):
            _note(notes, f"ServiceNow incident {number} is already open; added a work note", job)
        else:
            _note(notes, f"ServiceNow incident {number} opened", job)
    except snr.ServiceNowError as err:
        _note(notes, f"ServiceNow incident not opened: {str(err)[:300]}", job)
    except Exception as err:
        logger.exception("alert_escalation: the incident was not opened")
        _note(notes, f"ServiceNow incident not opened: {type(err).__name__}: {str(err)[:300]}", job)

    try:
        problem = _post_teams(server, alert, detail, incident, job, action)
        if problem:
            _note(notes, problem, job)
        else:
            posted = True
            _note(notes, "Posted to Microsoft Teams", job)
    except Exception as err:
        # No traceback: an exception raised by requests carries the webhook URL, which is a secret.
        logger.error(f"alert_escalation: the Teams post failed: {type(err).__name__}")
        _note(notes, f"Teams post failed: {type(err).__name__}", job)

    return {"incident": incident, "teams": posted, "notes": notes}
