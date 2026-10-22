"""
Teardown plugin for the Google Kubernetes Engine blueprint.

Deletes the GKE cluster (GKE deletes its node pools and nodes with it), then
the Kubernetes container orchestrator and per-cluster Environment CloudBolt
registered for it, and the GKE Node Pool sub-resources.

Namespace landing zones (GKE Namespace blueprint) are sub-resources of the
cluster owned by their teams. Deleting the cluster would take them all down,
so the teardown refuses while any remain; a FAILURE from this teardown item
stops the Delete before CloudBolt touches the sub-resources.
  https://cloud.google.com/kubernetes-engine/docs/reference/rest/v1/projects.locations.clusters/delete
"""

from common.methods import set_progress
from shared_modules.gcp_gke import (
    NODE_POOL_RESOURCE_TYPE,
    GKEError,
    active_namespaces,
    cluster_for_resource,
    delete_orchestrator,
    pool_sub_resources,
)
from utilities.logger import ThreadLogger

logger = ThreadLogger(__name__)


def node_pool_teardown(resource):
    """Deleting a GKE Node Pool sub-resource in CloudBolt.

    Node pool sub-resources carry the cluster's blueprint, so CloudBolt runs
    this teardown for them too. The record goes only when its pool no longer
    exists in GKE (or its cluster is gone); a live pool is deleted with the
    cluster's Delete GKE Node Pool action, which keeps the last pool.
    """
    pool_name = resource.get_cf_values_as_dict().get("gke_pool_name") or resource.name
    parent = resource.parent_resource
    if parent is None or not parent.get_cf_values_as_dict().get("gke_cluster_name"):
        return "SUCCESS", f"Removed node pool record '{pool_name}'; it belongs to no GKE cluster.", ""
    try:
        cluster = cluster_for_resource(parent)
        live = cluster.get(allow_404=True) is not None and cluster.get_pool(pool_name, allow_404=True) is not None
    except GKEError as exc:
        return "FAILURE", "", f"Could not check node pool '{pool_name}' in GKE: {exc}"
    if live:
        return ("FAILURE", "", f"Node pool '{pool_name}' still runs in GKE cluster '{cluster.name}'. Delete it with "
                f"the cluster's Delete GKE Node Pool action instead.")
    return "SUCCESS", f"Removed the record of node pool '{pool_name}', which GKE no longer has.", ""


def run(job, *args, **kwargs):
    resource = kwargs.get("resource") or (kwargs.get("resources").first() if kwargs.get("resources") else None)
    if resource is None:
        return "SUCCESS", "No resource was supplied.", ""
    if getattr(resource.resource_type, "name", "") == NODE_POOL_RESOURCE_TYPE:
        return node_pool_teardown(resource)

    namespaces = active_namespaces(resource)
    if namespaces.exists():
        listing = ", ".join(
            f"{ns.name} ({ns.group.name if ns.group else 'no group'})" for ns in namespaces[:10]
        )
        more = f" and {namespaces.count() - 10} more" if namespaces.count() > 10 else ""
        return (
            "FAILURE",
            "",
            f"Cluster {resource.name} still hosts {namespaces.count()} namespace landing zone(s): "
            f"{listing}{more}. Delete those namespace resources first, then delete the cluster.",
        )

    values = resource.get_cf_values_as_dict()
    cluster = None
    if not all(values.get(key) for key in ("gke_cluster_name", "gke_project_id", "gke_location")):
        set_progress(f"'{resource.name}' records no GKE cluster; removing it from CloudBolt only.")
    else:
        # A cluster that is recorded but cannot be reached (handler gone or
        # failing) stops the teardown, so CloudBolt never forgets a live cluster.
        try:
            cluster = cluster_for_resource(resource)
        except GKEError as exc:
            return "FAILURE", "", f"Could not reach GKE cluster '{values.get('gke_cluster_name')}': {exc}"

    if cluster is not None:
        set_progress(f"Deleting GKE cluster '{cluster.name}' in {cluster.location} (project {cluster.project})...")
        try:
            existed = cluster.delete()
        except GKEError as exc:
            logger.exception("GKE delete failed")
            return "FAILURE", "", f"GKE could not delete '{cluster.name}': {exc}"
        set_progress(f"GKE cluster '{cluster.name}' {'deleted' if existed else 'was already gone'}.")

    set_progress(delete_orchestrator(resource))
    pools = pool_sub_resources(resource)
    removed = pools.count()  # delete() would also count the rows it cascades to
    pools.delete()
    if removed:
        set_progress(f"Removed {removed} GKE Node Pool sub-resource(s).")

    if cluster is None:
        return "SUCCESS", f"Removed '{resource.name}' from CloudBolt; it recorded no GKE cluster to delete.", ""
    return "SUCCESS", f"GKE cluster '{resource.name}' deleted.", ""
