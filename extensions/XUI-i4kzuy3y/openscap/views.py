"""
OpenSCAP XUI

Adds an OpenSCAP tab to Linux servers (or to servers labelled OpenScap). The
tab runs OpenSCAP scans on the server as CloudBolt jobs, shows the last scan's
score, failed rules and history, and links to the stored reports. Resources
that have such servers get an OpenSCAP tab too: it scans all of them in one
job and rolls up each server's latest result (oscap_resource.py,
oscap_rollup.py), linking to each server's own report.

- The tab itself renders a shell and loads its panel asynchronously; nothing
  on the tab render touches the network or the server.
- The panel (openscap_panel) is one small JSON read plus at most one Job
  query: the server's last readiness check, its latest scans and the scan or
  check in progress. It answers with an inline message instead of raising.
- Start (openscap_start) runs a readiness check or a scan as a job
  (oscap_jobs.py). The job runs oscap_scan.sh on the server as root through
  Server.execute_script; the server uploads its result files itself to the
  result repository (oscap_repo.py), an S3 bucket or the appliance, so the
  files never pass through the CloudBolt database.
- Artifact (openscap_artifact) opens a stored file: a redirect to a
  short-lived presigned S3 link, or the local file streamed.
- Upload (openscap_upload) takes the local backend's uploads. It needs no
  CloudBolt login; the signed token in its X-Oscap-Token header is the
  credential.
- The resource tab follows the same pattern: a shell, a panel fragment
  (openscap_resource_panel) and a start call (openscap_resource_start) that
  queues one parent job, which starts the servers' own jobs.
- Settings is an admin page (Admin > Custom Extensions > OpenSCAP Settings).
  Its title must differ from the tab's: CloudBolt indexes both by title.

Viewing needs the view permission on the server (Server.objects_for_profile),
and for the resource tab on the resource (Resource.objects_for_profile) and on
each server it lists. Running a scan needs can_scan below, checked per server.
"""
import functools
import json

from django.core.exceptions import PermissionDenied
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import render
from django.urls import reverse
from django.views.decorators.csrf import csrf_exempt

from extensions.views import admin_extension, tab_extension, TabExtensionDelegate
from infrastructure.models import Server
from resources.models import Resource
from utilities.logger import ThreadLogger
from utilities.middleware import login_not_required
from utilities.permissions import cbadmin_required

from . import oscap_jobs, oscap_repo, oscap_resource, oscap_rollup, oscap_store as store
from .oscap_jobs import can_scan

logger = ThreadLogger(__name__)

TEMPLATES = "openscap/templates/"

# Folder name of this XUI and the admin view's function name; together they
# form the settings page URL /extensions/admin/<package>/<view>.
XUI_PACKAGE = "openscap"
PAGE_VIEW = "settings"

# The most servers one resource scan request may name.
MAX_SERVERS = 500


# ---------------------------------------------------------------------------
# Permission helpers
# ---------------------------------------------------------------------------
def _viewable_server(request, server_id):
    """The server when the viewer may view it, else None. Never a bare lookup."""
    profile = request.get_user_profile()
    return Server.objects_for_profile(profile).filter(pk=server_id).first()


def _viewable_resource(request, resource_id):
    """The resource when the viewer may view it, else None. Never a bare lookup."""
    profile = request.get_user_profile()
    return Resource.objects_for_profile(profile).filter(pk=resource_id).first()


def _resource_servers(request, resource, visibility):
    """The servers the resource tab lists for this viewer: the resource's live
    servers that the viewer may view and that would get the server tab, never
    Windows. Deploy order."""
    profile = request.get_user_profile()
    viewable = set(Server.objects_for_profile(profile).filter(resource=resource).values_list("id", flat=True))
    return [
        server for server in oscap_resource.live_servers(resource)
        if server.id in viewable and oscap_resource.eligible(server, visibility)
    ]


def _base_url(request):
    """The address servers use to reach CloudBolt: the default portal's
    configured site URL, or "" when it is not set. Never built from the
    request's Host header, which a client controls."""
    from portals.models import PortalConfig

    portal = PortalConfig.get_current_portal(request)
    url = (portal.site_url if portal else None) or ""
    return url.rstrip("/")


def _settings_url():
    return reverse("render_admin_extension", kwargs={"package": XUI_PACKAGE, "view_id": PAGE_VIEW})


# ---------------------------------------------------------------------------
# Tab
# ---------------------------------------------------------------------------
class OpenSCAPTabDelegate(TabExtensionDelegate):
    """Linux servers, or servers labelled OpenScap. Cheap: no network, no remote call."""

    def should_display(self):
        return oscap_jobs.shows_tab(self.instance, store.get_settings()["visibility"])


@tab_extension(
    model=Server,
    title="OpenSCAP",
    delegate=OpenSCAPTabDelegate,
    description="OpenSCAP compliance scans of the server, with score, failed rules and stored reports",
)
def openscap_tab(request, obj_id):
    server = _viewable_server(request, obj_id)
    if server is None:
        raise Http404
    profile = request.get_user_profile()
    is_admin = bool(profile.is_cbadmin)
    return render(request, TEMPLATES + "oscap_tab.html", {
        "server": server,
        "panel_url": reverse("openscap_panel", args=[server.id]),
        "shell": {
            "can_scan": can_scan(profile, server),
            "is_admin": is_admin,
            "settings_url": _settings_url() if is_admin else "",
            "start_url": reverse("openscap_start", args=[server.id]),
            "repo_label": oscap_repo.describe()["label"],
        },
    })


# ---------------------------------------------------------------------------
# Panel (HTML fragment loaded asynchronously by the tab)
# ---------------------------------------------------------------------------
def _scan_view(server, scan):
    """A scan summary as the panel shows it, with the links to its stored files."""
    view = dict(scan)
    files = scan.get("files") or {}
    for kind, name in oscap_repo.FILES.items():
        view[kind + "_url"] = (
            reverse("openscap_artifact", args=[server.id, scan["id"], kind]) if files.get(name) else ""
        )
    return view


def _panel_context(server, profile, request):
    cfg = store.get_settings()
    index = store.read_index(server.id)
    scans = [_scan_view(server, scan) for scan in index["scans"]]
    latest = next((scan for scan in scans if scan.get("status") == "completed"), None)
    state = dict(index["state"])
    state.setdefault("checked", "")
    state.setdefault("oscap_version", "")
    state.setdefault("os_name", "")
    state.setdefault("ready", False)
    state.setdefault("problem", "")
    state.setdefault("datastreams", [])
    oscap = {
        "denied": False,
        "can_scan": can_scan(profile, server),
        "is_admin": bool(profile.is_cbadmin),
        "start_url": reverse("openscap_start", args=[server.id]),
        "panel_url": reverse("openscap_panel", args=[server.id]),
        "repo": oscap_repo.describe(cfg),
        "state": state,
        "active": oscap_jobs.panel_active(server.id, index),
        "allow_install": cfg["allow_install"],
        "latest": latest,
        "last_failed": scans[0] if scans and scans[0].get("status") == "failed" else None,
        "history": scans,
    }
    if oscap["is_admin"]:
        oscap["settings_url"] = _settings_url()
    return {"oscap": oscap}


def openscap_panel(request, server_id):
    server = _viewable_server(request, server_id)
    if server is None:
        context = {
            "server": None,
            "oscap": {"denied": True},
            "error": "You do not have permission to view this server.",
        }
        return render(request, TEMPLATES + "oscap_panel.html", context)
    profile = request.get_user_profile()
    try:
        context = _panel_context(server, profile, request)
    except store.StoreError:
        logger.exception("OpenSCAP: the index of server %s cannot be read", server.id)
        context = {"oscap": {"denied": False}, "error": oscap_jobs.INDEX_ERROR}
    except Exception:
        logger.exception("OpenSCAP panel failed for server %s", server.id)
        context = {
            "oscap": {"denied": False},
            "error": "Could not load this panel. The CloudBolt application log has the details.",
        }
    context["server"] = server
    return render(request, TEMPLATES + "oscap_panel.html", context)


# ---------------------------------------------------------------------------
# Resource tab: every Linux server of a resource, scanned in one job
# ---------------------------------------------------------------------------
class OpenSCAPResourceTabDelegate(TabExtensionDelegate):
    """Resources with at least one live server that would get the server tab. Cheap:
    one query for the servers, no network, no remote call. Which servers the
    viewer may see is applied by the tab and the panel."""

    def should_display(self):
        visibility = store.get_settings()["visibility"]
        return any(oscap_resource.eligible(server, visibility) for server in oscap_resource.live_servers(self.instance))


@tab_extension(
    model=Resource,
    title="OpenSCAP",
    delegate=OpenSCAPResourceTabDelegate,
    description="OpenSCAP compliance scans of all the Linux servers of a resource at once, with each server's "
    "latest score, the failed rules across servers and links to each server's report",
)
def openscap_resource_tab(request, obj_id):
    resource = _viewable_resource(request, obj_id)
    if resource is None:
        raise Http404
    profile = request.get_user_profile()
    is_admin = bool(profile.is_cbadmin)
    servers = _resource_servers(request, resource, store.get_settings()["visibility"])
    return render(request, TEMPLATES + "oscap_resource_tab.html", {
        "resource": resource,
        "panel_url": reverse("openscap_resource_panel", args=[resource.id]),
        "shell": {
            "can_scan": any(can_scan(profile, server) for server in servers),
            "is_admin": is_admin,
            "settings_url": _settings_url() if is_admin else "",
            "start_url": reverse("openscap_resource_start", args=[resource.id]),
            "repo_label": oscap_repo.describe()["label"],
            "server_count": len(servers),
        },
    })


def _duration_text(seconds):
    seconds = int(seconds or 0)
    return "{} min {} s".format(seconds // 60, seconds % 60) if seconds >= 60 else "{} s".format(seconds)


def _server_row(server, profile, active):
    """One line of the Servers table: the server's latest completed result, with
    the status of its newest scan or job. active is the resource run's dict, or None."""
    row = {
        "id": server.id,
        "hostname": server.hostname,
        "tier": server.service_item.name if server.service_item else "",
        "url": server.get_absolute_url() + "#tab-openscap",
        "scannable": can_scan(profile, server),
        "powered": server.power_status == "POWERON",
        "latest": None,
        "state": {},
        "status": "none",
        "status_title": "",
        "profile_label": "",
    }
    try:
        index = store.read_index(server.id)
        running = oscap_jobs.active_job(server.id, index)
    except store.StoreError:
        logger.exception("OpenSCAP: the index of server %s cannot be read", server.id)
        row["status"] = "unreadable"
        return row
    scans = [_scan_view(server, scan) for scan in index["scans"]]
    latest = next((scan for scan in scans if scan.get("status") == "completed"), None)
    state = index["state"]
    row["state"] = state
    row["latest"] = latest
    if latest is not None:
        row["profile_label"] = latest.get("profile_title") or latest.get("profile_id") or ""
    newest = scans[0] if scans else None
    if running is not None:
        row["status"] = "checking" if running[0].get("kind") == "check" else "scanning"
    elif active and server.id in active["server_ids"] and server.id not in active["finished_ids"]:
        row["status"] = "queued"
    elif not row["powered"]:
        row["status"] = "off"
    elif newest is not None and newest.get("status") == "failed":
        row["status"], row["status_title"] = "failed", newest.get("error") or ""
    elif latest is None:
        if state.get("checked") and not state.get("ready"):
            row["status"], row["status_title"] = "notready", state.get("problem") or ""
    elif latest.get("pruned"):
        row["status"] = "pruned"
    elif not latest.get("stored"):
        row["status"], row["status_title"] = "unstored", latest.get("store_problem") or ""
    else:
        row["status"] = "completed"
    return row


def _profile_options(rows):
    """The profiles to offer for a scan of these servers: the datastream and
    profile pairs that every one's last check found, in the order the first
    server lists them, titled as it titles them."""
    common = None
    titles = {}
    for row in rows:
        pairs = {}
        for ds in row["state"].get("datastreams") or []:
            for item in ds.get("profiles") or []:
                pairs[(ds.get("file"), item.get("id"))] = item.get("title") or item.get("id")
        for key, title in pairs.items():
            titles.setdefault(key, title)
        common = set(pairs) if common is None else common & set(pairs)
    return [
        {"value": "{}|{}".format(file, pid), "ds": file, "profile": pid, "title": title}
        for (file, pid), title in titles.items()
        if common and (file, pid) in common
    ]


def _run_view(run, viewable, titles=None):
    """A run record as the History table shows it. The record names every server
    of the run, so its per-server detail is cut to the servers the viewer may see
    (viewable is the set of their ids); the figures that are counts stay whole.
    titles maps (datastream, profile id) and ("", profile id) to a profile title, so
    a run that completed no scan (its record has no title) still shows one."""
    view = dict(run)
    if run.get("profile_id") and not run.get("profile_title") and titles:
        view["profile_title"] = (
            titles.get((run.get("datastream") or "", run["profile_id"])) or titles.get(("", run["profile_id"])) or ""
        )
    attempted = int(run.get("servers_attempted") or 0)
    completed = int(run.get("servers_completed") or 0)
    view["failed_count"] = max(attempted - completed, 0)
    view["outcome"] = "completed" if completed and completed == attempted else ("partial" if completed else "failed")
    if run.get("canceled"):
        view["outcome"] = "canceled"
    items = [item for item in run.get("servers") or [] if item.get("status") != "completed"]
    seen = [item for item in items if item.get("server_id") in viewable]
    problems = ["{}: {}".format(item.get("hostname") or item.get("server_id"), item.get("reason") or item.get("status"))
                for item in seen]
    if len(seen) < len(items):
        problems.append("{} more not shown".format(len(items) - len(seen)))
    view["problems"] = "; ".join(problems)
    view["servers"] = [item for item in run.get("servers") or [] if item.get("server_id") in viewable]
    if run.get("job_id"):
        view["job_url"] = reverse("job_detail", args=[str(run["job_id"])])
    return view


def _resource_panel_context(request, resource, profile):
    cfg = store.get_settings()
    servers = _resource_servers(request, resource, cfg["visibility"])
    index = store.read_resource_index(resource.id)
    active = oscap_resource.panel_active(resource.id, index)
    rows = [_server_row(server, profile, active) for server in servers]
    scanned = [row for row in rows if row["latest"] is not None]
    rolled = oscap_rollup.rollup(
        oscap_resource.summary_input(row["id"], row["hostname"], row["latest"]) for row in scanned
    )
    names = oscap_rollup.short_names(column["hostname"] for column in rolled["columns"])
    columns = [dict(column, short=short) for column, short in zip(rolled["columns"], names)]
    for rule in rolled["grid"]:
        rule["cells"] = list(zip(columns, rule["fails"]))

    run_servers = {item.get("server_id") for run in index["runs"] for item in run.get("servers") or []}
    viewable = set(
        Server.objects_for_profile(profile).filter(id__in=[i for i in run_servers if isinstance(i, int)])
        .values_list("id", flat=True)
    )
    titles = {}
    for row in rows:
        for ds in row["state"].get("datastreams") or []:
            for item in ds.get("profiles") or []:
                if item.get("id") and item.get("title"):
                    titles.setdefault((ds.get("file") or "", item["id"]), item["title"])
                    titles.setdefault(("", item["id"]), item["title"])
    runs = [_run_view(run, viewable, titles) for run in index["runs"]]
    scan_runs = [run for run in runs if run.get("mode") == "scan" and run.get("servers_completed")]
    scored = [run for run in scan_runs if run.get("average_score") is not None]
    trend = None
    if len(scored) >= 2:
        # The live average against the run before the latest one; no line without an earlier run.
        trend = oscap_rollup.trend(rolled["average"], scored[1]["average_score"])
        if trend is not None:
            trend["since"] = str(scored[1].get("started") or "")[:10]
    last_run = scan_runs[0] if scan_runs else None
    # One profile across the servers' latest scans, or Mixed (the table then shows each server's).
    mixed = len({row["latest"].get("profile_id") or row["latest"].get("profile_title") or "" for row in scanned}) > 1
    os_names = {row["state"].get("os_name") for row in scanned if row["state"].get("os_name")}
    scannable = [row for row in rows if row["scannable"]]
    powered = [row for row in scannable if row["powered"]]
    last_scan = max((row["latest"].get("started") or "" for row in scanned), default="")
    summary = {
        "rolled": rolled,
        "columns": columns,
        "trend": trend,
        "lowest_url": next((row["url"] for row in rows if rolled["lowest"] and row["id"] == rolled["lowest"]["id"]), ""),
        "profile": scanned[0]["profile_label"] if scanned and not mixed else "",
        "mixed": mixed,
        "started": last_run["started"] if last_run else last_scan,
        "duration": _duration_text(last_run.get("duration_s")) if last_run else "",
        "requested_by": last_run.get("requested_by") if last_run else "",
    }
    oscap = {
        "denied": False,
        "can_scan": bool(scannable),
        "is_admin": bool(profile.is_cbadmin),
        "start_url": reverse("openscap_resource_start", args=[resource.id]),
        "panel_url": reverse("openscap_resource_panel", args=[resource.id]),
        "repo": oscap_repo.describe(cfg),
        "active": active,
        "any_scanning": any(row["status"] in ("scanning", "checking") for row in rows),
        "allow_install": cfg["allow_install"],
        "servers": rows,
        "scanned_count": len(scanned),
        "show_unknown": any((row["latest"].get("fail_by_severity") or {}).get("unknown") for row in scanned),
        "server_count": len(rows),
        "os_name": next(iter(os_names)) if len(os_names) == 1 else "",
        "last_scan": last_scan,
        "summary": summary if scanned else None,
        "history": runs,
        "max_active": oscap_resource.MAX_ACTIVE,
        "profile_options": _profile_options(powered),
        "profile_server_count": len(powered),
        "form_servers": scannable,
        "check_ids": ",".join(str(row["id"]) for row in powered),
    }
    if oscap["is_admin"]:
        oscap["settings_url"] = _settings_url()
    return {"oscap": oscap}


def openscap_resource_panel(request, resource_id):
    resource = _viewable_resource(request, resource_id)
    if resource is None:
        context = {
            "resource": None,
            "oscap": {"denied": True},
            "error": "You do not have permission to view this resource.",
        }
        return render(request, TEMPLATES + "oscap_resource_panel.html", context)
    profile = request.get_user_profile()
    try:
        context = _resource_panel_context(request, resource, profile)
    except store.StoreError:
        logger.exception("OpenSCAP: the index of resource %s cannot be read", resource.id)
        context = {"oscap": {"denied": False}, "error": oscap_resource.RESOURCE_INDEX_ERROR}
    except Exception:
        logger.exception("OpenSCAP panel failed for resource %s", resource.id)
        context = {
            "oscap": {"denied": False},
            "error": "Could not load this panel. The CloudBolt application log has the details.",
        }
    context["resource"] = resource
    return render(request, TEMPLATES + "oscap_resource_panel.html", context)


# ---------------------------------------------------------------------------
# JSON API: start a scan or a check
# ---------------------------------------------------------------------------
def _api(view):
    """Turn permission and request errors into JSON error responses. Request
    bodies are small and carry no secrets."""

    @functools.wraps(view)
    def wrapper(request, *args, **kwargs):
        try:
            return view(request, *args, **kwargs)
        except PermissionDenied:
            return JsonResponse({"error": "You may not run OpenSCAP on this server."}, status=403)
        except Http404:
            return JsonResponse({"error": "This server was not found."}, status=404)
        except Exception:
            logger.exception("OpenSCAP API call failed: %s %s", request.method, request.path)
            return JsonResponse({"error": "Unexpected error; see the CloudBolt application log."}, status=500)

    return wrapper


def _body(request):
    """The JSON object in the request body, or None when it is not one."""
    try:
        data = json.loads(request.body.decode("utf-8") or "{}")
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


@_api
def openscap_start(request, server_id):
    """POST {mode: scan|check, datastream, profile, install}: starts the job,
    answers 202 with {job_id, job_url}."""
    if request.method != "POST":
        return JsonResponse({"error": "Use POST."}, status=405)
    server = _viewable_server(request, server_id)
    if server is None:
        raise Http404
    profile = request.get_user_profile()
    if not can_scan(profile, server):
        raise PermissionDenied
    data = _body(request)
    if data is None:
        return JsonResponse({"error": "The request body must be a JSON object."}, status=400)
    job = oscap_jobs.start(
        profile,
        server,
        str(data.get("mode") or "scan"),
        str(data.get("datastream") or ""),
        str(data.get("profile") or ""),
        bool(data.get("install")),
        base_url=_base_url(request),
    )
    if isinstance(job, oscap_jobs.Refusal):
        return JsonResponse({"error": job.message}, status=job.status)
    return JsonResponse({"job_id": job.id, "job_url": job.get_absolute_url()}, status=202)


@_api
def openscap_resource_start(request, resource_id):
    """POST {mode: scan|check, datastream, profile, install, servers: [ids]}:
    queues the resource's parent job, answers 202 with {job_id, job_url}. Only
    servers the requester may view and scan, among the servers the tab lists,
    are included."""
    if request.method != "POST":
        return JsonResponse({"error": "Use POST."}, status=405)
    resource = _viewable_resource(request, resource_id)
    if resource is None:
        return JsonResponse({"error": "This resource was not found."}, status=404)
    profile = request.get_user_profile()
    data = _body(request)
    if data is None:
        return JsonResponse({"error": "The request body must be a JSON object."}, status=400)
    wanted = data.get("servers")
    if not isinstance(wanted, list) or len(wanted) > MAX_SERVERS or not all(
        isinstance(item, int) and not isinstance(item, bool) for item in wanted
    ):
        return JsonResponse({"error": "The servers must be a list of server ids."}, status=400)
    scannable = {
        server.id: server
        for server in _resource_servers(request, resource, store.get_settings()["visibility"])
        if can_scan(profile, server)
    }
    if not scannable:
        return JsonResponse({"error": "You may not run OpenSCAP on the servers of this resource."}, status=403)
    chosen = [scannable[server_id] for server_id in dict.fromkeys(wanted) if server_id in scannable]
    if not chosen:
        return JsonResponse({"error": "Choose at least one server you may scan."}, status=400)
    job = oscap_resource.start(
        profile,
        resource,
        chosen,
        str(data.get("mode") or "scan"),
        str(data.get("datastream") or ""),
        str(data.get("profile") or ""),
        bool(data.get("install")),
        base_url=_base_url(request),
    )
    if isinstance(job, oscap_jobs.Refusal):
        return JsonResponse({"error": job.message}, status=job.status)
    return JsonResponse({"job_id": job.id, "job_url": job.get_absolute_url()}, status=202)


# ---------------------------------------------------------------------------
# Stored result files
# ---------------------------------------------------------------------------
def openscap_artifact(request, server_id, scan_id, kind):
    """The report, the XCCDF results or the ARF file of one scan."""
    server = _viewable_server(request, server_id)
    if server is None:
        raise Http404
    oscap_repo.kind_name(kind)
    try:
        scan = store.find_scan(server.id, scan_id)
    except store.StoreError:
        logger.exception("OpenSCAP: the index of server %s cannot be read", server.id)
        raise Http404
    if scan is None:
        raise Http404
    try:
        return oscap_repo.link_or_response(server, scan, kind, request)
    except oscap_repo.RepoError as exc:
        # The detail stays in the log; everyone, administrators included, gets one fixed sentence.
        logger.error("OpenSCAP: cannot open %s of scan %s: %s", kind, scan_id, exc.detail)
    return HttpResponse(oscap_repo.GENERIC_PUBLIC, status=502, content_type="text/plain")


@login_not_required
@csrf_exempt
def openscap_upload(request, scan_id, name):
    """Local backend: a server PUTs one result file here. No login; the signed
    token is the credential, and every refusal is a plain 404."""
    try:
        oscap_repo.receive_upload(request, scan_id, name)
    except Http404:
        logger.info("OpenSCAP: refused an upload from %s", request.META.get("REMOTE_ADDR", "unknown address"))
        raise
    return HttpResponse(status=200)


# ---------------------------------------------------------------------------
# Settings page
# ---------------------------------------------------------------------------
def _handlers():
    from resourcehandlers.aws.models import AWSHandler

    return [{"id": str(h.id), "name": h.name} for h in AWSHandler.objects.all().order_by("name")]


@admin_extension(
    title="OpenSCAP Settings",
    description="Choose where OpenSCAP scan reports are stored (S3 or this appliance), which profiles scans "
    "try, how long they may run, and which servers show the OpenSCAP tab.",
)
@cbadmin_required
def settings(request):
    saved = False
    errors = []
    current = store.get_settings()
    if request.method == "POST":
        clean, errors = store.validate_settings(request.POST)
        if errors:
            current = dict(clean)
        else:
            store.save_settings(clean)
            current = store.get_settings()
            saved = True
    return render(request, TEMPLATES + "oscap_settings.html", {
        "settings": current,
        "handlers": _handlers(),
        "saved": saved,
        "errors": errors,
        "local_usage": oscap_repo.local_usage(),
    })
