"""
Teardown plugin for the Azure Kubernetes Service blueprint.

Deletes the AKS managed cluster (AKS deletes its node pools and node resource
group with it), then the Kubernetes container orchestrator and per-cluster
Environment CloudBolt registered for it, and the AKS Node Pool sub-resources.

Namespace landing zones (AKS Namespace blueprint) are sub-resources of the
cluster owned by their teams. Deleting the cluster would take them all down,
so the teardown refuses while any remain; a FAILURE from this teardown item
stops the Delete before CloudBolt touches the sub-resources.
  https://learn.microsoft.com/en-us/rest/api/aks/managed-clusters/delete
"""

from common.methods import set_progress
from shared_modules.azure_aks import (
    AKSError,
    active_namespaces,
    cluster_for_resource,
    delete_orchestrator,
    pool_sub_resources,
)
from utilities.logger import ThreadLogger

logger = ThreadLogger(__name__)


def run(job, *args, **kwargs):
    resource = kwargs.get("resource") or (kwargs.get("resources").first() if kwargs.get("resources") else None)
    if resource is None:
        return "SUCCESS", "No resource was supplied.", ""

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

    try:
        cluster = cluster_for_resource(resource)
    except AKSError as exc:
        set_progress(f"{exc} Removing it from CloudBolt only.")
        cluster = None

    if cluster is not None:
        set_progress(f"Deleting AKS cluster '{cluster.name}' in resource group '{cluster.resource_group}'...")
        try:
            existed = cluster.delete()
        except AKSError as exc:
            logger.exception("AKS delete failed")
            return "FAILURE", "", f"AKS could not delete '{cluster.name}': {exc}"
        set_progress(f"AKS cluster '{cluster.name}' {'deleted' if existed else 'was already gone'}.")

    set_progress(delete_orchestrator(resource))
    pools = pool_sub_resources(resource)
    removed = pools.count()  # delete() would also count the rows it cascades to
    pools.delete()
    if removed:
        set_progress(f"Removed {removed} AKS Node Pool sub-resource(s).")

    return "SUCCESS", f"AKS cluster '{resource.name}' deleted.", ""
