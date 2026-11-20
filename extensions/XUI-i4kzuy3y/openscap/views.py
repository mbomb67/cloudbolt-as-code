"""
OpenSCAP XUI

Adds an OpenSCAP tab to Linux servers (or to servers labelled OpenScap). The
tab runs OpenSCAP scans on the server as CloudBolt jobs, shows the last scan's
score, failed rules and history, and links to the stored reports.

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
- Settings is an admin page (Admin > Custom Extensions > OpenSCAP Settings).
  Its title must differ from the tab's: CloudBolt indexes both by title.

Viewing needs the view permission on the server (Server.objects_for_profile).
Running a scan needs can_scan below.
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
from utilities.logger import ThreadLogger
from utilities.middleware import login_not_required
from utilities.permissions import cbadmin_required

from . import oscap_jobs, oscap_repo, oscap_store as store

logger = ThreadLogger(__name__)

TEMPLATES = "openscap/templates/"

# Folder name of this XUI and the admin view's function name; together they
# form the settings page URL /extensions/admin/<package>/<view>.
XUI_PACKAGE = "openscap"
PAGE_VIEW = "settings"

# Running a scan runs a script as root on the server, the same thing a Remote
# Script server action does. CloudBolt lets a user run any server action on a
# server when their role in the server's group (or a management group, or as
# the server's owner) has server.all_actions; utilities/permissions.py
# server_action_permitted checks that, and so does has_permission on a Server.
SCAN_PERMISSION = "server.all_actions"

# The tab shows on servers whose OS family belongs to this root family, or
# that carry this label.
LINUX_FAMILY = "Linux"
LABEL = "OpenScap"


# ---------------------------------------------------------------------------
# Permission helpers
# ---------------------------------------------------------------------------
def _viewable_server(request, server_id):
    """The server when the viewer may view it, else None. Never a bare lookup."""
    profile = request.get_user_profile()
    return Server.objects_for_profile(profile).filter(pk=server_id).first()


def can_scan(profile, server):
    """Whether the profile may run OpenSCAP on the server: a CB Admin, or the
    server.all_actions permission on it."""
    if getattr(profile, "is_cbadmin", False):
        return True
    try:
        return bool(profile.has_permission(SCAN_PERMISSION, server))
    except Exception:
        logger.exception("Permission check '%s' failed for server %s", SCAN_PERMISSION, server.id)
        return False


def _is_linux(server):
    family = server.os_family
    return bool(family and family.get_base_name() == LINUX_FAMILY)


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
        server = self.instance
        if server.status == "HISTORICAL":
            return False
        if store.get_settings()["visibility"] == "linux" and _is_linux(server):
            return True
        return LABEL in server.labels


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
        except oscap_jobs.ScanError as exc:
            return JsonResponse({"error": str(exc)}, status=exc.status)
        except Exception:
            logger.exception("OpenSCAP API call failed: %s %s", request.method, request.path)
            return JsonResponse({"error": "Unexpected error; see the CloudBolt application log."}, status=500)

    return wrapper


def _body(request):
    try:
        data = json.loads(request.body.decode("utf-8") or "{}")
    except ValueError:
        raise oscap_jobs.ScanError("The request body must be a JSON object.")
    if not isinstance(data, dict):
        raise oscap_jobs.ScanError("The request body must be a JSON object.")
    return data


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
    job = oscap_jobs.start(
        profile,
        server,
        str(data.get("mode") or "scan"),
        str(data.get("datastream") or ""),
        str(data.get("profile") or ""),
        bool(data.get("install")),
        base_url=_base_url(request),
    )
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
        logger.error("OpenSCAP: cannot open %s of scan %s: %s", kind, scan_id, exc.detail)
        # Administrators see why; everyone else a generic sentence.
        text = exc.detail if request.get_user_profile().is_cbadmin else str(exc)
        return HttpResponse(text, status=502, content_type="text/plain")


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
