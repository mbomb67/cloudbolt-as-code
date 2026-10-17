"""
Server Workflows XUI

An admin page (Admin > Custom Extensions > Server Workflows) for building
server workflows: named, ordered lists of CloudBolt actions that the Run
Workflow server action (server_actions/SVA-j7rbsh39) runs on a server. This
module renders the list page, the workflow editor, the Delete dialog, and the
JSON API under xui/server_workflow_builder/api/ that the editor calls.

Workflows, their filters, the step picker and validation live in the
server_workflows shared module (shared_modules/SHM-nznzhpp0), which arrives
with the Run Workflow server action, so sync that action before using this
page. Everything here requires CB Admin: the admin page itself is also open to
Global Viewers, so every view checks.
"""
import functools
import json

from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.http import Http404, HttpResponseRedirect, JsonResponse
from django.shortcuts import render
from django.urls import reverse
from django.utils.html import format_html

from extensions.views import admin_extension
from utilities.decorators import dialog_view
from utilities.logger import ThreadLogger
from utilities.permissions import cbadmin_required

# The Actions tab's page. CloudBolt finds admin extensions only in views.py.
from .action_views import actions  # noqa: F401

logger = ThreadLogger(__name__)

TEMPLATES = "server_workflow_builder/templates/"

# Folder name of this XUI and the admin view's function name; together they
# form the list page URL /extensions/admin/<package>/<view>.
XUI_PACKAGE = "server_workflow_builder"
PAGE_VIEW = "workflows"

# Steps listed in a Steps cell before "+N more".
STEPS_SHOWN = 4

MISSING_MODULE = (
    "The server_workflows shared module is not on this appliance. Sync the Run Workflow server action "
    "(server_actions/SVA-j7rbsh39) or the Server Workflows content package first; it brings the module."
)


def _swf():
    """The shared module, imported on use so the page can explain when it is missing."""
    try:
        from shared_modules import server_workflows
    except ImportError:
        logger.exception("Could not import shared_modules.server_workflows")
        return None
    return server_workflows


def _page_url():
    return reverse("render_admin_extension", kwargs={"package": XUI_PACKAGE, "view_id": PAGE_VIEW})


def _when(value):
    """'2026-10-16T18:04:05Z' as '2026-10-16 18:04 UTC'."""
    text = str(value or "")
    if len(text) >= 16 and "T" in text:
        return "{} {} UTC".format(text[:10], text[11:16])
    return text


def _row(swf, workflow):
    described = swf.describe_workflow(workflow)
    steps = described["steps"]
    return {
        "id": workflow["id"],
        "name": workflow.get("name"),
        "description": workflow.get("description") or "",
        "enabled": workflow.get("enabled", True),
        "filters": described["filter_summary"],
        "steps": steps,
        "steps_shown": steps[:STEPS_SHOWN],
        "steps_more": max(0, len(steps) - STEPS_SHOWN),
        "missing_steps": sum(1 for step in steps if not step["exists"]),
        "updated": _when(workflow.get("updated_at")),
        "updated_by": workflow.get("updated_by") or "",
        "edit_url": reverse("server_workflow_builder_edit", args=[workflow["id"]]),
        "delete_url": reverse("server_workflow_builder_delete", args=[workflow["id"]]),
        "copy_url": reverse("server_workflow_builder_api_copy", args=[workflow["id"]]),
    }


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------
@admin_extension(
    title="Server Workflows",
    description="Build workflows that run several CloudBolt actions in order on a server, with filters, "
    "defaults and outputs passed between steps, for the Run Workflow server action.",
)
def workflows(request):
    profile = request.get_user_profile()
    swf = _swf()
    context = {
        "denied": not profile.is_cbadmin,
        "missing_module": swf is None,
        "missing_message": MISSING_MODULE,
        "rows": [],
        "add_url": reverse("server_workflow_builder_add"),
    }
    if not context["denied"] and swf is not None:
        context["rows"] = [_row(swf, workflow) for workflow in swf.list_workflows()]
    return render(request, TEMPLATES + "workflows.html", context)


@cbadmin_required
def workflow_editor(request, workflow_id=None):
    """The editor for a new workflow, or for an existing one."""
    swf = _swf()
    if swf is None:
        return render(
            request,
            TEMPLATES + "workflows.html",
            {"denied": False, "missing_module": True, "missing_message": MISSING_MODULE, "rows": []},
        )
    workflow = None
    if workflow_id:
        try:
            workflow = swf.get_workflow(workflow_id)
        except swf.WorkflowNotFound as exc:
            raise Http404(str(exc))
    data = {
        "workflow": workflow,
        "filters": swf.filter_choices(),
        "step_types": {key: {"label": info["label"], "icon": info["icon"]} for key, info in swf.STEP_TYPES.items()},
        "modes": list(swf.MODES),
        # None when the File Archives content is not installed.
        "archives": swf.archive_choices(),
        "reserved_keys": sorted(swf.RESERVED_KEYS),
        "limits": {
            "retries": swf.MAX_RETRIES,
            "retry_delay": swf.MAX_RETRY_DELAY,
            "timeout_minutes": swf.MAX_TIMEOUT_MINUTES,
            "steps": swf.MAX_STEPS,
        },
        "urls": {
            "list": _page_url(),
            "workflows": reverse("server_workflow_builder_api_workflows"),
            "workflow": (
                reverse("server_workflow_builder_api_workflow", args=[workflow["id"]]) if workflow else ""
            ),
            "actions": reverse("server_workflow_builder_api_actions"),
            "action": reverse("server_workflow_builder_api_action", args=["ACTION_ID"]),
            "edit": reverse("server_workflow_builder_edit", args=["WORKFLOW_ID"]),
        },
    }
    context = {
        "workflow": workflow,
        "data": data,
        "list_url": _page_url(),
        "title": workflow["name"] if workflow else "New workflow",
    }
    return render(request, TEMPLATES + "editor.html", context)


@dialog_view
@cbadmin_required
def workflow_delete(request, workflow_id):
    swf = _swf()
    if swf is None:
        raise Http404(MISSING_MODULE)
    try:
        workflow = swf.get_workflow(workflow_id)
    except swf.WorkflowNotFound as exc:
        raise Http404(str(exc))
    if request.method == "POST":
        swf.delete_workflow(workflow["id"])
        messages.success(request, 'Workflow "{}" deleted.'.format(workflow["name"]))
        return HttpResponseRedirect(_page_url())
    count = len(workflow.get("steps") or [])
    content = format_html(
        "<p>Delete the workflow <b>{}</b>?</p>"
        "<p>Run Workflow stops offering it at once. Its {} step action{} stay on this appliance unchanged, "
        "and jobs that already ran it keep their logs.</p>",
        workflow["name"], count, "" if count == 1 else "s",
    )
    return {
        "title": "Delete workflow",
        "content": content,
        "action_url": reverse("server_workflow_builder_delete", args=[workflow["id"]]),
        "use_ajax": True,
        "submit": "Delete",
        "theme": "danger",
    }


# ---------------------------------------------------------------------------
# JSON API (session auth with CSRF, CB Admin only)
# ---------------------------------------------------------------------------
def _api(view):
    """Turn workflow and permission errors into JSON error responses."""

    @functools.wraps(view)
    def wrapper(request, *args, **kwargs):
        swf = _swf()
        if swf is None:
            return JsonResponse({"error": MISSING_MODULE}, status=503)
        try:
            return view(request, swf, *args, **kwargs)
        except PermissionDenied:
            return JsonResponse({"error": "This requires CB Admin or Super Admin."}, status=403)
        except swf.WorkflowNotFound as exc:
            return JsonResponse({"error": str(exc)}, status=404)
        except swf.WorkflowError as exc:
            return JsonResponse({"error": str(exc)}, status=400)
        except Exception:
            logger.exception("Server workflow API call failed: %s %s", request.method, request.path)
            return JsonResponse({"error": "Unexpected error; see the CloudBolt application log."}, status=500)

    return wrapper


def _admin(view):
    """cbadmin_required for the API views, which take the shared module as their second argument."""

    @functools.wraps(view)
    def wrapper(request, swf, *args, **kwargs):
        profile = request.get_user_profile()
        if not profile.is_cbadmin:
            raise PermissionDenied
        return view(request, swf, *args, **kwargs)

    return wrapper


def _method_not_allowed(allowed):
    response = JsonResponse({"error": "Use {}.".format(" or ".join(allowed))}, status=405)
    response["Allow"] = ", ".join(allowed)
    return response


def _body(request, swf):
    if not request.body:
        return {}
    try:
        data = json.loads(request.body.decode("utf-8"))
    except ValueError:
        raise swf.WorkflowError("The request body must be a JSON object.")
    if not isinstance(data, dict):
        raise swf.WorkflowError("The request body must be a JSON object.")
    return data


@_api
@_admin
def api_workflows(request, swf):
    """GET: every workflow. POST: create one."""
    if request.method == "GET":
        return JsonResponse({"workflows": [swf.describe_workflow(wf) for wf in swf.list_workflows()]})
    if request.method == "POST":
        workflow = swf.create_workflow(_body(request, swf), profile=request.get_user_profile())
        return JsonResponse(
            {"workflow": workflow, "edit_url": reverse("server_workflow_builder_edit", args=[workflow["id"]])},
            status=201,
        )
    return _method_not_allowed(["GET", "POST"])


@_api
@_admin
def api_workflow(request, swf, workflow_id):
    """GET one workflow; POST or PUT replaces it; DELETE removes it."""
    if request.method == "GET":
        return JsonResponse({"workflow": swf.describe_workflow(swf.get_workflow(workflow_id))})
    if request.method in ("POST", "PUT"):
        workflow = swf.update_workflow(workflow_id, _body(request, swf), profile=request.get_user_profile())
        return JsonResponse({"workflow": workflow})
    if request.method == "DELETE":
        swf.delete_workflow(workflow_id)
        return JsonResponse({"deleted": workflow_id})
    return _method_not_allowed(["GET", "POST", "PUT", "DELETE"])


@_api
@_admin
def api_workflow_copy(request, swf, workflow_id):
    """POST: a disabled copy of the workflow."""
    if request.method != "POST":
        return _method_not_allowed(["POST"])
    workflow = swf.copy_workflow(workflow_id, profile=request.get_user_profile())
    return JsonResponse(
        {"workflow": workflow, "edit_url": reverse("server_workflow_builder_edit", args=[workflow["id"]])},
        status=201,
    )


@_api
@_admin
def api_actions(request, swf):
    """POST {filters, include_all}: the Add step picker for those filters."""
    if request.method != "POST":
        return _method_not_allowed(["POST"])
    data = _body(request, swf)
    return JsonResponse(swf.actions_for_filters(data.get("filters") or {}, include_all=bool(data.get("include_all"))))


@_api
@_admin
def api_action(request, swf, action_id):
    """GET: one action's inputs and known outputs, for configuring a step."""
    if request.method != "GET":
        return _method_not_allowed(["GET"])
    return JsonResponse({"action": swf.action_detail(swf.resolve_action(action_id))})
