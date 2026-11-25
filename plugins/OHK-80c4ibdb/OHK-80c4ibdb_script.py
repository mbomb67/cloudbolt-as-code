"""
Day-2 action for the Azure Kubernetes Service blueprint: change how many
nodes a node pool runs.

Manual sets a fixed node count and turns the cluster autoscaler off for the
pool; AKS does not take a node count by hand while the autoscaler runs it.
Autoscale turns the cluster autoscaler on (or changes its range) with the
chosen minimum and maximum; the current node count is kept, or moved to the
nearest end of the range when it lies outside it. User pools may scale to no
nodes; System pools need at least one.

The job waits for AKS to finish and refreshes the pool's AKS Node Pool
sub-resource.
  https://learn.microsoft.com/en-us/azure/aks/scale-cluster
  https://learn.microsoft.com/en-us/azure/aks/cluster-autoscaler
  https://learn.microsoft.com/en-us/rest/api/aks/agent-pools/create-or-update
"""

from common.methods import set_progress
from shared_modules.azure_aks import (
    AKSError,
    NODE_POOL_NAME_RE,
    cluster_for_resource,
    pool_label,
    pool_values,
    upsert_pool_sub_resource,
)
from utilities.logger import ThreadLogger

logger = ThreadLogger(__name__)

MANUAL = "Manual"
AUTOSCALE = "Autoscale"
METHODS = [
    (MANUAL, "Manual - a fixed node count"),
    (AUTOSCALE, "Autoscale - the cluster autoscaler keeps the count between a minimum and maximum"),
]

# Node counts: 0-1000 for User pools, 1-1000 for System pools.
# https://learn.microsoft.com/en-us/rest/api/aks/agent-pools/create-or-update#agentpool
MAX_NODES = 1000

# Provisioning states in which a node pool takes a new operation. AKS runs one
# operation at a time on a node pool.
# https://learn.microsoft.com/en-us/azure/aks/upgrade-node-pools#validation-rules-for-upgrades
SETTLED_STATES = ("succeeded", "failed", "canceled")


def _text(value):
    """A rendered input as plain text; an input left empty renders as 'None'."""
    text = str(value or "").strip()
    return "" if text == "None" else text


def _int(value, default=None):
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _cluster_resource(kwargs):
    """The cluster resource an option generator runs for, or None outside a resource."""
    resource = kwargs.get("resource")
    return resource if hasattr(resource, "get_cf_values_as_dict") else None


def describe_scale(pool_json):
    """'manual, 3 node(s)' or 'autoscale 1-5, 3 node(s)'."""
    values = pool_values(pool_json)
    if values["aks_pool_scale_method"] == AUTOSCALE:
        method = f"autoscale {values['aks_pool_min_count']}-{values['aks_pool_max_count']}"
    else:
        method = "manual"
    return f"{method}, {values['aks_pool_node_count']} node(s)"


# ---------------------------------------------------------------------------
# Dialog options
# ---------------------------------------------------------------------------

def generate_options_for_aks_scale_pool(field=None, **kwargs):
    """The cluster's node pools as AKS reports them, with their current scaling."""
    resource = _cluster_resource(kwargs)
    if resource is None:
        return []
    try:
        pools = cluster_for_resource(resource).list_pools()
    except AKSError as exc:
        logger.warning("Could not list node pools of %s: %s", resource, exc)
        return [("", f"------ Could not list node pools: {str(exc)[:120]} ------")]
    if not pools:
        return [("", "------ This cluster has no node pools ------")]
    return [(pool.get("name"), pool_label(pool)) for pool in pools]


def generate_options_for_aks_scale_method(field=None, **kwargs):
    return {"options": METHODS, "initial_value": MANUAL, "sort": False}


# ---------------------------------------------------------------------------
# Scale
# ---------------------------------------------------------------------------

def run(job, resource, **kwargs):
    name = _text("{{ aks_scale_pool }}")
    method = _text("{{ aks_scale_method }}") or MANUAL
    count = _int("{{ aks_scale_count }}")
    min_count = _int("{{ aks_scale_min_count }}")
    max_count = _int("{{ aks_scale_max_count }}")

    # ---- Validate the inputs ----------------------------------------------
    if not NODE_POOL_NAME_RE.match(name):
        return "FAILURE", "", "Choose a node pool to scale."
    if method not in (MANUAL, AUTOSCALE):
        return "FAILURE", "", f"Scale Method must be Manual or Autoscale, not '{method}'."
    if method == MANUAL:
        if count is None:
            return "FAILURE", "", "Manual scaling needs a Node Count."
        if not 0 <= count <= MAX_NODES:
            return "FAILURE", "", f"Node Count must be between 0 and {MAX_NODES}."
    else:
        if min_count is None or max_count is None:
            return "FAILURE", "", "Autoscale needs both an Autoscale Minimum and an Autoscale Maximum."
        if min_count < 0 or not 1 <= max_count <= MAX_NODES:
            return ("FAILURE", "", f"Autoscale Minimum must be 0 or more and Autoscale Maximum between 1 "
                    f"and {MAX_NODES}.")
        if min_count > max_count:
            return ("FAILURE", "", f"Autoscale Minimum ({min_count}) is above Autoscale Maximum "
                    f"({max_count}).")

    try:
        cluster = cluster_for_resource(resource)
        pool = cluster.get_pool(name, allow_404=True)
    except AKSError as exc:
        return "FAILURE", "", str(exc)
    if pool is None:
        return ("FAILURE", "", f"Cluster '{cluster.name}' has no node pool named '{name}'. Run List AKS "
                "Node Pools to refresh CloudBolt.")

    props = pool.get("properties") or {}
    state = str(props.get("provisioningState") or "")
    if state.lower() not in SETTLED_STATES:
        return ("FAILURE", "", f"Node pool '{name}' is {state or 'busy'}; AKS runs one operation on a node "
                "pool at a time. Try again once it is Succeeded.")

    # https://learn.microsoft.com/en-us/azure/aks/scale-cluster#scale-user-node-pools-to-0
    system = str(props.get("mode") or "").lower() == "system"
    if system and method == MANUAL and count < 1:
        return "FAILURE", "", f"'{name}' is a System node pool and needs at least one node."
    if system and method == AUTOSCALE and min_count < 1:
        return "FAILURE", "", f"'{name}' is a System node pool; its Autoscale Minimum must be at least 1."

    # ---- Work out the change -----------------------------------------------
    autoscaling = bool(props.get("enableAutoScaling"))
    current_count = props.get("count")
    before = describe_scale(pool)

    if method == MANUAL:
        if not autoscaling and current_count == count:
            upsert_pool_sub_resource(resource, pool)
            return "SUCCESS", f"Node pool '{name}' already runs {count} node(s) with manual scaling.", ""

        def mutate(properties):
            # Turn the autoscaler off and set the count in the same update.
            # https://learn.microsoft.com/en-us/azure/aks/cluster-autoscaler#disable-the-cluster-autoscaler-on-a-node-pool
            properties["enableAutoScaling"] = False
            properties.pop("minCount", None)
            properties.pop("maxCount", None)
            properties["count"] = count

        target = f"manual, {count} node(s)"
    else:
        base = current_count if current_count is not None else min_count
        new_count = min(max(base, min_count), max_count)
        if (autoscaling and props.get("minCount") == min_count and props.get("maxCount") == max_count
                and new_count == current_count):
            upsert_pool_sub_resource(resource, pool)
            return ("SUCCESS", f"Node pool '{name}' already autoscales between {min_count} and "
                    f"{max_count} node(s).", "")

        def mutate(properties):
            # https://learn.microsoft.com/en-us/azure/aks/cluster-autoscaler#use-the-cluster-autoscaler-on-multiple-node-pools
            properties["enableAutoScaling"] = True
            properties["minCount"] = min_count
            properties["maxCount"] = max_count
            properties["count"] = new_count

        target = f"autoscale {min_count}-{max_count}"
        if new_count != current_count:
            target += f", starting at {new_count} node(s)"

    # ---- Apply -------------------------------------------------------------
    set_progress(f"Scaling node pool '{name}' on '{cluster.name}' from {before} to {target}...")
    try:
        pool = cluster.update_pool(name, mutate, what=f"Scaling node pool '{name}'")
    except AKSError as exc:
        logger.exception("AKS node pool scale failed")
        return "FAILURE", "", f"AKS refused or failed to scale node pool '{name}': {exc}"

    upsert_pool_sub_resource(resource, pool)
    return "SUCCESS", f"Node pool '{name}' now runs {describe_scale(pool)} (was {before}).", ""
