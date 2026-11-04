"""
Day-2 action for the Google Kubernetes Engine blueprint: change how many
nodes a node pool runs.

GKE counts a node pool's nodes per zone: a pool in a regional cluster runs
the chosen number in each of its zones.
  * Manual turns the cluster autoscaler off for the pool, when it was on,
    then resizes the pool to the chosen number of nodes per zone.
  * Autoscale turns the cluster autoscaler on (or changes its range) with
    the chosen minimum and maximum per zone; the autoscaler then moves the
    pool into that range.
The job waits for GKE to finish and refreshes the pool's GKE Node Pool
sub-resource.
  https://cloud.google.com/kubernetes-engine/docs/how-to/resizing-a-cluster
  https://cloud.google.com/kubernetes-engine/docs/how-to/cluster-autoscaler
  https://cloud.google.com/kubernetes-engine/docs/reference/rest/v1/projects.locations.clusters.nodePools/setSize
  https://cloud.google.com/kubernetes-engine/docs/reference/rest/v1/projects.locations.clusters.nodePools/setAutoscaling
"""

from common.methods import set_progress
from shared_modules.gcp_gke import (
    GKEError,
    NODE_POOL_NAME_RE,
    autoscale_uses_totals,
    cluster_for_resource,
    int_input,
    pool_label,
    text_input,
    upsert_pool_sub_resource,
)
from utilities.logger import ThreadLogger

logger = ThreadLogger(__name__)

MANUAL = "Manual"
AUTOSCALE = "Autoscale"
METHODS = [
    (MANUAL, "Manual - a fixed number of nodes per zone"),
    (AUTOSCALE, "Autoscale - the cluster autoscaler keeps each zone between a minimum and maximum"),
]

# A sanity bound on the per-zone node counts.
MAX_NODES = 1000

# Node pool statuses in which GKE takes a new operation.
# https://cloud.google.com/kubernetes-engine/docs/reference/rest/v1/projects.locations.clusters.nodePools#status
SETTLED_STATUSES = ("RUNNING", "RUNNING_WITH_ERROR")


def _cluster_resource(kwargs):
    """The cluster resource an option generator runs for, or None outside a resource."""
    resource = kwargs.get("resource")
    return resource if hasattr(resource, "get_cf_values_as_dict") else None


def describe_scale(pool_json):
    """'manual', 'autoscale 1-5 per zone', or 'autoscale 3-15 in total'."""
    autoscaling = pool_json.get("autoscaling") or {}
    if not autoscaling.get("enabled"):
        return "manual"
    if autoscale_uses_totals(autoscaling):
        return (f"autoscale {autoscaling.get('totalMinNodeCount', 0)}-{autoscaling.get('totalMaxNodeCount')} "
                "in total")
    return f"autoscale {autoscaling.get('minNodeCount', 0)}-{autoscaling.get('maxNodeCount')} per zone"


# ---------------------------------------------------------------------------
# Dialog options
# ---------------------------------------------------------------------------

def generate_options_for_gke_scale_pool(field=None, **kwargs):
    """The cluster's node pools as GKE reports them, with their current scaling."""
    resource = _cluster_resource(kwargs)
    if resource is None:
        return []
    try:
        cluster = cluster_for_resource(resource)
        pools = cluster.list_pools()
        sizes = cluster.pool_sizes(pools)
    except GKEError as exc:
        logger.warning("Could not list node pools of %s: %s", resource, exc)
        return [("", f"------ Could not list node pools: {str(exc)[:120]} ------")]
    if not pools:
        return [("", "------ This cluster has no node pools ------")]
    return [(pool.get("name"), pool_label(pool, total_nodes=sizes.get(pool.get("name")))) for pool in pools]


def generate_options_for_gke_scale_method(field=None, **kwargs):
    return {"options": METHODS, "initial_value": MANUAL, "sort": False}


# ---------------------------------------------------------------------------
# Scale
# ---------------------------------------------------------------------------

def run(job, resource, **kwargs):
    name = text_input("{{ gke_scale_pool }}")
    method = text_input("{{ gke_scale_method }}") or MANUAL
    count = int_input("{{ gke_scale_node_count }}", None)
    min_count = int_input("{{ gke_scale_min_count }}", None)
    max_count = int_input("{{ gke_scale_max_count }}", None)

    # ---- Validate the inputs ----------------------------------------------
    if not NODE_POOL_NAME_RE.match(name):
        return "FAILURE", "", "Choose a node pool to scale."
    if method not in (MANUAL, AUTOSCALE):
        return "FAILURE", "", f"Scale Method must be Manual or Autoscale, not '{method}'."
    if method == MANUAL:
        if count is None:
            return "FAILURE", "", "Manual scaling needs Nodes per Zone."
        if not 0 <= count <= MAX_NODES:
            return "FAILURE", "", f"Nodes per Zone must be between 0 and {MAX_NODES}."
    else:
        if min_count is None or max_count is None:
            return "FAILURE", "", "Autoscale needs both an Autoscale Minimum and an Autoscale Maximum."
        if min_count < 0 or not 1 <= max_count <= MAX_NODES:
            return ("FAILURE", "", f"Autoscale Minimum must be 0 or more and Autoscale Maximum between 1 and "
                    f"{MAX_NODES}.")
        if min_count > max_count:
            return "FAILURE", "", f"Autoscale Minimum ({min_count}) is above Autoscale Maximum ({max_count})."

    try:
        cluster = cluster_for_resource(resource)
        cluster_json = cluster.get()
        pool = cluster.get_pool(name, allow_404=True)
    except GKEError as exc:
        return "FAILURE", "", str(exc)
    if pool is None:
        return ("FAILURE", "", f"Cluster '{cluster.name}' has no node pool named '{name}'. Run List GKE Node "
                "Pools to refresh CloudBolt.")
    status = str(pool.get("status") or "")
    if status not in SETTLED_STATUSES:
        return ("FAILURE", "", f"Node pool '{name}' is {status or 'busy'}; GKE runs one operation on it at a "
                "time. Try again once it is RUNNING.")

    autoscaling = pool.get("autoscaling") or {}
    before = describe_scale(pool)

    # The cluster's only node pool keeps at least one node per zone, or its
    # system pods have nowhere to run.
    try:
        only_pool = len(cluster.list_pools()) == 1
    except GKEError as exc:
        return "FAILURE", "", str(exc)
    if only_pool and ((method == MANUAL and count < 1) or (method == AUTOSCALE and min_count < 1)):
        return ("FAILURE", "", f"Node pool '{name}' is the cluster's only node pool; it keeps at least one node "
                "per zone.")

    # ---- Apply -------------------------------------------------------------
    try:
        if method == MANUAL:
            if autoscaling.get("enabled"):
                set_progress(f"Turning off the cluster autoscaler for node pool '{name}'...")
                cluster.set_pool_autoscaling(name, False)
            set_progress(f"Resizing node pool '{name}' on '{cluster.name}' to {count} node(s) per zone...")
            pool = cluster.set_pool_size(name, count)
            after = f"manual, {count} node(s) per zone"
        else:
            if autoscale_uses_totals(autoscaling):
                set_progress(f"Node pool '{name}' autoscales within limits for the whole pool ({before}); "
                             "the new limits count nodes per zone.")
            elif (autoscaling.get("enabled") and autoscaling.get("minNodeCount", 0) == min_count
                    and autoscaling.get("maxNodeCount") == max_count):
                upsert_pool_sub_resource(resource, pool, cluster_json, cluster.pool_sizes([pool]).get(pool.get("name")))
                return ("SUCCESS", f"Node pool '{name}' already autoscales between {min_count} and {max_count} "
                        "node(s) per zone.", "")
            set_progress(f"Scaling node pool '{name}' on '{cluster.name}' from {before} to autoscale "
                         f"{min_count}-{max_count} per zone...")
            pool = cluster.set_pool_autoscaling(name, True, min_count, max_count)
            after = describe_scale(pool)
    except GKEError as exc:
        if "did not finish within" in str(exc):
            return ("WARNING", "", f"{exc} GKE carries on; run List GKE Node Pools once it finishes to "
                    "refresh CloudBolt.")
        logger.exception("GKE node pool scale failed")
        return "FAILURE", "", f"GKE refused or failed to scale node pool '{name}': {exc}"

    upsert_pool_sub_resource(resource, pool, cluster_json, cluster.pool_sizes([pool]).get(pool.get("name")))
    return "SUCCESS", f"Node pool '{name}' now runs {after} (was {before}).", ""
