"""
The Actions tab of the Server Workflows page.

Lists the base actions a workflow step or Run Task can use (Remote Scripts,
CloudBolt plug-ins, webhooks, emails, copy-file actions) with their filters,
whether Run Task offers them, and what uses them, and creates Remote Scripts
and plug-ins with no server, resource or orchestration action around them.
Editing and deleting stay on CloudBolt's own action pages, which the tab
links to. The logic lives in action_library.py.

views.py imports the page function, because CloudBolt looks for admin
extensions only in an extension's views.py. Everything requires CB Admin.
"""
import functools
import json

from django.core.exceptions import PermissionDenied
from django.http import Http404, JsonResponse
from django.shortcuts import render
from django.urls import reverse

from extensions.views import admin_extension
from utilities.logger import ThreadLogger
from utilities.permissions import cbadmin_required

from . import action_library as library

logger = ThreadLogger(__name__)

TEMPLATES = "server_workflow_builder/templates/"
XUI_PACKAGE = "server_workflow_builder"
PAGE_VIEW = "actions"

MISSING_MODULE = (
    "The server_tasks and server_workflows shared modules must be on this appliance. Sync the Server "
    "Automation content package (or the Run Task and Run Workflow server actions) first; they bring them."
)

NEW_KINDS = {
    "windows": ("script", "windows", "New Windows Remote Script"),
    "linux": ("script", "linux", "New Linux Remote Script"),
    "plugin": ("plugin", "", "New CloudBolt Plug-in"),
}


def _modules():
    """(server_tasks, server_workflows), or None when either is missing."""
    try:
        from shared_modules import server_tasks, server_workflows
    except ImportError:
        logger.exception("Could not import the server_tasks or server_workflows shared module")
        return None
    return server_tasks, server_workflows


def page_url():
    return reverse("render_admin_extension", kwargs={"package": XUI_PACKAGE, "view_id": PAGE_VIEW})


def _new_urls():
    return {key: reverse("server_workflow_builder_action_new", args=[key]) for key in NEW_KINDS}


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------
@admin_extension(
    title="Server Workflow Actions",
    description="Create Remote Scripts and plug-ins with no server, resource or orchestration action, and see "
    "each base action's filters, whether Run Task offers it, and what uses it.",
)
def actions(request):
    profile = request.get_user_profile()
    modules = _modules()
    context = {
        "denied": not profile.is_cbadmin,
        "missing_module": modules is None,
        "missing_message": MISSING_MODULE,
        "new_urls": _new_urls(),
        "api_url": reverse("server_workflow_builder_api_base_actions"),
        "created": request.GET.get("created", ""),
        "run_task_problem": None,
    }
    if not context["denied"] and modules is not None:
        context["run_task_problem"] = library.run_task_state()
    return render(request, TEMPLATES + "actions.html", context)


@cbadmin_required
def action_new(request, kind):
    """The editor for a new Remote Script (Windows or Linux) or plug-in."""
    if kind not in NEW_KINDS:
        raise Http404("Unknown action kind.")
    action_kind, platform, title = NEW_KINDS[kind]
    modules = _modules()
    context = {"title": title, "list_url": page_url(), "missing_module": modules is None, "problem": ""}
    if modules is None:
        context["problem"] = MISSING_MODULE
        return render(request, TEMPLATES + "action_editor.html", context)
    swf = modules[1]
    try:
        defaults = library.template_defaults(platform) if action_kind == "script" else library.plugin_defaults()
    except library.ActionError as err:
        context["problem"] = str(err)
        return render(request, TEMPLATES + "action_editor.html", context)
    filters = {f["key"]: f for f in swf.filter_choices()}
    if action_kind == "script":
        families = [{"value": f.global_id, "label": f.name} for f in library.platform_families(platform)]
        root = library.root_family(platform)
        selected = [root.global_id] if root else []
    else:
        families = filters["os_families"]["options"]
        selected = []
    context["data"] = {
        "kind": action_kind,
        "platform": platform,
        "platform_label": library.PLATFORMS[platform]["label"] if platform else "",
        "defaults": defaults,
        "os_families": {"options": families, "selected": selected},
        "groups": filters["groups"]["options"],
        "environments": filters["environments"]["options"],
        "input_types": [{"value": key, "label": label} for key, label in library.INPUT_TYPES.items()],
        "option_types": sorted(library.OPTION_TYPES),
        "pattern_types": sorted(library.PATTERN_TYPES),
        "limits": {
            "inputs": library.MAX_INPUTS,
            "timeout_min": library.TIMEOUT_MIN,
            "timeout_max": library.TIMEOUT_MAX,
            "windows_budget": library.WINDOWS_SCRIPT_BUDGET,
        },
        "urls": {"list": page_url(), "api": reverse("server_workflow_builder_api_base_actions")},
    }
    return render(request, TEMPLATES + "action_editor.html", context)


# ---------------------------------------------------------------------------
# JSON API (session auth with CSRF, CB Admin only)
# ---------------------------------------------------------------------------
def _api(view):
    """CB Admin only; library and permission errors become JSON error responses."""

    @functools.wraps(view)
    def wrapper(request, *args, **kwargs):
        modules = _modules()
        if modules is None:
            return JsonResponse({"error": MISSING_MODULE}, status=503)
        server_tasks, swf = modules
        try:
            if not request.get_user_profile().is_cbadmin:
                raise PermissionDenied
            return view(request, server_tasks, swf, *args, **kwargs)
        except PermissionDenied:
            return JsonResponse({"error": "This requires CB Admin or Super Admin."}, status=403)
        except (library.ActionError, server_tasks.TaskError, swf.WorkflowError) as exc:
            return JsonResponse({"error": str(exc)}, status=400)
        except Exception:
            logger.exception("Server workflow actions API call failed: %s %s", request.method, request.path)
            return JsonResponse({"error": "Unexpected error; see the CloudBolt application log."}, status=500)

    return wrapper


def _body(request):
    if not request.body:
        return {}
    try:
        data = json.loads(request.body.decode("utf-8"))
    except ValueError:
        raise library.ActionError("The request body must be a JSON object.")
    if not isinstance(data, dict):
        raise library.ActionError("The request body must be a JSON object.")
    return data


def _method_not_allowed(allowed):
    response = JsonResponse({"error": "Use {}.".format(" or ".join(allowed))}, status=405)
    response["Allow"] = ", ".join(allowed)
    return response


@_api
def api_base_actions(request, server_tasks, swf):
    """
    GET: every base action a workflow step or Run Task can use, with filters,
    Run Task availability and the workflows that use it. ?server=<hostname, id
    or SVR- id> adds whether each fits that server; ?builtin=1 adds CloudBolt's
    own; ?ids=<OHK-,...>&used_by=1 returns just those, with everything else
    that uses them (CloudBolt's dependents, slow on many actions).
    POST {kind, platform, name, ...}: create a Remote Script or plug-in;
    with "preview": true, return the file it would write instead.
    """
    if request.method == "GET":
        server = None
        wanted = (request.GET.get("server") or "").strip()
        if wanted:
            server = _server(server_tasks, wanted, request.get_user_profile())
        ids = [part.strip() for part in (request.GET.get("ids") or "").split(",") if part.strip()] or None
        rows = library.action_rows(
            swf,
            server_tasks,
            include_builtin=request.GET.get("builtin") == "1",
            server=server,
            ids=ids,
            with_dependents=request.GET.get("used_by") == "1",
        )
        return JsonResponse(
            {
                "actions": rows,
                "server": {"id": server.global_id, "hostname": server.hostname} if server else None,
                "run_task_problem": library.run_task_state(),
            }
        )
    if request.method == "POST":
        data = _body(request)
        cleaned = library.clean_request(data)
        source, warnings = library.build_source(cleaned)
        if data.get("preview"):
            return JsonResponse({"source": source, "warnings": warnings})
        hook = library.create_action(cleaned, source, profile=request.get_user_profile())
        return JsonResponse(
            {
                "action": {"id": hook.global_id, "name": hook.name, "url": hook.get_absolute_url()},
                "warnings": warnings,
                "list_url": f"{page_url()}?created={hook.global_id}",
            },
            status=201,
        )
    return _method_not_allowed(["GET", "POST"])


def _server(server_tasks, value, profile):
    from infrastructure.models import Server

    try:
        return server_tasks.resolve_server(value, profile)
    except server_tasks.TaskError:
        server = (
            Server.objects.exclude(status="HISTORICAL")
            .filter(hostname__iexact=value, id__in=Server.objects_for_profile(profile).values("id"))
            .first()
        )
        if server is None:
            raise library.ActionError(f"There is no active server '{value}'.")
        return server
