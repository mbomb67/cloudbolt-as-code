"""
Day-2 action for the Azure Kubernetes Service blueprint: re-read the cluster
and its node pools from AKS.

Refreshes the cluster's own values (Kubernetes version, provisioning and
power state, network, upgrade channel, ...) and makes the AKS Node Pool
sub-resources match the pools AKS reports: missing ones are created, existing
ones refreshed, and ones AKS no longer has removed. Run it after a change made
outside CloudBolt, such as in the Azure portal or by cluster auto-upgrade.
  https://learn.microsoft.com/en-us/rest/api/aks/managed-clusters/get
  https://learn.microsoft.com/en-us/rest/api/aks/agent-pools/list
"""

from common.methods import set_progress
from shared_modules.azure_aks import (
    AKSError,
    cluster_for_resource,
    pool_label,
    stamp_cluster,
    sync_pool_sub_resources,
)
from utilities.logger import ThreadLogger

logger = ThreadLogger(__name__)


def run(job, resource, **kwargs):
    try:
        cluster = cluster_for_resource(resource)
        cluster_json = cluster.get()
        pools = cluster.list_pools()
    except AKSError as exc:
        return "FAILURE", "", str(exc)

    stamp_cluster(resource, cluster_json)
    created, updated, removed = sync_pool_sub_resources(resource, pools)

    props = cluster_json.get("properties") or {}
    lines = [pool_label(pool) for pool in pools]
    for line in lines:
        set_progress(line)
    logger.info("Synced %s node pool(s) of %s", len(pools), cluster.name)

    summary = (
        f"Cluster '{cluster.name}' (Kubernetes {props.get('currentKubernetesVersion')}, "
        f"{props.get('provisioningState')}) has {len(pools)} node pool(s). AKS Node Pool "
        f"sub-resources: {created} created, {updated} updated, {removed} removed."
    )
    return "SUCCESS", "\n".join([summary] + lines), ""
