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
  Settings). CloudBolt indexes every tab and admin page of an extension as one
  component per title, whatever its type, and refuses a repeated title, so the
  three titles here ("Vulnerabilities", "Vulnerability Settings",
  "Vulnerability Search") must stay distinct.
- Vulnerability Search (admin page under Custom Extensions, function
  vulnerabilities) is the fleet search: a shell page plus four JSON routes
  (vuln_fleet_summary, vuln_fleet_cves, vuln_fleet_cve_servers,
  vuln_fleet_servers) that return one page of the findings store each.

Viewing needs the view permission on the server (Server.objects_for_profile).
Starting a scan needs vuln_jobs.can_scan, checked per server.
"""
import functools
import json
import os
import re
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

from . import vuln_db, vuln_hooks, vuln_jobs, vuln_match, vuln_repo, vuln_store as store, vuln_tools

logger = ThreadLogger(__name__)

TEMPLATES = "io_cloudbolt_vulnerabilities/templates/"

# Folder name of this XUI and the admin view's function name; together they
# form the settings page URL /extensions/admin/<package>/<view>.
XUI_PACKAGE = "io_cloudbolt_vulnerabilities"
PAGE_VIEW = "settings"
FLEET_VIEW = "vulnerabilities"

# The server page opens a tab named by its slug (the lower-cased title) after a "#tab-".
SERVER_TAB_HASH = "#tab-vulnerabilities"
FLEET_PAGE_SIZE = 25
# What a CVE or advisory id may look like when it comes from a URL: letters, digits and a few
# separators, 64 characters at most. It is user input and reaches the findings store.
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$")

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


def _fleet_url():
    return reverse("render_admin_extension", kwargs={"package": XUI_PACKAGE, "view_id": FLEET_VIEW})


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
            "fleet_url": _fleet_url() if is_admin else "",
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
        "unassigned": vuln_jobs.in_unassigned_group(server),
        "unassigned_text": vuln_jobs.UNASSIGNED,
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
    severity = request.GET.get("severity", "").strip().capitalize()
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
    if vuln_jobs.in_unassigned_group(server):
        return JsonResponse({"error": vuln_jobs.UNASSIGNED}, status=409)
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


def _local_time(stamp):
    """A UTC ISO stamp as a datetime in the appliance's local zone, or None."""
    try:
        when = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return when.astimezone().replace(tzinfo=None)


PROVISION_ERROR = ("The settings were saved, but the scan after provisioning could not be switched. "
                   "The CloudBolt application log has the details.")  # shown as a warning, not as a save error
SCHEDULES_ERROR = "The schedules cannot be read. The CloudBolt application log has the details."

# What each schedule does, in one sentence, by the key ensure_schedules returns.
SCHEDULE_TEXT = {
    "rematch": "Matches every stored software list against the newest vulnerability database, "
               "and removes the results of servers that no longer exist.",
    "inventory": "Collects a fresh software list from every server that can be scanned without anyone "
                 "present, a few at a time.",
}
WEEKDAYS = ("Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday")


def _cron_words(cron):
    """A plain-words reading of the two cron shapes this extension creates (a daily and a weekly
    time); anything else is "Custom"."""
    parts = str(cron or "").split()
    if len(parts) == 5 and parts[0].isdigit() and parts[1].isdigit() and parts[2] == parts[3] == "*":
        time = "{:02d}:{:02d}".format(int(parts[1]) % 24, int(parts[0]) % 60)
        if parts[4] == "*":
            return "Every day at " + time
        if parts[4].isdigit():
            return "Every {} at {}".format(WEEKDAYS[int(parts[4]) % 7], time)
    return "Custom"


def _schedule_rows(rows):
    """The Schedules card's rows from vuln_hooks.ensure_schedules()."""
    out = []
    for row in rows or []:
        last = row.get("last_run")
        if isinstance(last, str):
            last = _local_time(last)
        out.append({
            "name": row.get("name") or "",
            "what": SCHEDULE_TEXT.get(row.get("key"), ""),
            "cron": row.get("schedule") or "",
            "words": _cron_words(row.get("schedule")),
            "enabled": bool(row.get("enabled")),
            "last_run": last,
            "url": row.get("url") or "",
            "error": row.get("error") or "",
        })
    return out


def _scanner_context():
    """What the Scanner card shows. Each part is read on its own so one failure
    leaves the rest of the page usable."""
    context = {"setup_active": None, "tools": None, "store_stats": None, "db_age_days": None, "db_checked": None,
               "scanner_error": ""}
    try:
        context["tools"] = vuln_tools.status()
        context["db_age_days"] = _db_age_days(context["tools"].get("db", {}).get("built"))
        context["db_checked"] = _local_time(context["tools"].get("db", {}).get("checked"))
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
    post_warnings = []
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
            # The settings are saved; a failure here is shown without losing them.
            try:
                vuln_hooks.apply_provision_setting(bool(clean.get("scan_on_provision")))
            except Exception:
                logger.exception("Vulnerabilities: cannot update the scan after provisioning action")
                post_warnings.append(PROVISION_ERROR)
    context = {
        "settings": current,
        "saved": saved,
        "errors": list(errors.values()),
        "warnings": post_warnings,
        "field_errors": errors,
        "start_url": reverse("vuln_setup"),
        "fleet_url": "",
        "schedules": [],
        "schedules_error": "",
    }
    try:
        context["fleet_url"] = _fleet_url()
    except Exception:
        logger.exception("Vulnerabilities: cannot build the Vulnerabilities page URL")
    try:
        context["schedules"] = _schedule_rows(vuln_hooks.ensure_schedules())
    except Exception:
        logger.exception("Vulnerabilities: cannot read the schedules")
        context["schedules_error"] = SCHEDULES_ERROR
    context.update(_scanner_context())
    return render(request, TEMPLATES + "vuln_settings.html", context)


# ---------------------------------------------------------------------------
# Vulnerabilities page (fleet search for CloudBolt admins)
# ---------------------------------------------------------------------------
def _server_urls(rows):
    """{server id: server page URL on the Vulnerabilities tab} for the servers of one page of
    rows that still exist and are not historical. One Server query for the whole page; a
    failure leaves every row without a link."""
    ids = set()
    for row in rows:
        try:
            ids.add(int(row.get("server_id")))
        except (TypeError, ValueError):
            continue
    if not ids:
        return {}
    try:
        servers = Server.objects.filter(id__in=ids).exclude(status="HISTORICAL")
        return {server.id: server.get_absolute_url() + SERVER_TAB_HASH for server in servers}
    except Exception:
        logger.exception("Vulnerabilities: cannot resolve the server links of a page")
        return {}


def _with_urls(result):
    """The page dict with a url on each row whose server still exists and is not historical."""
    rows = result.get("rows") or []
    urls = _server_urls(rows)
    for row in rows:
        try:
            link = urls.get(int(row.get("server_id")))
        except (TypeError, ValueError):
            link = None
        if link:
            row["url"] = link
    return result


def _page_args(request):
    return {"page": _int_param(request, "page", 1), "page_size": FLEET_PAGE_SIZE}


@admin_extension(
    title="Vulnerability Search",
    description="Search every scanned server for a CVE or advisory, see which vulnerabilities affect the "
    "most servers, and open a server's Vulnerabilities tab.",
)
@cbadmin_required
def vulnerabilities(request):
    """The page is a shell; its data arrives from the four JSON routes. Each URL is built on its
    own so one failure leaves the others."""
    context = {"urls": {}, "settings_url": "", "page_error": ""}
    for key, name in (("summary", "vuln_fleet_summary"), ("cves", "vuln_fleet_cves"),
                      ("cve_servers", "vuln_fleet_cve_servers"), ("servers", "vuln_fleet_servers")):
        try:
            context["urls"][key] = reverse(name)
        except Exception:
            logger.exception("Vulnerabilities: cannot build the URL %s", name)
            context["page_error"] = "This page could not be set up. The CloudBolt application log has the details."
    try:
        context["settings_url"] = _settings_url()
    except Exception:
        logger.exception("Vulnerabilities: cannot build the settings URL")
    return render(request, TEMPLATES + "vuln_fleet.html", context)


def _fleet_summary_state():
    """(summary dict or None, state). A store written by the previous release is upgraded here, once,
    because readers never do (the upgrade is idempotent and transactional; this route is for CB Admins)."""
    try:
        version = vuln_db.schema_version()
        if version is not None and version < vuln_db.SCHEMA_VERSION:
            vuln_db.upgrade()
    except Exception:
        logger.exception("Vulnerabilities: cannot upgrade the findings store")
    summary = vuln_db.fleet_summary()
    if summary is not None:
        return (summary, "ready") if summary.get("servers") else (summary, "empty")
    if not os.path.exists(store.FINDINGS_DB):
        return None, "empty"
    return None, "unavailable"


@_api
@cbadmin_required
def vuln_fleet_summary(request):
    """GET: the fleet totals and the scanner's database date: {state, summary, db_built}. state is
    "ready" (summary is the totals), "empty" (no store yet, or no server in it) or "unavailable" (a
    store exists but cannot be read or upgraded right now; the page retries)."""
    if request.method != "GET":
        return JsonResponse({"error": "Use GET."}, status=405)
    db_built = ""
    try:
        db_built = vuln_tools.status().get("db", {}).get("built") or ""
    except Exception:
        logger.exception("Vulnerabilities: cannot read the database date")
    summary, state = _fleet_summary_state()
    return JsonResponse({"state": state, "summary": summary, "db_built": db_built})


@_api
@cbadmin_required
def vuln_fleet_cves(request):
    """GET ?q=&severity=&kev=1&page=: one page of the vulnerabilities that affect scanned servers."""
    if request.method != "GET":
        return JsonResponse({"error": "Use GET."}, status=405)
    severity = request.GET.get("severity", "").strip().capitalize()
    if severity not in vuln_match.SEVERITIES:
        severity = ""
    result = vuln_db.fleet_cves(
        search=request.GET.get("q", "").strip()[:100],
        severity=severity,
        kev_only=request.GET.get("kev") == "1",
        **_page_args(request)
    )
    return JsonResponse(result)


def _cve_info(cve):
    """The fleet-wide row {cve, severity, sev_rank, kev, servers, fixable} of exactly this id, or None
    when the fleet table has none. fleet_cves searches by substring, so the pages of that search are
    walked (at most five of 100) until the exact id appears."""
    try:
        for page in range(1, 6):
            result = vuln_db.fleet_cves(search=cve, page=page, page_size=100)
            for row in result.get("rows") or []:
                if str(row.get("cve", "")).lower() == cve.lower():
                    return row
            if page >= (result.get("pages") or 1):
                break
    except Exception:
        logger.exception("Vulnerabilities: cannot read the fleet row of %s", cve)
    return None


@_api
@cbadmin_required
def vuln_fleet_cve_servers(request):
    """GET ?cve=&q=&page=: one page of the servers a vulnerability affects, plus cve_info, the
    fleet-wide severity, exploited flag and counts of that id (null when the fleet table has none)."""
    if request.method != "GET":
        return JsonResponse({"error": "Use GET."}, status=405)
    cve = request.GET.get("cve", "").strip()
    if not ID_RE.match(cve):
        return JsonResponse({"error": "That is not a valid CVE or advisory id."}, status=400)
    if cve.upper().startswith("CVE-"):
        cve = cve.upper()
    result = _with_urls(vuln_db.cve_servers(cve, search=request.GET.get("q", "").strip()[:100], **_page_args(request)))
    result["cve_info"] = _cve_info(cve)
    return JsonResponse(result)


@_api
@cbadmin_required
def vuln_fleet_servers(request):
    """GET ?q=&kev=1&page=: one page of the scanned servers, worst first."""
    if request.method != "GET":
        return JsonResponse({"error": "Use GET."}, status=405)
    result = vuln_db.fleet_servers(
        search=request.GET.get("q", "").strip()[:100],
        kev_only=request.GET.get("kev") == "1",
        **_page_args(request)
    )
    return JsonResponse(_with_urls(result))
