"""
Day-2 action for the Azure Kubernetes Service blueprint: choose which
CloudBolt groups may order namespaces on the cluster.

A group may order an AKS namespace on a cluster when the cluster's CloudBolt
Environment is available to it (Group.get_available_environments()):
entitled to the group or one of its ancestors, or unconstrained, with no
groups at all. This action changes that Environment's groups, so a cluster's
owners manage them without being environment administrators:

* Add entitles the chosen groups and drops CloudBolt's Unassigned placeholder.
* Remove takes the chosen groups off; when none is left, the Environment is
  unconstrained and every group may order namespaces on the cluster.
* Allow all groups removes every group, with the same result.

Descendant groups inherit (or lose) the Environment and its history records
each change, as with CloudBolt's own Edit groups dialog. Namespaces already
ordered keep their own environments and are not affected. The cluster's
Groups tab starts this action from its buttons.
"""

from common.methods import set_progress
from shared_modules.azure_aks import (
    AKSError,
    RESTRICTED,
    UNCONSTRAINED,
    add_environment_groups,
    allow_all_groups,
    assignable_groups,
    cluster_environment,
    environment_groups,
    group_ids_in,
    groups_for_ids,
    is_aks_cluster,
    remove_environment_groups,
)
from utilities.logger import ThreadLogger

logger = ThreadLogger(__name__)

ADD = "Add"
REMOVE = "Remove"
ALLOW_ALL = "Allow all groups"
OPERATIONS = [
    (ADD, "Add - entitle the chosen groups"),
    (REMOVE, "Remove - take the chosen groups off; with none left, every group may order namespaces"),
    (ALLOW_ALL, "Allow all groups - remove every group so every group may order namespaces"),
]


def _text(value):
    """A rendered input as plain text; an input left empty renders as 'None'."""
    text = str(value or "").strip()
    return "" if text == "None" else text


def _cluster_resource(kwargs):
    """The cluster resource an option generator runs for, or None outside a resource."""
    resource = kwargs.get("resource")
    return resource if hasattr(resource, "get_cf_values_as_dict") else None


def describe(env):
    """Who may order namespaces through the Environment, as a phrase."""
    state, groups = environment_groups(env)
    if state == UNCONSTRAINED:
        return "unconstrained: every group may order namespaces on the cluster"
    if state == RESTRICTED:
        return "restricted: it has only CloudBolt's Unassigned placeholder, so no group may order namespaces yet"
    return "entitled to " + ", ".join(g.name for g in groups)


# ---------------------------------------------------------------------------
# Dialog options
# ---------------------------------------------------------------------------

def generate_options_for_aks_groups_operation(field=None, **kwargs):
    return {"options": OPERATIONS, "initial_value": ADD, "sort": False}


def generate_options_for_aks_groups(field=None, **kwargs):
    """Every CloudBolt group but the Unassigned placeholders; those entitled now are marked."""
    entitled = set()
    resource = _cluster_resource(kwargs)
    if resource is not None:
        try:
            entitled = {g.id for g in environment_groups(cluster_environment(resource))[1]}
        except AKSError as exc:
            logger.info("No cluster environment for %s: %s", resource, exc)
    return [
        (g.global_id, f"{g.name} (entitled)" if g.id in entitled else g.name)
        for g in assignable_groups()
    ]


# ---------------------------------------------------------------------------
# Change the groups
# ---------------------------------------------------------------------------

def run(job, resource, **kwargs):
    operation = _text("{{ aks_groups_operation }}") or ADD
    group_ids = group_ids_in("""{{ aks_groups }}""")

    if not is_aks_cluster(resource):
        return "FAILURE", "", f"'{resource.name}' is not a cluster of the Azure Kubernetes Service blueprint."
    if operation not in (ADD, REMOVE, ALLOW_ALL):
        return "FAILURE", "", f"Operation must be {ADD}, {REMOVE}, or {ALLOW_ALL}, not '{operation}'."
    try:
        env = cluster_environment(resource)
    except AKSError as exc:
        return "FAILURE", "", str(exc)
    groups, unknown = groups_for_ids(group_ids)
    if unknown:
        return "FAILURE", "", f"No CloudBolt group to entitle has the ID(s) {', '.join(unknown)}."
    if operation != ALLOW_ALL and not groups:
        return "FAILURE", "", f"Choose at least one group to {operation.lower()}."

    set_progress(f"Environment '{env.name}' is {describe(env)}.")
    if operation == ADD:
        changed = add_environment_groups(env, groups, profile=job.owner, job=job)
        set_progress(f"Entitled {', '.join(changed)}." if changed else "Every chosen group was already entitled.")
    elif operation == REMOVE:
        changed = remove_environment_groups(env, groups, profile=job.owner, job=job)
        set_progress(f"Removed {', '.join(changed)}." if changed else "No chosen group was entitled.")
    else:
        changed = allow_all_groups(env, profile=job.owner, job=job)
        set_progress(f"Removed {', '.join(changed)}." if changed else "The environment had no groups.")

    return "SUCCESS", f"Environment '{env.name}' of cluster '{resource.name}' is {describe(env)}.", ""
