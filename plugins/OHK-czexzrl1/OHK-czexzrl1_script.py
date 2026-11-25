"""
Day-2 action for the Azure Kubernetes Service blueprint: delete one node pool.

The dialog lists the cluster's node pools as AKS reports them. A cluster
always needs at least one System node pool, so the only System pool is left
out of the list, and the job checks the rule again against the live pools
before it deletes anything. The job waits for AKS to finish and removes the
pool's AKS Node Pool sub-resource.
  https://learn.microsoft.com/en-us/rest/api/aks/agent-pools/delete
  https://learn.microsoft.com/en-us/azure/aks/use-system-pools
"""

from common.methods import set_progress
from shared_modules.azure_aks import (
    AKSError,
    NODE_POOL_NAME_RE,
    cluster_for_resource,
    pool_label,
    remove_pool_sub_resource,
)
from utilities.logger import ThreadLogger

logger = ThreadLogger(__name__)


def _text(value):
    """A rendered input as plain text; an input left empty renders as 'None'."""
    text = str(value or "").strip()
    return "" if text == "None" else text


def _cluster_resource(kwargs):
    """The cluster resource an option generator runs for, or None outside a resource."""
    resource = kwargs.get("resource")
    return resource if hasattr(resource, "get_cf_values_as_dict") else None


def _is_system(pool):
    return str((pool.get("properties") or {}).get("mode") or "").lower() == "system"


def deletable_pools(pools):
    """Every User pool, and System pools while another System pool remains.

    AKS deletes a System pool only when another System pool can take its place.
    https://learn.microsoft.com/en-us/azure/aks/use-system-pools#system-and-user-node-pools
    """
    system_pools = [pool for pool in pools if _is_system(pool)]
    return [pool for pool in pools if not _is_system(pool) or len(system_pools) > 1]


def generate_options_for_aks_delete_pool(field=None, **kwargs):
    """The cluster's node pools AKS would let this action delete, read live from AKS."""
    resource = _cluster_resource(kwargs)
    if resource is None:
        return []
    try:
        pools = cluster_for_resource(resource).list_pools()
    except AKSError as exc:
        logger.warning("Could not list node pools of %s: %s", resource, exc)
        return [("", f"------ Could not list node pools: {str(exc)[:120]} ------")]
    options = [(pool.get("name"), pool_label(pool)) for pool in deletable_pools(pools)]
    if not options:
        return [("", "------ No node pool can be deleted: AKS keeps the last System pool ------")]
    return options


def run(job, resource, **kwargs):
    name = _text("{{ aks_delete_pool }}")
    if not NODE_POOL_NAME_RE.match(name):
        return "FAILURE", "", "Choose a node pool to delete."

    try:
        cluster = cluster_for_resource(resource)
        pools = cluster.list_pools()
    except AKSError as exc:
        return "FAILURE", "", str(exc)

    pool = next((p for p in pools if p.get("name") == name), None)
    if pool is None:
        removed = remove_pool_sub_resource(resource, name)
        return (
            "SUCCESS",
            f"Node pool '{name}' was already gone from '{cluster.name}'"
            + ("; removed its sub-resource." if removed else "."),
            "",
        )
    if name not in {p.get("name") for p in deletable_pools(pools)}:
        return (
            "FAILURE",
            "",
            f"'{name}' is the only System node pool of '{cluster.name}', and AKS requires at least one. "
            "Add another System pool with Add AKS Node Pool before deleting this one.",
        )

    set_progress(f"Deleting node pool {pool_label(pool)} from '{cluster.name}'...")
    try:
        existed = cluster.delete_pool(name)
    except AKSError as exc:
        logger.exception("AKS node pool delete failed")
        return "FAILURE", "", f"AKS could not delete node pool '{name}': {exc}"

    removed = remove_pool_sub_resource(resource, name)
    if removed:
        set_progress(f"Removed the AKS Node Pool sub-resource '{name}'.")
    state = "deleted from" if existed else "was already gone from"
    return "SUCCESS", f"Node pool '{name}' {state} '{cluster.name}'.", ""
