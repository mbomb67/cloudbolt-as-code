import json
from accounts.models import UserProfile
from django.shortcuts import render
from extensions.views import dashboard_extension
from infrastructure.models import Server
from resources.models import Resource
from servicecatalog.models import ServiceBlueprint
from utilities.logger import ThreadLogger
from django.conf import settings

from xui.io_cloudbolt_widgets.common import (
    empty_job_order_stats,
    filter_rh_status_for_profile,
    merge_job_or_order_stats_from_by_group,
    merge_rh_stats_for_profile,
)

logger = ThreadLogger(__name__)

RECENT_COUNT = 5


def _job_stats_for_profile(profile, data):
    if not isinstance(data, dict):
        return empty_job_order_stats()
    if "global" in data:
        if profile.is_super_admin:
            return data["global"]
        return merge_job_or_order_stats_from_by_group(data.get("by_group_id"), profile)
    if profile.is_super_admin:
        return data
    return merge_job_or_order_stats_from_by_group({}, profile)


def _group_stats_rows_for_profile(profile, data):
    if not isinstance(data, dict):
        return []
    if profile.is_super_admin:
        return data.get("groups") or []
    by_gid = data.get("by_group_id") or {}
    rows = []
    for g in profile.get_groups().order_by("name"):
        row = by_gid.get(str(g.id))
        if row:
            rows.append(row)
    return rows


@dashboard_extension(
    title="# Servers by Environment",
    description="Visual breakdown of active server counts across all environments.",
)
def env_widget(request):
    profile: UserProfile = request.user.userprofile
    return render(
        request,
        template_name="io_cloudbolt_widgets/templates/env_widget.html",
        context={
            "is_super_admin": profile.is_super_admin,
        },
    )


@dashboard_extension(
    title="Cloud Availability",
    description="Real-time connection status for each cloud resource handler. Green means healthy; red means potentially unreachable.",
)
def rh_status_widget(request):
    rh_status = {"resource_handlers": []}

    try:
        with open(f"{settings.PROSERV_DIR}/data/rh_status.json", "r") as fd:
            rh_status = json.load(fd)
    except Exception:
        pass

    profile: UserProfile = request.user.userprofile
    handlers = filter_rh_status_for_profile(rh_status, profile)

    return render(
        request,
        template_name="io_cloudbolt_widgets/templates/rh_status_widget.html",
        context={
            "rh_status": handlers,
            "is_super_admin": profile.is_super_admin,
        },
    )


@dashboard_extension(
    title="Server Stats",
    description="Active server counts by resource handler and environment, with associated costs.",
)
def rh_stats_widget(request):
    rh_stats = {"resource_handlers": []}

    try:
        with open(f"{settings.PROSERV_DIR}/data/rh_stats.json", "r") as fd:
            rh_stats = json.load(fd)
    except Exception:
        pass

    profile: UserProfile = request.user.userprofile
    rhs = merge_rh_stats_for_profile(rh_stats, profile)

    return render(
        request,
        template_name="io_cloudbolt_widgets/templates/rh_stats_widget.html",
        context={
            "rh_status": rhs,
            "is_super_admin": profile.is_super_admin,
        },
    )


@dashboard_extension(
    title="Group Stats",
    description="Server and resource counts with cost summaries for each top-level group.",
)
def group_stats_widget(request):
    group_stats = {}

    try:
        with open(f"{settings.PROSERV_DIR}/data/group_stats.json", "r") as fd:
            group_stats = json.load(fd)
    except Exception:
        pass

    profile: UserProfile = request.user.userprofile
    rows = _group_stats_rows_for_profile(profile, group_stats)

    return render(
        request,
        template_name="io_cloudbolt_widgets/templates/group_stats_widget.html",
        context={
            "group_stats": rows,
        },
    )


@dashboard_extension(
    title="Job Counts",
    description="Job activity trends with daily, weekly, and monthly counts plus period-over-period changes.",
)
def jobs_badge(request):
    stats = empty_job_order_stats()

    try:
        with open(f"{settings.PROSERV_DIR}/data/job_stats.json", "r") as fd:
            stats = _job_stats_for_profile(request.user.userprofile, json.load(fd))
    except Exception:
        pass

    return render(
        request,
        template_name="io_cloudbolt_widgets/templates/badge_jobs.html",
        context={"stats": stats},
    )


@dashboard_extension(
    title="Order Counts",
    description="Order activity trends with daily, weekly, and monthly counts plus period-over-period changes.",
)
def orders_badge(request):
    stats = empty_job_order_stats()

    try:
        with open(f"{settings.PROSERV_DIR}/data/order_stats.json", "r") as fd:
            stats = _job_stats_for_profile(request.user.userprofile, json.load(fd))
    except Exception:
        pass

    return render(
        request,
        template_name="io_cloudbolt_widgets/templates/badge_orders.html",
        context={"stats": stats},
    )


@dashboard_extension(
    title="Newest Servers",
    description="The most recently provisioned active servers in your scope.",
)
def recent_servers_widget(request):

    profile: UserProfile = request.user.userprofile
    servers = (
        Server.objects_for_profile(profile)
        .filter(status="ACTIVE")
        .order_by("-add_date")
    )

    return render(
        request,
        template_name="io_cloudbolt_widgets/templates/widget_recent_servers.html",
        context={
            "servers": servers[:RECENT_COUNT],
        },
    )


@dashboard_extension(
    title="Newest Resources",
    description="The most recently created active resources in your scope.",
)
def recent_resources_widget(request):

    profile: UserProfile = request.user.userprofile
    resources = (
        Resource.objects_for_profile(profile)
        .filter(lifecycle="ACTIVE")
        .exclude(resource_type__internal_only=True)
        .order_by("-created")
    )

    return render(
        request,
        template_name="io_cloudbolt_widgets/templates/widget_recent_resources.html",
        context={
            "resources": resources[:RECENT_COUNT],
        },
    )


@dashboard_extension(
    title="Featured Blueprints",
    description="Favorited blueprints from the service catalog, ready to deploy.",
)
def featured_blueprints_widget(request):
    profile: UserProfile = request.user.userprofile
    blueprints = (
        ServiceBlueprint.objects_for_profile(profile)
        .filter(favorited=True, status="ACTIVE")
        .order_by("sequence", "name")
    )

    bp_cards = []
    for bp in blueprints:
        image_url = ""
        if bp.list_image:
            try:
                image_url = bp.list_image.url
            except Exception:
                pass
        bp_cards.append(
            {
                "name": bp.name,
                "image_url": image_url,
                "order_url": bp.get_order_url(),
            }
        )

    return render(
        request,
        template_name="io_cloudbolt_widgets/templates/featured_blueprints_widget.html",
        context={
            "blueprints": bp_cards,
        },
    )
