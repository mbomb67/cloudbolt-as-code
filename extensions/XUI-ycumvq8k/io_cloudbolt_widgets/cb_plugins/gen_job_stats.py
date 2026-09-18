import json

from accounts.models import Group
from django.conf import settings
from jobs.models import Job

from xui.io_cloudbolt_widgets.common import (
    calc_delta,
    get_start_of_week,
    get_first_datetime_of_last_month,
    get_one_day_ago,
    get_one_month_ago,
    get_one_week_ago,
    get_today,
)


def _compute_job_stats(job_qs, today):
    job_stats = {
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

    one_week_ago = get_one_week_ago(today)
    one_month_ago = get_one_month_ago(today)
    one_day_ago = get_one_day_ago(today)

    day_count = job_qs.filter(
        created_date__gte=today.replace(hour=0, minute=0, second=0, microsecond=0)
    ).count()
    prev_day_count = job_qs.filter(
        created_date__range=(
            one_day_ago.replace(hour=0, minute=0, second=0, microsecond=0),
            today.replace(hour=0, minute=0, second=0, microsecond=0),
        )
    ).count()
    day_count_dod = job_qs.filter(
        created_date__range=(
            one_day_ago.replace(hour=0, minute=0, second=0, microsecond=0),
            one_day_ago,
        )
    ).count()
    day_count_dod_delta, day_count_dod_dir = calc_delta(day_count_dod, day_count)

    week_count = job_qs.filter(created_date__gte=get_start_of_week(today)).count()
    prev_week_count = job_qs.filter(
        created_date__range=(get_start_of_week(one_week_ago), get_start_of_week(today)),
    ).count()
    week_count_wow = job_qs.filter(
        created_date__range=(get_start_of_week(one_week_ago), one_week_ago)
    ).count()
    week_count_wow_delta, week_count_wow_dir = calc_delta(week_count_wow, week_count)

    month_count = job_qs.filter(
        created_date__year=today.year, created_date__month=today.month
    ).count()
    prev_month_count = job_qs.filter(
        created_date__year=one_month_ago.year, created_date__month=one_month_ago.month
    ).count()

    first_day_last_month = get_first_datetime_of_last_month(today)
    month_count_mom = job_qs.filter(
        created_date__range=(first_day_last_month, one_month_ago)
    ).count()
    month_count_mom_delta, month_count_mom_dir = calc_delta(month_count_mom, month_count)

    job_stats["asof"] = today.strftime("%Y-%m-%d %H:%M:%S %Z")

    job_stats["day_count"] = day_count
    job_stats["prev_day_count"] = prev_day_count
    job_stats["day_count_dod"] = day_count_dod
    job_stats["day_count_dod_delta"] = day_count_dod_delta
    job_stats["day_count_dod_dir"] = day_count_dod_dir

    job_stats["week_count"] = week_count
    job_stats["prev_week_count"] = prev_week_count
    job_stats["week_count_wow"] = week_count_wow
    job_stats["week_count_wow_delta"] = week_count_wow_delta
    job_stats["week_count_wow_dir"] = week_count_wow_dir

    job_stats["month_count"] = month_count
    job_stats["prev_month_count"] = prev_month_count
    job_stats["month_count_mom"] = month_count_mom
    job_stats["month_count_mom_delta"] = month_count_mom_delta
    job_stats["month_count_mom_dir"] = month_count_mom_dir

    return job_stats


def run(job, **kwargs):
    today = get_today()

    global_stats = _compute_job_stats(Job.objects.all(), today)

    by_group_id = {}
    for group in Group.objects.iterator():
        g_qs = Job.objects.filter(
            job_parameters__orderitem__order__group_id=group.id
        ).distinct()
        by_group_id[str(group.id)] = _compute_job_stats(g_qs, today)

    out = {"global": global_stats, "by_group_id": by_group_id}

    with open(f"{settings.PROSERV_DIR}/data/job_stats.json", "w") as fd:
        json.dump(out, fd, indent=True)

    return "SUCCESS", "", ""
