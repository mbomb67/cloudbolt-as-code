"""
Day-2 action for the AWS EKS blueprint: change which CloudBolt groups may
order AWS EKS Namespace landing zones on the cluster.

Namespace orders offer a cluster to the groups its own Environment is
available to (group.get_available_environments()): the environment the
build and discovery create on the cluster's container orchestrator, without
the namespace pin that namespace landing zones' environments carry. This
action edits that environment's groups the way CloudBolt's own Edit groups
dialog for an environment does (infrastructure EnvGroupsForm):
Environment.group_set, add_env_to_descendants and
remove_env_from_descendants for subgroups, and an environment history event.

- Add: entitle the chosen groups and drop the Unassigned placeholder that
  new environments get when CloudBolt restricts them.
- Remove: take the chosen groups off. Removing the last group leaves the
  environment with no groups, so it is unconstrained and every group may
  order namespaces on the cluster; it never goes back to Unassigned.
- Allow all groups: clear every group, Unassigned included.
"""

import re

from django.utils.html import escape

from accounts.models import Group
from common.methods import set_progress
from containerorchestrators.kuberneteshandler.models import Kubernetes
from infrastructure.models import Environment
from utilities.logger import ThreadLogger

logger = ThreadLogger(__name__)

OPERATIONS = [
    ("add", "Add"),
    ("remove", "Remove"),
    ("allow_all", "Allow all groups"),
]
# A multi-select input renders as a Python list of the chosen values.
GROUP_ID_RE = re.compile(r"GRP-[A-Za-z0-9]+")
# Parameter that pins a namespace landing zone's own Environment.
NAMESPACE_ENV_FIELD = "kubernetes_namespace"


def cluster_environment(resource):
    """The cluster's own Environment on its container orchestrator, or None."""
    orchestrator_id = resource.get_cf_values_as_dict().get("container_orchestrator_id")
    try:
        orchestrator = Kubernetes.objects.filter(id=int(str(orchestrator_id).strip())).first()
    except (TypeError, ValueError):
        return None
    if orchestrator is None:
        return None
    # Filter on the foreign key: container_orchestrator_id= is read as a
    # lookup on the custom field of that name and matches nothing.
    return (
        Environment.objects.filter(container_orchestrator=orchestrator)
        .exclude(custom_field_options__field__name=NAMESPACE_ENV_FIELD)
        .order_by("id")
        .first()
    )


def entitled_groups(env):
    """Groups on the environment, without the Unassigned placeholders."""
    return env.group_set.exclude(id__in=Group.unassigned.ids()).order_by("name")


def names(groups):
    return ", ".join(g.name for g in groups)


def generate_options_for_eks_groups_operation(**kwargs):
    return OPERATIONS


def generate_options_for_eks_groups(**kwargs):
    """
    Every CloudBolt group except the Unassigned and tenant-default
    placeholders, the ones already entitled to the cluster labelled so.
    """
    resource = kwargs.get("resource")
    env = cluster_environment(resource) if resource is not None else None
    current = set(entitled_groups(env).values_list("id", flat=True)) if env else set()
    return [
        (g.global_id, "{} (entitled)".format(g.name) if g.id in current else g.name)
        for g in Group.without_unassigned.order_by("name")
    ]


def record(job, env, message):
    env.add_event("INFO", message, profile=job.owner, job=job)
    set_progress(message)


def add_groups(job, env, cluster, groups):
    placeholders = list(env.group_set.filter(id__in=Group.unassigned.ids()))
    if placeholders:
        env.group_set.remove(*placeholders)
        for placeholder in placeholders:
            placeholder.remove_env_from_descendants(env)
        set_progress("Dropped the placeholder group {} from environment '{}'".format(names(placeholders), env.name))
    env.group_set.add(*groups)
    for group in groups:
        group.add_env_to_descendants(env)
    record(job, env, 'Groups {} may order namespaces on cluster "{}" through environment "{}"'.format(
        escape(names(groups)), escape(cluster), escape(env.name)
    ))


def remove_groups(job, env, cluster, groups):
    current = set(entitled_groups(env))
    skipped = [g for g in groups if g not in current]
    removing = [g for g in groups if g in current]
    if skipped:
        set_progress("Not entitled to this cluster, so nothing to remove: {}".format(names(skipped)))
    if not removing:
        return False
    env.group_set.remove(*removing)
    for group in removing:
        group.remove_env_from_descendants(env)
    message = 'Groups {} may no longer order namespaces on cluster "{}" (environment "{}")'.format(
        escape(names(removing)), escape(cluster), escape(env.name)
    )
    if not entitled_groups(env).exists():
        # Never back to Unassigned: with no groups left the environment is
        # unconstrained, like the Edit groups dialog with nothing selected.
        leftovers = list(env.group_set.all())
        env.group_set.clear()
        for placeholder in leftovers:
            placeholder.remove_env_from_descendants(env)
        message += "; it has no groups left, so every group may order namespaces on the cluster"
    record(job, env, message)
    return True


def allow_all_groups(job, env, cluster):
    previous = list(env.group_set.all())
    env.group_set.clear()
    for group in previous:
        group.remove_env_from_descendants(env)
    record(job, env, 'Every group removed from environment "{}", so every group may order namespaces on cluster "{}"'.format(
        escape(env.name), escape(cluster)
    ))


def run(job, resource, **kwargs):
    operation = "{{ eks_groups_operation }}".strip()
    group_ids = GROUP_ID_RE.findall("""{{ eks_groups }}""")

    if operation not in dict(OPERATIONS):
        return "FAILURE", "", "Unknown operation '{}'; choose one of: {}.".format(
            operation, ", ".join(label for _, label in OPERATIONS)
        )
    env = cluster_environment(resource)
    if env is None:
        return (
            "FAILURE",
            "",
            "No CloudBolt environment is linked to this cluster's container orchestrator "
            "(container_orchestrator_id).",
        )
    cluster = resource.get_cf_values_as_dict().get("eks_cluster_name") or resource.name
    set_progress("Environment '{}' of cluster '{}' has groups: {}".format(
        env.name, cluster, names(env.group_set.order_by("name")) or "none (unconstrained)"
    ))

    if operation == "allow_all":
        allow_all_groups(job, env, cluster)
        return "SUCCESS", "Every group can order namespaces on {}.".format(cluster), ""

    groups = list(Group.without_unassigned.filter(global_id__in=group_ids).order_by("name"))
    unknown = sorted(set(group_ids) - {g.global_id for g in groups})
    if unknown:
        set_progress("Ignoring ids that are not CloudBolt groups or are Unassigned placeholders: {}".format(
            ", ".join(unknown)
        ))
    if not groups:
        return "FAILURE", "", "Choose at least one group to {}.".format(operation)

    if operation == "add":
        add_groups(job, env, cluster, groups)
    elif not remove_groups(job, env, cluster, groups):
        return "SUCCESS", "None of the chosen groups was entitled, so nothing changed.", ""

    now = names(entitled_groups(env)) or "every group (unconstrained)"
    return "SUCCESS", "Groups that may order namespaces on {}: {}.".format(cluster, now), ""
