"""
Filtering, facet counts and the rows of each result section, over the event
dicts that sources.py builds. No Django imports, so the rules can be checked
without CloudBolt.

Every event has: source ("order", "job" or "history"), agent, client,
session, marker, user_id (0 when the record has no owner), when (datetime) and
text (the lower-cased string the free-text filter searches). Order events add
order_id, order_name, order_status, recipient_id and event_type; job events
job_id, job_title, job_label, job_type_name, job_status and parent_job_id;
history events history_id, kind, kind_label, object_id, object_name,
event_type, message and job_id.
"""
import math
from collections import Counter

from . import config
from .filters import FACETS

SECTIONS = ("sessions", "orders", "jobs", "history")


def user_values(event):
    """The users an event counts for: its owner, and with RECIPIENT_IS_PERSONA an order's recipient."""
    values = {event["user_id"]}
    if config.RECIPIENT_IS_PERSONA and event.get("recipient_id"):
        values.add(event["recipient_id"])
    return values


def facet_values(event, facet):
    if facet == "user":
        return user_values(event)
    return {event[facet]}


def matches(event, filters, ignore=()):
    """Whether the event passes the text filter and every facet filter not in ignore."""
    if filters.text and filters.needle not in event["text"]:
        return False
    for facet in FACETS:
        if facet in ignore:
            continue
        wanted = filters.get(facet)
        if wanted is not None and wanted not in facet_values(event, facet):
            return False
    return True


def ignored_for(facet):
    """The filters that do not narrow this facet's own list (decision 5)."""
    if config.FACET_MODE == "cascade":
        return set(config.FACET_ORDER[config.FACET_ORDER.index(facet):])
    return {facet}


def row_key(event):
    """The result row an event lands in: one per order and session, job and session, or history record."""
    if event["source"] == "order":
        return ("order", event["order_id"], event["session"])
    if event["source"] == "job":
        return ("job", event["job_id"], event["session"])
    return ("history", event["history_id"])


def facet_counts(events, filters):
    """
    {facet: {value: {"count", "last", "agent"}}}: each list narrowed by the other
    filters. count is the number of order, job and history rows the value would
    show, so it matches the tabs.
    """
    result = {}
    for facet in FACETS:
        ignore = ignored_for(facet)
        seen = {}
        for event in events:
            if not matches(event, filters, ignore):
                continue
            for value in facet_values(event, facet):
                entry = seen.setdefault(value, {"rows": set(), "last": event["when"], "agent": event["agent"]})
                entry["rows"].add(row_key(event))
                if event["when"] >= entry["last"]:
                    entry["last"] = event["when"]
                    entry["agent"] = event["agent"]
        selected = filters.get(facet)
        if selected is not None and selected not in seen:
            seen[selected] = {"rows": set(), "last": None, "agent": ""}
        for entry in seen.values():
            entry["count"] = len(entry.pop("rows"))
        result[facet] = seen
    return result


def session_rows(events):
    """One row per session, newest activity first."""
    groups = {}
    for event in events:
        row = groups.get(event["session"])
        if row is None:
            row = groups[event["session"]] = {
                "session": event["session"],
                "first": event["when"],
                "last": event["when"],
                "agent": event["agent"],
                "client": event["client"],
                "agents": Counter(),
                "user_ids": set(),
                "order_ids": set(),
                "job_ids": set(),
                "history": 0,
            }
        if event["when"] < row["first"]:
            row["first"] = event["when"]
        if event["when"] >= row["last"]:
            row["last"] = event["when"]
            row["agent"] = event["agent"]
            row["client"] = event["client"]
        row["agents"][event["agent"]] += 1
        row["user_ids"].add(event["user_id"])
        if event["source"] == "order":
            row["order_ids"].add(event["order_id"])
        elif event["source"] == "job":
            row["job_ids"].add(event["job_id"])
        else:
            row["history"] += 1
    rows = sorted(groups.values(), key=lambda row: row["last"], reverse=True)
    for row in rows:
        row["orders"] = len(row.pop("order_ids"))
        row["jobs"] = len(row.pop("job_ids"))
    return rows


def order_rows(events):
    """One row per order and session: what the session did to the order, newest first."""
    groups = {}
    # Rows written in the same second keep the order they were written in.
    for event in sorted((e for e in events if e["source"] == "order"), key=lambda e: (e["when"], e["history_id"])):
        key = (event["order_id"], event["session"])
        row = groups.get(key)
        if row is None:
            row = groups[key] = {
                "order_id": event["order_id"],
                "order_name": event["order_name"],
                "order_status": event["order_status"],
                "recipient_id": event["recipient_id"],
                "user_ids": [],
                "agent": event["agent"],
                "client": event["client"],
                "session": event["session"],
                "first": event["when"],
                "last": event["when"],
                "events": [],
            }
        row["last"] = event["when"]
        if event["user_id"] not in row["user_ids"]:
            row["user_ids"].append(event["user_id"])
        if not row["events"] or row["events"][-1] != event["event_type"]:
            row["events"].append(event["event_type"])
    return sorted(groups.values(), key=lambda row: row["last"], reverse=True)


def job_rows(events):
    """One row per job and session, newest first."""
    rows = {}
    for event in events:
        if event["source"] != "job":
            continue
        key = (event["job_id"], event["session"])
        if key not in rows or event["when"] < rows[key]["when"]:
            rows[key] = event
    return sorted(rows.values(), key=lambda row: (row["when"], row["job_id"]), reverse=True)


def history_rows(events):
    """One row per server, resource or other history record, newest first."""
    rows = [event for event in events if event["source"] == "history"]
    return sorted(rows, key=lambda row: (row["when"], row["history_id"]), reverse=True)


BUILDERS = {
    "sessions": session_rows,
    "orders": order_rows,
    "jobs": job_rows,
    "history": history_rows,
}


def paginate(rows, page):
    pages = max(1, int(math.ceil(len(rows) / float(config.PAGE_SIZE))))
    page = min(max(1, page), pages)
    start = (page - 1) * config.PAGE_SIZE
    return rows[start:start + config.PAGE_SIZE], {"page": page, "pages": pages, "total": len(rows)}
