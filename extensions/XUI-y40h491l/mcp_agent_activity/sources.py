"""
Reads the records the MCP agent attribution patch (ps_008) marks, through the
ORM, and turns each into an event dict (fields listed in grouping.py).

Every query is bounded by a window on an indexed column:
- OrderHistory and the other HistoryModel subclasses by action_time, which
  HistoryModel indexes.
- Job progress messages by job id. Job.created_date has no index, so the
  window is turned into a job id range first: the newest job created before
  each edge, found by walking the primary key down from the newest job. Rows
  outside the window are then dropped by created_date.
Each source returns at most MAX_ROWS_PER_SOURCE rows, newest first.
"""
import datetime
import functools
import html
from collections import Counter

from django.contrib.contenttypes.models import ContentType
from django.db.models import Q
from django.utils.html import strip_tags

from history.models import HistoryModel, OrderHistory
from jobs.models import Job, ProgressMessage

from . import access, config, markers

MESSAGE_CHARS = 300
JOB_ID_MARGIN = datetime.timedelta(days=1)


def restricted(profile):
    """Whether this profile sees only its own records (decision 1): orders it owns or
    acted on, jobs it owns, and history written under its name."""
    return not access.sees_all(profile)


def _capped(rows, label, notices):
    if len(rows) > config.MAX_ROWS_PER_SOURCE:
        notices.append(
            "Only the newest {:,} {} records were read. Narrow the dates to see older ones.".format(
                config.MAX_ROWS_PER_SOURCE, label
            )
        )
        return rows[: config.MAX_ROWS_PER_SOURCE]
    return rows


def _text(*parts):
    return " ".join(str(part) for part in parts if part not in (None, "")).lower()


@functools.lru_cache(maxsize=1)
def _job_type_names():
    return {job_type: str(name) for job_type, name, _module in Job.JOB_TYPES}


def _kind(real_type_id):
    """('server', 'Server') for a ServerHistory content type id."""
    if not real_type_id:
        return "history", "History"
    content_type = ContentType.objects.get_for_id(real_type_id)
    kind = content_type.model[: -len("history")] if content_type.model.endswith("history") else content_type.model
    model = content_type.model_class()
    label = str(model._meta.verbose_name) if model else content_type.model
    if label.lower().endswith(" history"):
        label = label[: -len(" history")]
    return kind, label[:1].upper() + label[1:]


def order_events(window, profile, notices, session=None):
    qs = OrderHistory.objects.filter(
        action_time__gte=window.start,
        action_time__lt=window.end,
        event_message__contains=markers.PREFIX,
    )
    if session:
        qs = qs.filter(event_message__contains=markers.session_needle(session))
    if restricted(profile):
        qs = qs.filter(Q(owner=profile) | Q(order__owner=profile))
    rows = list(
        qs.order_by("-action_time", "-id").values_list(
            "id",
            "order_id",
            "owner_id",
            "action_time",
            "event_type",
            "event_message",
            "order__name",
            "order__status",
            "order__recipient",
            "order__owner",
        )[: config.MAX_ROWS_PER_SOURCE + 1]
    )
    events = []
    for row_id, order_id, owner_id, when, event_type, message, name, status, recipient_id, order_owner_id in _capped(
        rows, "order history", notices
    ):
        marker = markers.last_marker(message)
        if marker is None:
            continue
        events.append({
            "source": "order",
            "history_id": row_id,
            "agent": marker.agent,
            "client": marker.client,
            "session": marker.session,
            "marker": marker.raw,
            # Rows the job engine writes (APPROVED, MODIFICATION, COMPLETED) have no
            # owner; they belong to the order's owner.
            "user_id": owner_id or order_owner_id or 0,
            "when": when,
            "order_id": order_id,
            "order_name": name or "",
            "order_status": status or "",
            "recipient_id": recipient_id,
            "event_type": event_type,
            "text": _text(name, "order #{}".format(order_id), "#{}".format(order_id), marker.raw),
        })
    return events


def job_id_range(window):
    """(floor, ceiling): jobs created in the window have floor < id <= ceiling; None when there are none.

    Job ids follow created_date except across a clock change (CloudBolt stores
    naive local time), so each edge is widened by JOB_ID_MARGIN and the exact
    window is applied to created_date afterwards.
    """
    ceiling = (
        Job.objects.filter(created_date__lt=window.end + JOB_ID_MARGIN)
        .order_by("-id")
        .values_list("id", flat=True)
        .first()
    )
    if ceiling is None:
        return None
    floor = (
        Job.objects.filter(created_date__lt=window.start - JOB_ID_MARGIN)
        .order_by("-id")
        .values_list("id", flat=True)
        .first()
    )
    return floor or 0, ceiling


def job_events(window, profile, notices, session=None):
    bounds = job_id_range(window)
    if bounds is None:
        return []
    qs = ProgressMessage.objects.filter(
        job_id__gt=bounds[0],
        job_id__lte=bounds[1],
        message__startswith=markers.PREFIX,
    )
    if session:
        qs = qs.filter(message__contains=markers.session_needle(session))
    if restricted(profile):
        qs = qs.filter(job__owner=profile)
    rows = list(
        qs.order_by("-job_id", "id").values_list(
            "job_id",
            "message",
            "job__owner",
            "job__created_date",
            "job__type",
            "job__status",
            "job__title",
            "job__label",
            "job__parent_job",
        )[: config.MAX_ROWS_PER_SOURCE + 1]
    )
    type_names = _job_type_names()
    events = []
    for job_id, message, owner_id, created, job_type, status, title, label, parent_id in _capped(
        rows, "job", notices
    ):
        if created is None or not (window.start <= created < window.end):
            continue
        marker = markers.leading_marker(message)
        if marker is None:
            continue
        type_name = label or type_names.get(job_type, job_type)
        events.append({
            "source": "job",
            "agent": marker.agent,
            "client": marker.client,
            "session": marker.session,
            "marker": marker.raw,
            "user_id": owner_id or 0,
            "when": created,
            "job_id": job_id,
            "job_title": title or "",
            "job_label": label or "",
            "job_type_name": type_name,
            "job_status": status or "",
            "parent_job_id": parent_id,
            "text": _text(title, type_name, "job {}".format(job_id), marker.raw),
        })
    return events


def history_events(window, profile, notices, session=None):
    """Server, resource and every other history record except order history."""
    base = HistoryModel.objects.all()
    if restricted(profile):
        base = base.filter(Q(owner=profile) | Q(owner__isnull=True, job__owner=profile))
    qs = base.filter(
        action_time__gte=window.start,
        action_time__lt=window.end,
        event_message__contains=markers.PREFIX,
    ).exclude(real_type_id=ContentType.objects.get_for_model(OrderHistory).id)
    if session:
        qs = qs.filter(event_message__contains=markers.session_needle(session))
    rows = list(
        qs.order_by("-action_time", "-id").values_list(
            "id",
            "real_type_id",
            "owner_id",
            "action_time",
            "event_type",
            "event_message",
            "job_id",
            "job__owner",
            "serverhistory__server",
            "serverhistory__server__hostname",
            "resourcehistory__resource",
            "resourcehistory__resource__name",
        )[: config.MAX_ROWS_PER_SOURCE + 1]
    )
    events = []
    for (row_id, real_type_id, owner_id, when, event_type, message, job_id, job_owner_id,
         server_id, hostname, resource_id, resource_name) in _capped(rows, "history", notices):
        marker = markers.last_marker(message)
        if marker is None:
            continue
        kind, kind_label = _kind(real_type_id)
        object_id = server_id or resource_id
        object_name = hostname if server_id else (resource_name if resource_id else "")
        # Messages may hold HTML (links, &nbsp;); keep the text only.
        text = html.unescape(strip_tags(markers.strip_markers(message)))
        events.append({
            "source": "history",
            "history_id": row_id,
            "agent": marker.agent,
            "client": marker.client,
            "session": marker.session,
            "marker": marker.raw,
            "user_id": owner_id or job_owner_id or 0,
            "when": when,
            "kind": kind,
            "kind_label": kind_label,
            "object_id": object_id,
            "object_name": object_name or "",
            "event_type": event_type,
            "message": text[:MESSAGE_CHARS] + ("…" if len(text) > MESSAGE_CHARS else ""),
            "job_id": job_id,
            "text": _text(object_name, marker.raw),
        })
    return events


def fill_session_users(events):
    """
    Give each record that still has no user the user of its session. A session is
    one MCP connection made with one user's token, so its records share that user.
    """
    users = {}
    for event in events:
        if event["user_id"]:
            users.setdefault(event["session"], Counter())[event["user_id"]] += 1
    for event in events:
        if not event["user_id"] and event["session"] in users:
            event["user_id"] = users[event["session"]].most_common(1)[0][0]
    return events


def fetch_events(window, profile, session=None):
    """Every marked record in the window from all three sources; returns (events, notices)."""
    notices = []
    events = []
    events.extend(order_events(window, profile, notices, session=session))
    events.extend(job_events(window, profile, notices, session=session))
    events.extend(history_events(window, profile, notices, session=session))
    return fill_session_users(events), notices
