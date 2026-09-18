import json

from accounts.models import Group
from django.conf import settings


def run(job, **kwargs):
    group_stats = {
        "groups": [],
        "by_group_id": {},
    }

    for group in Group.objects.filter(parent=None):
        group_stats["groups"].append(
            {
                "name": group.name,
                "server_count": group.server_count,
                "resource_count": group.resource_count,
                "cost": group.rate_display,
            }
        )

    for group in Group.objects.iterator():
        group_stats["by_group_id"][str(group.id)] = {
            "name": group.name,
            "server_count": group.server_count,
            "resource_count": group.resource_count,
            "cost": group.rate_display,
        }

    with open(f"{settings.PROSERV_DIR}/data/group_stats.json", "w") as fd:
        json.dump(group_stats, fd, indent=3)

    return "SUCCESS", "", ""
