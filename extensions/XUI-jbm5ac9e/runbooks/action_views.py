"""
The Actions tab of the Runbooks page.

Lists the base actions a workflow step or Run Task can use (Remote Scripts,
CloudBolt plug-ins, webhooks, emails, copy-file actions) with their filters,
whether Run Task offers them, and what uses them, and creates Remote Scripts
and plug-ins with no server, resource or orchestration action around them.
Editing and deleting stay on CloudBolt's own action pages, which the tab
links to. The logic lives in action_library.py.

The tab is a page of the extension (xui/runbooks/actions/), not an admin
extension of its own, so Admin lists Runbooks once. Everything requires CB
Admin.
"""
import functools
import json

from django.core.exceptions import PermissionDenied
from django.http import Http404, JsonResponse
from django.shortcuts import render
from django.urls import reverse

from utilities.logger import ThreadLogger
from utilities.permissions import cbadmin_required

from . import action_library as library

logger = ThreadLogger(__name__)

TEMPLATES = "runbooks/templates/"

MISSING_MODULE = (
    "The runbook_tasks and runbook_workflows shared modules must be on this appliance. Sync the Runbooks "
    "content package (or the Run Task and Run Workflow actions) first; they bring them."
)

NEW_KINDS = {
    "windows": ("script", "windows", "New Windows Remote Script"),
    "linux": ("script", "linux", "New Linux Remote Script"),
    "plugin": ("plugin", "", "New CloudBolt Plug-in"),
}


def _modules():
    """(runbook_tasks, runbook_workflows), or None when either is missing."""
    try:
        from shared_modules import runbook_tasks, runbook_workflows
    except ImportError:
        logger.exception("Could not import the runbook_tasks or runbook_workflows shared module")
        return None
    return runbook_tasks, runbook_workflows


def page_url():
    return reverse("runbooks_actions")


def _new_urls():
    return {key: reverse("runbooks_action_new", args=[key]) for key in NEW_KINDS}


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------
def actions(request):
    """The Actions tab: create Remote Scripts and plug-ins, and see each base action's filters and uses."""
    profile = request.get_user_profile()
    modules = _modules()
    context = {
        "uplink": {"url": reverse("admin_home"), "label": "Admin"},
        "denied": not profile.is_cbadmin,
        "missing_module": modules is None,
        "missing_message": MISSING_MODULE,
        "new_urls": _new_urls(),
        "api_url": reverse("runbooks_api_base_actions"),
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
        "urls": {"list": page_url(), "api": reverse("runbooks_api_base_actions")},
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
        runbook_tasks, swf = modules
        try:
            if not request.get_user_profile().is_cbadmin:
                raise PermissionDenied
            return view(request, runbook_tasks, swf, *args, **kwargs)
        except PermissionDenied:
            return JsonResponse({"error": "This requires CB Admin or Super Admin."}, status=403)
        except library.ActionError as exc:
            # A TaskError from runbook_tasks.resolve_server becomes an ActionError in _server.
            return JsonResponse({"error": exc.public_message}, status=400)
        except swf.WorkflowError as exc:
            return JsonResponse({"error": getattr(exc, "public_message", "The workflow request was refused.")}, status=400)
        except Exception:
            logger.exception("Runbook actions API call failed: %s %s", request.method, request.path)
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
def api_base_actions(request, runbook_tasks, swf):
    """
    GET: every base action a workflow step or Run Task can use, with filters,
    Run Task availability and the workflows that use it. ?server=<hostname, id
    or SVR- id> adds whether each fits that VM; ?builtin=1 adds CloudBolt's
    own; ?ids=<OHK-,...>&used_by=1 returns just those, with everything else
    that uses them (CloudBolt's dependents, slow on many actions).
    POST {kind, platform, name, ...}: create a Remote Script or plug-in;
    with "preview": true, return the file it would write instead.
    """
    if request.method == "GET":
        server = None
        wanted = (request.GET.get("server") or "").strip()
        if wanted:
            server = _server(runbook_tasks, wanted, request.get_user_profile())
        ids = [part.strip() for part in (request.GET.get("ids") or "").split(",") if part.strip()] or None
        rows = library.action_rows(
            swf,
            runbook_tasks,
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


def _server(runbook_tasks, value, profile):
    from infrastructure.models import Server

    try:
        return runbook_tasks.resolve_server(value, profile)
    except runbook_tasks.TaskError:
        server = (
            Server.objects.exclude(status="HISTORICAL")
            .filter(hostname__iexact=value, id__in=Server.objects_for_profile(profile).values("id"))
            .first()
        )
        if server is None:
            raise library.ActionError(f"There is no active VM '{value}'.")
        return server
