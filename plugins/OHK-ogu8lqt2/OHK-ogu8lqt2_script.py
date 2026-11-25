"""
CloudBolt inbound webhook plug-in: Monitoring Alerts (webhooks/IWH-i7b44zvs).

A monitoring system posts an alert here; CloudBolt maps the alert to a server and starts the remediation
server action named in REMEDIATIONS as a job. It runs in the web process, so it only starts jobs and never
waits for them; the job health-checks the fix and escalates when it did not work
(server_actions/SVA-5bg39wkr, shared_modules/SHM-qlvol7k8).

URL (trailing slash required): /api/v3/cmp/inboundWebHooks/monitoring-alerts/run/

POST the Prometheus Alertmanager webhook body (version 4):
https://prometheus.io/docs/alerting/latest/configuration/#webhook_config
    {"version": "4", "status": "firing", "alerts": [{"status": "firing", "labels": {...},
      "annotations": {...}, "startsAt": ..., "generatorURL": ..., "fingerprint": ...}]}
Only alerts[].status, labels, annotations, fingerprint, startsAt and generatorURL are read.

Answers 202 when at least one job started, otherwise 200:
    {"received": n, "started": n, "results": [{"alert", "result", "server", "server_url", "action", "job",
      "job_url"}]}
result is one of: started, already running, ignored: resolved, no remediation mapped, no matching server,
ambiguous server, not permitted to run <action> on this server, action not found or disabled,
submitted for approval, action did not start (server not eligible), ignored: too many alerts in one request
(only the first 50 alerts of a request are handled).

Authentication: the webhook ships in "token" mode, because a monitoring tool can send only a static secret:
the caller adds ?token=<the webhook's token> and no CloudBolt user is identified. The plug-in then acts as
the service account named in SERVICE_ACCOUNT. A caller that also signs in or sends a bearer token acts as
that user instead. Either way the profile sees only the servers CloudBolt shows it
(Server.objects_for_profile), may run the action only where it is permitted to, and owns the jobs, which
bounds what an alert can touch. Token mode lets a request through when the stored token is empty and the
request sends an empty ?token= (api/permissions.py, CloudBoltTokenBasedPermission), and a Source Control
Repos sync blanks the token, so an anonymous call is refused with 503 while the token is empty.
"""
import re

from accounts.models import UserProfile
from cbhooks.models import InboundWebHook, ServerAction
from cbhooks.services.server_action import ServerActionService
from infrastructure.models import Server
from jobs.models import Job
from utilities.logger import ThreadLogger

from shared_modules import alert_escalation

logger = ThreadLogger(__name__)

# alertname -> label of the server action that remediates it. Add a line to map another alert.
REMEDIATIONS = {"ApacheDown": "Restart Apache"}

# Labels that name the server, in the order they are tried; the first one that matches a server is used.
SERVER_LABELS = ("cloudbolt_server", "name", "vmi", "hostname", "host", "instance", "nodename")

ACTIVE_JOB_STATUSES = ("INIT", "PENDING", "QUEUED", "RUNNING")
MAX_TEXT = 2000
MAX_ALERTS = 50

# The CloudBolt user an anonymous (token mode) call acts as, and the webhook whose token must be set.
SERVICE_ACCOUNT = "monitoring-alerts"
WEBHOOK_PATH = "monitoring-alerts"


def _response(status, body):
    return {"iwh_status_code": status, "iwh_embedded_response": body}


def _error(status, message):
    return _response(status, {"error": message})


def _text(value):
    return "" if value is None else str(value).strip()


def _compact(alert):
    """A small JSON-safe copy of the alert for the job's arguments."""
    labels = alert.get("labels") if isinstance(alert.get("labels"), dict) else {}
    annotations = alert.get("annotations") if isinstance(alert.get("annotations"), dict) else {}
    return {
        "alertname": _text(labels.get("alertname")),
        "severity": _text(labels.get("severity")),
        "summary": _text(annotations.get("summary"))[:MAX_TEXT],
        "description": _text(annotations.get("description"))[:MAX_TEXT],
        "fingerprint": _text(alert.get("fingerprint")),
        "startsAt": _text(alert.get("startsAt")),
        "generatorURL": _text(alert.get("generatorURL")),
        "labels": {str(k): _text(v)[:MAX_TEXT] for k, v in labels.items()},
    }


def _server_refs(labels):
    """(label, value) for every label that names a server, in SERVER_LABELS order, with any :port removed from instance."""
    refs = []
    for key in SERVER_LABELS:
        value = _text(labels.get(key))
        if not value:
            continue
        if key == "instance":
            match = re.match(r"^([^:\[\]]+):\d+$", value)
            if match:
                value = match.group(1)
        refs.append((key, value))
    return refs


def _unique(servers):
    """The servers once each: a profile's visibility query can return a row twice."""
    seen = {}
    for server in servers:
        seen.setdefault(server.pk, server)
    return list(seen.values())


def _find_server(profile, labels):
    """(server, problem): the one active server the profile can see that the alert names.

    Labels are tried in order. The first one that matches exactly one server wins, one that matches
    several stops the search as ambiguous, and one that matches none moves on to the next label.
    """
    servers = Server.objects_for_profile(profile).filter(status="ACTIVE")
    for key, value in _server_refs(labels):
        if key == "cloudbolt_server":
            matches = _unique(servers.filter(global_id=value))
        else:
            matches = _unique(servers.filter(hostname__iexact=value))
            if not matches:
                matches = _unique(servers.filter(ip=value))
        if len(matches) == 1:
            return matches[0], ""
        if len(matches) > 1:
            return None, "ambiguous server"
    return None, "no matching server"


def _active_job(action, server):
    """A job of this action on this server that has not finished."""
    return (
        Job.objects.filter(
            status__in=ACTIVE_JOB_STATUSES,
            job_parameters__hookparameters__server_action=action,
            server=server,
        )
        .order_by("-id")
        .first()
    )


def _handle(alert, profile):
    """One alert's result dict."""
    result = {"alert": "", "result": ""}
    if not isinstance(alert, dict):
        result["result"] = "ignored: not an alert object"
        return result
    labels = alert.get("labels") if isinstance(alert.get("labels"), dict) else {}
    name = _text(labels.get("alertname"))
    result["alert"] = name

    if _text(alert.get("status")).lower() != "firing":
        result["result"] = "ignored: resolved"
        return result

    label = REMEDIATIONS.get(name)
    if not label:
        result["result"] = "no remediation mapped"
        return result

    server, problem = _find_server(profile, labels)
    if server is None:
        result["result"] = problem
        return result
    result["server"] = server.hostname
    result["server_url"] = alert_escalation.absolute_url(server.get_absolute_url())

    action = ServerAction.objects.filter(label=label, enabled=True).first()
    if action is None:
        result["result"] = "action not found or disabled"
        return result
    result["action"] = action.label

    # The check the server's action list and the server actions API use (cbhooks/services/server_action.py).
    allowed = ServerActionService(action).can_user_run_action_on_server(profile, [], server)
    if not allowed:
        result["result"] = f"not permitted to run {action.label} on this server"
        return result

    running = _active_job(action, server)
    if running is not None:
        result["result"] = "already running"
        result["job"] = running.global_id
        result["job_url"] = alert_escalation.absolute_url(running.get_absolute_url())
        return result

    # ServerAction.run_hook_as_job (cbhooks/models.py): keywords it does not use itself, such as alert,
    # are stored in the job's arguments and reach the plug-in's run() as keywords.
    jobs = action.run_hook_as_job(owner=profile, servers=[server], alert=_compact(alert))
    if not isinstance(jobs, list):
        # An action that requires approval returns an order, not jobs.
        result["result"] = "submitted for approval"
        return result
    if not jobs:
        result["result"] = "action did not start (server not eligible)"
        return result
    job = jobs[0]
    result["result"] = "started"
    result["job"] = job.global_id
    result["job_url"] = alert_escalation.absolute_url(job.get_absolute_url())
    return result


def inbound_web_hook_get(*args, parameters=None, profile=None, **kwargs):
    return _response(200, {"usage": "POST an Alertmanager webhook body to this URL; see the Monitoring Alerts README."})


def _anonymous_profile():
    """(profile, error response) for a call that identified no user: the service account, once a token is set."""
    hook = InboundWebHook.objects.filter(uri_path=WEBHOOK_PATH).first()
    if hook is None or not (hook.token or "").strip():
        return None, _error(503, "Set a token on the Monitoring Alerts webhook before using it.")
    profile = UserProfile.objects.filter(user__username=SERVICE_ACCOUNT, user__is_active=True).first()
    if profile is None:
        return None, _error(
            503,
            f"Create a CloudBolt user named '{SERVICE_ACCOUNT}' and give it a role that can run Restart Apache "
            "on the servers alerts may act on.",
        )
    return profile, None


def inbound_web_hook_post(*args, parameters=None, profile=None, **kwargs):
    if profile is None:
        profile, failure = _anonymous_profile()
        if failure:
            return failure
    alerts = parameters.get("alerts") if hasattr(parameters, "get") else None
    if not isinstance(alerts, list):
        return _error(400, "The body must be an Alertmanager webhook payload with an alerts list.")

    results = []
    for index, alert in enumerate(alerts):
        if index >= MAX_ALERTS:
            results.append({"alert": "", "result": "ignored: too many alerts in one request"})
            continue
        try:
            result = _handle(alert, profile)
        except Exception as err:
            logger.exception("Monitoring Alerts: an alert failed")
            result = {"alert": "", "result": f"error: {type(err).__name__}"}
        logger.info(
            f"Monitoring Alerts: {result.get('alert') or '(no alertname)'} on {result.get('server', '-')}: "
            f"{result['result']}"
        )
        results.append(result)

    started = sum(1 for r in results if r["result"] == "started")
    body = {"received": len(alerts), "started": started, "results": results}
    return _response(202 if started else 200, body)
