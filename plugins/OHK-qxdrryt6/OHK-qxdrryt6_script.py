"""
Day-2 action for the Google Kubernetes Engine blueprint: delete one node pool.

The dialog lists the cluster's node pools as GKE reports them. A Standard
cluster's pods need nodes to run on, so the cluster's last node pool is left
out of the list, and the job checks again against the live pools before it
deletes anything. The job waits for GKE to finish and removes the pool's GKE
Node Pool sub-resource.
  https://cloud.google.com/kubernetes-engine/docs/reference/rest/v1/projects.locations.clusters.nodePools/delete
  https://cloud.google.com/kubernetes-engine/docs/how-to/node-pools
"""

from common.methods import set_progress
from shared_modules.gcp_gke import (
    GKEError,
    NODE_POOL_NAME_RE,
    cluster_for_resource,
    pool_label,
    remove_pool_sub_resource,
    text_input,
)
from utilities.logger import ThreadLogger

logger = ThreadLogger(__name__)


def _cluster_resource(kwargs):
    """The cluster resource an option generator runs for, or None outside a resource."""
    resource = kwargs.get("resource")
    return resource if hasattr(resource, "get_cf_values_as_dict") else None


def generate_options_for_gke_delete_pool(field=None, **kwargs):
    """The cluster's node pools, read live from GKE, unless only one is left."""
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
    if len(pools) < 2:
        return [("", "------ No node pool can be deleted: the cluster keeps its last node pool ------")]
    return [(pool.get("name"), pool_label(pool, total_nodes=sizes.get(pool.get("name")))) for pool in pools]


def run(job, resource, **kwargs):
    name = text_input("{{ gke_delete_pool }}")
    if not NODE_POOL_NAME_RE.match(name):
        return "FAILURE", "", "Choose a node pool to delete."

    try:
        cluster = cluster_for_resource(resource)
        pools = cluster.list_pools()
    except GKEError as exc:
        return "FAILURE", "", str(exc)

    pool = next((p for p in pools if p.get("name") == name), None)
    if pool is None:
        removed = remove_pool_sub_resource(resource, name)
        return (
            "SUCCESS",
            f"Node pool '{name}' was already gone from '{cluster.name}'"
            + ("; removed its sub-resource." if removed else "."),
            "",
        )
    if len(pools) < 2:
        return ("FAILURE", "", f"'{name}' is the last node pool of '{cluster.name}'. Add another with Add GKE "
                "Node Pool before deleting this one.")

    set_progress(f"Deleting node pool {pool_label(pool)} from '{cluster.name}'...")
    try:
        existed = cluster.delete_pool(name)
    except GKEError as exc:
        logger.exception("GKE node pool delete failed")
        return "FAILURE", "", f"GKE could not delete node pool '{name}': {exc}"

    removed = remove_pool_sub_resource(resource, name)
    if removed:
        set_progress(f"Removed the GKE Node Pool sub-resource '{name}'.")
    state = "deleted from" if existed else "was already gone from"
    return "SUCCESS", f"Node pool '{name}' {state} '{cluster.name}'.", ""
