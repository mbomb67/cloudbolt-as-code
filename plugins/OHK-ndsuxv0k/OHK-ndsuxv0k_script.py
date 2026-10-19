"""
POC Onboarding: Action Approval (MCP Tool Action plugin).

Turns "Requires approval" on or off for a server action or resource
action (for example Resize VM), so a day-2 action from a requestor pauses
as an order for an approver (test plan 2.4.2). The v3 API offers no way to
change it, hence this tool. Synchronous. Admin only.

Every declared input is read from its rendered template token in
_read_inputs(); keep each one referenced that way or CloudBolt's token scan
deletes the input when the script is saved.
"""
import html
import re

GLOBAL_ID_RE = re.compile(r"^[A-Z]{2,4}-[A-Za-z0-9]{6,16}$")


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
        "onb_action": """{{ onb_action }}""",
        "onb_requires_approval": """{{ onb_requires_approval }}""",
    }
    return {
        "action": _text(raw["onb_action"]),
        "requires_approval": _text(raw["onb_requires_approval"]).lower(),
    }


def _resolve_action(ref):
    from cbhooks.models import ResourceAction, ServerAction

    for model, kind in ((ServerAction, "server action"), (ResourceAction, "resource action")):
        action = None
        if GLOBAL_ID_RE.match(ref):
            action = model.objects.filter(global_id=ref).first()
        if action is None:
            matches = list(model.objects.filter(label__iexact=ref))
            if len(matches) > 1:
                return None, None, "Several {}s are labelled '{}'; pass the global id.".format(kind, ref)
            action = matches[0] if matches else None
        if action is not None:
            return action, kind, None
    sample = ", ".join(a.label for a in ServerAction.objects.all().order_by("label")[:25])
    return None, None, "No server or resource action matches '{}'. Server actions include: {}.".format(ref, sample)


def run(job, *args, **kwargs):
    profile = _caller(job, kwargs)
    if profile is None:
        return _failure("No calling user profile was supplied. Run this tool through the MCP server as an authenticated user.")
    if not profile.is_cbadmin:
        return _failure("Only CloudBolt admins may change action approval settings.")

    inputs = _read_inputs()
    if not inputs["action"]:
        return _failure("onb_action is required: a server or resource action global id (SVA-, RSA-) or its label, for example Resize VM.")
    if inputs["requires_approval"] not in ("true", "false", "1", "0", "yes", "no", "on", "off", ""):
        return _failure("onb_requires_approval must be true or false.")
    wanted = inputs["requires_approval"] in ("true", "1", "yes", "on", "")
    action, kind, error = _resolve_action(inputs["action"])
    if error:
        return _failure(error)

    before = bool(action.requires_approval)
    if before != wanted:
        action.requires_approval = wanted
        action.save()
    base = _portal_url()
    try:
        url = base + action.get_absolute_url()
    except Exception:
        url = None
    outputs = {
        "action": {"id": action.global_id, "label": action.label, "kind": kind, "enabled": bool(getattr(action, "enabled", True)), "url": url},
        "requiresApproval": {"before": before, "after": bool(action.requires_approval)},
        "next": (
            "Have a user without the approver role run the action (run_server_action or run_resource_action); the call "
            "returns an order id instead of a job id, and fetch_orders_pending_my_approval shows it to an approver."
            if wanted else "The action now runs without approval."
        ),
    }
    message = "'{}' requires approval: {} -> {}.".format(action.label, before, bool(action.requires_approval))
    return _success(message, outputs)
