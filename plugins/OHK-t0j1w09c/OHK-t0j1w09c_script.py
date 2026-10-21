"""
Day-2 action for the Azure Kubernetes Service blueprint: upgrade one node
pool to the control plane's Kubernetes version, or to the latest node image
for the version it already runs.

Upgrade AKS Version with "Control plane only" leaves the node pools behind,
and AKS publishes new node images (OS and runtime patches) every week. This
action closes either gap for one pool at a time:
  * Kubernetes: the pool's orchestratorVersion is set to the control plane's
    version. A node pool can never run a newer version than the control
    plane, so that is the only Kubernetes target offered.
  * Node image: AKS reimages the nodes with the latest image for the pool's
    current version, without changing Kubernetes.
AKS upgrades the nodes in turn, draining each one first. The job waits for
AKS to finish and refreshes the pool's AKS Node Pool sub-resource.

One dropdown lists every pool's valid targets, built from each pool's upgrade
profile, so no field dependency is needed.
  https://learn.microsoft.com/en-us/azure/aks/upgrade-node-pools
  https://learn.microsoft.com/en-us/azure/aks/upgrade-node-image
  https://learn.microsoft.com/en-us/rest/api/aks/agent-pools/get-upgrade-profile
  https://learn.microsoft.com/en-us/rest/api/aks/agent-pools/upgrade-node-image-version
"""

import re

from common.methods import set_progress
from shared_modules.azure_aks import (
    AKSError,
    NODE_POOL_NAME_RE,
    cluster_for_resource,
    upsert_pool_sub_resource,
    version_key,
)
from utilities.logger import ThreadLogger

logger = ThreadLogger(__name__)

# The option value is '<pool>|<Kubernetes version>' or '<pool>|node-image'.
NODE_IMAGE = "node-image"
VERSION_RE = re.compile(r"^\d+\.\d+(\.\d+)?$")

# Provisioning states in which a node pool takes a new operation. AKS runs one
# operation at a time on a node pool.
# https://learn.microsoft.com/en-us/azure/aks/upgrade-node-pools#validation-rules-for-upgrades
SETTLED_STATES = ("succeeded", "failed", "canceled")


def _text(value):
    """A rendered input as plain text; an input left empty renders as 'None'."""
    text = str(value or "").strip()
    return "" if text == "None" else text


def _cluster_resource(kwargs):
    """The cluster resource an option generator runs for, or None outside a resource."""
    resource = kwargs.get("resource")
    return resource if hasattr(resource, "get_cf_values_as_dict") else None


def _still_running(exc):
    """True when the job only stopped waiting; the operation carries on in Azure."""
    return "did not finish within" in str(exc)


def control_plane_version(cluster_json):
    props = cluster_json.get("properties") or {}
    return props.get("currentKubernetesVersion") or props.get("kubernetesVersion") or ""


def pool_version(props):
    return props.get("currentOrchestratorVersion") or props.get("orchestratorVersion") or ""


def latest_node_image(cluster, name):
    """properties.latestNodeImageVersion of the pool's upgrade profile."""
    return (cluster.pool_upgrade_profile(name).get("properties") or {}).get("latestNodeImageVersion") or ""


# ---------------------------------------------------------------------------
# Dialog options
# ---------------------------------------------------------------------------

def generate_options_for_aks_pool_upgrade(field=None, **kwargs):
    """One entry per pool and target.

    The control plane's version for a pool behind it, and the latest node
    image for a pool whose nodeImageVersion differs from it.
    https://learn.microsoft.com/en-us/azure/aks/upgrade-node-image#check-for-available-node-image-upgrades
    """
    resource = _cluster_resource(kwargs)
    if resource is None:
        return []
    try:
        cluster = cluster_for_resource(resource)
        control_plane = control_plane_version(cluster.get())
        pools = cluster.list_pools()
    except AKSError as exc:
        logger.warning("Could not read node pools of %s: %s", resource, exc)
        return [("", f"------ Could not read the node pools: {str(exc)[:120]} ------")]

    options = []
    for pool in pools:
        name = pool.get("name")
        props = pool.get("properties") or {}
        current = pool_version(props)
        if current and control_plane and version_key(current) < version_key(control_plane):
            options.append((
                f"{name}|{control_plane}",
                f"{name}: Kubernetes {current} -> {control_plane} (control plane version)",
            ))
        try:
            latest = latest_node_image(cluster, name)
        except AKSError as exc:
            logger.warning("Could not read the upgrade profile of node pool %s: %s", name, exc)
            continue
        image = props.get("nodeImageVersion") or ""
        if latest and latest != image:
            options.append((f"{name}|{NODE_IMAGE}", f"{name}: node image {image or 'unknown'} -> {latest}"))

    if not options:
        return [("", "------ Every node pool runs the control plane's version and the latest node image ------")]
    return options


# ---------------------------------------------------------------------------
# Upgrade
# ---------------------------------------------------------------------------

def upgrade_node_image(resource, cluster, name, pool):
    """Move the pool to the latest node image for its current Kubernetes version."""
    props = pool.get("properties") or {}
    try:
        latest = latest_node_image(cluster, name)
    except AKSError as exc:
        return "FAILURE", "", str(exc)
    current = props.get("nodeImageVersion") or ""
    if latest and latest == current:
        upsert_pool_sub_resource(resource, pool)
        return "SUCCESS", f"Node pool '{name}' already runs the latest node image, {current}.", ""

    set_progress(f"Updating the node image of '{name}' on '{cluster.name}' from {current or 'unknown'} "
                 f"to {latest or 'the latest'}; AKS reimages the nodes in turn...")
    try:
        pool = cluster.upgrade_node_image(name)
    except AKSError as exc:
        if _still_running(exc):
            return ("WARNING", "", f"{exc} AKS carries on in Azure; run List AKS Node Pools once it "
                    "finishes to refresh CloudBolt.")
        logger.exception("AKS node image upgrade failed")
        return "FAILURE", "", f"AKS refused or failed to update the node image of '{name}': {exc}"

    upsert_pool_sub_resource(resource, pool)
    new_image = (pool.get("properties") or {}).get("nodeImageVersion") or ""
    return "SUCCESS", f"Node pool '{name}' now runs node image {new_image} (was {current or 'unknown'}).", ""


def upgrade_kubernetes(resource, cluster, name, pool, target):
    """Set the pool's orchestratorVersion to ``target``, which may not pass the control plane."""
    props = pool.get("properties") or {}
    try:
        control_plane = control_plane_version(cluster.get())
    except AKSError as exc:
        return "FAILURE", "", str(exc)
    current = pool_version(props)

    # https://learn.microsoft.com/en-us/azure/aks/upgrade-node-pools#validation-rules-for-upgrades
    if control_plane and version_key(target) > version_key(control_plane):
        return ("FAILURE", "", f"Kubernetes {target} is newer than the control plane ({control_plane}), "
                "and a node pool can never run a newer version than the control plane. Run Upgrade AKS "
                "Version first.")
    if current and version_key(target) < version_key(current):
        return ("FAILURE", "", f"Node pool '{name}' already runs Kubernetes {current}; AKS does not "
                f"downgrade it to {target}.")
    if current and version_key(target) == version_key(current):
        upsert_pool_sub_resource(resource, pool)
        return "SUCCESS", f"Node pool '{name}' already runs Kubernetes {current}.", ""

    def mutate(properties):
        properties["orchestratorVersion"] = target

    set_progress(f"Upgrading node pool '{name}' on '{cluster.name}' from Kubernetes {current} to {target}; "
                 "AKS drains and upgrades the nodes in turn...")
    try:
        pool = cluster.update_pool(name, mutate, what=f"Upgrading node pool '{name}' to Kubernetes {target}")
    except AKSError as exc:
        if _still_running(exc):
            return ("WARNING", "", f"{exc} AKS carries on in Azure; run List AKS Node Pools once it "
                    "finishes to refresh CloudBolt.")
        logger.exception("AKS node pool upgrade failed")
        return "FAILURE", "", f"AKS refused or failed to upgrade node pool '{name}': {exc}"

    upsert_pool_sub_resource(resource, pool)
    new_props = pool.get("properties") or {}
    return (
        "SUCCESS",
        f"Node pool '{name}' now runs Kubernetes {pool_version(new_props)}, node image "
        f"{new_props.get('nodeImageVersion')} (was Kubernetes {current}).",
        "",
    )


def run(job, resource, **kwargs):
    choice = _text("{{ aks_pool_upgrade }}")
    name, _, target = choice.partition("|")
    name, target = name.strip(), target.strip()
    if not NODE_POOL_NAME_RE.match(name) or not target:
        return "FAILURE", "", "Choose a node pool and what to upgrade it to."
    if target != NODE_IMAGE and not VERSION_RE.match(target):
        return "FAILURE", "", f"'{target}' is neither a Kubernetes version nor '{NODE_IMAGE}'."

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

    if target == NODE_IMAGE:
        return upgrade_node_image(resource, cluster, name, pool)
    return upgrade_kubernetes(resource, cluster, name, pool, target)
