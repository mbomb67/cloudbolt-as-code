"""
Runs a search and shapes the result for the page, the JSON API and CSV.

A search reads every marked record in its date window (sources.py), narrows
them by the filters and builds the four sections (grouping.py). A session
filter without dates first finds the session's own first and last record in
the MAX_DAYS before today and searches exactly that span.
"""
import datetime

from django.conf import settings
from django.urls import reverse

from accounts.models import UserProfile
from history.models import HistoryModel
from jobs.models import Job
from orders.models import Order

from . import access, config, grouping, mcp_log, sources, xlsx
from .filters import NO_USER, lookback_window, make_window, parse_filters

STATUS_TONES = {
    "SUCCESS": "ok",
    "COMPLETE": "ok",
    "WARNING": "warn",
    "PENDING": "warn",
    "PAUSED": "warn",
    "FCTLPAUSED": "warn",
    "FAILURE": "bad",
    "DENIED": "bad",
    "CANCELED": "bad",
    "TERMINATED": "bad",
    "ACTIVE": "live",
    "RUNNING": "live",
    "QUEUED": "live",
    "INIT": "live",
}
ORDER_STATUSES = dict(getattr(Order, "ORDER_STATUS_TITLES", {}))
JOB_STATUSES = dict(getattr(Job, "STATUS_CHOICES", ()))
URL_NAMES = {"order": "order_detail", "job": "job_detail", "server": "server_detail", "resource": "resource_detail"}


class Result(object):
    def __init__(self, profile, filters, notices, events, matching, session):
        self.profile = profile
        self.filters = filters
        self.notices = notices
        self.events = events
        self.matching = matching
        self.session = session
        self.users = load_users(_user_ids(events) | (_user_ids(session["events"]) if session else set()))


def run(params, profile):
    """The search a query string asks for."""
    filters, notices = parse_filters(params)
    session = None
    if filters.session:
        last_day = filters.window.last_day if filters.dates_given else datetime.date.today()
        session = session_detail(filters.session, profile, last_day)
        if session is None:
            notices.append(
                "Session {} has no records in the {} days up to {}.".format(filters.session, config.MAX_DAYS, last_day)
            )
        elif not filters.dates_given:
            filters.window = make_window(session["first"].date(), session["last"].date(), notices=notices)
    events, more = sources.fetch_events(filters.window, profile)
    notices.extend(more)
    matching = [event for event in events if grouping.matches(event, filters)]
    return Result(profile, filters, notices, events, matching, session)


def session_detail(session, profile, last_day):
    """The session's whole span in the MAX_DAYS ending last_day, read with the session in the query."""
    window = lookback_window(last_day)
    events, _notices = sources.fetch_events(window, profile, session=session)
    rows = grouping.session_rows(events)
    if not rows:
        return None
    detail = rows[0]
    detail["clients"] = sorted({event["client"] for event in events})
    detail["events"] = events
    detail["may_start_earlier"] = detail["first"].date() <= window.first_day
    detail["lookback_from"] = window.first_day
    return detail


# ---------------------------------------------------------------------------
# Presentation
# ---------------------------------------------------------------------------
def _when(value):
    return value.strftime("%Y-%m-%d %H:%M") if value else ""


def _iso(value):
    return value.isoformat(sep=" ", timespec="seconds") if value else ""


def span_text(first, last):
    """'3 d 4 h', '2 h 5 min', '40 s'."""
    seconds = int((last - first).total_seconds()) if first and last else 0
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    minutes, seconds = divmod(rest, 60)
    if days:
        return "{} d {} h".format(days, hours)
    if hours:
        return "{} h {} min".format(hours, minutes)
    if minutes:
        return "{} min".format(minutes)
    return "{} s".format(seconds)


def _user_ids(events):
    ids = set()
    for event in events:
        ids.add(event["user_id"])
        if event.get("recipient_id"):
            ids.add(event["recipient_id"])
    return ids


def load_users(ids):
    users = {NO_USER: {"id": NO_USER, "name": "No user", "username": "", "url": ""}}
    wanted = [user_id for user_id in ids if user_id]
    rows = UserProfile.objects.filter(id__in=wanted).values_list(
        "id", "user__username", "user__first_name", "user__last_name"
    )
    for user_id, username, first, last in rows:
        name = " ".join(part for part in (first, last) if part) or username
        users[user_id] = {
            "id": user_id,
            "name": name,
            "username": username,
            "url": reverse("user_detail", args=[user_id]),
        }
    for user_id in wanted:
        users.setdefault(user_id, {"id": user_id, "name": "User {}".format(user_id), "username": "", "url": ""})
    return users


def _people(users, user_ids):
    """Users to show for a row. Records with no owner (written by the job engine) are
    left out when the row also has a real user."""
    ids = [user_id for user_id in user_ids if user_id != NO_USER] or list(user_ids)
    return [users[user_id] for user_id in ids]


def _link(kind, object_id, name):
    if not object_id:
        return None
    return {"id": object_id, "name": name, "url": reverse(URL_NAMES[kind], args=[object_id])}


def _status(code, titles):
    return {"code": code, "label": str(titles.get(code, code.title())), "tone": STATUS_TONES.get(code, "")}


def _marked(row):
    return {"agent": row["agent"], "client": row["client"], "session": row["session"]}


def present_session(row, users):
    data = _marked(row)
    data.update({
        "agents": sorted(row["agents"]) if len(row["agents"]) > 1 else [],
        "users": _people(users, sorted(row["user_ids"])),
        "first": _when(row["first"]),
        "last": _when(row["last"]),
        "first_iso": _iso(row["first"]),
        "last_iso": _iso(row["last"]),
        "span": span_text(row["first"], row["last"]),
        "orders": row["orders"],
        "jobs": row["jobs"],
        "history": row["history"],
    })
    return data


def present_order(row, users):
    data = _marked(row)
    data.update({
        "order": _link("order", row["order_id"], row["order_name"] or "Order {}".format(row["order_id"])),
        "status": _status(row["order_status"], ORDER_STATUSES),
        "events": row["events"],
        "users": _people(users, row["user_ids"]),
        "recipient": users.get(row["recipient_id"]) if row["recipient_id"] else None,
        "first": _when(row["first"]),
        "last": _when(row["last"]),
        "last_iso": _iso(row["last"]),
    })
    return data


def present_job(row, users):
    data = _marked(row)
    data.update({
        "job": _link("job", row["job_id"], "Job {}".format(row["job_id"])),
        "type": row["job_type_name"],
        "title": row["job_title"],
        "status": _status(row["job_status"], JOB_STATUSES),
        "parent": _link("job", row["parent_job_id"], "Job {}".format(row["parent_job_id"])),
        "user": users[row["user_id"]],
        "when": _when(row["when"]),
        "when_iso": _iso(row["when"]),
    })
    return data


def _other_object(history_id):
    """Name and link of a history record's object other than a server or resource, or None."""
    try:
        obj = HistoryModel.objects.get(id=history_id).related_object
    except Exception:  # noqa: BLE001 -- a deleted object or an old record without real_type
        return None
    try:
        url = obj.get_absolute_url()
    except Exception:  # noqa: BLE001 -- not every model has a detail page
        url = ""
    return {"id": getattr(obj, "id", None), "name": str(obj), "url": url}


def present_history(row, users, resolve_other=True):
    data = _marked(row)
    if row["kind"] in ("server", "resource"):
        obj = _link(row["kind"], row["object_id"], row["object_name"])
    else:
        obj = _other_object(row["history_id"]) if resolve_other else None
    data.update({
        "kind": row["kind"],
        "kind_label": row["kind_label"],
        "object": obj,
        "event_type": row["event_type"],
        "message": row["message"],
        "user": users[row["user_id"]],
        "job": _link("job", row["job_id"], "Job {}".format(row["job_id"])),
        "when": _when(row["when"]),
        "when_iso": _iso(row["when"]),
    })
    return data


PRESENTERS = {
    "sessions": present_session,
    "orders": present_order,
    "jobs": present_job,
    "history": present_history,
}


def section_rows(result, section):
    """Every row of one section, unpresented."""
    return grouping.BUILDERS[section](result.matching)


def client_apps(result, sessions):
    """{session: client app} from mcp.log, for viewers who may read it (CLIENT_APP_FROM_LOG)."""
    username = access.log_username(result.profile)
    if not config.CLIENT_APP_FROM_LOG or username is False:
        return {}
    return mcp_log.client_apps(sorted(set(sessions)), username=username)


def _add_client_apps(result, rows):
    """Set client_app on presented rows with one pass over the log."""
    apps = client_apps(result, [row["session"] for row in rows])
    for row in rows:
        row["client_app"] = apps.get(row["session"])
    return rows


def section_page(result, section, page=1):
    rows, info = grouping.paginate(section_rows(result, section), page)
    info["rows"] = _add_client_apps(result, [PRESENTERS[section](row, result.users) for row in rows])
    return info


def present_facets(result):
    counts = grouping.facet_counts(result.events, result.filters)
    users = result.users
    facets = {}

    user_options = []
    for user_id, entry in counts["user"].items():
        user = users.get(user_id) or {"name": "User {}".format(user_id), "username": ""}
        label = user["name"] if not user["username"] or user["username"] == user["name"] else "{} ({})".format(
            user["name"], user["username"]
        )
        user_options.append({
            "value": "none" if user_id == NO_USER else str(user_id),
            "label": label,
            "count": entry["count"],
        })
    facets["user"] = sorted(user_options, key=lambda option: option["label"].lower())

    facets["agent"] = sorted(
        ({"value": agent, "label": agent or "(blank)", "count": entry["count"]} for agent, entry in counts["agent"].items()),
        key=lambda option: (-option["count"], option["label"].lower()),
    )

    sessions = sorted(
        counts["session"].items(),
        key=lambda item: item[1]["last"] or datetime.datetime.min,
        reverse=True,
    )
    selected = result.filters.session
    shown = sessions[: config.MAX_SESSION_OPTIONS]
    if selected and all(value != selected for value, _entry in shown):
        shown += [item for item in sessions if item[0] == selected]
    facets["session"] = [
        {
            "value": value,
            "label": "{} · {}{}".format(value or "(none)", entry["agent"] or "?", " · " + _when(entry["last"]) if entry["last"] else ""),
            "count": entry["count"],
        }
        for value, entry in shown
        if value
    ]
    facets["session_more"] = max(0, len([item for item in sessions if item[0]]) - config.MAX_SESSION_OPTIONS)
    return facets


def present_session_detail(result):
    detail = result.session
    if not detail:
        return None
    data = present_session(detail, result.users)
    data.update({
        "clients": detail["clients"],
        "may_start_earlier": detail["may_start_earlier"],
        "lookback_from": detail["lookback_from"].isoformat(),
    })
    return data


def search_json(result, sections=grouping.SECTIONS):
    data = {
        "filters": result.filters.as_json(),
        "window": result.filters.window.as_json(),
        "notices": result.notices,
        "time_zone": settings.TIME_ZONE,
        "facets": present_facets(result),
        "counts": {},
        "sections": {},
        "session": present_session_detail(result),
    }
    for section in grouping.SECTIONS:
        rows = section_rows(result, section)
        data["counts"][section] = len(rows)
        if section in sections:
            page, info = grouping.paginate(rows, 1)
            info["rows"] = [PRESENTERS[section](row, result.users) for row in page]
            data["sections"][section] = info
    shown = [row for info in data["sections"].values() for row in info["rows"]]
    _add_client_apps(result, shown + ([data["session"]] if data["session"] else []))
    return data


def facets_json(result):
    return {
        "filters": result.filters.as_json(),
        "window": result.filters.window.as_json(),
        "notices": result.notices,
        "facets": present_facets(result),
    }


def recent_json(result):
    """Facets and the first page of sessions, newest first (decision 2)."""
    data = facets_json(result)
    data["time_zone"] = settings.TIME_ZONE
    data["sessions"] = section_page(result, "sessions", 1)
    return data


def _user_text(user):
    if not user:
        return ""
    return "{} ({})".format(user["name"], user["username"]) if user["username"] else user["name"]


def _abs(request, link):
    return request.build_absolute_uri(link["url"]) if link and link.get("url") else ""


def csv_rows(result, section, request):
    """(header, rows) for one section, every row the filters match."""
    users = result.users
    rows = section_rows(result, section)
    if section == "sessions":
        header = ["session", "agent", "client_app", "client_id", "users", "first_seen", "last_seen", "orders", "jobs",
                  "history_records"]
        apps = client_apps(result, [row["session"] for row in rows])
        body = [
            [row["session"], row["agent"], (apps.get(row["session"]) or {}).get("raw", ""), row["client"],
             "; ".join(_user_text(u) for u in _people(users, sorted(row["user_ids"]))),
             _iso(row["first"]), _iso(row["last"]), row["orders"], row["jobs"], row["history"]]
            for row in rows
        ]
    elif section == "orders":
        header = ["order_id", "order", "status", "events", "user", "recipient", "agent", "client_id", "session",
                  "first_seen", "last_seen", "url"]
        body = []
        for row in rows:
            item = present_order(row, users)
            body.append([
                row["order_id"], row["order_name"], item["status"]["label"], " > ".join(row["events"]),
                "; ".join(_user_text(user) for user in item["users"]), _user_text(item["recipient"]),
                row["agent"], row["client"], row["session"], _iso(row["first"]), _iso(row["last"]),
                _abs(request, item["order"]),
            ])
    elif section == "jobs":
        header = ["job_id", "type", "title", "status", "parent_job_id", "user", "agent", "client_id", "session",
                  "created", "url"]
        body = []
        for row in rows:
            item = present_job(row, users)
            body.append([
                row["job_id"], row["job_type_name"], row["job_title"], item["status"]["label"],
                row["parent_job_id"] or "", _user_text(item["user"]), row["agent"], row["client"], row["session"],
                _iso(row["when"]), _abs(request, item["job"]),
            ])
    else:
        header = ["time", "type", "object", "event_type", "message", "user", "job_id", "agent", "client_id",
                  "session", "url"]
        body = []
        for row in rows:
            # Objects other than servers and resources are named without a lookup per row.
            item = present_history(row, users, resolve_other=False)
            body.append([
                _iso(row["when"]), row["kind_label"], row["object_name"], row["event_type"], row["message"],
                _user_text(item["user"]), row["job_id"] or "", row["agent"], row["client"], row["session"],
                _abs(request, item["object"]),
            ])
    return header, body


SHEETS = (("orders", "Orders"), ("jobs", "Jobs"), ("history", "Server and resource history"))


def _summary_rows(result, request):
    filters = result.filters
    window = filters.window
    profile = result.profile
    rows = []
    detail = present_session_detail(result)
    if detail:
        app = (client_apps(result, [detail["session"]]) or {}).get(detail["session"])
        rows += [
            ("Session", detail["session"]),
            ("Client app", app["label"] + (" " + app["version"] if app and app["version"] else "") if app else ""),
            ("Client info (raw)", app["raw"] if app else ""),
            ("Connected through", app["bridge"] if app else ""),
            ("OAuth client", detail["agent"]),
            ("OAuth client id", ", ".join(client for client in detail["clients"] if client)),
            ("User", ", ".join(_user_text(user) for user in detail["users"])),
            ("First seen", detail["first_iso"]),
            ("Last seen", detail["last_iso"]),
            ("Span", detail["span"]),
            ("Orders", detail["orders"]),
            ("Jobs", detail["jobs"]),
            ("History records", detail["history"]),
        ]
    else:
        names = {"user": "User", "agent": "Agent", "q": "Text"}
        json_filters = filters.as_json()
        rows += [(label, json_filters[key]) for key, label in names.items() if json_filters[key]]
    rows += [
        ("Dates", "{} to {} ({})".format(window.first_day.isoformat(), window.last_day.isoformat(), settings.TIME_ZONE)),
        ("Exported", datetime.datetime.now().isoformat(sep=" ", timespec="seconds") + " " + settings.TIME_ZONE),
        ("Exported by", "{} ({})".format(profile.user.get_full_name() or profile.user.username, profile.user.username)),
        ("CloudBolt", request.build_absolute_uri("/")),
    ]
    rows += [("Note", notice) for notice in result.notices]
    return rows


def _tool_call_rows(result):
    """The session's tool calls from mcp.log, or None when the viewer may not read the log."""
    username = access.log_username(result.profile)
    if username is False or not result.filters.session:
        return None
    reply = mcp_log.session_lines(result.filters.session, username=username)
    header = ["time", "finished", "user", "tool", "mcp_tool_action", "result", "duration_ms", "error", "call_id",
              "client_info", "arguments"]
    rows = [
        [call["time"], call["done_time"], call["user"], call["tool"], call["action"], call["outcome"],
         "" if call["ms"] is None else call["ms"], call["error"], call["call"], call["client_info"], call["args"]]
        for call in reply["calls"]
    ]
    if reply["error"]:
        rows.append(["", "", "", "", "", "", "", reply["error"], "", "", ""])
    elif reply["limited"]:
        rows.append(["", "", "", "", "", "", "", "Newest calls only: older lines and rotated files are not read.",
                     "", "", ""])
    return header, rows


def workbook(result, request):
    """An .xlsx of the search: Summary, the record sections, and a session's tool calls."""
    book = xlsx.Workbook()
    book.add_sheet("Summary", ["field", "value"], _summary_rows(result, request))
    for section, title in SHEETS:
        header, rows = csv_rows(result, section, request)
        book.add_sheet(title, header, rows)
    if not result.filters.session:
        header, rows = csv_rows(result, "sessions", request)
        book.add_sheet("Sessions", header, rows)
    calls = _tool_call_rows(result)
    if calls:
        book.add_sheet("Tool calls", *calls)
    return book.to_bytes()
