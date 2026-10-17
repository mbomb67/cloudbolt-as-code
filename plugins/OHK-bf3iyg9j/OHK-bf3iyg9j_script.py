"""
CloudBolt inbound webhook plug-in: Server Tasks (webhooks/IWH-cdf543g3).

Serves the Run Task custom form (forms/FRM-a5edrdj4) of the Run Task server
action (server_actions/SVA-4sin3s0q). Logic lives in the server_tasks shared
module (shared_modules/SHM-571l7249).

URL (trailing slash required): /api/v3/cmp/inboundWebHooks/server-tasks/run/

GET ?source=tasks&server=<id>[&servers=<ids>]
    The tasks that fit the server, or every server of a bulk run:
    {"options": [{"value": "<OHK- global ID>", "title": "<name>"}, ...]}.
GET ?source=inputs&server=<id>[&servers=<ids>]&task=<value>
    The chosen task's inputs as a Dynamic Panel template, and whether a
    server needs credentials:
    {"panel": {title, description, elements, omitted, blocking},
     "credentials": {"needed": [hostnames], "can_save": bool}}.
POST {"source": "credentials", "server": <id>, "username": ..., "password": ...}
    Saves a username and password on the server, as CloudBolt's own
    credential prompt does (needs Manage Credentials on that server):
    {"saved": true}. This is how the form keeps the password out of the
    action's payload, which CloudBolt writes to the job log.

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


def inbound_web_hook_get(*args, parameters=None, profile=None, **kwargs):
    if profile is None:
        return _error(403, "Sign in to CloudBolt to use this form.")
    source = _param(parameters, "source")
    try:
        servers = _servers(parameters, profile)
        if source == "tasks":
            options = server_tasks.task_options(servers)
            return {"options": [{"value": value, "title": title} for value, title in options]}
        if source == "inputs":
            task = _param(parameters, "task")
            if not task:
                return {"panel": None, "credentials": {"needed": [], "can_save": False}}
            hook = server_tasks.resolve_task(task)
            server_tasks.check_task(hook, servers)
            needed = [server for server in servers if server_tasks.needs_credentials(hook, server)]
            can_save = len(servers) == 1 and bool(needed) and server_tasks.can_manage_credentials(profile, servers[0])
            return {
                "panel": server_tasks.task_panel(hook),
                "credentials": {"needed": [server.hostname for server in needed], "can_save": can_save},
            }
    except server_tasks.TaskError as err:
        return _error(400, str(err))
    return _error(400, "source must be tasks or inputs.")


def inbound_web_hook_post(*args, parameters=None, profile=None, **kwargs):
    if profile is None:
        return _error(403, "Sign in to CloudBolt to use this form.")
    if _param(parameters, "source") != "credentials":
        return _error(400, "source must be credentials.")
    try:
        server = server_tasks.resolve_server(_param(parameters, "server"), profile)
        if not server_tasks.can_manage_credentials(profile, server):
            return _error(403, f"You do not have permission to manage the credentials of {server.hostname}.")
        server_tasks.save_credentials(
            server, profile, parameters.get("username"), parameters.get("password")
        )
    except server_tasks.TaskError as err:
        return _error(400, str(err))
    logger.info("Run Task saved credentials on %s for %s", server.hostname, profile)
    return {"saved": True, "server": server.hostname}
