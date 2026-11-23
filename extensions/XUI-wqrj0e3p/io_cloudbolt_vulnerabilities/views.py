"""
Vulnerabilities XUI

Adds a Vulnerabilities tab to Linux servers (or to servers labelled
Vulnerabilities). The tab collects a software list from the server as a
CloudBolt job, matches it against a vulnerability database on the appliance and
shows the findings, paged and filtered on the server side.

- The tab itself renders a shell and loads its panel asynchronously; nothing on
  the tab render touches the network, the server or the findings file.
- The panel (vuln_panel) is server-rendered HTML for the cards, banners and
  toolbar: one small JSON read, one SQLite row and at most one Job query. It
  answers with an inline message instead of raising.
- The table and pager are rendered in the browser from vuln_findings, which
  returns one page of the findings store as JSON.
- Start (vuln_start) runs a collection and match as a job (vuln_jobs.py). The
  job runs vuln_scan.sh on the server as root; the server sends its software
  list to the appliance itself (vuln_repo.py), so it never passes through the
  CloudBolt database.
- Upload and tool take the servers' requests. They need no CloudBolt login;
  the signed token in their X-Vuln-Token header is the credential.
- Setup (vuln_setup) installs or updates the scanner as a job, for CloudBolt
  admins.
- Settings is an admin page (Admin > Custom Extensions > Vulnerability
  Settings). Its title must differ from the tab's: CloudBolt indexes both by
  title.

Viewing needs the view permission on the server (Server.objects_for_profile).
Starting a scan needs vuln_jobs.can_scan, checked per server.
"""
import functools
import json
from datetime import datetime, timezone

from django.core.exceptions import PermissionDenied
from django.http import Http404, JsonResponse
from django.shortcuts import render
from django.urls import reverse
from django.views.decorators.csrf import csrf_exempt

from extensions.views import admin_extension, tab_extension, TabExtensionDelegate
from infrastructure.models import Server
from utilities.logger import ThreadLogger
from utilities.middleware import login_not_required
from utilities.permissions import cbadmin_required

from . import vuln_db, vuln_jobs, vuln_match, vuln_repo, vuln_store as store, vuln_tools

logger = ThreadLogger(__name__)

TEMPLATES = "io_cloudbolt_vulnerabilities/templates/"

# Folder name of this XUI and the admin view's function name; together they
# form the settings page URL /extensions/admin/<package>/<view>.
XUI_PACKAGE = "io_cloudbolt_vulnerabilities"
PAGE_VIEW = "settings"

PANEL_ERROR = "Could not load this panel. The CloudBolt application log has the details."
STORE_ERROR = "The scan history of this server cannot be read. The CloudBolt application log has the details."


# ---------------------------------------------------------------------------
# Permission helpers
# ---------------------------------------------------------------------------
def _viewable_server(request, server_id):
    """The server when the viewer may view it, else None. Never a bare lookup."""
    profile = request.get_user_profile()
    return Server.objects_for_profile(profile).filter(pk=server_id).first()


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
class VulnerabilitiesTabDelegate(TabExtensionDelegate):
    """Linux servers, or servers labelled Vulnerabilities. Cheap: no network, no remote call."""

    def should_display(self):
        return vuln_jobs.shows_tab(self.instance, store.get_settings()["visibility"])


@tab_extension(
    model=Server,
    title="Vulnerabilities",
    delegate=VulnerabilitiesTabDelegate,
    description="Known vulnerabilities on the server, found by matching its installed software against a "
    "vulnerability database, with the fix version and the CISA known-exploited flag",
)
def vulnerabilities_tab(request, obj_id):
    server = _viewable_server(request, obj_id)
    if server is None:
        raise Http404
    profile = request.get_user_profile()
    is_admin = bool(profile.is_cbadmin)
    return render(request, TEMPLATES + "vuln_tab.html", {
        "server": server,
        "panel_url": reverse("vuln_panel", args=[server.id]),
        "shell": {
            "can_scan": vuln_jobs.can_scan(profile, server),
            "is_admin": is_admin,
            "settings_url": _settings_url() if is_admin else "",
            "start_url": reverse("vuln_start", args=[server.id]),
            "findings_url": reverse("vuln_findings", args=[server.id]),
        },
    })


# ---------------------------------------------------------------------------
# Panel (HTML fragment loaded asynchronously by the tab)
# ---------------------------------------------------------------------------
def _panel_context(server, profile):
    summary = vuln_db.server_summary(server.id)
    state = store.read_state(server.id)
    active = vuln_jobs.panel_active(server.id)
    last = state.get("last") or None
    last_failed = last if last and last.get("status") == "failed" else None
    # The script re-fetches the table only when this changes, so it holds everything that
    # changes what the table shows: the match time, a scan starting or ending, the newest scan.
    key = "|".join([
        str((summary or {}).get("matched_at") or ""),
        "1" if active else "0",
        str((last or {}).get("scan_id") or ""),
        str((last or {}).get("status") or ""),
    ])
    tools = vuln_tools.status()
    vuln = {
        "denied": False,
        "can_scan": vuln_jobs.can_scan(profile, server),
        "is_admin": bool(profile.is_cbadmin),
        "start_url": reverse("vuln_start", args=[server.id]),
        "tools": tools,
        "ready": bool(tools.get("ready")),
        "summary": summary,
        "last_failed": last_failed,
        "ever_scanned": bool(summary or last),
        "active": active,
        "key": key,
    }
    if vuln["is_admin"]:
        vuln["settings_url"] = _settings_url()
    return {"vuln": vuln}


def vuln_panel(request, server_id):
    server = _viewable_server(request, server_id)
    if server is None:
        context = {
            "server": None,
            "vuln": {"denied": True},
            "error": "You do not have permission to view this server.",
        }
        return render(request, TEMPLATES + "vuln_panel.html", context)
    profile = request.get_user_profile()
    try:
        context = _panel_context(server, profile)
    except store.StoreError:
        logger.exception("Vulnerabilities: the state of server %s cannot be read", server.id)
        context = {"vuln": {"denied": False}, "error": STORE_ERROR}
    except Exception:
        logger.exception("Vulnerabilities panel failed for server %s", server.id)
        context = {"vuln": {"denied": False}, "error": PANEL_ERROR}
    context["server"] = server
    return render(request, TEMPLATES + "vuln_panel.html", context)


# ---------------------------------------------------------------------------
# JSON API
# ---------------------------------------------------------------------------
def _api(view):
    """Turn permission and request errors into JSON error responses. Request
    bodies are small and carry no secrets."""

    @functools.wraps(view)
    def wrapper(request, *args, **kwargs):
        try:
            return view(request, *args, **kwargs)
        except PermissionDenied:
            return JsonResponse({"error": "You do not have permission to do this."}, status=403)
        except Http404:
            return JsonResponse({"error": "This server was not found."}, status=404)
        except Exception:
            logger.exception("Vulnerabilities API call failed: %s %s", request.method, request.path)
            return JsonResponse({"error": "Unexpected error; see the CloudBolt application log."}, status=500)

    return wrapper


def _body(request):
    """The JSON object in the request body, or None when it is not one."""
    try:
        data = json.loads(request.body.decode("utf-8") or "{}")
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _int_param(request, name, default):
    """A positive integer query parameter; anything else falls back to the default."""
    try:
        value = int(request.GET.get(name, ""))
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


@_api
def vuln_findings(request, server_id):
    """GET ?view=packages|cves&q=&severity=&kev=1&page=&page_size=: one page of the
    findings, the store's dict unchanged. Bad parameters fall back to the defaults."""
    if request.method != "GET":
        return JsonResponse({"error": "Use GET."}, status=405)
    server = _viewable_server(request, server_id)
    if server is None:
        raise Http404
    view = "cves" if request.GET.get("view") == "cves" else "packages"
    severity = request.GET.get("severity", "")
    if severity not in vuln_match.SEVERITIES:
        severity = ""
    result = (vuln_db.server_cves if view == "cves" else vuln_db.server_packages)(
        server.id,
        search=request.GET.get("q", "")[:100],
        severity=severity,
        kev_only=request.GET.get("kev") == "1",
        page=_int_param(request, "page", 1),
        page_size=_int_param(request, "page_size", 25),
    )
    return JsonResponse(result)


@_api
def vuln_start(request, server_id):
    """POST {}: collects the software list and matches it as a job, answers 202 with
    {job_id, job_url}."""
    if request.method != "POST":
        return JsonResponse({"error": "Use POST."}, status=405)
    server = _viewable_server(request, server_id)
    if server is None:
        raise Http404
    profile = request.get_user_profile()
    if not vuln_jobs.can_scan(profile, server):
        raise PermissionDenied
    if _body(request) is None:
        return JsonResponse({"error": "The request body must be a JSON object."}, status=400)
    job = vuln_jobs.start(profile, server, _base_url(request))
    if isinstance(job, vuln_jobs.Refusal):
        return JsonResponse({"error": job.message}, status=job.status)
    return JsonResponse({"job_id": job.id, "job_url": job.get_absolute_url()}, status=202)


@_api
@cbadmin_required
def vuln_setup(request):
    """POST {}: installs or updates the scanner as a job, answers 202 with {job_id, job_url}."""
    if request.method != "POST":
        return JsonResponse({"error": "Use POST."}, status=405)
    if _body(request) is None:
        return JsonResponse({"error": "The request body must be a JSON object."}, status=400)
    job = vuln_jobs.start_setup(request.get_user_profile())
    if isinstance(job, vuln_jobs.Refusal):
        return JsonResponse({"error": job.message}, status=job.status)
    return JsonResponse({"job_id": job.id, "job_url": job.get_absolute_url()}, status=202)


# ---------------------------------------------------------------------------
# Routes the servers call
# ---------------------------------------------------------------------------
@login_not_required
@csrf_exempt
def vuln_upload(request, scan_id, name):
    """A server PUTs its software list here. No login; the signed token is the
    credential, and every refusal is a plain 404."""
    try:
        return vuln_repo.receive_upload(request, scan_id, name)
    except Http404:
        logger.info("Vulnerabilities: refused an upload from %s", request.META.get("REMOTE_ADDR", "unknown address"))
        raise


@login_not_required
@csrf_exempt
def vuln_tool(request, scan_id, name):
    """A server GETs the inventory tool here. No login; the signed token is the
    credential, and every refusal is a plain 404."""
    try:
        return vuln_repo.serve_tool(request, scan_id, name)
    except Http404:
        logger.info("Vulnerabilities: refused a tool download from %s", request.META.get("REMOTE_ADDR", "unknown address"))
        raise


# ---------------------------------------------------------------------------
# Settings page
# ---------------------------------------------------------------------------
def _db_age_days(built):
    """Whole days since the database was built, or None when unknown."""
    try:
        when = datetime.fromisoformat(str(built).replace("Z", "+00:00"))
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return max((datetime.now(timezone.utc) - when).days, 0)


def _scanner_context():
    """What the Scanner card shows. Each part is read on its own so one failure
    leaves the rest of the page usable."""
    context = {"setup_active": None, "tools": None, "store_stats": None, "db_age_days": None, "scanner_error": ""}
    try:
        context["tools"] = vuln_tools.status()
        context["db_age_days"] = _db_age_days(context["tools"].get("db", {}).get("built"))
        context["setup_active"] = vuln_jobs.setup_active()
    except Exception:
        logger.exception("Vulnerabilities: cannot read the scanner status")
        context["scanner_error"] = "The scanner status cannot be read. The CloudBolt application log has the details."
    try:
        context["store_stats"] = vuln_db.stats()
    except Exception:
        logger.exception("Vulnerabilities: cannot read the findings store")
    return context


@admin_extension(
    title="Vulnerability Settings",
    description="Set up the vulnerability scanner on this appliance, choose how long scans may run and which "
    "servers show the Vulnerabilities tab.",
)
@cbadmin_required
def settings(request):
    saved = False
    errors = {}
    current = store.get_settings()
    if request.method == "POST":
        clean, errors = store.validate_settings(request.POST)
        errors = dict(errors) if isinstance(errors, dict) else {}
        if errors:
            # Show the bad values back to the admin so they can be corrected.
            current = dict(current, **dict(clean))
            for key in errors:
                if key in request.POST:
                    current[key] = request.POST.get(key)
        else:
            store.save_settings(clean)
            current = store.get_settings()
            saved = True
    context = {
        "settings": current,
        "saved": saved,
        "errors": list(errors.values()),
        "field_errors": errors,
        "start_url": reverse("vuln_setup"),
    }
    context.update(_scanner_context())
    return render(request, TEMPLATES + "vuln_settings.html", context)
