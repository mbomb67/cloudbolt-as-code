import json

from accounts.models import Group
from common.configuration import CustomFieldAsAttributeQuerySet
from django.conf import settings
from infrastructure.models import Environment
from resourcehandlers.models import ResourceHandler


def _build_rh_stats(group=None):
    """
    If group is None, counts include all servers (super-admin / global view).
    Otherwise only servers whose group is in the given group's descendant set.
    """
    rh_stats = {
        "resource_handlers": [],
    }

    env_qs: CustomFieldAsAttributeQuerySet = Environment.objects.prefetch_related(
        "server_set"
    ).prefetch_related("resource_handler")

    rhs = ResourceHandler.objects.all()

    group_ids = None
    if group is not None:
        group_ids = [g.id for g in group.get_descendant_list()]

    for rh in rhs:
        rh = rh.cast()
        envs = []

        env: Environment
        for env in env_qs.filter(resource_handler=rh):
            if group_ids is None:
                server_count = env.server_set.filter(status="ACTIVE").count()
            else:
                server_count = env.server_set.filter(
                    status="ACTIVE", group_id__in=group_ids
                ).count()
            if server_count == 0 and group is not None:
                continue
            envs.append(
                {
                    "name": env.name,
                    "id": env.id,
                    "server_count": server_count,
                    "rate": env.get_rate_display(),
                }
            )

        rh_stats["resource_handlers"].append(
            {
                "name": rh.name,
                "id": rh.id,
                "tech_name": rh.resource_technology.name,
                "tech_slug": rh.resource_technology.slug,
                "envs": envs,
            }
        )

    unassigned_env = Environment.objects.get(name="Unassigned")
    if group_ids is None:
        ua_count = unassigned_env.server_set.filter(status="ACTIVE").count()
    else:
        ua_count = unassigned_env.server_set.filter(
            status="ACTIVE", group_id__in=group_ids
        ).count()

    if group is None or ua_count > 0:
        rh_stats["resource_handlers"].append(
            {
                "name": "unassigned",
                "id": "",
                "tech_name": "",
                "tech_slug": "",
                "envs": [
                    {
                        "name": "Unassigned",
                        "id": unassigned_env.id,
                        "server_count": ua_count,
                    },
                ],
            }
        )

    return rh_stats


def run(job, **kwargs):
    global_stats = _build_rh_stats(group=None)
    by_group_id = {}
    for group in Group.objects.iterator():
        by_group_id[str(group.id)] = _build_rh_stats(group=group)

    out = {
        "resource_handlers": global_stats["resource_handlers"],
        "by_group_id": by_group_id,
    }

    with open(f"{settings.PROSERV_DIR}/data/rh_stats.json", "w") as fd:
        json.dump(out, fd, indent=3)

    return "SUCCESS", "", ""
