"""
Agent Activity XUI

Lists the orders, jobs and server, resource and other history records that AI
agents created or changed through CloudBolt's MCP server, by user, agent and
MCP session. It reads the attribution markers that the MCP agent attribution
patch (ps_008) writes into those records; without the patch the page is empty.

Three ways in, one page, plus a page per session:
- Admin > Custom Extensions > Agent Activity, for CB Admins, Super Admins and
  Global Viewers (CloudBolt lets only them open admin extensions).
- /xui/mcp-agent-activity/, the same page for any signed-in user.
- /xui/mcp-agent-activity/sessions/<id>/, one session: what it did and the
  tool calls it made, with a breadcrumb back to the list.
- An AI Agent Activity tab on a user's profile: on your own profile, and on
  anyone's for those who see every user's records.
Who sees which records is in access.py (decisions 1, 3 and 11 in config.py).
The page loads its data from the JSON endpoints under xui/mcp-agent-activity/
(GET, session auth).
"""
import csv
import functools
import io

from django.conf import settings
from django.core.exceptions import PermissionDenied
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, render
from django.urls import reverse

from accounts.models import UserProfile
from extensions.views import TabExtensionDelegate, admin_extension, tab_extension
from utilities.logger import ThreadLogger

from . import access, activity, config, grouping, markers, mcp_log
from .filters import make_window

logger = ThreadLogger(__name__)

TEMPLATES = "mcp_agent_activity/templates/"

# Folder name of this XUI and the admin view's function name; together they
# form the admin page URL /extensions/admin/<package>/<view>.
XUI_PACKAGE = "mcp_agent_activity"
PAGE_VIEW = "agent_activity"
FILTER_KEYS = ("from", "to", "user", "agent", "session", "q")
DENIED = "Agent Activity is available to CB Admins, Super Admins and Global Viewers."


def viewer_required(view):
    """PermissionDenied unless the profile may open Agent Activity (decision 1)."""

    @functools.wraps(view)
    def wrapper(request, *args, **kwargs):
        if not access.may_view(request.get_user_profile()):
            raise PermissionDenied(DENIED)
        return view(request, *args, **kwargs)

    return wrapper


def _admin_page_url():
    return reverse("render_admin_extension", kwargs={"package": XUI_PACKAGE, "view_id": PAGE_VIEW})


def _list_url(profile):
    """Where the breadcrumb goes back to: the admin page for those who can open it."""
    return _admin_page_url() if access.sees_all(profile) else reverse("mcp_agent_activity_page")


def _page_data(request, page_url, embedded=False, preset=None, session=""):
    """What the page script needs: endpoint URLs, settings, and the filters to start from."""
    profile = request.get_user_profile()
    window = make_window()
    own_log = access.log_username(profile)
    return {
        "urls": {
            "page": page_url,
            "full_page": reverse("mcp_agent_activity_page"),
            "session_page": reverse("mcp_agent_activity_session", args=["SESSION"]),
            "search": reverse("mcp_agent_activity_search"),
            "facets": reverse("mcp_agent_activity_facets"),
            "recent": reverse("mcp_agent_activity_recent"),
            "section": reverse("mcp_agent_activity_section", args=["SECTION"]),
            "export": reverse("mcp_agent_activity_export", args=["SECTION"]) if config.CSV_EXPORT_ENABLED else "",
            "xlsx": reverse("mcp_agent_activity_xlsx") if config.XLSX_EXPORT_ENABLED else "",
            "log": reverse("mcp_agent_activity_log", args=["SESSION"]) if own_log is not False else "",
        },
        "defaults": {"from": window.first_day.isoformat(), "to": window.last_day.isoformat()},
        # Filters from the address bar run a search at once; a preset only fills the form.
        "initial": {} if embedded or session else {key: request.GET.get(key, "") for key in FILTER_KEYS},
        "session": session,
        "preset": preset or {},
        "embedded": embedded,
        "scope": "all" if access.sees_all(profile) else "own",
        "own_log_lines": bool(own_log),
        "default_view": config.DEFAULT_VIEW,
        "facet_mode": config.FACET_MODE,
        "facet_order": list(config.FACET_ORDER),
        "max_days": config.MAX_DAYS,
        "default_days": config.DEFAULT_DAYS,
        "page_size": config.PAGE_SIZE,
        "time_zone": settings.TIME_ZONE,
    }


def _render_page(request, page_url, uplink=None, topnav="", session="", title="Agent Activity"):
    profile = request.get_user_profile()
    context = {
        "page_title": title,
        "uplink": uplink,
        "topnav": topnav,
        "denied": not access.may_view(profile),
        "denied_message": DENIED,
        "own_scope": not access.sees_all(profile),
        "data": _page_data(request, page_url, session=session),
        "csv": config.CSV_EXPORT_ENABLED,
    }
    return render(request, TEMPLATES + "agent_activity.html", context)


@admin_extension(
    title="Agent Activity",
    description="Orders, jobs and server and resource history that AI agents created or changed through "
    "CloudBolt's MCP server, by user, agent and session.",
)
def agent_activity(request):
    return _render_page(
        request, _admin_page_url(), uplink={"url": reverse("admin_home"), "label": "Admin"}, topnav="admin"
    )


def agent_activity_page(request):
    """The same page for any signed-in user, at /xui/mcp-agent-activity/."""
    return _render_page(request, reverse("mcp_agent_activity_page"))


def session_page(request, session):
    """One session's page: summary, records and tool calls, without the filters."""
    if not markers.is_session(session):
        raise Http404("Not a session id.")
    profile = request.get_user_profile()
    return _render_page(
        request,
        reverse("mcp_agent_activity_session", args=[session]),
        uplink={"url": _list_url(profile), "label": "Agent Activity"},
        topnav="admin" if access.sees_all(profile) else "",
        session=session,
        title="Session {}".format(session),
    )


def _may_see_user(viewer, target):
    return access.may_view(viewer) and (access.sees_all(viewer) or target.id == viewer.id)


class UserTabDelegate(TabExtensionDelegate):
    def should_display(self):
        return _may_see_user(self.viewer, self.instance)


# CloudBolt keeps one component per title in an extension, so the tab cannot
# share the admin page's title.
@tab_extension(
    model=UserProfile,
    title="AI Agent Activity",
    delegate=UserTabDelegate,
    description="This user's MCP agent sessions, orders, jobs and history.",
)
def user_agent_activity_tab(request, obj_id):
    profile = request.get_user_profile()
    target = get_object_or_404(UserProfile, pk=obj_id)
    if not _may_see_user(profile, target):
        raise PermissionDenied("Only CB Admins, Super Admins and Global Viewers see another user's Agent Activity.")
    context = {
        "denied": False,
        "own_scope": not access.sees_all(profile),
        "data": _page_data(
            request, reverse("mcp_agent_activity_page"), embedded=True, preset={"user": str(target.id)}
        ),
        "csv": config.CSV_EXPORT_ENABLED,
    }
    return render(request, TEMPLATES + "agent_activity_tab.html", context)


# ---------------------------------------------------------------------------
# JSON API (GET, session auth)
# ---------------------------------------------------------------------------
def _api(view):
    """Turn permission and unexpected errors into JSON error responses."""

    @functools.wraps(view)
    def wrapper(request, *args, **kwargs):
        if request.method != "GET":
            response = JsonResponse({"error": "Use GET."}, status=405)
            response["Allow"] = "GET"
            return response
        try:
            return view(request, *args, **kwargs)
        except PermissionDenied:
            return JsonResponse({"error": DENIED}, status=403)
        except Http404:
            return JsonResponse({"error": "Not found."}, status=404)
        except Exception:
            logger.exception("Agent Activity API call failed: %s", request.get_full_path())
            return JsonResponse({"error": "Unexpected error; see the CloudBolt application log."}, status=500)

    return wrapper


def _section(section):
    if section not in grouping.SECTIONS:
        raise Http404("Unknown section.")
    return section


def _error(status, message):
    return JsonResponse({"error": message}, status=status)


def _page(request):
    try:
        return max(1, int(request.GET.get("page", "1")))
    except ValueError:
        return 1


@_api
@viewer_required
def api_search(request):
    """Facets, counts, the first page of every section, and the session detail when a session is chosen."""
    result = activity.run(request.GET, request.get_user_profile())
    return JsonResponse(activity.search_json(result))


@_api
@viewer_required
def api_facets(request):
    """The user, agent and session lists for the current filters, without results."""
    result = activity.run(request.GET, request.get_user_profile())
    return JsonResponse(activity.facets_json(result))


@_api
@viewer_required
def api_recent(request):
    """Facets and the first page of sessions: the page before any search (decision 2)."""
    result = activity.run(request.GET, request.get_user_profile())
    return JsonResponse(activity.recent_json(result))


@_api
@viewer_required
def api_section(request, section):
    """?page=N of one section (sessions, orders, jobs or history)."""
    if section not in grouping.SECTIONS:
        return _error(404, "Unknown section; use sessions, orders, jobs or history.")
    result = activity.run(request.GET, request.get_user_profile())
    data = activity.section_page(result, section, _page(request))
    data["notices"] = result.notices
    return JsonResponse(data)


@_api
@viewer_required
def api_session_log(request, session):
    """mcp.log lines that name the session (decision 3); only the caller's own lines in the own scope."""
    username = access.log_username(request.get_user_profile())
    if username is False:
        return _error(404, "The MCP log section is not available (MCP_LOG_ENABLED, MCP_LOG_OWN_LINES in config.py).")
    if not markers.is_session(session):
        return _error(404, "Not a session id.")
    return JsonResponse(mcp_log.session_lines(session, username=username))


# ---------------------------------------------------------------------------
# CSV export (decision 7)
# ---------------------------------------------------------------------------
def _cell(value):
    """Text for a CSV cell; values a spreadsheet would run as a formula start with a quote."""
    text = "" if value is None else str(value)
    if text[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + text
    return text


@viewer_required
def export_csv(request, section):
    if not config.CSV_EXPORT_ENABLED:
        raise Http404("CSV export is turned off (CSV_EXPORT_ENABLED in config.py).")
    section = _section(section)
    result = activity.run(request.GET, request.get_user_profile())
    header, rows = activity.csv_rows(result, section, request)
    buffer = io.StringIO()
    buffer.write("﻿")  # so Excel reads UTF-8
    writer = csv.writer(buffer)
    writer.writerow(header)
    for row in rows:
        writer.writerow([_cell(value) for value in row])
    window = result.filters.window
    response = HttpResponse(buffer.getvalue(), content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = 'attachment; filename="agent-activity-{}-{}-to-{}.csv"'.format(
        section, window.first_day.isoformat(), window.last_day.isoformat()
    )
    return response


@viewer_required
def export_xlsx(request):
    """One workbook of the search (a session's when the session filter is set)."""
    if not config.XLSX_EXPORT_ENABLED:
        raise Http404("Excel export is turned off (XLSX_EXPORT_ENABLED in config.py).")
    result = activity.run(request.GET, request.get_user_profile())
    window = result.filters.window
    if result.filters.session:
        name = "agent-activity-session-{}.xlsx".format(result.filters.session)
    else:
        name = "agent-activity-{}-to-{}.xlsx".format(window.first_day.isoformat(), window.last_day.isoformat())
    response = HttpResponse(
        activity.workbook(result, request),
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    response["Content-Disposition"] = 'attachment; filename="{}"'.format(name)
    return response
