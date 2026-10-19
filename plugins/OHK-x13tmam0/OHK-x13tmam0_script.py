"""
Build plugin for the Azure Kubernetes Service blueprint.

Creates an AKS managed cluster in the chosen Azure environment and resource
group, registers it with CloudBolt as a Kubernetes container orchestrator
with its own Environment (which the AKS Namespace blueprint uses to decide who
may order namespaces on it), records the cluster and its node pools on the
resource, and returns once AKS reports the cluster Succeeded.

The cluster follows Microsoft's baseline for a shared, multi-team cluster:
  * system-assigned managed identity (no service principal secret)
  * AKS-managed Microsoft Entra ID with Azure RBAC for Kubernetes
    authorization, so namespace access is granted with Azure role assignments
    scoped to a namespace
  * Azure CNI Overlay with the Cilium dataplane, which also enforces the
    namespace NetworkPolicies the namespace blueprint applies
  * automatic upgrades on the chosen channel and weekly node image updates
  * OIDC issuer and workload identity enabled
Local accounts stay enabled because CloudBolt reaches the API server with
the cluster's admin credential.
  https://learn.microsoft.com/en-us/rest/api/aks/managed-clusters/create-or-update
  https://learn.microsoft.com/en-us/azure/aks/manage-azure-rbac
  https://learn.microsoft.com/en-us/azure/aks/azure-cni-powered-by-cilium
  https://learn.microsoft.com/en-us/azure/aks/auto-upgrade-cluster
  https://learn.microsoft.com/en-us/azure/aks/auto-upgrade-node-os-image
  https://learn.microsoft.com/en-us/azure/architecture/reference-architectures/containers/aks/baseline-aks

RBAC: users pick an Environment from group.get_available_environments(); the
Azure resource handler is never shown (docs/agents/rbac-and-security.md).
"""

import re

from common.methods import set_progress
from infrastructure.models import Environment
from shared_modules.azure_aks import (
    AKSCluster,
    AKSError,
    ArmClient,
    CLUSTER_NAME_RE,
    azure_environments_for_group,
    environment_node_sizes,
    environment_resource_groups,
    guid_or_none,
    handler_for_environment,
    kubernetes_versions,
    register_orchestrator,
    resolve_group,
    stamp_cluster,
    sync_pool_sub_resources,
)
from utilities.logger import ThreadLogger

logger = ThreadLogger(__name__)

SYSTEM_POOL_NAME = "systempool"

# Pricing tiers: Free has no uptime SLA; Standard adds the SLA; Premium adds
# long-term support. https://learn.microsoft.com/en-us/azure/aks/free-standard-pricing-tiers
TIERS = [
    ("Free", "Free - dev/test, no uptime SLA"),
    ("Standard", "Standard - production, uptime SLA"),
    ("Premium", "Premium - Standard plus long-term support"),
]

# Cluster auto-upgrade channels. https://learn.microsoft.com/en-us/azure/aks/auto-upgrade-cluster
UPGRADE_CHANNELS = [
    ("patch", "Enabled with patch (recommended)"),
    ("stable", "Enabled with stable"),
    ("rapid", "Enabled with rapid"),
    ("none", "Disabled"),
]

# Fallback when an environment offers no node_size options. System pools need
# at least 4 vCPUs and 4 GiB and no B-series sizes.
# https://learn.microsoft.com/en-us/azure/aks/use-system-pools#system-and-user-node-pools
DEFAULT_NODE_SIZES = ["Standard_D4ds_v5", "Standard_D4s_v5", "Standard_D8ds_v5", "Standard_D8s_v5"]


# ---------------------------------------------------------------------------
# Order-form options
# ---------------------------------------------------------------------------

def _environment(control_value):
    if not control_value:
        return None
    try:
        return Environment.objects.filter(id=int(str(control_value).strip())).first()
    except ValueError:
        return None


def generate_options_for_aks_env_id(field, **kwargs):
    """Azure environments the ordering group may use."""
    envs = azure_environments_for_group(resolve_group(kwargs.get("group")))
    if not envs:
        return [("", "------ No Azure environments available to this group ------")]
    return [(env.id, env.name) for env in envs]


def generate_options_for_aks_resource_group(field, control_value=None, **kwargs):
    env = _environment(control_value)
    if env is None:
        return [("", "------ First, select an environment ------")]
    groups = environment_resource_groups(env)
    if not groups:
        return [("", "------ The environment offers no resource groups ------")]
    return [(rg, rg) for rg in groups]


def generate_options_for_aks_node_size(field, control_value=None, **kwargs):
    """The environment's node_size options without B-series sizes, else a default list."""
    env = _environment(control_value)
    if env is None:
        return [("", "------ First, select an environment ------")]
    sizes = [s for s in environment_node_sizes(env) if not s.lower().startswith("standard_b")] or DEFAULT_NODE_SIZES
    initial = "Standard_D4ds_v5" if "Standard_D4ds_v5" in sizes else sizes[0]
    return {"options": [(s, s) for s in sizes], "initial_value": initial}


def generate_options_for_aks_kubernetes_version(field, control_value=None, **kwargs):
    """Generally available Kubernetes minor versions in the environment's region; the default preselected."""
    env = _environment(control_value)
    if env is None:
        return [("", "------ First, select an environment ------")]
    try:
        versions = kubernetes_versions(ArmClient(handler_for_environment(env)), env.node_location)
    except AKSError as exc:
        logger.warning("Could not list AKS versions for %s: %s", env, exc)
        return [("", f"------ Could not list versions: {str(exc)[:120]} ------")]
    options, initial = [], None
    for v in versions:
        if v["is_preview"]:
            continue
        label = f"{v['version']} (latest patch {v['latest_patch']})"
        if v["is_default"]:
            label += " - default"
            initial = v["version"]
        options.append((v["version"], label))
    return {"options": options, "initial_value": initial or (options[0][0] if options else None), "sort": False}


def generate_options_for_aks_sku_tier(field, **kwargs):
    return {"options": TIERS, "initial_value": "Free", "sort": False}


def generate_options_for_aks_upgrade_channel(field, **kwargs):
    return {"options": UPGRADE_CHANNELS, "initial_value": "patch", "sort": False}


# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------

def _tags(resource):
    """Tags that tie the Azure resource back to CloudBolt."""
    tags = {"cloudbolt-resource": str(resource.global_id)}
    if resource.group is not None:
        tags["cloudbolt-group"] = resource.group.name[:256]
    return tags


def _dns_prefix(name):
    """DNS prefix: 1-54 letters, digits, and hyphens, starting and ending with a letter or digit.
    https://learn.microsoft.com/en-us/rest/api/aks/managed-clusters/create-or-update#managedcluster
    """
    prefix = re.sub(r"[^A-Za-z0-9-]", "-", name)[:50].strip("-")
    return f"{prefix}-dns"


def cluster_body(resource, env, name, version, tier, node_size, node_count, max_count,
                 private, channel, admin_groups):
    """The managedCluster PUT body.

    https://learn.microsoft.com/en-us/rest/api/aks/managed-clusters/create-or-update
    https://learn.microsoft.com/en-us/rest/api/aks/agent-pools/create-or-update#agentpoolmode
    """
    pool = {
        "name": SYSTEM_POOL_NAME,
        "mode": "System",
        "type": "VirtualMachineScaleSets",
        "osType": "Linux",
        "vmSize": node_size,
        "count": node_count,
        "orchestratorVersion": version,
    }
    if max_count > node_count:
        pool.update(enableAutoScaling=True, minCount=node_count, maxCount=max_count)
    aad = {"managed": True, "enableAzureRBAC": True}
    if admin_groups:
        aad["adminGroupObjectIDs"] = admin_groups
    return {
        "location": env.node_location,
        "tags": _tags(resource),
        "sku": {"name": "Base", "tier": tier},
        "identity": {"type": "SystemAssigned"},
        "properties": {
            "kubernetesVersion": version,
            "dnsPrefix": _dns_prefix(name),
            "enableRBAC": True,
            "agentPoolProfiles": [pool],
            # https://learn.microsoft.com/en-us/azure/aks/manage-azure-rbac
            "aadProfile": aad,
            "disableLocalAccounts": False,
            # https://learn.microsoft.com/en-us/azure/aks/azure-cni-powered-by-cilium
            "networkProfile": {
                "networkPlugin": "azure",
                "networkPluginMode": "overlay",
                "networkDataplane": "cilium",
                "networkPolicy": "cilium",
            },
            # https://learn.microsoft.com/en-us/azure/aks/private-clusters
            "apiServerAccessProfile": {"enablePrivateCluster": private},
            # https://learn.microsoft.com/en-us/azure/aks/auto-upgrade-cluster
            # https://learn.microsoft.com/en-us/azure/aks/auto-upgrade-node-os-image
            "autoUpgradeProfile": {"upgradeChannel": channel, "nodeOSUpgradeChannel": "NodeImage"},
            # https://learn.microsoft.com/en-us/azure/aks/workload-identity-deploy-cluster
            "oidcIssuerProfile": {"enabled": True},
            "securityProfile": {"workloadIdentity": {"enabled": True}},
        },
    }


def _int(text, default):
    try:
        return int(str(text).strip())
    except (TypeError, ValueError):
        return default


def run(job, resource, **kwargs):
    name = "{{ aks_cluster_name }}".strip()
    env_id = "{{ aks_env_id }}".strip()
    resource_group = "{{ aks_resource_group }}".strip()
    version = "{{ aks_kubernetes_version }}".strip()
    node_size = "{{ aks_node_size }}".strip()
    node_count = _int("{{ aks_node_count }}", 2)
    max_count = _int("{{ aks_max_node_count }}", 0)
    tier = "{{ aks_sku_tier }}".strip() or "Free"
    private = "{{ aks_private_cluster }}".strip().lower() in ("true", "1", "yes", "on")
    channel = "{{ aks_upgrade_channel }}".strip() or "patch"
    admin_groups_text = """{{ aks_admin_group_ids }}"""

    # ---- Validate ----------------------------------------------------------
    if not CLUSTER_NAME_RE.match(name):
        return ("FAILURE", "", f"'{name}' is not a valid AKS cluster name: 1-63 letters, digits, "
                "underscores, or hyphens, starting and ending with a letter or digit.")
    env = Environment.objects.filter(id=_int(env_id, -1)).first()
    if env is None:
        return "FAILURE", "", "Choose an Azure environment."
    group = resource.group
    if group is not None and env.id not in [e.id for e in azure_environments_for_group(group)]:
        return "FAILURE", "", f"Group '{group.name}' may not order into environment '{env.name}'."
    if not resource_group:
        return "FAILURE", "", "Choose a resource group."
    if not node_size:
        return "FAILURE", "", "Choose a node size."
    if not 1 <= node_count <= 1000:
        return "FAILURE", "", "The system node pool needs between 1 and 1000 nodes."
    if max_count and max_count < node_count:
        return "FAILURE", "", f"Autoscale maximum ({max_count}) is below the node count ({node_count})."
    admin_groups, bad = [], []
    for token in re.split(r"[\s,;]+", admin_groups_text.strip()):
        if token:
            (admin_groups if guid_or_none(token) else bad).append(guid_or_none(token) or token)
    if bad:
        return "FAILURE", "", f"Not Microsoft Entra group object IDs: {', '.join(bad)}."

    try:
        rh = handler_for_environment(env)
        arm = ArmClient(rh)
    except AKSError as exc:
        return "FAILURE", "", str(exc)

    cluster = AKSCluster(arm, resource_group, name)
    try:
        if cluster.get(allow_404=True) is not None:
            return ("FAILURE", "", f"An AKS cluster named '{name}' already exists in resource group "
                    f"'{resource_group}'. Choose another name, or run the blueprint's discovery to import it.")
    except AKSError as exc:
        return "FAILURE", "", str(exc)

    resource.name = name
    resource.set_value_for_custom_field("aks_cluster_name", name)
    resource.set_value_for_custom_field("aks_resource_group", resource_group)
    resource.set_value_for_custom_field("azure_rh_id", str(rh.id))
    resource.set_value_for_custom_field("env_id", str(env.id))
    resource.save()

    # ---- Create ----------------------------------------------------------
    set_progress(
        f"Creating AKS cluster '{name}' in {env.node_location} ({resource_group}): Kubernetes {version}, "
        f"{tier} tier, system node pool {node_count} x {node_size}"
        + (f" autoscaling to {max_count}" if max_count > node_count else "") + "..."
    )
    body = cluster_body(resource, env, name, version, tier, node_size, node_count, max_count,
                        private, channel, admin_groups)
    try:
        created = cluster.put(body, what=f"Creating AKS cluster '{name}'")
    except AKSError as exc:
        logger.exception("AKS create failed")
        return "FAILURE", "", f"AKS refused or failed to create '{name}': {exc}"
    stamp_cluster(resource, created, rh=rh, env=env)
    set_progress(f"AKS reports '{name}' {created['properties'].get('provisioningState')}.")

    # ---- Node pools --------------------------------------------------------
    try:
        pools = cluster.list_pools()
        sync_pool_sub_resources(resource, pools)
        set_progress(f"Recorded {len(pools)} node pool(s) as AKS Node Pool sub-resources.")
    except AKSError as exc:
        set_progress(f"Warning: the node pools could not be recorded: {exc}")

    # ---- CloudBolt orchestrator and per-cluster environment ----------------
    if private:
        set_progress(
            "Private cluster: CloudBolt registers the orchestrator, but reaches the API server only "
            "when the appliance can resolve and route to its private endpoint."
        )
    try:
        orchestrator, cluster_env, _ = register_orchestrator(resource, cluster, env.node_location)
    except AKSError as exc:
        return ("FAILURE", "", f"The cluster was created, but CloudBolt could not register it as a "
                f"container orchestrator: {exc}")
    set_progress(f"Registered container orchestrator '{orchestrator.name}' and environment '{cluster_env.name}'.")

    return (
        "SUCCESS",
        f"AKS cluster '{name}' is ready (Kubernetes {created['properties'].get('currentKubernetesVersion')}). "
        f"Entitle groups to environment '{cluster_env.name}' to let them order namespaces on it.",
        "",
    )
