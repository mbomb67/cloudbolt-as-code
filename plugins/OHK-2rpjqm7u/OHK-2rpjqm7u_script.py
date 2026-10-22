"""
Day-2 action for the Google Kubernetes Engine blueprint: upgrade one node
pool to the control plane's Kubernetes version.

Upgrade GKE Version with "Control plane only" leaves the node pools behind;
on a release channel GKE upgrades them itself later. This action closes the
gap for one pool now. A node pool can never run a newer version than the
control plane, so the control plane's version is the only target offered,
and the dropdown lists only pools behind it. GKE upgrades the nodes in turn,
draining each one first; the node image type is kept. The job waits for GKE
to finish and refreshes the pool's GKE Node Pool sub-resource.
  https://cloud.google.com/kubernetes-engine/docs/how-to/upgrading-a-cluster
  https://cloud.google.com/kubernetes-engine/docs/concepts/node-pool-upgrade-strategies
  https://cloud.google.com/kubernetes-engine/docs/reference/rest/v1/projects.locations.clusters.nodePools/update
"""

from common.methods import set_progress
from shared_modules.gcp_gke import (
    GKEError,
    NODE_POOL_NAME_RE,
    cluster_for_resource,
    text_input,
    upsert_pool_sub_resource,
    version_key,
)
from utilities.logger import ThreadLogger

logger = ThreadLogger(__name__)

# Node pool statuses in which GKE takes a new operation.
# https://cloud.google.com/kubernetes-engine/docs/reference/rest/v1/projects.locations.clusters.nodePools#status
SETTLED_STATUSES = ("RUNNING", "RUNNING_WITH_ERROR")


def _cluster_resource(kwargs):
    """The cluster resource an option generator runs for, or None outside a resource."""
    resource = kwargs.get("resource")
    return resource if hasattr(resource, "get_cf_values_as_dict") else None


def behind(pool, control_plane):
    """True when the pool runs an older version than the control plane."""
    version = pool.get("version") or ""
    return bool(version and control_plane and version_key(version) < version_key(control_plane))


# ---------------------------------------------------------------------------
# Dialog options
# ---------------------------------------------------------------------------

def generate_options_for_gke_upgrade_pool(field=None, **kwargs):
    """Every pool behind the control plane, with the version it moves to."""
    resource = _cluster_resource(kwargs)
    if resource is None:
        return []
    try:
        cluster = cluster_for_resource(resource)
        control_plane = cluster.get().get("currentMasterVersion") or ""
        pools = cluster.list_pools()
    except GKEError as exc:
        logger.warning("Could not read node pools of %s: %s", resource, exc)
        return [("", f"------ Could not read the node pools: {str(exc)[:120]} ------")]
    options = [
        (pool.get("name"), f"{pool.get('name')}: {pool.get('version')} -> {control_plane} (control plane version)")
        for pool in pools if behind(pool, control_plane)
    ]
    if not options:
        return [("", "------ Every node pool runs the control plane's version ------")]
    return options


# ---------------------------------------------------------------------------
# Upgrade
# ---------------------------------------------------------------------------

def run(job, resource, **kwargs):
    name = text_input("{{ gke_upgrade_pool }}")
    if not NODE_POOL_NAME_RE.match(name):
        return "FAILURE", "", "Choose a node pool to upgrade."

    try:
        cluster = cluster_for_resource(resource)
        cluster_json = cluster.get()
        pool = cluster.get_pool(name, allow_404=True)
    except GKEError as exc:
        return "FAILURE", "", str(exc)
    if pool is None:
        return ("FAILURE", "", f"Cluster '{cluster.name}' has no node pool named '{name}'. Run List GKE Node "
                "Pools to refresh CloudBolt.")

    control_plane = cluster_json.get("currentMasterVersion") or ""
    current = pool.get("version") or ""
    if not behind(pool, control_plane):
        upsert_pool_sub_resource(resource, pool, cluster_json, cluster.pool_sizes([pool]).get(pool.get("name")))
        return "SUCCESS", f"Node pool '{name}' already runs the control plane's version, {current}.", ""
    status = str(pool.get("status") or "")
    if status not in SETTLED_STATUSES:
        return ("FAILURE", "", f"Node pool '{name}' is {status or 'busy'}; GKE runs one operation on it at a "
                "time. Try again once it is RUNNING.")

    set_progress(f"Upgrading node pool '{name}' on '{cluster.name}' from {current} to {control_plane}; GKE "
                 "drains and upgrades the nodes in turn...")
    try:
        pool = cluster.upgrade_pool(name, "-")
    except GKEError as exc:
        if "did not finish within" in str(exc):
            return ("WARNING", "", f"{exc} GKE carries on; run List GKE Node Pools once it finishes to "
                    "refresh CloudBolt.")
        logger.exception("GKE node pool upgrade failed")
        return "FAILURE", "", f"GKE refused or failed to upgrade node pool '{name}': {exc}"

    upsert_pool_sub_resource(resource, pool, cluster_json, cluster.pool_sizes([pool]).get(pool.get("name")))
    return "SUCCESS", f"Node pool '{name}' now runs {pool.get('version')} (was {current}).", ""
