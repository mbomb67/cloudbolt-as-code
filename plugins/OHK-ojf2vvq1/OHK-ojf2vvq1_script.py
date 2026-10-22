"""
Day-2 action for the Azure Kubernetes Service blueprint: upgrade the
cluster's Kubernetes version.

The versions offered are the control-plane upgrades AKS lists in the
cluster's upgrade profile: generally available versions first, newest first,
then preview versions, labelled as such. AKS lists only versions it accepts
from the current one, so it never offers a skip over a minor version.

Upgrade Scope:
  * Control plane and all node pools: AKS upgrades the control plane, then
    each node pool in turn, draining nodes as it goes.
  * Control plane only: the node pools stay on their versions; bring them up
    later with Upgrade AKS Node Pool. AKS refuses this while the cluster has
    an automatic upgrade channel other than none, because cluster auto-upgrade
    always moves the control plane and the node pools together.
Afterwards the job refreshes the cluster's values and the AKS Node Pool
sub-resources.
  https://learn.microsoft.com/en-us/azure/aks/upgrade-aks-cluster
  https://learn.microsoft.com/en-us/azure/aks/upgrade-aks-control-plane
  https://learn.microsoft.com/en-us/azure/aks/auto-upgrade-cluster#control-plane-upgrade-constraints
  https://learn.microsoft.com/en-us/rest/api/aks/managed-clusters/get-upgrade-profile
"""

import re

from common.methods import set_progress
from shared_modules.azure_aks import (
    AKSError,
    cluster_for_resource,
    stamp_cluster,
    sync_pool_sub_resources,
    version_key,
)
from utilities.logger import ThreadLogger

logger = ThreadLogger(__name__)

ALL = "all"
CONTROL_PLANE = "control-plane"
SCOPES = [
    (ALL, "Control plane and all node pools"),
    (CONTROL_PLANE, "Control plane only"),
]
VERSION_RE = re.compile(r"^\d+\.\d+(\.\d+)?$")

# Provisioning states in which the cluster takes a new operation. AKS runs one
# operation at a time on a cluster.
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


def control_plane_upgrades(profile):
    """(current version, upgrades) from the cluster's upgrade profile.

    upgrades are properties.controlPlaneProfile.upgrades items
    (kubernetesVersion, isPreview), generally available first, newest first.
    https://learn.microsoft.com/en-us/rest/api/aks/managed-clusters/get-upgrade-profile
    """
    control_plane = (profile.get("properties") or {}).get("controlPlaneProfile") or {}
    upgrades = [u for u in control_plane.get("upgrades") or [] if u.get("kubernetesVersion")]
    upgrades.sort(key=lambda u: version_key(u["kubernetesVersion"]), reverse=True)
    upgrades.sort(key=lambda u: bool(u.get("isPreview")))
    return control_plane.get("kubernetesVersion") or "", upgrades


# ---------------------------------------------------------------------------
# Dialog options
# ---------------------------------------------------------------------------

def generate_options_for_aks_upgrade_version(field=None, **kwargs):
    resource = _cluster_resource(kwargs)
    if resource is None:
        return []
    try:
        current, upgrades = control_plane_upgrades(cluster_for_resource(resource).upgrade_profile())
    except AKSError as exc:
        logger.warning("Could not read the upgrade profile of %s: %s", resource, exc)
        return [("", f"------ Could not list upgrades: {str(exc)[:120]} ------")]
    if not upgrades:
        return [("", f"------ No newer version is available; the cluster runs {current or 'an unknown version'} ------")]
    options = [
        (u["kubernetesVersion"], u["kubernetesVersion"] + (" (preview)" if u.get("isPreview") else ""))
        for u in upgrades
    ]
    return {"options": options, "initial_value": options[0][0], "sort": False}


def generate_options_for_aks_upgrade_scope(field=None, **kwargs):
    return {"options": SCOPES, "initial_value": ALL, "sort": False}


# ---------------------------------------------------------------------------
# Upgrade
# ---------------------------------------------------------------------------

def run(job, resource, **kwargs):
    version = _text("{{ aks_upgrade_version }}")
    scope = _text("{{ aks_upgrade_scope }}") or ALL

    if not VERSION_RE.match(version):
        return "FAILURE", "", "Choose a Kubernetes version; AKS may offer none newer than the cluster's version."
    if scope not in (ALL, CONTROL_PLANE):
        return "FAILURE", "", f"Upgrade Scope must be '{ALL}' or '{CONTROL_PLANE}', not '{scope}'."

    try:
        cluster = cluster_for_resource(resource)
        cluster_json = cluster.get()
        _, upgrades = control_plane_upgrades(cluster.upgrade_profile())
    except AKSError as exc:
        return "FAILURE", "", str(exc)

    props = cluster_json.get("properties") or {}
    current = props.get("currentKubernetesVersion") or props.get("kubernetesVersion") or ""
    if current and version_key(version) == version_key(current):
        return ("SUCCESS", f"Cluster '{cluster.name}' already runs Kubernetes {current}. Bring node pools "
                "up to it with Upgrade AKS Node Pool.", "")
    offered = [u["kubernetesVersion"] for u in upgrades]
    if version not in offered:
        return ("FAILURE", "", f"AKS does not offer an upgrade of '{cluster.name}' from Kubernetes {current} "
                f"to {version}. It offers: {', '.join(offered) or 'nothing newer'}.")

    # https://learn.microsoft.com/en-us/azure/aks/auto-upgrade-cluster#control-plane-upgrade-constraints
    channel = str((props.get("autoUpgradeProfile") or {}).get("upgradeChannel") or "none")
    if scope == CONTROL_PLANE and channel.lower() != "none":
        return (
            "FAILURE",
            "",
            f"Cluster '{cluster.name}' upgrades automatically on the '{channel}' channel, and AKS cannot "
            "upgrade only the control plane while cluster auto-upgrade is on: it always upgrades the control "
            "plane and the node pools together. Choose 'Control plane and all node pools', or set the "
            "cluster's automatic upgrade channel to none first.",
        )

    state = str(props.get("provisioningState") or "")
    if state.lower() not in SETTLED_STATES:
        return ("FAILURE", "", f"Cluster '{cluster.name}' is {state or 'busy'}; AKS runs one operation on a "
                "cluster at a time. Try again once it is Succeeded.")

    # ---- Upgrade -----------------------------------------------------------
    with_pools = scope == ALL
    set_progress(
        f"Upgrading '{cluster.name}' from Kubernetes {current} to {version}: "
        + ("the control plane, then each node pool in turn..." if with_pools else "the control plane only...")
    )
    try:
        updated = cluster.upgrade_control_plane(version, include_node_pools=with_pools)
    except AKSError as exc:
        if _still_running(exc):
            return ("WARNING", "", f"{exc} AKS carries on in Azure; run List AKS Node Pools once it "
                    "finishes to refresh CloudBolt.")
        logger.exception("AKS cluster upgrade failed")
        return "FAILURE", "", f"AKS refused or failed to upgrade '{cluster.name}' to {version}: {exc}"

    stamp_cluster(resource, updated)
    new_version = (updated.get("properties") or {}).get("currentKubernetesVersion") or version
    try:
        pools = cluster.list_pools()
        sync_pool_sub_resources(resource, pools)
    except AKSError as exc:
        set_progress(f"Warning: the node pools could not be refreshed: {exc}")
        pools = []

    behind = []
    for pool in pools:
        pool_props = pool.get("properties") or {}
        pool_version = pool_props.get("currentOrchestratorVersion") or pool_props.get("orchestratorVersion")
        if pool_version and version_key(pool_version) < version_key(new_version):
            behind.append(f"{pool.get('name')} ({pool_version})")

    message = f"Cluster '{cluster.name}' now runs Kubernetes {new_version} (was {current})."
    if behind:
        message += (f" Node pools on an older version: {', '.join(behind)}; upgrade them with Upgrade AKS "
                    "Node Pool.")
    logger.info(message)
    return "SUCCESS", message, ""
