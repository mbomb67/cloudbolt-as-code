"""
Day-2 action for the Google Kubernetes Engine blueprint: upgrade the
cluster's Kubernetes version.

The versions offered are those GKE accepts for the control plane above the
cluster's current version: the release channel's valid versions in the
cluster's location (or, for a cluster on no channel, the location's valid
master versions), newest first. Kubernetes moves the control plane one minor
version at a time, so later minor versions are left out
(gcp_gke.master_upgrade_targets).

Upgrade Scope:
  * Control plane and all node pools: GKE upgrades the control plane, then
    this action upgrades each node pool behind it to the control plane's
    version, one pool at a time; GKE drains nodes as it goes.
  * Control plane only: the node pools stay on their versions; bring them up
    later with Upgrade GKE Node Pool. On a release channel GKE upgrades them
    itself in a later maintenance window.
Afterwards the job refreshes the cluster's values and the GKE Node Pool
sub-resources.
  https://cloud.google.com/kubernetes-engine/docs/how-to/upgrading-a-cluster
  https://cloud.google.com/kubernetes-engine/docs/concepts/cluster-upgrades
  https://cloud.google.com/kubernetes-engine/docs/reference/rest/v1/projects.locations.clusters/update
  https://cloud.google.com/kubernetes-engine/docs/reference/rest/v1/projects.locations.clusters.nodePools/update
"""

from common.methods import set_progress
from shared_modules.gcp_gke import (
    GKEError,
    channel_versions,
    cluster_for_resource,
    master_upgrade_targets,
    stamp_cluster,
    sync_pool_sub_resources,
    text_input,
    version_key,
)
from utilities.logger import ThreadLogger

logger = ThreadLogger(__name__)

ALL = "control_plane_and_pools"
CONTROL_PLANE = "control_plane"
SCOPES = [
    (ALL, "Control plane and all node pools"),
    (CONTROL_PLANE, "Control plane only"),
]

# Cluster statuses in which GKE takes a new operation; RECONCILING means one is
# under way. https://cloud.google.com/kubernetes-engine/docs/reference/rest/v1/projects.locations.clusters#status
SETTLED_STATUSES = ("RUNNING", "DEGRADED")


def _cluster_resource(kwargs):
    """The cluster resource an option generator runs for, or None outside a resource."""
    resource = kwargs.get("resource")
    return resource if hasattr(resource, "get_cf_values_as_dict") else None


def _still_running(exc):
    """True when the job only stopped waiting; the operation carries on in Google Cloud."""
    return "did not finish within" in str(exc)


def upgrade_targets(cluster):
    """(cluster JSON, targets newest first, channel default, channel upgrade target)."""
    cluster_json = cluster.get()
    config = cluster.api.server_config(cluster.project, cluster.location)
    channel = (cluster_json.get("releaseChannel") or {}).get("channel") or ""
    default, _, target = channel_versions(config, channel) if channel else ("", [], "")
    return cluster_json, master_upgrade_targets(config, cluster_json), default, target


# ---------------------------------------------------------------------------
# Dialog options
# ---------------------------------------------------------------------------

def generate_options_for_gke_upgrade_version(field=None, **kwargs):
    resource = _cluster_resource(kwargs)
    if resource is None:
        return []
    try:
        cluster_json, targets, default, target = upgrade_targets(cluster_for_resource(resource))
    except GKEError as exc:
        logger.warning("Could not list upgrades of %s: %s", resource, exc)
        return [("", f"------ Could not list upgrades: {str(exc)[:120]} ------")]
    current = cluster_json.get("currentMasterVersion") or "an unknown version"
    if not targets:
        return [("", f"------ No newer version is available; the cluster runs {current} ------")]
    options = []
    for version in targets:
        notes = []
        if version == default:
            notes.append("channel default")
        if version == target:
            notes.append("channel upgrade target")
        options.append((version, version + (f" ({', '.join(notes)})" if notes else "")))
    return {"options": options, "initial_value": options[0][0], "sort": False}


def generate_options_for_gke_upgrade_scope(field=None, **kwargs):
    return {"options": SCOPES, "initial_value": ALL, "sort": False}


# ---------------------------------------------------------------------------
# Upgrade
# ---------------------------------------------------------------------------

def run(job, resource, **kwargs):
    version = text_input("{{ gke_upgrade_version }}")
    scope = text_input("{{ gke_upgrade_scope }}") or ALL

    if not version:
        return "FAILURE", "", "Choose a Kubernetes version; GKE may offer none newer than the cluster's version."
    if scope not in (ALL, CONTROL_PLANE):
        return "FAILURE", "", f"Upgrade Scope must be '{ALL}' or '{CONTROL_PLANE}', not '{scope}'."

    try:
        cluster = cluster_for_resource(resource)
        cluster_json, targets, _, _ = upgrade_targets(cluster)
    except GKEError as exc:
        return "FAILURE", "", str(exc)

    current = cluster_json.get("currentMasterVersion") or ""
    if current and version_key(version) == version_key(current):
        return ("SUCCESS", f"Cluster '{cluster.name}' already runs Kubernetes {current}. Bring node pools up "
                "to it with Upgrade GKE Node Pool.", "")
    if version not in targets:
        return ("FAILURE", "", f"GKE does not offer an upgrade of '{cluster.name}' from {current} to {version}. "
                f"It offers: {', '.join(targets) or 'nothing newer'}.")
    status = str(cluster_json.get("status") or "")
    if status not in SETTLED_STATUSES:
        return ("FAILURE", "", f"Cluster '{cluster.name}' is {status or 'busy'}; GKE runs one operation on a "
                "cluster at a time. Try again once it is RUNNING.")

    # ---- Control plane -----------------------------------------------------
    with_pools = scope == ALL
    set_progress(
        f"Upgrading '{cluster.name}' from Kubernetes {current} to {version}: "
        + ("the control plane, then each node pool in turn..." if with_pools else "the control plane only...")
    )
    try:
        updated = cluster.upgrade_control_plane(version)
    except GKEError as exc:
        if _still_running(exc):
            return ("WARNING", "", f"{exc} GKE carries on; run List GKE Node Pools once it finishes to "
                    "refresh CloudBolt.")
        logger.exception("GKE control plane upgrade failed")
        return "FAILURE", "", f"GKE refused or failed to upgrade '{cluster.name}' to {version}: {exc}"
    stamp_cluster(resource, updated, cluster.project)
    new_version = updated.get("currentMasterVersion") or version
    set_progress(f"The control plane of '{cluster.name}' runs Kubernetes {new_version}.")

    # ---- Node pools --------------------------------------------------------
    failures = []
    try:
        pools = cluster.list_pools()
    except GKEError as exc:
        set_progress(f"Warning: the node pools could not be read: {exc}")
        pools = []
    if with_pools:
        for pool in pools:
            name = pool.get("name")
            if not pool.get("version") or version_key(pool["version"]) >= version_key(new_version):
                continue
            set_progress(f"Upgrading node pool '{name}' from {pool['version']} to {new_version}...")
            try:
                cluster.upgrade_pool(name, "-")
            except GKEError as exc:
                logger.warning("Node pool %s upgrade failed: %s", name, exc)
                failures.append(f"{name}: {exc}")
                if _still_running(exc):
                    break
        try:
            pools = cluster.list_pools()
        except GKEError as exc:
            set_progress(f"Warning: the node pools could not be re-read: {exc}")
    if pools:
        sync_pool_sub_resources(resource, pools, updated, cluster.pool_sizes(pools))

    behind = [f"{p.get('name')} ({p.get('version')})" for p in pools
              if p.get("version") and version_key(p["version"]) < version_key(new_version)]
    message = f"Cluster '{cluster.name}' now runs Kubernetes {new_version} (was {current})."
    if behind:
        message += f" Node pools on an older version: {', '.join(behind)}; upgrade them with Upgrade GKE Node Pool."
    if failures:
        return "FAILURE", message, "Node pool upgrades failed: " + "; ".join(failures)
    logger.info(message)
    return "SUCCESS", message, ""
