"""
CloudBolt inbound webhook plug-in: Server Workflows (webhooks/IWH-xjlrmvc8).

Serves the Run Workflow custom form (forms/FRM-ecw50rd0) of the Run Workflow
server action (server_actions/SVA-j7rbsh39). Logic lives in the
server_workflows shared module (shared_modules/SHM-nznzhpp0).

URL (trailing slash required): /api/v3/cmp/inboundWebHooks/server-workflows/run/

GET ?source=workflows&server=<id>[&servers=<ids>]
    The enabled workflows offered on the server, or on every server of a
    bulk run: {"options": [{"value": "<SWF- id>", "title": "<name>"}, ...]}.
GET ?source=prompts&server=<id>[&servers=<ids>]&workflow=<value>
    The chosen workflow's steps and the inputs it asks for, as a Dynamic Panel
    template, plus what blocks it on these servers and whether a server needs
    credentials for a script step:
    {"panel": {title, description, elements, steps, blocking},
     "credentials": {"needed": [hostnames], "can_save": bool}}.
POST {"source": "credentials", "server": <id>, "username": ..., "password": ...}
    Saves a username and password on the server, as CloudBolt's own
    credential prompt does (needs Manage Credentials on that server):
    {"saved": true}. This keeps the password out of the action's payload,
    which CloudBolt writes to the job log.

server is the form's object_id (the server's numeric id; an SVR- global ID
also works). servers is the form's object_ids (a JSON list of ids, or a
comma-separated list). Errors answer 400 or 403 with {"error": "..."} plus an
empty "options" list, so a failure shows in the form without breaking it.

Authentication: 'normal' mode, so the form's same-origin request carries the
user's session (and, for POST, the page's CSRF token) and this plug-in gets the
caller's UserProfile. The webhook endpoint itself checks no permissions, so
this plug-in does: every server must be one the caller can see, and saving
credentials needs Manage Credentials on the server.
"""
import json
import re

from utilities.logger import ThreadLogger

from shared_modules import server_tasks
from shared_modules import server_workflows as swf

logger = ThreadLogger(__name__)


def _error(status, message):
    return {"iwh_status_code": status, "iwh_embedded_response": {"options": [], "error": message}}


def _param(parameters, name):
    value = parameters.get(name) if parameters is not None else None
    if isinstance(value, list):
        value = value[0] if value else None
    return "" if value is None else str(value).strip()


def _servers(parameters, profile):
    """The server in context plus any others of a bulk run, each visible to profile."""
    values = []
    first = _param(parameters, "server") or _param(parameters, "object_id")
    if first:
        values.append(first)
    many = _param(parameters, "servers") or _param(parameters, "object_ids")
    if many:
        try:
            parsed = json.loads(many)
            items = parsed if isinstance(parsed, list) else [parsed]
        except ValueError:
            items = re.split(r"[\s,]+", many)
        values.extend(str(item).strip() for item in items if str(item).strip())
    if not values:
        raise server_tasks.TaskError("The form did not send the server's id (object_id).")
    servers, seen = [], set()
    for value in values:
        server = server_tasks.resolve_server(value, profile)
        if server.id not in seen:
            seen.add(server.id)
            servers.append(server)
    return servers


def _credentials(workflow, servers, profile):
    """Which servers CloudBolt cannot log in to for a script step, and whether the caller can fix that here."""
    hooks = [step.hook for step in swf.load_steps(workflow) if step.kind == "script"]
    needed = [server for server in servers if any(server_tasks.needs_credentials(hook, server) for hook in hooks)]
    can_save = len(servers) == 1 and bool(needed) and server_tasks.can_manage_credentials(profile, servers[0])
    return {"needed": [server.hostname for server in needed], "can_save": can_save}


def inbound_web_hook_get(*args, parameters=None, profile=None, **kwargs):
    if profile is None:
        return _error(403, "Sign in to CloudBolt to use this form.")
    source = _param(parameters, "source")
    try:
        servers = _servers(parameters, profile)
        if source == "workflows":
            options = swf.workflow_options(servers)
            return {"options": [{"value": value, "title": title} for value, title in options]}
        if source == "prompts":
            value = _param(parameters, "workflow")
            if not value:
                return {"panel": None, "credentials": {"needed": [], "can_save": False}}
            workflow = swf.resolve_workflow(value)
            refusals = [swf.workflow_refusal(workflow, server) for server in servers]
            refusals = [reason for reason in refusals if reason]
            if refusals:
                raise swf.WorkflowError(" ".join(refusals))
            return {
                "panel": swf.prompt_panel(workflow, servers, profile=profile),
                "credentials": _credentials(workflow, servers, profile),
            }
    except (server_tasks.TaskError, swf.WorkflowError) as err:
        return _error(400, str(err))
    return _error(400, "source must be workflows or prompts.")


def inbound_web_hook_post(*args, parameters=None, profile=None, **kwargs):
    if profile is None:
        return _error(403, "Sign in to CloudBolt to use this form.")
    if _param(parameters, "source") != "credentials":
        return _error(400, "source must be credentials.")
    try:
        server = server_tasks.resolve_server(_param(parameters, "server"), profile)
        if not server_tasks.can_manage_credentials(profile, server):
            return _error(403, f"You do not have permission to manage the credentials of {server.hostname}.")
        server_tasks.save_credentials(server, profile, parameters.get("username"), parameters.get("password"))
    except server_tasks.TaskError as err:
        return _error(400, str(err))
    logger.info("Run Workflow saved credentials on %s for %s", server.hostname, profile)
    return {"saved": True, "server": server.hostname}
