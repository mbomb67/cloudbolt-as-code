"""
Day-2 action for the Azure Kubernetes Service blueprint: add a node pool to
the cluster.

The new pool runs the control plane's Kubernetes version on Virtual Machine
Scale Sets, with Linux nodes of the chosen size and OS SKU. When Autoscale
Maximum is greater than Node Count, the cluster autoscaler is enabled with
Node Count as its minimum. The job waits for AKS to finish and records the
pool as an AKS Node Pool sub-resource of the cluster.

User pools host application pods and may have no nodes. System pools host
critical system pods such as CoreDNS, need at least one node, and cannot use
B-series sizes.
  https://learn.microsoft.com/en-us/rest/api/aks/agent-pools/create-or-update
  https://learn.microsoft.com/en-us/azure/aks/create-node-pools
  https://learn.microsoft.com/en-us/azure/aks/use-system-pools
  https://learn.microsoft.com/en-us/azure/aks/cluster-autoscaler

RBAC: the Azure handler comes from the environment recorded on the cluster
resource and is never shown to the user (docs/agents/rbac-and-security.md).
"""

from common.methods import set_progress
from infrastructure.models import Environment
from shared_modules.azure_aks import (
    AKSError,
    NODE_POOL_NAME_RE,
    cluster_for_resource,
    environment_node_sizes,
    pool_label,
    upsert_pool_sub_resource,
)
from utilities.logger import ThreadLogger

logger = ThreadLogger(__name__)

# Node pool modes. https://learn.microsoft.com/en-us/rest/api/aks/agent-pools/create-or-update#agentpoolmode
MODES = [
    ("User", "User - application workloads"),
    ("System", "System - critical system pods such as CoreDNS"),
]

# Linux OS SKUs offered here; the API accepts more.
# https://learn.microsoft.com/en-us/rest/api/aks/agent-pools/create-or-update#ossku
OS_SKUS = [
    ("Ubuntu", "Ubuntu"),
    ("AzureLinux", "Azure Linux"),
]

ZONES = [
    ("", "None"),
    ("1,2,3", "Zones 1, 2, and 3"),
]
VALID_ZONES = ("1", "2", "3")

# Fallback when the cluster's environment offers no node_size options; the
# same sizes the build plugin falls back to, all valid for System pools.
DEFAULT_NODE_SIZES = ["Standard_D4ds_v5", "Standard_D4s_v5", "Standard_D8ds_v5", "Standard_D8s_v5"]
DEFAULT_NODE_SIZE = "Standard_D4ds_v5"

# Node counts: 0-1000 for User pools, 1-1000 for System pools.
# https://learn.microsoft.com/en-us/rest/api/aks/agent-pools/create-or-update#agentpool
MAX_NODES = 1000


def _text(value):
    """A rendered input as plain text; an input left empty renders as 'None'."""
    text = str(value or "").strip()
    return "" if text == "None" else text


def _int(value, default=None):
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _cluster_resource(kwargs):
    """The cluster resource an option generator runs for, or None outside a resource."""
    resource = kwargs.get("resource")
    return resource if hasattr(resource, "get_cf_values_as_dict") else None


def _cluster_environment(resource):
    """The Environment the cluster was ordered into (env_id), or None for a discovered cluster."""
    env_id = resource.get_cf_values_as_dict().get("env_id")
    try:
        return Environment.objects.filter(id=int(str(env_id).strip())).first() if env_id else None
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Dialog options
# ---------------------------------------------------------------------------

def generate_options_for_aks_new_pool_mode(field=None, **kwargs):
    return {"options": MODES, "initial_value": "User", "sort": False}


def generate_options_for_aks_new_pool_vm_size(field=None, **kwargs):
    """The node sizes the cluster's environment offers, else a short D-series list.

    B-series sizes stay listed because User pools may use them; the job
    refuses them for a System pool.
    """
    resource = _cluster_resource(kwargs)
    if resource is None:
        return []
    env = _cluster_environment(resource)
    sizes = (environment_node_sizes(env) if env is not None else []) or DEFAULT_NODE_SIZES
    initial = DEFAULT_NODE_SIZE if DEFAULT_NODE_SIZE in sizes else sizes[0]
    return {"options": [(size, size) for size in sizes], "initial_value": initial}


def generate_options_for_aks_new_pool_os_sku(field=None, **kwargs):
    return {"options": OS_SKUS, "initial_value": "Ubuntu", "sort": False}


def generate_options_for_aks_new_pool_zones(field=None, **kwargs):
    return {"options": ZONES, "initial_value": "", "sort": False}


# ---------------------------------------------------------------------------
# Add the pool
# ---------------------------------------------------------------------------

def pool_properties(mode, vm_size, os_sku, count, max_count, zones, version):
    """The agentPool properties for the new pool.

    https://learn.microsoft.com/en-us/rest/api/aks/agent-pools/create-or-update
    """
    properties = {
        "mode": mode,
        "vmSize": vm_size,
        "count": count,
        "osType": "Linux",
        "osSKU": os_sku,
        "type": "VirtualMachineScaleSets",
        "orchestratorVersion": version,
    }
    if max_count > count:
        # https://learn.microsoft.com/en-us/azure/aks/cluster-autoscaler
        properties.update(enableAutoScaling=True, minCount=count, maxCount=max_count)
    if zones:
        # availabilityZones requires VirtualMachineScaleSets.
        properties["availabilityZones"] = zones
    return properties


def run(job, resource, **kwargs):
    name = _text("{{ aks_new_pool_name }}")
    mode = _text("{{ aks_new_pool_mode }}") or "User"
    vm_size = _text("{{ aks_new_pool_vm_size }}")
    os_sku = _text("{{ aks_new_pool_os_sku }}") or "Ubuntu"
    count = _int("{{ aks_new_pool_count }}")
    max_count = _int("{{ aks_new_pool_max_count }}", 0)
    zones_text = _text("{{ aks_new_pool_zones }}")

    # ---- Validate ----------------------------------------------------------
    if not NODE_POOL_NAME_RE.match(name):
        return ("FAILURE", "", f"'{name}' is not a valid node pool name: 1-12 lowercase letters and "
                "digits, starting with a letter.")
    if mode not in [value for value, _ in MODES]:
        return "FAILURE", "", f"Mode must be User or System, not '{mode}'."
    if os_sku not in [value for value, _ in OS_SKUS]:
        return "FAILURE", "", f"OS SKU must be Ubuntu or AzureLinux, not '{os_sku}'."
    if not vm_size:
        return "FAILURE", "", "Choose a node size."
    # https://learn.microsoft.com/en-us/azure/aks/use-system-pools#system-and-user-node-pools
    if mode == "System" and vm_size.lower().startswith("standard_b"):
        return ("FAILURE", "", f"{vm_size} is a B-series size, which AKS does not support for System "
                "node pools. Choose another size or make the pool a User pool.")
    if count is None or not 0 <= count <= MAX_NODES:
        return "FAILURE", "", f"Node Count must be between 0 and {MAX_NODES}."
    if mode == "System" and count < 1:
        return "FAILURE", "", "A System node pool needs at least one node; only User pools may have none."
    if not 0 <= max_count <= MAX_NODES:
        return "FAILURE", "", f"Autoscale Maximum must be between 0 and {MAX_NODES}."
    if max_count and max_count < count:
        return ("FAILURE", "", f"Autoscale Maximum ({max_count}) is below the Node Count ({count}). "
                "Leave it empty for a fixed node count.")
    zones = [zone.strip() for zone in zones_text.split(",") if zone.strip()]
    if any(zone not in VALID_ZONES for zone in zones):
        return "FAILURE", "", f"'{zones_text}' is not a list of availability zones 1, 2, and 3."

    try:
        cluster = cluster_for_resource(resource)
        cluster_json = cluster.get()
        if cluster.get_pool(name, allow_404=True) is not None:
            return ("FAILURE", "", f"Cluster '{cluster.name}' already has a node pool named '{name}'. "
                    "Choose another name.")
    except AKSError as exc:
        return "FAILURE", "", str(exc)

    # A new pool joins on the control plane's version; a pool can never run a
    # newer version than the control plane.
    # https://learn.microsoft.com/en-us/azure/aks/upgrade-node-pools#validation-rules-for-upgrades
    cluster_props = cluster_json.get("properties") or {}
    version = cluster_props.get("currentKubernetesVersion") or cluster_props.get("kubernetesVersion")
    if not version:
        return "FAILURE", "", f"AKS reports no Kubernetes version for cluster '{cluster.name}'."

    # ---- Create ----------------------------------------------------------
    properties = pool_properties(mode, vm_size, os_sku, count, max_count, zones, version)
    scale = f"autoscaling {count}-{max_count}" if max_count > count else f"{count} node(s)"
    set_progress(
        f"Adding {mode} node pool '{name}' to '{cluster.name}': {vm_size}, {os_sku}, {scale}, "
        f"Kubernetes {version}" + (f", zones {', '.join(zones)}" if zones else "") + "..."
    )
    try:
        pool = cluster.put_pool(name, properties, what=f"Creating node pool '{name}'")
    except AKSError as exc:
        logger.exception("AKS node pool create failed")
        return "FAILURE", "", f"AKS refused or failed to create node pool '{name}': {exc}"

    upsert_pool_sub_resource(resource, pool)
    return "SUCCESS", f"Added node pool {pool_label(pool)} to '{cluster.name}'.", ""
