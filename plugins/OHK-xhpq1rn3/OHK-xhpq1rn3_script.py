"""
CloudBolt inbound webhook plugin: Aria Migration Form Options
(webhooks/IWH-xr0508nf).

GET endpoint the "Migrate Aria Automation Deployments to Resources" order form
(forms/FRM-i53vgmce) calls through choicesByUrl to fill the matrix dropdowns
that are not declared plugin inputs, so generate_options_for_* cannot serve
them.

URL (trailing slash required):
  GET /api/v3/cmp/inboundWebHooks/aria-migration-form-options/run/
      ?source=<source>&group={group}&...

Query parameters (all strings; 'filter' and 'last' are reserved by the API):
  source           aria_projects | aria_blueprint_resources |
                   cloudbolt_blueprint_tiers | cloudbolt_groups
  group            the form's group value: the relative href the standard
                   group dropdown submits ('/api/v3/cmp/groups/GRP-.../'), a
                   group global ID, pk or name. Always required; the caller
                   must be a member of that group or a cb_admin.
  aria_connection  with the aria_* sources: the id of a ConnectionInfo labeled
                   vra8 or aria (what the Aria Connection dropdown submits).
  blueprint_id     with aria_blueprint_resources: the Aria cloud template id;
                   with cloudbolt_blueprint_tiers: the CloudBolt blueprint pk
                   or global ID.

Response: {"options": [{"value": ..., "title": ...}, ...]}; the form uses
  choicesByUrl {"path": "options", "valueName": "value", "titleName": "title"}.
  A dependent source called before its controlling field has a value returns
  200 with an empty list, because SurveyJS fires every choicesByUrl on load.
  Bad parameters -> 400, anonymous or non-member caller -> 403, both with
  {"options": [], "error": "..."} as the body so a failure is visible in the
  browser's network tab without breaking the form.

Authorization: the IWH is 'normal' mode, so the form's same-origin XHR
authenticates with the user's session and the runtime passes the caller's
UserProfile as ``profile``. The IWH endpoint performs no RBAC of its own, so
this plugin checks group membership itself and only accepts ConnectionInfo
objects carrying the vra8 or aria label (same pattern as plugins/OHK-fx500o2r).
"""
import re

from django.db.models import Q

from accounts.models import Group
from servicecatalog.models import ServiceBlueprint
from utilities.logger import ThreadLogger
from utilities.models import ConnectionInfo
from shared_modules.aria_connection import AriaAutomationConnection

logger = ThreadLogger(__name__)

SOURCES = ("aria_projects", "aria_blueprint_resources",
           "cloudbolt_blueprint_tiers", "cloudbolt_groups")
GROUP_GLOBAL_ID_RE = re.compile(r"(GRP-[a-z0-9]{8,12})")
BP_GLOBAL_ID_RE = re.compile(r"(BP-[a-z0-9]{8,12})")


def _fail(status, message):
    # Non-200: return BOTH keys; anything else in the dict is dropped.
    return {"iwh_status_code": status,
            "iwh_embedded_response": {"options": [], "error": message}}


def _option(value, title=None):
    return {"value": value, "title": value if title is None else title}


def _is_cbadmin(profile):
    flag = getattr(profile, "is_cbadmin", False)
    return bool(flag() if callable(flag) else flag)


def _resolve_group(group_ref):
    """Accept a relative href, a GRP- global ID, a pk or a name."""
    if group_ref is None or group_ref == "":
        return None
    text = str(group_ref).strip()
    match = GROUP_GLOBAL_ID_RE.search(text)
    if match:
        return Group.objects.filter(global_id=match.group(1)).first()
    if "/" in text:
        text = text.rstrip("/").rsplit("/", 1)[-1]
    if text.isdigit():
        return Group.objects.filter(id=int(text)).first()
    return Group.objects.filter(name=text).first()


def _profile_may_act_for_group(profile, group):
    if profile is None or group is None:
        return False
    if _is_cbadmin(profile):
        return True
    return profile.get_groups(include_inherited=True).filter(id=group.id).exists()


def _resolve_blueprint(ref):
    if not ref:
        return None
    text = str(ref).strip()
    match = BP_GLOBAL_ID_RE.search(text)
    if match:
        return ServiceBlueprint.objects.filter(global_id=match.group(1)).first()
    if text.isdigit():
        return ServiceBlueprint.objects.filter(id=int(text)).first()
    return None


def _aria_connection(ref):
    """Return an Aria client for a ConnectionInfo id, None while the form has
    not selected one yet, or raise when the id is not an Aria connection."""
    if not ref:
        return None
    text = str(ref).strip()
    if not text.isdigit():
        raise ValueError("aria_connection must be a ConnectionInfo id.")
    exists = ConnectionInfo.objects.filter(
        Q(labels__name="vra8") | Q(labels__name="aria"), id=int(text)
    ).exists()
    if not exists:
        raise ValueError(f"ConnectionInfo {text} is not labeled vra8 or aria.")
    return AriaAutomationConnection(int(text))


def _group_options(profile):
    if _is_cbadmin(profile):
        groups = Group.objects.all()
    else:
        groups = profile.get_groups(include_inherited=True)
    return [_option(g.id, g.name) for g in groups.order_by("name")]


def _blueprint_tier_options(bp):
    # Only server tiers and sub-blueprint tiers can receive migrated
    # deployment resources (mirrors validate_deployment_map in the plugin).
    items = bp.serviceitem_set.filter(
        Q(blueprintserviceitem__isnull=False)
        | Q(provisionserverserviceitem__isnull=False)
    )
    return [_option(si.name) for si in items.order_by("name")]


def _aria_project_options(vra):
    return [_option(pid, name) for pid, name in vra.get_project_options()]


def _aria_blueprint_resource_options(vra, blueprint_id):
    content = vra.get_blueprint_content_as_dict(blueprint_id) or {}
    resources = content.get("resources") or {}
    return [_option(name) for name in resources.keys()]


def inbound_web_hook_get(*args, parameters=None, profile=None, **kwargs):
    parameters = parameters or {}
    if profile is None:
        return _fail(403, "Authentication required.")
    source = parameters.get("source")
    if source not in SOURCES:
        return _fail(400, f"source must be one of: {', '.join(SOURCES)}.")
    group = _resolve_group(parameters.get("group"))
    if group is None or not _profile_may_act_for_group(profile, group):
        return _fail(403, "Not a member of that group.")
    try:
        if source == "cloudbolt_groups":
            return {"options": _group_options(profile)}
        if source == "cloudbolt_blueprint_tiers":
            bp = _resolve_blueprint(parameters.get("blueprint_id"))
            if bp is None:
                return {"options": []}
            return {"options": _blueprint_tier_options(bp)}
        vra = _aria_connection(parameters.get("aria_connection"))
        if vra is None:
            return {"options": []}
        if source == "aria_projects":
            return {"options": _aria_project_options(vra)}
        blueprint_id = parameters.get("blueprint_id")
        if not blueprint_id:
            return {"options": []}
        return {"options": _aria_blueprint_resource_options(vra, blueprint_id)}
    except Exception as exc:  # noqa: BLE001 -- an uncaught exception is a 500 whose text reaches the browser
        logger.exception("Aria migration form options failed")
        return _fail(400, str(exc))
