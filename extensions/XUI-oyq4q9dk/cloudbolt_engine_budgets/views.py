"""
CloudBolt Engine Budgets XUI

Adds a Budgets tab to the Group detail page. The tab shows the CloudBolt Engine
budget for the group (monthly amount, spend so far, month-end projection,
budget against actual by month) and lets people with the right permission set,
change or remove it. Everything about Engine lives in the shared module
`cloudbolt_engine` (shared_modules/SHM-6v7fxtdd); this file only checks access,
validates input and shapes JSON for the page.

CloudBolt does not permission-check tab URLs (render_tab_extension in
extensions/views.py only looks up the extension), so the tab and every JSON
endpoint below load the Group and check access themselves:
- view: CB admin, or the group.view permission on the group
- manage (register, edit, remove): CB admin, or group.change_attributes on the group
Permission names are the ones CloudBolt's roles define (accounts migration
0011_migrate_existing_roles; the group API checks group.change_attributes).

The JSON endpoints are under xui/engine-budgets/<group_id>/ (session auth): state/
(GET, read-only, no Engine calls), refresh/, register/, update/ and remove/ (POST,
JSON body, CSRF header). Status codes: 400 for bad input or connection setup, 409
when the group's state does not allow the call, 424 for other Engine failures
(never 502, which proxies replace), 403 and 404 for access.
"""
import functools
import importlib
import json
import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

from django.core.exceptions import PermissionDenied
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.utils.html import format_html

from accounts.models import Group
from extensions.views import TabExtensionDelegate, tab_extension
from utilities.logger import ThreadLogger

logger = ThreadLogger(__name__)

TEMPLATES = "cloudbolt_engine_budgets/templates/"
SHARED_MODULE = "shared_modules.cloudbolt_engine"

PERM_VIEW = "group.view"
PERM_MANAGE = "group.change_attributes"

# A viewer's Refresh re-fetches from Engine only when the stored snapshot is older than this.
VIEWER_REFRESH_AFTER = timedelta(minutes=5)

MAX_NAME = 255  # the shared module's limit
MAX_DESCRIPTION = 500
MAX_AMOUNT = Decimal("1000000000000")

MODULE_MISSING = (
    "The CloudBolt Engine shared module is not on this CloudBolt. Sync the Refresh CloudBolt Engine Budgets "
    "recurring job, which brings it, then restart the web service."
)


VIEWER_ERROR = "The last refresh from CloudBolt Engine failed. Ask a group manager to check it."
GENERIC_ENGINE_ERROR = "CloudBolt Engine could not complete the request. Ask a group manager to check it."


class ModuleMissing(Exception):
    """The shared module is not synced (or not yet loaded by this web process)."""


class ApiFailure(Exception):
    """A failure with its own HTTP status; the message is shown to the person."""

    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


class InputError(Exception):
    """A request value failed validation; the message is shown to the person."""


def _engine():
    try:
        return importlib.import_module(SHARED_MODULE)
    except ModuleNotFoundError as exc:
        # Only "the shared module itself is absent" means not synced; a missing
        # import inside it is a real error and propagates to be logged.
        if exc.name in (SHARED_MODULE, SHARED_MODULE.split(".")[0]):
            raise ModuleMissing(MODULE_MISSING)
        raise


# ---------------------------------------------------------------------------
# Access
# ---------------------------------------------------------------------------
def _is_admin(profile):
    return bool(profile.is_cbadmin)


def can_view(profile, group):
    return _is_admin(profile) or profile.has_permission(PERM_VIEW, group)


def can_manage(profile, group):
    return _is_admin(profile) or profile.has_permission(PERM_MANAGE, group)


def _load_group(group_id):
    return get_object_or_404(Group, pk=group_id)


class BudgetsTabDelegate(TabExtensionDelegate):
    """Show the tab only to people who may view the group."""

    def should_display(self):
        return can_view(self.viewer, self.instance)


# ---------------------------------------------------------------------------
# Tab
# ---------------------------------------------------------------------------
@tab_extension(
    model=Group,
    title="Budgets",
    delegate=BudgetsTabDelegate,
    description="The group's CloudBolt Engine budget: monthly amount, spend so far, month-end projection and "
    "budget against actual by month.",
)
def group_budgets_tab(request, obj_id):
    profile = request.get_user_profile()
    group = _load_group(obj_id)
    if not can_view(profile, group):
        # Not an exception: CloudBolt would log a traceback for it.
        return HttpResponse(
            format_html(
                '<div class="alert alert-secondary" role="status">{}</div>',
                "You do not have access to this group's budget.",
            )
        )
    fatal = ""
    try:
        _engine()
        module_missing = False
    except ModuleMissing:
        module_missing = True
    except Exception:
        logger.exception("Engine Budgets: could not load the shared module")
        module_missing = False
        fatal = "The CloudBolt Engine shared module could not be loaded. See the CloudBolt application log."
    config = {
        "group_id": group.id,
        # CloudBolt's tab pane id: "tab-" + the title in lower case (tabs/views.py, extensions/views.py).
        "pane": "tab-budgets",
        "module_missing": module_missing,
        "module_missing_message": MODULE_MISSING,
        "fatal": fatal,
        "urls": {
            "state": reverse("engine_budgets_state", args=[group.id]),
            "refresh": reverse("engine_budgets_refresh", args=[group.id]),
            "register": reverse("engine_budgets_register", args=[group.id]),
            "update": reverse("engine_budgets_update", args=[group.id]),
            "remove": reverse("engine_budgets_remove", args=[group.id]),
        },
    }
    return render(
        request,
        TEMPLATES + "ebx_tab.html",
        {
            "root_id": "ebx-root-{}".format(group.id),
            "config_id": "ebx-config-{}".format(group.id),
            "config": config,
        },
    )


# ---------------------------------------------------------------------------
# JSON API wrapper
# ---------------------------------------------------------------------------
def _json_error(status, message, **extra):
    body = {"error": message}
    body.update(extra)
    return JsonResponse(body, status=status)


def _api(method):
    """Method check plus JSON errors for permission, 404, Engine and unexpected failures."""

    def decorator(view):
        @functools.wraps(view)
        def wrapper(request, *args, **kwargs):
            if request.method != method:
                response = _json_error(405, "Use {}.".format(method))
                response["Allow"] = method
                return response
            try:
                return view(request, *args, **kwargs)
            except PermissionDenied as exc:
                return _json_error(403, str(exc) or "You do not have permission to do this.")
            except Http404:
                return _json_error(404, "Group not found.")
            except ModuleMissing as exc:
                return _json_error(424, str(exc), code="module_missing")
            except ApiFailure as exc:
                return _json_error(exc.status, str(exc))
            except InputError as exc:
                return _json_error(400, str(exc))
            except Exception as exc:
                engine = None
                try:
                    engine = _engine()
                except Exception:
                    pass
                base = getattr(engine, "EngineError", None)
                if base is not None and isinstance(exc, base):
                    config_error = getattr(engine, "EngineConfigError", None)
                    status = 400 if config_error is not None and isinstance(exc, config_error) else 424
                    if not getattr(request, "_ebx_manage", False):
                        logger.warning("Engine Budgets: %s failed for a viewer: %s", request.path, exc)
                        return _json_error(status, GENERIC_ENGINE_ERROR)
                    return _json_error(status, str(exc))
                logger.exception("Engine Budgets API call failed: %s", request.get_full_path())
                return _json_error(500, "Unexpected error; see the CloudBolt application log.")

        return wrapper

    return decorator


def _require_view(request, group_id):
    profile = request.get_user_profile()
    group = _load_group(group_id)
    if not can_view(profile, group):
        raise PermissionDenied("You do not have permission to view this group's budget.")
    request._ebx_manage = can_manage(profile, group)
    return profile, group


def _require_manage(request, group_id):
    profile = request.get_user_profile()
    group = _load_group(group_id)
    if not can_manage(profile, group):
        raise PermissionDenied("You do not have permission to change this group's budget.")
    request._ebx_manage = True
    return profile, group


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------
def _body(request):
    try:
        data = json.loads(request.body.decode("utf-8") or "{}")
    except (ValueError, UnicodeDecodeError):
        raise InputError("The request body is not valid JSON.")
    if not isinstance(data, dict):
        raise InputError("The request body must be a JSON object.")
    return data


def _amount(value):
    try:
        amount = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        raise InputError("Enter the monthly amount as a number, for example 2500 or 2500.50.")
    if not amount.is_finite() or amount <= 0:
        raise InputError("The monthly amount must be greater than zero.")
    if amount >= MAX_AMOUNT:
        raise InputError("The monthly amount is too large.")
    if amount != amount.quantize(Decimal("0.01")):
        raise InputError("Use at most two decimal places in the monthly amount.")
    return amount.quantize(Decimal("0.01"))


def _currency(value):
    code = str(value or "").strip()
    if not re.fullmatch(r"[A-Z]{3}", code):
        raise InputError("The currency must be a three-letter code in capitals, for example USD.")
    return code


def _text(value, label, limit):
    text = str(value or "").strip()
    if len(text) > limit:
        raise InputError("The {} can be at most {} characters.".format(label, limit))
    return text


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------
def _parse_time(value):
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _snapshot_is_stale(snapshot):
    fetched = _parse_time((snapshot or {}).get("fetched_at"))
    if fetched is None:
        return True
    return datetime.now(timezone.utc) - fetched > VIEWER_REFRESH_AFTER


def _connections(engine):
    return [{"global_id": c.global_id, "name": c.name} for c in engine.list_connections()]


def _registration_out(registration, connections, manage):
    """The registration without what the page does not need; connection details only for managers."""
    if not registration:
        return None
    pending = registration.get("pending")
    snapshot = registration.get("snapshot") or {}
    budget = snapshot.get("budget") or {}
    out = {
        "tag_key": registration.get("tag_key"),
        "tag_value": registration.get("tag_value"),
        "pending": (
            {
                "name": pending.get("name"),
                "amount": pending.get("amount"),
                "currency": pending.get("currency"),
                "description": pending.get("description"),
            }
            if pending
            else None
        ),
        "active": bool(registration.get("budget_id")),
        "budget_name": budget.get("name"),
        "budget_description": budget.get("description"),
        "created_at": registration.get("created_at"),
    }
    if manage:
        names = {c["global_id"]: c["name"] for c in connections}
        out["connection"] = registration.get("connection")
        out["connection_name"] = names.get(registration.get("connection"), "")
    return out


def _snapshot_out(registration, manage, group):
    """The snapshot as the page uses it. Engine error text goes to managers only."""
    snapshot = (registration or {}).get("snapshot")
    if not snapshot:
        return None
    out = {key: snapshot.get(key) for key in ("fetched_at", "error", "months", "daily", "trend")}
    budget = snapshot.get("budget") or {}
    out["budget"] = {key: budget.get(key) for key in ("name", "description", "currency", "amount")}
    if out["error"] and not manage:
        logger.info(
            "Engine Budgets: stored refresh error for %s hidden from a viewer: %s", group.global_id, out["error"]
        )
        out["error"] = VIEWER_ERROR
    return out


def _build_state(profile, group, engine, extra=None):
    """Read-only: stored data and the labeled connections. No call to CloudBolt Engine."""
    manage = can_manage(profile, group)
    connections = _connections(engine)
    registration = engine.get_registration(group)
    if registration:
        status = "active" if registration.get("budget_id") else "pending"
    elif not connections:
        status = "no_connection"
    else:
        status = "not_registered"
    state = {
        "status": status,
        "can_manage": manage,
        "registration": _registration_out(registration, connections, manage),
        "snapshot": _snapshot_out(registration, manage, group),
        "refreshed": False,
        "refresh_note": "",
    }
    if manage:
        state["connection_label"] = engine.CONNECTION_INFO_LABEL
        state["connections"] = connections
        state["defaults"] = {
            "budget_name": engine.default_budget_name(group),
            "tag_key": engine.DEFAULT_TAG_KEY,
            "tag_value": engine.default_tag_value(group),
            "currency": "USD",
        }
    state.update(extra or {})
    return state


def _state_response(profile, group, engine, extra=None):
    return JsonResponse(_build_state(profile, group, engine, extra))


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------
@_api("GET")
def api_state(request, group_id):
    profile, group = _require_view(request, group_id)
    return _state_response(profile, group, _engine())


@_api("POST")
def api_refresh(request, group_id):
    """Fetch fresh numbers from Engine. Managers any time; viewers only past the throttle."""
    profile, group = _require_view(request, group_id)
    engine = _engine()
    registration = engine.get_registration(group)
    if not registration:
        raise ApiFailure(409, "This group has no budget.")
    active = bool(registration.get("budget_id"))
    if not request._ebx_manage and not (active and _snapshot_is_stale(registration.get("snapshot"))):
        return _state_response(
            profile, group, engine, {"refresh_note": "The data was fetched less than five minutes ago."}
        )
    if not active:
        registration = engine.ensure_budget(group) or registration
        active = bool(registration.get("budget_id"))
    if active:
        # Does not raise for Engine failures: the snapshot's error says what went wrong.
        engine.refresh_snapshot(group)
    return _state_response(profile, group, engine, {"refreshed": True})


@_api("POST")
def api_register(request, group_id):
    profile, group = _require_manage(request, group_id)
    engine = _engine()
    data = _body(request)
    if engine.get_registration(group):
        raise ApiFailure(409, "This group already has a budget. Edit it instead.")
    connection = str(data.get("connection") or "").strip()
    # The id only selects among the labeled connections; the shared module re-checks the label.
    if connection not in {c["global_id"] for c in _connections(engine)}:
        raise InputError("Choose one of the CloudBolt Engine connections in the list.")
    amount = _amount(data.get("amount"))
    currency = _currency(data.get("currency") or "USD")
    name = _text(data.get("name"), "budget name", MAX_NAME) or None
    description = _text(data.get("description"), "description", MAX_DESCRIPTION) or None
    warning = ""
    try:
        engine.register_group(group, connection, amount, currency=currency, name=name, description=description)
    except engine.EngineError as exc:
        # The module saves the registration before it creates the Engine budget. If the
        # request is saved (as pending), show the pending state with the message; otherwise
        # nothing was saved and the error goes back to the form.
        if not engine.get_registration(group):
            raise
        warning = str(exc)
    registration = engine.get_registration(group)
    if registration and registration.get("budget_id"):
        engine.refresh_snapshot(group)
    return _state_response(profile, group, engine, {"warning": warning} if warning else None)


@_api("POST")
def api_update(request, group_id):
    """Only the fields present change. The module refreshes the snapshot itself."""
    profile, group = _require_manage(request, group_id)
    engine = _engine()
    data = _body(request)
    if not engine.get_registration(group):
        raise ApiFailure(409, "This group has no budget to edit.")
    amount = _amount(data["amount"]) if data.get("amount") not in (None, "") else None
    name = (_text(data.get("name"), "budget name", MAX_NAME) or None) if "name" in data else None
    description = _text(data.get("description"), "description", MAX_DESCRIPTION) if "description" in data else None
    if amount is None and name is None and description is None:
        raise InputError("Change the amount, name or description.")
    engine.update_group_budget(group, amount=amount, name=name, description=description)
    return _state_response(profile, group, engine)


@_api("POST")
def api_remove(request, group_id):
    """Delete the budget in Engine and here. {"local_only": true} forgets it here without calling Engine."""
    profile, group = _require_manage(request, group_id)
    engine = _engine()
    data = _body(request)
    if not engine.get_registration(group):
        raise ApiFailure(409, "This group has no budget to remove.")
    engine.remove_group_budget(group, delete_remote=not bool(data.get("local_only")))
    return _state_response(profile, group, engine)
