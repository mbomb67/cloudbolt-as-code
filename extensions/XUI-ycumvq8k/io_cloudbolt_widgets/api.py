from accounts.models import UserProfile
from django.db.models import Count
from infrastructure.models import Server
from utilities.logger import ThreadLogger
from utilities.decorators import json_view

from xui.io_cloudbolt_widgets.common import collect_profile_scope_group_ids

logger = ThreadLogger(__name__)

_ENV_LABEL_MAX_LEN = 36


def _short_env_label(name):
    name = name or ""
    if len(name) <= _ENV_LABEL_MAX_LEN:
        return name
    return name[: _ENV_LABEL_MAX_LEN - 3] + "..."


@json_view
def fetch_env_data(request):
    profile: UserProfile = request.user.userprofile

    base = Server.objects.filter(status="ACTIVE", environment__isnull=False)

    if profile.is_super_admin:
        qs = base
    else:
        scope_ids = collect_profile_scope_group_ids(profile)
        if not scope_ids:
            return []
        qs = base.filter(group_id__in=scope_ids)

    rows = (
        qs.values("environment_id", "environment__name")
        .annotate(y=Count("id"))
        .order_by("environment__name")
    )

    results = []
    for row in rows:
        if row["y"] == 0:
            continue
        full_name = row["environment__name"] or ""
        results.append(
            {
                "label": _short_env_label(full_name),
                "full_name": full_name,
                "id": row["environment_id"],
                "y": row["y"],
                "sliced": full_name == "Unassigned",
            }
        )
    return results
