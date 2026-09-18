import copy
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from dateutil.relativedelta import relativedelta

from django.conf import settings


def collect_profile_scope_group_ids(profile):
    """
    For each group the profile belongs to, include that group and all descendants.
    Used to merge per-group stats without double-counting (jobs/orders belong to one group).
    """
    if profile.is_super_admin:
        return None
    ids = set()
    for g in profile.get_groups():
        ids.update(x.id for x in g.get_descendant_list())
    return ids


def empty_job_order_stats():
    return {
        "asof": None,
        "month_count": 0,
        "prev_month_count": 0,
        "month_count_mom": 0,
        "month_count_mom_delta": 0.0,
        "month_count_mom_dir": "",
        "day_count": 0,
        "prev_day_count": 0,
        "day_count_dod": 0,
        "day_count_dod_delta": 0,
        "day_count_dod_dir": "",
        "week_count": 0,
        "prev_week_count": 0,
        "week_count_wow": 0.0,
        "week_count_wow_delta": 0.0,
        "week_count_wow_dir": "",
        "year_count": 0,
        "prev_year_count": 0,
        "year_count_yoy": 0,
        "year_count_yoy_delta": 0.0,
        "year_count_yoy_dir": "",
    }


_SUMMABLE_JOB_ORDER_KEYS = (
    "month_count",
    "prev_month_count",
    "month_count_mom",
    "day_count",
    "prev_day_count",
    "day_count_dod",
    "week_count",
    "prev_week_count",
    "week_count_wow",
    "year_count",
    "prev_year_count",
    "year_count_yoy",
)


def get_start_of_week(today):
    days_since_sunday = (today.weekday() + 1) % 7
    start_of_week = today - timedelta(days=days_since_sunday)
    start_of_week = start_of_week.replace(hour=0, minute=0, second=0, microsecond=0)
    return start_of_week


def calc_delta(last_period, this_period):
    if this_period == 0:
        return 100.0, "down"

    if last_period == 0:
        return 100.0, "up"

    if this_period < last_period:
        delta = ((this_period / last_period) - 1) * 100
    else:
        delta = (1 - (last_period / this_period)) * 100

    if delta > 0:
        return delta, "up"
    elif delta < 0:
        return abs(delta), "down"
    else:
        return 0, ""


def merge_job_or_order_stats_from_by_group(by_group_id, profile):
    """
    Sum precomputed per-group (direct group_id) stats for all groups in the profile scope.
    Recalculates delta fields from merged totals.
    """
    if profile.is_super_admin:
        return None
    scope_ids = collect_profile_scope_group_ids(profile)
    if not scope_ids:
        return empty_job_order_stats()

    merged = empty_job_order_stats()
    asof = None
    for gid in scope_ids:
        block = (by_group_id or {}).get(str(gid))
        if not block:
            continue
        if asof is None:
            asof = block.get("asof")
        for k in _SUMMABLE_JOB_ORDER_KEYS:
            val = block.get(k, 0) or 0
            if isinstance(val, float):
                merged[k] = (merged.get(k, 0) or 0) + val
            else:
                merged[k] = (merged.get(k, 0) or 0) + int(val)

    merged["asof"] = asof
    merged["day_count_dod_delta"], merged["day_count_dod_dir"] = calc_delta(
        merged["day_count_dod"], merged["day_count"]
    )
    merged["week_count_wow_delta"], merged["week_count_wow_dir"] = calc_delta(
        merged["week_count_wow"], merged["week_count"]
    )
    merged["month_count_mom_delta"], merged["month_count_mom_dir"] = calc_delta(
        merged["month_count_mom"], merged["month_count"]
    )
    merged["year_count_yoy_delta"], merged["year_count_yoy_dir"] = calc_delta(
        merged["year_count_yoy"], merged["year_count"]
    )
    return merged


def _rh_merge_key(rh):
    return (str(rh.get("id", "")), rh.get("name", ""))


def merge_rh_stats_for_profile(payload, profile):
    """
    payload: parsed rh_stats.json with top-level resource_handlers and optional by_group_id.
    """
    global_rhs = payload.get("resource_handlers")
    if profile.is_super_admin:
        return global_rhs or []

    by_group_id = payload.get("by_group_id") or {}
    scope_ids = collect_profile_scope_group_ids(profile)
    if not scope_ids:
        return []

    rh_lists = []
    for gid in scope_ids:
        block = by_group_id.get(str(gid))
        if not block:
            continue
        rhs = block.get("resource_handlers")
        if rhs:
            rh_lists.append(rhs)

    if not rh_lists:
        return []

    merged_map = {}
    for rh_list in rh_lists:
        for rh in rh_list:
            key = _rh_merge_key(rh)
            if key not in merged_map:
                merged_map[key] = copy.deepcopy(rh)
                continue
            target = merged_map[key]
            env_by_id = {str(e.get("id", "")): e for e in target.get("envs", [])}
            for env in rh.get("envs", []):
                eid = str(env.get("id", ""))
                if eid not in env_by_id:
                    env_by_id[eid] = copy.deepcopy(env)
                else:
                    te = env_by_id[eid]
                    te["server_count"] = int(te.get("server_count", 0) or 0) + int(
                        env.get("server_count", 0) or 0
                    )
            target["envs"] = sorted(
                env_by_id.values(), key=lambda e: (e.get("name") or "").lower()
            )

    return sorted(merged_map.values(), key=lambda r: (r.get("name") or "").lower())


def _coerce_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def resource_handler_ids_from_group_environments(profile):
    """
    Unique Resource Handler IDs the profile can reach via group.get_available_environments().
    """
    ids = set()
    tenant = getattr(profile, "tenant", None)
    for group in profile.get_groups():
        for env in group.get_available_environments(tenant=tenant, profile=profile):
            rh_id = getattr(env, "resource_handler_id", None)
            if rh_id:
                ids.add(int(rh_id))
    return ids


def filter_rh_status_for_profile(payload, profile):
    """
    Non-super-admins see connection status for every RH that appears on at least one
    environment available to any of their groups (see Group.get_available_environments).
    """
    handlers = payload.get("resource_handlers") or []
    if profile.is_super_admin:
        return handlers

    visible = resource_handler_ids_from_group_environments(profile)
    if not visible:
        return []

    return [rh for rh in handlers if _coerce_int(rh.get("id")) in visible]


def get_today():
    return datetime.now(ZoneInfo(settings.TIME_ZONE))


def get_one_week_ago(today):
    return today - timedelta(weeks=1)


def get_one_month_ago(today):
    return today - relativedelta(months=1)


def get_one_day_ago(today):
    return today - timedelta(days=1)


def get_first_datetime_of_last_month(today):
    date_and_time = today.replace(day=1) - relativedelta(months=1)
    return date_and_time.replace(hour=0, minute=0, second=0, microsecond=0)
