"""
Vulnerabilities XUI

Adds a Vulnerabilities tab to Linux and Windows servers (or to servers labelled
Vulnerabilities). The tab collects a software list from a Linux server, or the
Windows build and installed updates of a Windows server, as a CloudBolt job,
matches it against a vulnerability database on the appliance (Grype for Linux,
Microsoft's published security updates for Windows) and shows the findings,
paged and filtered on the server side.

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
  component per title, whatever its type: a tab and an admin page may not
  share a title, and neither may two tabs on the same model. Two tabs on
  different models may, so the server tab (vulnerabilities_tab) and the
  resource tab (vulnerabilities_resource_tab) are both titled "Vulnerabilities",
  while "Vulnerability Settings" and "Vulnerability Search" stay distinct from
  it. The two tab functions must have different names.
- Vulnerability Search (admin page under Custom Extensions, function
  vulnerabilities) is the fleet search: a shell page plus four JSON routes
  (vuln_fleet_summary, vuln_fleet_cves, vuln_fleet_cve_servers,
  vuln_fleet_servers) that return one page of the findings store each.
- The resource tab rolls up the findings of the resource's own servers (not
  those of sub-resources) that the viewer may see: a shell page plus five
  routes (vuln_resource_summary, vuln_resource_cves, vuln_resource_cve_servers,
  vuln_resource_servers, vuln_resource_scan). It reuses the Vulnerability
  Search script (vuln_fleet_assets.html). Neither the tab dispatcher nor the
  delegate checks permissions, so every route starts with _viewable_resource
  and works only on the viewer's servers of that resource, the scan included.

Viewing needs the view permission on the server (Server.objects_for_profile),
and for the resource tab on the resource (Resource.objects_for_profile) and on
each server it lists. Starting a scan needs vuln_jobs.can_scan, checked per
server.
"""
import functools
import json
import os
import re
from datetime import datetime, timezone

from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.db.models import Q
from django.http import Http404, JsonResponse
from django.shortcuts import render
from django.urls import reverse
from django.views.decorators.csrf import csrf_exempt

from extensions.views import admin_extension, tab_extension, TabExtensionDelegate
from infrastructure.models import Server
from resources.models import Resource
from utilities.logger import ThreadLogger
from utilities.middleware import login_not_required
from utilities.permissions import cbadmin_required

from . import (vuln_db, vuln_fleet, vuln_hooks, vuln_jobs, vuln_match, vuln_msrc, vuln_repo, vuln_store as store,
               vuln_tools)

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
# The root OS family of Windows servers in CloudBolt (core's Server.is_windows() looks for this name).
WINDOWS_FAMILY = "Windows"
# What a CVE or advisory id may look like when it comes from a URL: letters, digits and a few
# separators, 64 characters at most. It is user input and reaches the findings store.
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$")

# A Windows row's db_built is the Microsoft index's own stamp ("2026-11-10.26edfc27ff"), not a time.
MSRC_STAMP_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}\.")

PANEL_ERROR = "Could not load this panel. The CloudBolt application log has the details."
STORE_ERROR = "The scan history of this server cannot be read. The CloudBolt application log has the details."


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
# Database dates
# ---------------------------------------------------------------------------
def _is_msrc_stamp(value):
    return bool(MSRC_STAMP_RE.match(str(value or "")))


def _stamp_date(value):
    """"YYYY-MM-DD" of a Microsoft index stamp, or "" when it is not one."""
    if not _is_msrc_stamp(value):
        return ""
    try:
        return vuln_msrc.stamp_date(value) or ""
    except Exception:
        return str(value)[:10]


def _display_built(value):
    """A stored db_built as the pages show it. A Grype build time is returned as it is. A Microsoft stamp becomes
    its date at noon UTC ("2026-11-10T12:00:00Z"): the pages format it in the browser's own zone, and midnight UTC
    would show the day before in the Americas. What is stored does not change."""
    day = _stamp_date(value)
    return day + "T12:00:00Z" if day else value


def _display_builts(values):
    """The db_builts list of a summary, converted. One build of each kind is the normal state (a Linux database
    and the Microsoft data are both current), so then one entry is left and the pages show no older-database
    note; more than one of a kind keeps them all."""
    values = [value for value in (values or []) if value]
    shown = []
    for value in values:
        day = _display_built(value)
        if day not in shown:
            shown.append(day)
    per_kind = {True: 0, False: 0}
    for value in values:
        per_kind[_is_msrc_stamp(value)] += 1
    if shown and max(per_kind.values()) <= 1:
        return shown[:1]
    return shown


def _with_built_display(result):
    """The page dict with db_built of each row (and db_builts of a summary) converted for display."""
    if isinstance(result, dict):
        for row in result.get("rows") or []:
            if isinstance(row, dict) and row.get("db_built"):
                row["db_built"] = _display_built(row["db_built"])
        if isinstance(result.get("db_builts"), list):
            result["db_builts"] = _display_builts(result["db_builts"])
    return result


# ---------------------------------------------------------------------------
# Tab
# ---------------------------------------------------------------------------
def _is_windows(server):
    """Whether the server is a Windows server; False when that cannot be told."""
    try:
        return bool(vuln_jobs.is_windows(server))
    except Exception:
        logger.exception("Vulnerabilities: cannot tell whether server %s is Windows", getattr(server, "id", ""))
        return False


class VulnerabilitiesTabDelegate(TabExtensionDelegate):
    """Linux and Windows servers, or servers labelled Vulnerabilities. Cheap: no network, no remote call."""

    def should_display(self):
        return vuln_jobs.shows_tab(self.instance, store.get_settings()["visibility"])


@tab_extension(
    model=Server,
    title="Vulnerabilities",
    delegate=VulnerabilitiesTabDelegate,
    description="Known vulnerabilities on the server, found by matching its installed software (Linux) or its "
    "Windows build and installed updates against a vulnerability database, with the fix version and the CISA "
    "known-exploited flag",
)
def vulnerabilities_tab(request, obj_id):
    server = _viewable_server(request, obj_id)
    if server is None:
        raise Http404
    profile = request.get_user_profile()
    is_admin = bool(profile.is_cbadmin)
    return render(request, TEMPLATES + "vuln_tab.html", {
        "server": server,
        "windows": _is_windows(server),
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
    _upgrade_store()  # readers never upgrade, and the summary selects the newest columns
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
    windows = _is_windows(server)
    if summary and summary.get("db_built"):
        summary = dict(summary, db_built=_display_built(summary["db_built"]))
    if summary and windows:
        # The Windows "software list" is the build and the installed updates; its OS name ends in "build <number>".
        found = re.search(r"build\s+(\S+)\s*$", str(summary.get("os_name") or ""))
        summary = dict(summary, windows_build=found.group(1) if found else (summary.get("os_name") or ""))
    vuln = {
        "denied": False,
        "windows": windows,
        "can_scan": vuln_jobs.can_scan(profile, server),
        "unassigned": vuln_jobs.in_unassigned_group(server),
        "unassigned_text": vuln_jobs.UNASSIGNED,
        "is_admin": bool(profile.is_cbadmin),
        "start_url": reverse("vuln_start", args=[server.id]),
        "tools": tools,
        "ready": bool(tools.get("ready_windows" if windows else "ready")),
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
    """GET ?view=packages|cves&q=&severity=&kev=1&fix=1&page=&page_size=: one page of the
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
        fix_only=request.GET.get("fix") == "1",
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
    """Whole days since the database was built, or None when unknown. A Microsoft stamp counts from its date."""
    if _is_msrc_stamp(built):
        built = _stamp_date(built)
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


def _msrc_tile(tools):
    """What the Microsoft security data tile shows. Read defensively: while a sync is half done the
    status has no "msrc". None when there is nothing to show."""
    msrc = (tools or {}).get("msrc")
    if not isinstance(msrc, dict):
        return None
    return dict(msrc, age_days=_db_age_days(msrc.get("built")) if msrc.get("built") else None,
                checked_local=_local_time(msrc.get("checked")) if msrc.get("checked") else None)


PROVISION_ERROR = ("The settings were saved, but the scan after provisioning could not be switched. "
                   "The CloudBolt application log has the details.")  # shown as a warning, not as a save error
SCHEDULES_ERROR = "The schedules cannot be read. The CloudBolt application log has the details."

# What each schedule does, in one sentence, by the key ensure_schedules returns.
SCHEDULE_TEXT = {
    "rematch": "Matches every stored software list or Windows build against the newest vulnerability data, "
               "and removes the results of servers that no longer exist.",
    "inventory": "Collects a fresh software list or Windows build from every server that can be scanned without "
                 "anyone present, a few at a time.",
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
               "msrc": None, "scanner_error": ""}
    try:
        context["tools"] = vuln_tools.status()
        context["db_age_days"] = _db_age_days(context["tools"].get("db", {}).get("built"))
        context["db_checked"] = _local_time(context["tools"].get("db", {}).get("checked"))
        context["msrc"] = _msrc_tile(context["tools"])
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
    description="Set up the vulnerability scanner and the Microsoft security data on this appliance, choose how "
    "long scans may run and which servers show the Vulnerabilities tab.",
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


def _upgrade_store():
    """A store written by the previous release is upgraded, once, because readers never do (the upgrade
    is idempotent and transactional). Called by the summary routes; never raises."""
    try:
        version = vuln_db.schema_version()
        if version is not None and version < vuln_db.SCHEMA_VERSION:
            vuln_db.upgrade()
    except Exception:
        logger.exception("Vulnerabilities: cannot upgrade the findings store")


def _fleet_summary_state():
    """(summary dict or None, state). The store is upgraded first (_upgrade_store; this route is for CB Admins)."""
    _upgrade_store()
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
    if isinstance(summary, dict):
        summary = _with_built_display(dict(summary))
    return JsonResponse({"state": state, "summary": summary, "db_built": _display_built(db_built)})


@_api
@cbadmin_required
def vuln_fleet_cves(request):
    """GET ?q=&severity=&kev=1&fix=1&page=: one page of the vulnerabilities that affect scanned servers."""
    if request.method != "GET":
        return JsonResponse({"error": "Use GET."}, status=405)
    severity = request.GET.get("severity", "").strip().capitalize()
    if severity not in vuln_match.SEVERITIES:
        severity = ""
    result = vuln_db.fleet_cves(
        search=request.GET.get("q", "").strip()[:100],
        severity=severity,
        kev_only=request.GET.get("kev") == "1",
        fix_only=request.GET.get("fix") == "1",
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
    result = _with_built_display(_with_urls(vuln_db.cve_servers(
        cve, search=request.GET.get("q", "").strip()[:100], **_page_args(request))))
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
    return JsonResponse(_with_built_display(_with_urls(result)))


# ---------------------------------------------------------------------------
# Resource tab (the findings of one resource's own servers)
# ---------------------------------------------------------------------------
# Linux or Windows at the server's own OS family or one or two levels up: the same meaning as
# vuln_jobs.is_linux and vuln_jobs.is_windows (the root families are named Linux and Windows), written
# as a filter so should_display costs one query and no Python loop over the servers.
_SUPPORTED = (Q(os_family__name=vuln_jobs.LINUX_FAMILY)
              | Q(os_family__parent__name=vuln_jobs.LINUX_FAMILY)
              | Q(os_family__parent__parent__name=vuln_jobs.LINUX_FAMILY)
              | Q(os_family__name=WINDOWS_FAMILY)
              | Q(os_family__parent__name=WINDOWS_FAMILY)
              | Q(os_family__parent__parent__name=WINDOWS_FAMILY))
RESOURCE_NOT_FOUND = "This resource was not found."
NOTHING_TO_SCAN = ("None of the servers of this resource can be scanned right now. They may be powered off or in an "
                   "Unassigned group, or you may not have permission to scan them.")


class VulnerabilitiesResourceTabDelegate(TabExtensionDelegate):
    """Resources with at least one live Linux or Windows server that would get the server tab. Runs on every view of
    every resource page for every user, so it is one query and never raises. It checks no permission:
    the tab and its routes restrict everything to the servers the viewer may see."""

    def should_display(self):
        try:
            servers = self.instance.server_set.exclude(status="HISTORICAL").filter(_SUPPORTED)
            if store.get_settings()["visibility"] == "label":
                servers = servers.filter(tags__name=vuln_jobs.LABEL)
            return servers.exists()
        except Exception:
            logger.exception("Vulnerabilities: cannot tell whether resource %s gets the tab", getattr(self.instance, "id", ""))
            return False


def _visible_ids(profile, resource):
    """Sorted ids of the resource's own live servers the profile may view. The view check is one
    objects_for_profile query turned into a set (never .distinct()); the live servers are a second,
    plain query, which is also what the pages below are cut from."""
    viewable = set(Server.objects_for_profile(profile).filter(resource=resource).values_list("id", flat=True))
    if not viewable:
        return []
    return sorted(resource.server_set.exclude(status="HISTORICAL").filter(id__in=viewable).values_list("id", flat=True))


@tab_extension(
    model=Resource,
    title="Vulnerabilities",
    delegate=VulnerabilitiesResourceTabDelegate,
    description="Known vulnerabilities across the servers of a resource, with the servers each one affects and "
    "a button that scans all of them",
)
def vulnerabilities_resource_tab(request, obj_id):
    resource = _viewable_resource(request, obj_id)
    if resource is None:
        raise Http404
    profile = request.get_user_profile()
    is_admin = bool(profile.is_cbadmin)
    ids = _visible_ids(profile, resource)
    urls = {}
    for key, name in (("summary", "vuln_resource_summary"), ("cves", "vuln_resource_cves"),
                      ("cve_servers", "vuln_resource_cve_servers"), ("servers", "vuln_resource_servers"),
                      ("scan", "vuln_resource_scan")):
        urls[key] = reverse(name, args=[resource.id])
    return render(request, TEMPLATES + "vuln_resource_tab.html", {
        "resource": resource,
        "no_servers": not ids,
        "urls": urls,
        "shell": {
            "is_admin": is_admin,
            "settings_url": _settings_url() if is_admin else "",
            "fleet_url": _fleet_url() if is_admin else "",
        },
    })


def _resource_gate(request, resource_id):
    """(resource, profile, ids) for a resource route, or (None, None, None) when the viewer may not
    see the resource. Every resource route starts here."""
    resource = _viewable_resource(request, resource_id)
    if resource is None:
        return None, None, None
    profile = request.get_user_profile()
    return resource, profile, _visible_ids(profile, resource)


def _severity(request):
    severity = request.GET.get("severity", "").strip().capitalize()
    return severity if severity in vuln_match.SEVERITIES else ""


@_api
def vuln_resource_summary(request, resource_id):
    """GET: {state, summary, db_built, run, can_scan} for the viewer's servers of the resource.
    summary is vuln_db.set_summary with servers (the same number as scanned, the name the shared
    script reads) and servers_total (the viewer's servers) added. state is "ready" (at least one
    server has a software list), "empty" or "unavailable" (a store exists but cannot be read now;
    the page retries). run is the scan in progress, or null (always null for a viewer who can see
    none of the resource's servers). can_scan is whether the viewer may scan at least one server
    (vuln_fleet.any_scannable); it is null while a run is in progress.
    GET ?run=1: only {run}, without touching the findings store: the page polls this every 5 seconds
    while a scan runs and asks for the full answer when it changes."""
    if request.method != "GET":
        return JsonResponse({"error": "Use GET."}, status=405)
    resource, profile, ids = _resource_gate(request, resource_id)
    if resource is None:
        return JsonResponse({"error": RESOURCE_NOT_FOUND}, status=404)
    run = None
    if ids:
        try:
            run = vuln_fleet.resource_run(resource.id)
        except Exception:
            logger.exception("Vulnerabilities: cannot read the scan of resource %s", resource.id)
    if request.GET.get("run") == "1":
        return JsonResponse({"run": run})
    db_built = ""
    try:
        db_built = vuln_tools.status().get("db", {}).get("built") or ""
    except Exception:
        logger.exception("Vulnerabilities: cannot read the database date")
    summary, state = None, "empty"
    if ids:
        _upgrade_store()
        found = vuln_db.set_summary(ids)
        if found is not None:
            summary = _with_built_display(dict(found))
            summary["servers"] = summary.get("scanned", 0)
            summary["servers_total"] = len(ids)
            if summary.get("truncated"):
                summary["servers_limit"] = vuln_db.MAX_SET
            state = "ready" if summary["servers"] else "empty"
        elif os.path.exists(store.FINDINGS_DB):
            state = "unavailable"
    can_scan = None
    if run is None and ids:
        try:
            can_scan = bool(vuln_fleet.any_scannable(profile, resource, store.get_settings()["visibility"]))
        except Exception:
            logger.exception("Vulnerabilities: cannot tell whether the viewer may scan servers of resource %s", resource.id)
    return JsonResponse({"state": state, "summary": summary, "db_built": _display_built(db_built), "run": run,
                         "can_scan": can_scan})


@_api
def vuln_resource_cves(request, resource_id):
    """GET ?q=&severity=&kev=1&fix=1&page=: one page of the vulnerabilities that affect the viewer's servers
    of the resource (vuln_db.set_cves: servers and fixable count the servers in that set)."""
    if request.method != "GET":
        return JsonResponse({"error": "Use GET."}, status=405)
    resource, profile, ids = _resource_gate(request, resource_id)
    if resource is None:
        return JsonResponse({"error": RESOURCE_NOT_FOUND}, status=404)
    result = vuln_db.set_cves(
        ids,
        search=request.GET.get("q", "").strip()[:100],
        severity=_severity(request),
        kev_only=request.GET.get("kev") == "1",
        fix_only=request.GET.get("fix") == "1",
        **_page_args(request)
    )
    return JsonResponse(result)


def _set_cve_info(ids, cve):
    """The row {cve, severity, sev_rank, kev, servers, fixable} of exactly this id among the set, or
    None (one indexed lookup, vuln_db.set_cve_info)."""
    try:
        return vuln_db.set_cve_info(ids, cve)
    except Exception:
        logger.exception("Vulnerabilities: cannot read the row of %s among a resource's servers", cve)
    return None


@_api
def vuln_resource_cve_servers(request, resource_id):
    """GET ?cve=&q=&page=: one page of the viewer's servers of the resource that a vulnerability
    affects, each with a url to the server's Vulnerabilities tab, plus cve_info (null when none of
    them has that id)."""
    if request.method != "GET":
        return JsonResponse({"error": "Use GET."}, status=405)
    resource, profile, ids = _resource_gate(request, resource_id)
    if resource is None:
        return JsonResponse({"error": RESOURCE_NOT_FOUND}, status=404)
    cve = request.GET.get("cve", "").strip()
    if not ID_RE.match(cve):
        return JsonResponse({"error": "That is not a valid CVE or advisory id."}, status=400)
    if cve.upper().startswith("CVE-"):
        cve = cve.upper()
    result = _with_built_display(_with_urls(vuln_db.set_cve_servers(
        ids, cve, search=request.GET.get("q", "").strip()[:100], **_page_args(request))))
    result["cve_info"] = _set_cve_info(ids, cve)
    return JsonResponse(result)


def _state_text(code):
    """The sentence for a server that has no software list: "Not scanned yet" for one that could be
    scanned (no reason code), else the reason code's text from vuln_fleet.REASONS with a capital."""
    if not code:
        return "Not scanned yet"
    text = vuln_fleet.REASONS.get(code) or "could not be checked"
    return text[:1].upper() + text[1:]


def _is_scanning(server_id):
    try:
        return vuln_jobs.active_job(server_id) is not None
    except Exception:
        logger.exception("Vulnerabilities: cannot read the scan state of server %s", server_id)
        return False


@_api
def vuln_resource_servers(request, resource_id):
    """GET ?q=&page=: the viewer's servers of the resource, hostname order, 25 to a page. The page is cut
    in the database; eligibility, the scan in progress and the stored findings are read for that page's
    servers only. state_text is "" for a server with a software list, "Not scanned yet" for one that
    could be scanned, otherwise the reason it cannot."""
    if request.method != "GET":
        return JsonResponse({"error": "Use GET."}, status=405)
    resource, profile, ids = _resource_gate(request, resource_id)
    if resource is None:
        return JsonResponse({"error": RESOURCE_NOT_FOUND}, status=404)
    if vuln_db.readable() is False:
        # The store exists but is busy, unreadable or not upgraded: say so (the page retries) rather than
        # show scanned servers as "Not scanned yet". None (no store yet) and True answer as usual.
        return JsonResponse({"total": 0, "page": 1, "page_size": FLEET_PAGE_SIZE, "pages": 1, "rows": [],
                             "unavailable": True})
    servers = Server.objects.filter(id__in=ids)
    term = request.GET.get("q", "").strip()[:100]
    if term:
        servers = servers.filter(hostname__icontains=term)
    servers = (servers.select_related("group", "os_family", "resource_handler").prefetch_related("tags")
               .order_by("hostname", "id"))
    paginator = Paginator(servers, FLEET_PAGE_SIZE)
    page = paginator.get_page(_int_param(request, "page", 1))
    on_page = list(page.object_list)
    stored = vuln_db.set_servers([server.id for server in on_page])
    visibility = store.get_settings()["visibility"]
    rows = []
    for server in on_page:
        found = stored.get(server.id) or {}
        scanning = _is_scanning(server.id)
        text = ""
        if not found and not scanning:
            text = _state_text(vuln_fleet.eligibility(server, visibility))
        family = server.os_family
        rows.append({
            "server_id": server.id,
            "hostname": server.hostname,
            "url": server.get_absolute_url() + SERVER_TAB_HASH,
            "os_name": found.get("os_name") or (family.name if family else ""),
            "vuln_critical": found.get("vuln_critical") or 0,
            "vuln_high": found.get("vuln_high") or 0,
            "kev": found.get("kev") or 0,
            "inventory_at": found.get("inventory_at") or "",
            "scanning": scanning,
            "state_text": text,
        })
    return JsonResponse({"total": paginator.count, "page": page.number, "page_size": FLEET_PAGE_SIZE,
                         "pages": paginator.num_pages, "rows": rows, "unavailable": False})


@_api
def vuln_resource_scan(request, resource_id):
    """POST {}: starts one job that scans the servers of the resource that THIS viewer may view and
    scan (vuln_fleet.scannable_ids), never the resource's other servers. Answers 202 {job_id,
    job_url}, 403 when none can be scanned, or the refusal's status and sentence (409 while a scan
    of the resource is running)."""
    if request.method != "POST":
        return JsonResponse({"error": "Use POST."}, status=405)
    resource, profile, ids = _resource_gate(request, resource_id)
    if resource is None:
        return JsonResponse({"error": RESOURCE_NOT_FOUND}, status=404)
    if _body(request) is None:
        return JsonResponse({"error": "The request body must be a JSON object."}, status=400)
    scannable = vuln_fleet.scannable_ids(profile, resource, store.get_settings()["visibility"]) if ids else []
    if not scannable:
        return JsonResponse({"error": NOTHING_TO_SCAN}, status=403)
    job = vuln_fleet.start_resource_scan(profile, resource, scannable, _base_url(request))
    job_id = getattr(job, "id", None)
    if not isinstance(job_id, int) or isinstance(job_id, bool):
        # A refusal: told apart by the integer id a Job has (as vuln_fleet does), not by isinstance on
        # a class that the job files reload.
        status = getattr(job, "status", None)
        return JsonResponse({"error": getattr(job, "message", "") or "The scan could not be started."},
                            status=status if isinstance(status, int) and not isinstance(status, bool) else 400)
    return JsonResponse({"job_id": job_id, "job_url": job.get_absolute_url()}, status=202)
