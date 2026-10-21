"""
Day-2 action for the Google Kubernetes Engine blueprint: re-read the cluster
and its node pools from GKE.

Refreshes the cluster's own values (Kubernetes version, status, endpoint,
release channel, network, ...) and makes the GKE Node Pool sub-resources
match the pools GKE reports: missing ones are created, existing ones
refreshed, and ones GKE no longer has removed. When the cluster has no
container orchestrator yet (a discovered cluster whose API server could not
be reached, or a build that stopped before registering it), the orchestrator
is registered too. Run it after a change made outside CloudBolt, such as in
the Google Cloud console or by an automatic upgrade.
  https://cloud.google.com/kubernetes-engine/docs/reference/rest/v1/projects.locations.clusters/get
  https://cloud.google.com/kubernetes-engine/docs/reference/rest/v1/projects.locations.clusters.nodePools/list
"""

from common.methods import set_progress
from shared_modules.gcp_gke import (
    GKEError,
    cluster_for_resource,
    orchestrator_for_resource,
    pool_label,
    register_orchestrator,
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
    except GKEError as exc:
        return "FAILURE", "", str(exc)

    stamp_cluster(resource, cluster_json, cluster.project)
    sizes = cluster.pool_sizes(pools)
    created, updated, removed = sync_pool_sub_resources(resource, pools, cluster_json, sizes)

    lines = [pool_label(pool, cluster_json, sizes.get(pool.get("name"))) for pool in pools]
    for line in lines:
        set_progress(line)
    logger.info("Synced %s node pool(s) of %s", len(pools), cluster.name)

    if orchestrator_for_resource(resource) is None:
        try:
            orchestrator, env, _ = register_orchestrator(resource, cluster, cluster_json)
            lines.append(f"Registered container orchestrator '{orchestrator.name}' and environment '{env.name}'.")
        except GKEError as exc:
            lines.append(f"The cluster has no container orchestrator, and it could not be registered: {exc}")
        set_progress(lines[-1])

    summary = (
        f"Cluster '{cluster.name}' (Kubernetes {cluster_json.get('currentMasterVersion')}, "
        f"{cluster_json.get('status')}) has {len(pools)} node pool(s). GKE Node Pool sub-resources: "
        f"{created} created, {updated} updated, {removed} removed."
    )
    return "SUCCESS", "\n".join([summary] + lines), ""
