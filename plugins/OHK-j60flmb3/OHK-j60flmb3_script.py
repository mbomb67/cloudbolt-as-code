"""
POC Onboarding: Install (MCP Tool Action plugin).

Finishes the toolkit install after a Source Control Repos sync or a
Content Package import: turns Synchronous Action on for the tools that
answer inline (the importer leaves it off and the REST API refuses to set
it, CMPITK-1391), makes sure every tool is enabled, checks outbound access
to the Content Library and to GitHub (where the playbook text lives), and
records the toolkit version so onb_status can report it. Idempotent: run it
after every toolkit sync. Runs as a job (asynchronous) so it works before
any Synchronous flag is set. Admin only.

Every declared input is read from its rendered template token in
_read_inputs(); keep each one referenced that way or CloudBolt's token scan
deletes the input when the script is saved.
"""
import html
import json
from datetime import datetime, timezone

TOOLKIT_VERSION = "0.1.0"
STATE_NAMESPACE = "poc_onboarding"
STATE_FIELD = "poc_onboarding_state"

# mcp_tool_name -> should answer inline (True) or run as a job (False).
TOOLS = {
    "onb_status": True,
    "onb_ui_link": True,
    "onb_recent_errors": True,
    "onb_failure_trace": True,
    "onb_playbook": True,
    "onb_set_item_status": True,
    "onb_verify_resource_handler": False,
    "onb_install": False,
    # Reused testing tools that ship in the same package.
    "sync_from_source_control_repo": True,
    "fetch_job_log": True,
    "manage_blueprint_permissions": True,
}
PLAYBOOK_URL = "https://raw.githubusercontent.com/mbomb67/cloudbolt-as-code/main/.claude/skills/cb-poc-onboarding/SKILL.md"


def _text(value):
    return html.unescape(value or "").strip()


def _failure(message):
    return {
        "status": "FAILURE",
        "output_message": "",
        "error_message": message,
        "outputs": {"error": message},
    }


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


def _progress(job, message):
    if job is None:
        return
    try:
        from common.methods import set_progress

        set_progress(message)
    except Exception:
        pass


def _read_inputs():
    raw = {"onb_action": """{{ onb_action }}"""}
    return {"action": (_text(raw["onb_action"]) or "install").lower()}


def _state_field(create=False):
    from infrastructure.models import CustomField, Namespace

    if not create:
        return CustomField.objects.filter(name=STATE_FIELD).first()
    namespace, _ = Namespace.objects.get_or_create(name=STATE_NAMESPACE)
    field, _ = CustomField.objects.get_or_create(
        name=STATE_FIELD,
        defaults=dict(
            label="POC Onboarding State",
            description="State recorded by the POC onboarding MCP tools: item statuses, handler verify results, install details.",
            type="TXT",
            namespace=namespace,
        ),
    )
    return field


def _update_state(mutate):
    from django.db import transaction
    from infrastructure.models import CustomField
    from orders.models import CustomFieldValue

    field = _state_field(create=True)
    with transaction.atomic():
        list(CustomField.objects.select_for_update().filter(pk=field.pk))
        cfv = CustomFieldValue.objects.filter(field=field).order_by("id").first()
        if cfv is None:
            cfv = CustomFieldValue(field=field)
            state = {}
        else:
            try:
                state = json.loads(cfv.txt_value or "")
            except ValueError:
                state = {}
            if not isinstance(state, dict):
                state = {}
        mutate(state)
        cfv.value = json.dumps(state, indent=2)
        cfv.save()
    return state


def _egress():
    checks = {}
    try:
        from utilities.helpers import cb_request
    except Exception as exc:
        return {"error": str(exc)[:200]}
    try:
        from utilities.cb_http import get_content_library_connection

        ci = get_content_library_connection("blueprints", raise_error=False)
        if ci is None:
            checks["contentLibrary"] = {"reachable": None, "note": "No Content Library connection is defined."}
        else:
            url = "{}://{}:{}/".format(ci.protocol or "https", ci.ip, ci.port or 443)
            try:
                response = cb_request("HEAD", url, timeout=8)
                checks["contentLibrary"] = {"reachable": True, "url": url, "httpStatus": response.status_code}
            except Exception as exc:
                checks["contentLibrary"] = {"reachable": False, "url": url, "error": str(exc)[:300]}
    except Exception as exc:
        checks["contentLibrary"] = {"reachable": None, "error": str(exc)[:300]}
    try:
        response = cb_request("HEAD", PLAYBOOK_URL, timeout=8)
        row = {"reachable": True, "url": PLAYBOOK_URL, "httpStatus": response.status_code}
        if response.status_code == 404:
            row["published"] = False
            row["note"] = "GitHub is reachable but the skill is not published on the main branch yet; onb_playbook needs onb_ref until it is."
        else:
            row["published"] = response.status_code == 200
        checks["playbook"] = row
    except Exception as exc:
        checks["playbook"] = {"reachable": False, "url": PLAYBOOK_URL, "error": str(exc)[:300]}
    return checks


def run(job, *args, **kwargs):
    profile = _caller(job, kwargs)
    if profile is None:
        return _failure("No calling user profile was supplied. Run this tool through the MCP server as an authenticated user.")
    if not profile.is_cbadmin:
        return _failure("Only CloudBolt admins may install the onboarding toolkit.")

    action = _read_inputs()["action"]
    if action not in ("install", "status"):
        return _failure("onb_action must be 'install' (default) or 'status'.")

    from cbhooks.models import MCPToolAction

    base = _portal_url()
    present = {a.mcp_tool_name: a for a in MCPToolAction.objects.filter(mcp_tool_name__in=list(TOOLS))}
    rows = {}
    changed = []
    for name, wants_sync in TOOLS.items():
        tool = present.get(name)
        if tool is None:
            rows[name] = {"present": False}
            continue
        before = {"enabled": bool(tool.enabled), "isSynchronous": bool(tool.is_synchronous)}
        if action == "install":
            updates = []
            if bool(tool.is_synchronous) != wants_sync:
                tool.is_synchronous = wants_sync
                updates.append("isSynchronous={}".format(wants_sync))
            if not tool.enabled:
                tool.enabled = True
                updates.append("enabled=True")
            if updates:
                tool.save()
                changed.append({"tool": name, "changes": updates})
        rows[name] = {
            "present": True,
            "id": tool.global_id,
            "enabled": bool(tool.enabled),
            "isSynchronous": bool(tool.is_synchronous),
            "wantsSynchronous": wants_sync,
            "before": before,
            "editUrl": base + "/actions/mcp_tool_actions/{}/edit/".format(tool.id),
        }
    missing = [name for name, row in rows.items() if not row["present"]]

    _progress(job, "Checking outbound access to the Content Library and GitHub")
    egress = _egress()

    outputs = {
        "action": action,
        "toolkitVersion": TOOLKIT_VERSION,
        "tools": rows,
        "missing": missing,
        "changed": changed,
        "egress": egress,
        "mcpToolActionsUrl": base + "/actions/mcp_tool_actions/",
    }
    if action == "install":
        try:
            when = datetime.now(timezone.utc).isoformat()

            def mutate(state):
                state["install"] = {
                    "toolkitVersion": TOOLKIT_VERSION,
                    "when": when,
                    "by": getattr(getattr(profile, "user", None), "username", None),
                    "missing": missing,
                    "egress": egress,
                }

            _update_state(mutate)
        except Exception as exc:
            outputs["stateNote"] = "Could not record the install: {}".format(str(exc)[:200])
    notes = []
    if changed:
        notes.append(
            "Synchronous Action was changed on {} tool(s). Reconnect the MCP client now: tool lists and "
            "behaviour are cached per session.".format(len(changed))
        )
    if missing:
        notes.append(
            "Missing tools: {}. Sync them from the repository (mcp_tool_actions folders) or import the "
            "POC Onboarding Toolkit content package, then run onb_install again.".format(", ".join(missing))
        )
    if egress.get("playbook", {}).get("reachable") is False:
        notes.append("GitHub is not reachable from the appliance, so onb_playbook will fail; read the skill from the repository instead.")
    if egress.get("contentLibrary", {}).get("reachable") is False:
        notes.append("The Content Library is not reachable; the extensibility phase will need the proxy or SSL settings fixed first.")
    outputs["notes"] = notes
    message = "Toolkit {}: {} present, {} missing, {} changed.".format(
        TOOLKIT_VERSION, len(TOOLS) - len(missing), len(missing), len(changed)
    )
    return _success(message, outputs)
