"""
Day-2 action for the Google Kubernetes Engine blueprint: add a node pool to
the cluster.

The new pool runs nodes of the chosen machine type in each of the cluster's
zones, with the same node defaults as the build (Container-Optimized OS,
Shielded VM features, the GKE metadata server, auto-upgrade and auto-repair;
gcp_gke.node_pool_body). It joins on the control plane's version, since no
version is given. When Autoscale Maximum is greater than Nodes per Zone, the
cluster autoscaler is enabled with Nodes per Zone as its minimum. Spot VMs
cost less and may be reclaimed at any time. The job waits for GKE to finish
and records the pool as a GKE Node Pool sub-resource of the cluster.
  https://cloud.google.com/kubernetes-engine/docs/reference/rest/v1/projects.locations.clusters.nodePools/create
  https://cloud.google.com/kubernetes-engine/docs/how-to/node-pools
  https://cloud.google.com/kubernetes-engine/docs/concepts/cluster-autoscaler
  https://cloud.google.com/kubernetes-engine/docs/how-to/spot-vms

RBAC: the GCP handler comes from the environment recorded on the cluster
resource and is never shown to the user (docs/agents/rbac-and-security.md).
"""

from common.methods import set_progress
from infrastructure.models import Environment
from shared_modules.gcp_gke import (
    GKEError,
    NODE_POOL_NAME_RE,
    cluster_for_resource,
    environment_machine_types,
    int_input,
    node_pool_body,
    pool_label,
    text_input,
    truthy,
    upsert_pool_sub_resource,
)
from utilities.logger import ThreadLogger

logger = ThreadLogger(__name__)

# Fallback when the cluster's environment offers no gcp_node_size options; the
# same types the build plugin falls back to. Shared-core types are left out.
DEFAULT_MACHINE_TYPE = "e2-standard-4"
DEFAULT_MACHINE_TYPES = ["e2-standard-4", "e2-standard-8", "n2-standard-4", "n2-standard-8"]
SHARED_CORE_SUFFIXES = ("-micro", "-small")

# A sanity bound on the per-zone node counts.
MAX_NODES = 1000


def _cluster_resource(kwargs):
    """The cluster resource an option generator runs for, or None outside a resource."""
    resource = kwargs.get("resource")
    return resource if hasattr(resource, "get_cf_values_as_dict") else None


def _cluster_environment(resource):
    """The Environment the cluster was ordered into (env_id), or None."""
    env_id = resource.get_cf_values_as_dict().get("env_id")
    try:
        return Environment.objects.filter(id=int(str(env_id).strip())).first() if env_id else None
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Dialog options
# ---------------------------------------------------------------------------

def generate_options_for_gke_new_pool_machine_type(field=None, **kwargs):
    """The machine types the cluster's environment offers, else a short default list."""
    resource = _cluster_resource(kwargs)
    if resource is None:
        return []
    env = _cluster_environment(resource)
    types = [t for t in (environment_machine_types(env) if env is not None else [])
             if not t.lower().endswith(SHARED_CORE_SUFFIXES)] or DEFAULT_MACHINE_TYPES
    initial = DEFAULT_MACHINE_TYPE if DEFAULT_MACHINE_TYPE in types else types[0]
    return {"options": [(t, t) for t in types], "initial_value": initial}


# ---------------------------------------------------------------------------
# Add the pool
# ---------------------------------------------------------------------------

def run(job, resource, **kwargs):
    name = text_input("{{ gke_new_pool_name }}")
    machine_type = text_input("{{ gke_new_pool_machine_type }}")
    count = int_input("{{ gke_new_pool_node_count }}", None)
    max_count = int_input("{{ gke_new_pool_max_count }}", 0)
    spot = truthy("{{ gke_new_pool_spot }}")

    # ---- Validate ----------------------------------------------------------
    if not NODE_POOL_NAME_RE.match(name):
        return ("FAILURE", "", f"'{name}' is not a valid node pool name: up to 40 lowercase letters, digits, "
                "and hyphens, starting with a letter and ending with a letter or digit.")
    if not machine_type:
        return "FAILURE", "", "Choose a machine type."
    if count is None or not 0 <= count <= MAX_NODES:
        return "FAILURE", "", f"Nodes per Zone must be between 0 and {MAX_NODES}."
    if not 0 <= max_count <= MAX_NODES:
        return "FAILURE", "", f"Autoscale Maximum must be between 0 and {MAX_NODES}."
    if max_count and max_count < count:
        return ("FAILURE", "", f"Autoscale Maximum ({max_count}) is below Nodes per Zone ({count}). Leave it "
                "empty for a fixed size.")

    try:
        cluster = cluster_for_resource(resource)
        cluster_json = cluster.get()
        if cluster.get_pool(name, allow_404=True) is not None:
            return ("FAILURE", "", f"Cluster '{cluster.name}' already has a node pool named '{name}'. "
                    "Choose another name.")
    except GKEError as exc:
        return "FAILURE", "", str(exc)

    # ---- Create ----------------------------------------------------------
    body = node_pool_body(name, machine_type, count, max_count, spot=spot)
    scale = f"autoscaling {count}-{max_count}" if max_count > count else f"{count} node(s)"
    set_progress(
        f"Adding node pool '{name}' to '{cluster.name}': {machine_type}, {scale} per zone"
        + (", Spot VMs" if spot else "")
        + f", Kubernetes {cluster_json.get('currentMasterVersion')}..."
    )
    try:
        pool = cluster.create_pool(body)
    except GKEError as exc:
        logger.exception("GKE node pool create failed")
        if "did not finish within" in str(exc):
            return ("WARNING", "", f"{exc} GKE carries on; run List GKE Node Pools once it finishes to "
                    "refresh CloudBolt.")
        return "FAILURE", "", f"GKE refused or failed to create node pool '{name}': {exc}"

    upsert_pool_sub_resource(resource, pool, cluster_json, cluster.pool_sizes([pool]).get(pool.get("name")))
    return "SUCCESS", f"Added node pool {pool_label(pool, cluster_json)} to '{cluster.name}'.", ""
