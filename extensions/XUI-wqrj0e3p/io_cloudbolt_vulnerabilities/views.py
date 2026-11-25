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
  vuln_fleet_servers) that return one page of the findings store each. Every
  one first brings the store up to date with CloudBolt's servers
  (vuln_fleet.reconcile_servers), so archived and deleted servers never show.
  The Servers view also filters, sorts and selects servers: vuln_fleet_server_filters
  lists the filter values, vuln_fleet_servers_select picks the servers "Select all"
  means, and vuln_fleet_servers_patch (POST) starts one Install Updates run for the
  selection through the Install Updates server action itself, so Requires approval
  and the per-server permission are honored.
- The resource tab rolls up the findings of the resource's own servers (not
  those of sub-resources) that the viewer may see: a shell page plus five
  routes (vuln_resource_summary, vuln_resource_cves, vuln_resource_cve_servers,
  vuln_resource_servers, vuln_resource_scan). It reuses the Vulnerability
  Search script (vuln_fleet_assets.html). Neither the tab dispatcher nor the
  delegate checks permissions, so every route starts with _viewable_resource
  and works only on the viewer's servers of that resource, the scan included.
  The summary also offers "Install updates on all servers" (a link to CloudBolt's own dialog for the resource
  action) and the result of the last update run; a run of kind patch is drawn as progress by the same script.

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

from . import (vuln_db, vuln_fleet, vuln_hooks, vuln_jobs, vuln_match, vuln_msrc, vuln_patch, vuln_repo,
               vuln_store as store, vuln_tools)

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
def _patch_offer(server, profile, request):
    """The Install updates button for this server as {"offer", "url", "label"}. Any failure, a missing patch module
    included, means no button: the panel never breaks because of it."""
    none = {"offer": False, "url": "", "label": ""}
    try:
        if vuln_patch.eligibility(server):
            return none
        button = vuln_hooks.patch_button(server, profile, request)
    except Exception:
        logger.exception("Vulnerabilities: cannot work out the Install updates button for server %s",
                         getattr(server, "id", ""))
        return none
    if not button or not button.get("url"):
        return none
    return {"offer": True, "url": button["url"], "label": button.get("label") or ""}


def _patch_last(state):
    """The stored record of the last update run, with the link to its job; None when there is none."""
    record = state.get("patch")
    if not isinstance(record, dict) or not record.get("status"):
        return None
    record = dict(record, job_url="")
    try:
        if record.get("job_id"):
            record["job_url"] = reverse("job_detail", args=[record["job_id"]])
    except Exception:
        logger.exception("Vulnerabilities: cannot build the job link of an update run")
    return record


def _panel_context(server, profile, request=None):
    _upgrade_store()  # readers never upgrade, and the summary selects the newest columns
    summary = vuln_db.server_summary(server.id)
    state = store.read_state(server.id)
    active = vuln_jobs.panel_active(server.id)
    last = state.get("last") or None
    last_failed = last if last and last.get("status") == "failed" else None
    patch_last = _patch_last(state)
    # The script re-fetches the table only when this changes, so it holds everything that
    # changes what the table shows: the match time, a scan starting or ending, the newest scan.
    key = "|".join([
        str((summary or {}).get("matched_at") or ""),
        "1" if active else "0",
        str((last or {}).get("scan_id") or ""),
        str((last or {}).get("status") or ""),
        str((patch_last or {}).get("job_id") or ""),
    ])
    tools = vuln_tools.status()
    windows = _is_windows(server)
    if summary and summary.get("db_built"):
        summary = dict(summary, db_built=_display_built(summary["db_built"]))
    if summary and windows:
        # The Windows "software list" is the build and the installed updates; its OS name ends in "build <number>".
        found = re.search(r"build\s+(\S+)\s*$", str(summary.get("os_name") or ""))
        summary = dict(summary, windows_build=found.group(1) if found else (summary.get("os_name") or ""))
    if active:
        patch = {"offer": False, "url": "", "label": ""}
    else:
        patch = _patch_offer(server, profile, request)
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
        "patch": patch,
        "patch_last": patch_last,
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
        context = _panel_context(server, profile, request)
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
def _resource_patch_action_state():
    """{"exists", "enabled", "action_id"} of the resource action that installs updates on all servers of a
    resource, or None when it cannot be read (the patch hooks may not be there yet): unknown is never an error."""
    try:
        state = vuln_hooks.resource_patch_action_state()
    except Exception:
        logger.exception("Vulnerabilities: cannot read the Install Updates on All Servers action")
        return None
    return state if isinstance(state, dict) else None


def _patch_action_state():
    """The Install Updates action's state for the settings page; never raises."""
    try:
        return vuln_hooks.patch_action_state()
    except Exception:
        logger.exception("Vulnerabilities: cannot read the Install Updates action")
        return {"exists": False, "enabled": False, "action_id": None}


PATCHING_ERROR = ("The settings were saved, but the Install Updates action could not be switched. "
                  "The CloudBolt application log has the details.")  # shown as a warning, not as a save error
RESOURCE_PATCHING_ERROR = ("The settings were saved, but the Install Updates on All Servers action could not be "
                           "switched. The CloudBolt application log has the details.")
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
            was_patching = bool(current.get("patching"))
            store.save_settings(clean)
            current = store.get_settings()
            saved = True
            # The settings are saved; a failure here is shown without losing them.
            try:
                vuln_hooks.apply_provision_setting(bool(clean.get("scan_on_provision")))
            except Exception:
                logger.exception("Vulnerabilities: cannot update the scan after provisioning action")
                post_warnings.append(PROVISION_ERROR)
            try:
                patching = bool(clean["patching"])
                # An unchanged setting must not re-enable an action the admin switched off in the action editor;
                # only a missing action (an appliance set up before the resource action existed) is created.
                resource_state = _resource_patch_action_state()
                missing = (not _patch_action_state()["exists"]) or (resource_state is not None
                                                                    and not resource_state.get("exists"))
                if patching != was_patching or (patching and missing):
                    # create_only: an unchanged setting creates what is missing and never re-enables an action.
                    vuln_hooks.apply_patching_setting(patching, create_only=(patching == was_patching))
            except Exception:
                logger.exception("Vulnerabilities: cannot update the Install Updates action")
                # The wrapper raises when either action could not be switched: name the one that did not follow.
                server_state, resource_state = _patch_action_state(), _resource_patch_action_state()
                server_off = bool(server_state.get("enabled")) != patching
                resource_off = resource_state is not None and bool(resource_state.get("enabled")) != patching
                if server_off or not resource_off:
                    post_warnings.append(PATCHING_ERROR)
                if resource_off:
                    post_warnings.append(RESOURCE_PATCHING_ERROR)
    context = {
        "patch_action": _patch_action_state(),
        "resource_patch_action": _resource_patch_action_state() or {"exists": False, "enabled": False, "action_id": None},
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
    """The page is a shell; its data arrives from the JSON routes. Each URL is built on its
    own so one failure leaves the others. The context is urls (summary, cves, cve_servers, servers,
    server_filters, servers_select, servers_patch), settings_url and page_error; the CSRF token is the
    template's own csrf_token."""
    context = {"urls": {}, "settings_url": "", "page_error": ""}
    for key, name in (("summary", "vuln_fleet_summary"), ("cves", "vuln_fleet_cves"),
                      ("cve_servers", "vuln_fleet_cve_servers"), ("servers", "vuln_fleet_servers"),
                      ("server_filters", "vuln_fleet_server_filters"), ("servers_select", "vuln_fleet_servers_select"),
                      ("servers_patch", "vuln_fleet_servers_patch")):
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
    _fresh_store()  # the hidden ids are ignored: the cards may count a hidden server until it is purged
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
    _fresh_store()  # the hidden ids are ignored: the counts may include a hidden server until it is purged
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
    _fresh_store()  # the hidden ids are ignored: a hidden server may be listed until it is purged
    result = _with_built_display(_with_urls(vuln_db.cve_servers(
        cve, search=request.GET.get("q", "").strip()[:100], **_page_args(request))))
    result["cve_info"] = _cve_info(cve)
    return JsonResponse(result)


BULK_ERROR = "Install updates could not be checked. The CloudBolt application log has the details."
NOT_OFFERED = "The Install Updates action is not offered for this server."
PATCH_START_ERROR = "Install updates could not be started. The CloudBolt application log has the details."
# "Select all N matching" looks at most this many times the cap into the matching servers, and never more than the
# ceiling, so one request costs a bounded number of state-file reads and permission checks.
SELECT_SCAN_FACTOR = 4
SELECT_SCAN_CEILING = 400
SKIPPED_LISTED = 20
MAX_IDS_TEXT = 2000


def _reconcile(force=False):
    """Make the findings store match CloudBolt's servers before a read (vuln_fleet.reconcile_servers: throttled,
    bounded) and return the ids of the servers the readers must hide. Never raises: a failure is logged and nothing
    is hidden."""
    try:
        result = vuln_fleet.reconcile_servers(force=force)
        hidden = []
        for value in (result or {}).get("hidden") or []:
            try:
                hidden.append(int(value))
            except (TypeError, ValueError):
                continue
        return hidden
    except Exception:
        logger.exception("Vulnerabilities: cannot bring the findings store up to date with the servers")
        return []


def _fresh_store():
    """The store upgraded and reconciled, in that order (the reconcile writes the newest columns), and the ids to
    hide. Called at the start of every fleet read route."""
    _upgrade_store()
    return _reconcile()


def _server_filter_args(request):
    """The keyword arguments of vuln_db.fleet_servers and fleet_server_ids that the query string sets: q (100
    characters at most), kev=1, fix=1, group, environment, os_build and owner (one exact value each, 200 at most,
    vuln_db.NONE_VALUE for none of os_build or owner), sort (only a key of vuln_db.FLEET_SERVER_SORTS, else "") and
    dir (desc, else asc). The values are bound by the store, and sort picks one of its fixed expressions."""
    def text(name, limit=200):
        return str(request.GET.get(name, ""))[:limit]

    sort = text("sort", 40)
    if sort not in vuln_db.FLEET_SERVER_SORTS:
        sort = ""
    return {
        "search": text("q", 400).strip()[:100],
        "kev_only": request.GET.get("kev") == "1",
        "group": text("group"),
        "environment": text("environment"),
        "os_build": text("os_build"),
        "owner": text("owner"),
        "fix_only": request.GET.get("fix") == "1",
        "sort": sort,
        "direction": "desc" if sort and request.GET.get("dir") == "desc" else "asc",
    }


def _row_ids(result):
    ids = []
    for row in (result or {}).get("rows") or []:
        try:
            ids.append(int(row.get("server_id")))
        except (AttributeError, TypeError, ValueError):
            continue
    return ids


def _load_servers(ids):
    """{id: Server} of the servers that exist, in ONE query with what the update checks read, or None when the
    query failed (logged). A server that does not exist is simply absent."""
    if not ids:
        return {}
    try:
        found = (Server.objects.filter(id__in=list(ids))
                 .select_related("group", "os_family__parent__parent", "resource_handler", "owner__user")
                 .prefetch_related("tags"))
        return {server.id: server for server in found}
    except Exception:
        logger.exception("Vulnerabilities: cannot read the servers of a page")
        return None


def _bulk_max():
    try:
        return max(1, int(store.get_settings()["bulk_patch_max"]))
    except Exception:
        logger.exception("Vulnerabilities: cannot read the bulk update limit")
        return store.DEFAULT_SETTINGS["bulk_patch_max"]


def _url(name, fallback=""):
    try:
        return reverse(name)
    except Exception:
        logger.exception("Vulnerabilities: cannot build the URL %s", name)
        return fallback


def _job_link(job_id):
    try:
        return reverse("job_detail", args=[job_id])
    except Exception:
        logger.exception("Vulnerabilities: cannot build the link of job %s", job_id)
        return ""


def _bulk_object(state, maximum):
    """The "bulk" object of the Servers answer from vuln_hooks.patch_bulk_action()'s state."""
    return {
        "enabled": bool(state.get("enabled")),
        "why": state.get("why") or "",
        "max": maximum,
        "action_label": state.get("action_label") or vuln_hooks.PATCH_ACTION_LABEL,
        "restart_default": bool(state.get("restart_default", True)),
        "requires_approval": bool(state.get("requires_approval", False)),
        "run_url": _url("vuln_fleet_servers_patch"),
        "select_url": _url("vuln_fleet_servers_select"),
    }


def _row_patch_state(profile, server, action, context, queued, full=False, request=None):
    """{"selectable", "why", "running"} of one server for a bulk Install updates, the first failing check wins:
    the server must exist; no scan or update may hold it (its state file's active marker, running = {job_id,
    job_url, kind, queued false}); it may not be waiting in an update job that has not ended (queued, a dict of
    server id to job id: running.queued true); vuln_fleet.quick_eligibility must find no problem; and the viewer
    must be permitted to run the Install Updates action on it (server_action_permitted: instant for the admins
    this page is for, as it answers before it asks the database).

    The page rows and "Select all" stop there (full false) because they must cost no query per server: context is
    {"problems", "unassigned", "visibility"}, each read once per request, and the quick check reads only what
    _load_servers loaded. "No way to run a script", a missing address or credentials and the other things only a
    remote or per-server read can tell are found at submit time or by the job. The submit (full true) is bounded
    by the selection limit, so it also runs the whole vuln_patch.eligibility and asks core whether the action is
    offered for the server (ServerAction.server_action_dict, which evaluates the action's Display Condition and the
    plug-in's OS family, group, environment and technology rules; the same test core's server lists apply). A
    refusal there is worded like the rest. Reads one state file per server; no remote call."""
    if server is None:
        return {"selectable": False, "why": "This server no longer exists.", "running": None}
    try:
        found = vuln_jobs.active_job(server.id)
    except Exception:
        # An unreadable state file is no proof of a running job; the job itself refuses a busy server.
        logger.exception("Vulnerabilities: cannot read the scan state of server %s", server.id)
        found = None
    if found is not None:
        marker, job = found
        kind = "patch" if marker.get("kind") == vuln_jobs.PATCH_KIND else "scan"
        running = {"job_id": job.id, "job_url": job.get_absolute_url(), "kind": kind, "queued": False}
        return {"selectable": False,
                "why": "A scan or update is already running on this server (job {}).".format(job.id),
                "running": running}
    waiting = queued.get(server.id)
    if waiting:
        running = {"job_id": waiting, "job_url": _job_link(waiting), "kind": "patch", "queued": True}
        return {"selectable": False, "why": "Waiting in update job {}.".format(waiting), "running": running}
    try:
        code = vuln_fleet.quick_eligibility(server, context["problems"], context["unassigned"],
                                            context["visibility"])
        if not code and full:
            code = vuln_patch.eligibility(server, context["problems"])
        if code:
            return {"selectable": False,
                    "why": "Cannot install updates: {}.".format(vuln_fleet.REASONS.get(code, code)), "running": None}
        # Imported here, as vuln_hooks does: the pages work whatever happens to this module.
        from utilities.permissions import server_action_permitted
        if not server_action_permitted(profile, server, action):
            return {"selectable": False, "why": "You do not have permission to install updates on this server.",
                    "running": None}
        if full and not action.server_action_dict(server, request):
            return {"selectable": False, "why": NOT_OFFERED, "running": None}
    except Exception:
        logger.exception("Vulnerabilities: cannot work out whether server %s may be updated", server.id)
        return {"selectable": False,
                "why": "Cannot install updates: {}.".format(vuln_fleet.REASONS["unknown"]), "running": None}
    return {"selectable": True, "why": "", "running": None}


def _patch_states(profile, ids, servers, action, full=False, request=None):
    """{server id: _row_patch_state} for the ids: the prerequisites, the waiting servers, the Unassigned group
    ids and the settings are read once for all of them. Raises when those cannot be read, so the caller can
    switch the bulk update off. full is for the submit only (see _row_patch_state)."""
    context = {
        "problems": vuln_fleet.prerequisites(vuln_fleet.default_base_url()),
        "unassigned": vuln_fleet.unassigned_ids(),
        "visibility": store.get_settings()["visibility"],
    }
    queued = vuln_hooks.queued_update_servers()
    return {server_id: _row_patch_state(profile, servers.get(server_id), action, context, queued, full, request)
            for server_id in ids}


def _fleet_servers_page(filters, page_args):
    """(result, servers) of one page. When a listed server is gone from CloudBolt or not current (the 15 second
    throttle can leave one in the store) the store is reconciled once, now, and the page is read once more."""
    hidden = _reconcile()
    result = vuln_db.fleet_servers(hide_ids=hidden, **filters, **page_args)
    servers = _load_servers(_row_ids(result))
    if servers is not None and any(
            server_id not in servers or servers[server_id].status not in vuln_fleet.CURRENT_STATUSES
            for server_id in _row_ids(result)):
        hidden = _reconcile(force=True)
        result = vuln_db.fleet_servers(hide_ids=hidden, **filters, **page_args)
        servers = _load_servers(_row_ids(result))
    return result, servers


@_api
@cbadmin_required
def vuln_fleet_servers(request):
    """GET ?q=&kev=1&fix=1&group=&environment=&os_build=&owner=&sort=&dir=&page=: one page of the scanned servers
    that are current in CloudBolt, worst first unless sort says otherwise (_server_filter_args). The store is
    brought up to date first (_fresh_store) and a page that still shows a server CloudBolt no longer has is
    read again once (_fleet_servers_page). Each row has url (its Vulnerabilities tab) and, when bulk updating is
    available, patch = {selectable, why, running}; running is null or {job_id, job_url, kind, queued} for a server
    held by a scan or an update (queued: waiting in an update job). bulk = {enabled, why, max, action_label,
    restart_default, requires_approval, run_url, select_url}; when it is not enabled, why says why and the rows carry no patch. The
    page size is fixed; a client page_size is ignored. Costs: one Server query for the page, the prerequisites and
    the waiting servers once, one state file per row."""
    if request.method != "GET":
        return JsonResponse({"error": "Use GET."}, status=405)
    _upgrade_store()
    result, servers = _fleet_servers_page(_server_filter_args(request), _page_args(request))
    rows = result.get("rows") or []
    ids = _row_ids(result)
    state, action = vuln_hooks.patch_bulk_action()
    bulk = _bulk_object(state, _bulk_max())
    if servers is None:
        bulk = dict(bulk, enabled=False, why=BULK_ERROR)
        servers = {}
    for row in rows:
        server = servers.get(row.get("server_id"))
        if server is not None:
            row["url"] = server.get_absolute_url() + SERVER_TAB_HASH
    if bulk["enabled"] and ids:
        try:
            states = _patch_states(request.get_user_profile(), ids, servers, action)
            for row in rows:
                row["patch"] = states.get(row.get("server_id"))
        except Exception:
            logger.exception("Vulnerabilities: cannot work out which servers of a page may be updated")
            bulk = dict(bulk, enabled=False, why=BULK_ERROR)
    result["bulk"] = bulk
    return JsonResponse(_with_built_display(result))


@_api
@cbadmin_required
def vuln_fleet_server_filters(request):
    """GET: what the Servers view's filters offer, from the current servers in the store:
    {state, group, environment, os_build, owner, truncated}. group and environment are lists of strings (sorted
    case-insensitively, 500 at most each); os_build and owner are lists of {value, label}, with a last entry whose
    value is vuln_db.NONE_VALUE ("No OS build", "No owner") when a server has none. state is "ready", "empty" (no
    server) or "unavailable" (the store cannot be read now; the page retries)."""
    if request.method != "GET":
        return JsonResponse({"error": "Use GET."}, status=405)
    hidden = _fresh_store()
    found = vuln_db.fleet_server_options(hide_ids=hidden)

    def labelled(key, none_label, has_none):
        options = [{"value": value, "label": value} for value in found.get(key) or []]
        if has_none:
            options.append({"value": vuln_db.NONE_VALUE, "label": none_label})
        return options

    answer = {
        "group": list(found.get("group") or []),
        "environment": list(found.get("environment") or []),
        "os_build": labelled("os_build", "No OS build", found.get("has_no_os_build")),
        "owner": labelled("owner", "No owner", found.get("has_no_owner")),
        "truncated": bool(found.get("truncated")),
    }
    if found.get("unavailable"):
        state = "unavailable"
    elif any(answer[key] for key in ("group", "environment", "os_build", "owner")):
        state = "ready"
    else:
        state = "empty"
    answer["state"] = state
    return JsonResponse(answer)


@_api
@cbadmin_required
def vuln_fleet_servers_select(request):
    """GET (the list's filters and sort): the servers "Select all N matching" picks, in the order the list shows:
    {total, max, ids, hostnames, scanned, skipped, skipped_count, complete}. At most bulk_patch_max ids, the first
    ones that _row_patch_state finds selectable among the first max x 4 matching servers (400 at most). skipped
    lists up to 20 of the servers looked at that cannot be updated, {id, hostname, why}, skipped_count counts them
    all. complete is true only when every matching server was looked at and every selectable one fit under max.
    409 {error: why} while bulk updating is not available."""
    if request.method != "GET":
        return JsonResponse({"error": "Use GET."}, status=405)
    filters = _server_filter_args(request)
    hidden = _fresh_store()
    state, action = vuln_hooks.patch_bulk_action()
    if not state.get("enabled"):
        return JsonResponse({"error": state.get("why") or BULK_ERROR}, status=409)
    maximum = _bulk_max()
    found = vuln_db.fleet_server_ids(hide_ids=hidden, limit=min(maximum * SELECT_SCAN_FACTOR, SELECT_SCAN_CEILING),
                                     **filters)
    ids = [int(i) for i in found.get("ids") or []]
    answer = {"total": int(found.get("total") or 0), "max": maximum, "ids": [], "hostnames": {}, "scanned": 0,
              "skipped": [], "skipped_count": 0, "complete": True}
    if found.get("unavailable"):
        answer["unavailable"] = True
        return JsonResponse(answer)
    servers = _load_servers(ids)
    if servers is None:
        return JsonResponse({"error": BULK_ERROR}, status=500)
    states = _patch_states(request.get_user_profile(), ids, servers, action)
    overflow = False
    for server_id in ids:
        found_state = states[server_id]
        if found_state["selectable"]:
            if len(answer["ids"]) >= maximum:
                overflow = True
                break
            answer["ids"].append(server_id)
            answer["hostnames"][str(server_id)] = servers[server_id].hostname
        else:
            answer["skipped_count"] += 1
            if len(answer["skipped"]) < SKIPPED_LISTED:
                server = servers.get(server_id)
                answer["skipped"].append({"id": server_id,
                                          "hostname": server.hostname if server else "Server {}".format(server_id),
                                          "why": found_state["why"]})
        answer["scanned"] += 1
    answer["complete"] = not overflow and len(ids) >= answer["total"]
    return JsonResponse(answer)


def _parse_ids(text, maximum):
    """The unique positive integers of a comma-separated string, in order, or None when the text is not that
    (an empty token, a sign, a non-digit, text over 2000 characters) or has no id or more than maximum."""
    if not isinstance(text, str) or len(text) > MAX_IDS_TEXT:
        return None
    ids = []
    for token in text.split(","):
        token = token.strip()
        if not (token.isascii() and token.isdigit()) or int(token) < 1:
            return None
        if int(token) not in ids:
            ids.append(int(token))
    return ids if 1 <= len(ids) <= maximum else None


def _order_answer(order, servers, submitted):
    """The "approval" answer for the order core made for the action. A submitted order (the action has Auto
    submit order) is the order that waits for approval; an order that is not submitted is the viewer's current
    order, in which the action only sits until they submit it."""
    order_id = order.id
    try:
        link = reverse("order_detail", args=[order_id]) if submitted else reverse("current_order")
    except Exception:
        logger.exception("Vulnerabilities: cannot build the link of order %s", order_id)
        link = ""
    answer = {"state": "approval", "order_id": order_id, "order_url": link, "servers": servers,
              "submitted": submitted}
    if submitted:
        # An approval hook may approve the order on the spot; its jobs then exist and nothing waits.
        answer["approved"] = getattr(order, "status", "") not in ("CART", "PENDING")
    return answer


@_api
@cbadmin_required
def vuln_fleet_servers_patch(request):
    """POST ids=<comma-separated server ids>&restart=1|0: starts ONE Install Updates run for the servers through
    the Install Updates server action itself, the way the last step of CloudBolt's own bulk server action dialog
    does, so Requires approval, Auto submit order and the per-server permission are honored. Steps: 409 {error}
    while bulk updating is not available; 400 {error} unless ids are 1 to bulk_patch_max unique positive
    integers; 409 {error, refused: [{id, hostname, why}]} and nothing started when any server cannot be updated
    now (the checks of the list plus the whole vuln_patch.eligibility and whether the action is offered for the
    server: _row_patch_state with full); then 200 {state: "started", job_id, job_url, servers}
    for a job, 200 {state: "approval", order_id, order_url, servers, submitted[, approved]} for an order, or 500
    {error} when the action started nothing or raised. It never calls vuln_patch.start_job, which does not
    honor Requires approval."""
    if request.method != "POST":
        return JsonResponse({"error": "Use POST."}, status=405)
    state, action = vuln_hooks.patch_bulk_action()
    if not state.get("enabled"):
        return JsonResponse({"error": state.get("why") or BULK_ERROR}, status=409)
    maximum = _bulk_max()
    ids = _parse_ids(request.POST.get("ids", ""), maximum)
    if ids is None:
        return JsonResponse({"error": "Select between 1 and {} servers.".format(maximum)}, status=400)
    try:
        profile = request.get_user_profile()
        servers = _load_servers(ids)
        if servers is None:
            raise RuntimeError("the servers could not be read")
        states = _patch_states(profile, ids, servers, action, full=True, request=request)
        refused = [{"id": server_id,
                    "hostname": servers[server_id].hostname if server_id in servers else "Server {}".format(server_id),
                    "why": states[server_id]["why"]} for server_id in ids if not states[server_id]["selectable"]]
        if refused:
            return JsonResponse({"error": "{} of the selected servers can no longer be updated.".format(len(refused)),
                                 "refused": refused}, status=409)
        restart = bool(vuln_patch.parse_restart_flag(request.POST.get("restart"), state.get("restart_default", True)))
        targets = [servers[server_id] for server_id in ids]
        # The context core's bulk dialog builds from the form: the input under its clean name as a bool, the
        # names of the inputs that carry a value (cbhooks/services/parsing.py get_hook_action_input_dict), and
        # bulk_changes (infrastructure/views.py servers_batch_server_actions).
        context = {"__sensitive_keys": [], "__untrusted_expressions": ["restart_if_needed"],
                   "restart_if_needed": restart, "bulk_changes": True}
        response = action.run_hook_as_job(
            owner=profile, servers=targets, context=context, scheduled_time=None,
            job_display_label="Install updates: {}".format(
                targets[0].hostname if len(targets) == 1 else "{} servers".format(len(targets))))
        if isinstance(response, list) and response and isinstance(getattr(response[0], "id", None), int):
            job = response[0]
            return JsonResponse({"state": "started", "job_id": job.id, "job_url": job.get_absolute_url(),
                                 "servers": len(targets)})
        if isinstance(response, tuple) and response and isinstance(getattr(response[0], "id", None), int):
            return JsonResponse(_order_answer(response[0], len(targets), True))
        order = getattr(response, "order", None)
        if isinstance(getattr(order, "id", None), int):
            return JsonResponse(_order_answer(order, len(targets), False))
        raise RuntimeError("the action started nothing: {!r}".format(response))
    except Exception:
        logger.exception("Vulnerabilities: cannot start Install updates on %s servers", len(ids))
        return JsonResponse({"error": PATCH_START_ERROR}, status=500)


# ---------------------------------------------------------------------------
# Resource tab (the findings of one resource's own servers)
# ---------------------------------------------------------------------------
RESOURCE_NOT_FOUND = "This resource was not found."
NOTHING_TO_SCAN = ("None of the servers of this resource can be scanned right now. They may be powered off or in an "
                   "Unassigned group, or you may not have permission to scan them.")


class VulnerabilitiesResourceTabDelegate(TabExtensionDelegate):
    """Resources with at least one live Linux or Windows server that would get the server tab. Runs on every view of
    every resource page for every user, so it is one query and never raises. It checks no permission:
    the tab and its routes restrict everything to the servers the viewer may see."""

    def should_display(self):
        try:
            # vuln_fleet.supported_q: Linux or Windows at the server's own OS family or one or two levels up, as a
            # filter so should_display costs one query and no Python loop over the servers.
            servers = self.instance.server_set.exclude(status="HISTORICAL").filter(vuln_fleet.supported_q())
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


def _resource_patch_offer(resource, profile, request, visibility):
    """The Install updates on all servers button of the resource as {"offer", "url", "label"}, or None.
    The caller has checked that no run is live and that the viewer sees servers. The action must be enabled and
    listed for this resource and permitted to the viewer (vuln_hooks.resource_patch_button) and at least one
    server must be patchable by the viewer (any_patchable stops at the first one). Any failure, a missing patch module included, means no button."""
    try:
        # Imported here, not at the top: the tab and every other view work whatever happens to this module.
        from . import vuln_patch_resource
        button = vuln_hooks.resource_patch_button(resource, profile, request)
        url = (button or {}).get("url") or ""
        if not url or not str(url).startswith("/"):
            return None
        if not vuln_patch_resource.any_patchable(profile, resource, visibility):
            return None
        return {"offer": True, "url": url, "label": button.get("label") or ""}
    except Exception:
        logger.exception("Vulnerabilities: cannot work out the Install updates on all servers button of resource %s",
                         getattr(resource, "id", ""))
        return None


def _resource_patch_last(resource_id):
    """The record of the last update run of the resource as {at, by, status, updated, restart_pending, failed,
    skipped, job_url}, or None; no_updates, the number of servers that had nothing to install, is added when the
    record counts it. Any failure means no line."""
    try:
        record = store.read_resource_patch(resource_id)
        if not isinstance(record, dict) or not record.get("status"):
            return None
        counts = record.get("counts") if isinstance(record.get("counts"), dict) else {}

        def count(name):
            try:
                return max(int(counts.get(name) or 0), 0)
            except (TypeError, ValueError):
                return 0

        job_url = ""
        if record.get("job_id"):
            job_url = reverse("job_detail", args=[record["job_id"]])
        return {"at": record.get("at") or "", "by": record.get("by") or "", "status": record["status"],
                "updated": count("updated"), "restart_pending": count("restart_pending"),
                "failed": count("failed"), "skipped": count("skipped"), "no_updates": count("no_updates"),
                "job_url": job_url}
    except Exception:
        logger.exception("Vulnerabilities: cannot read the update record of resource %s", resource_id)
        return None


@_api
def vuln_resource_summary(request, resource_id):
    """GET: {state, summary, db_built, run, can_scan, patch, patch_last} for the viewer's servers of the resource.
    summary is vuln_db.set_summary with servers (the same number as scanned, the name the shared
    script reads) and servers_total (the viewer's servers) added. state is "ready" (at least one
    server has a software list), "empty" or "unavailable" (a store exists but cannot be read now;
    the page retries). run is the scan in progress, or null (always null for a viewer who can see
    none of the resource's servers). can_scan is whether the viewer may scan at least one server
    (vuln_fleet.any_scannable); it is null while a run is in progress.
    patch is the "Install updates on all servers" button, {offer, url, label}, or null: it is set only
    when no run is live, the viewer sees servers, the setting is on, the action is enabled, listed for the
    resource and permitted to the viewer, and at least one server is patchable by the viewer. patch_last is the
    record of the last update run, {at, by, status, updated, restart_pending, failed, skipped, job_url}, or null
    (null while a run is live). run.kind is "scan" or "patch".
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
    patch, patch_last = None, None
    if run is None and ids:
        patch_last = _resource_patch_last(resource.id)
        try:
            visibility = store.get_settings()["visibility"]
        except Exception:
            logger.exception("Vulnerabilities: cannot read the settings")
            visibility = None
        if visibility is not None:
            patch = _resource_patch_offer(resource, profile, request, visibility)
    return JsonResponse({"state": state, "summary": summary, "db_built": _display_built(db_built), "run": run,
                         "can_scan": can_scan, "patch": patch, "patch_last": patch_last})


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
