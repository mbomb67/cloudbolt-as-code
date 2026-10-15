"""
CloudBolt teardown plugin: AWS EKS Namespace.

Deletes the namespace recorded on the Resource from its EKS cluster and
removes the CloudBolt Environment that was pinned to it.

Deleting a namespace deletes EVERYTHING inside it -- pods, deployments,
services, PVCs. So this plugin:
  1. un-entitles groups and deletes the CloudBolt Environment first, so
     nothing can be ordered into the namespace while it goes away (and so
     a misnamed environment can never block the cluster API: CloudBolt
     mints the EKS token from the orchestrator's first environment name);
  2. logs an inventory of what the namespace contains before deleting;
  3. waits for the namespace to finish terminating; then
  4. drops the orchestrator's inventory record for the namespace.

Idempotency: returns WARNING (not FAILURE) when the namespace, metadata, or
orchestrator is already gone, so re-runs tear down cleanly.

Returns a 3-tuple: (status, output_msg, error_msg)
"""

from common.methods import set_progress
from shared_modules.eks_namespace import (
    NamespaceClient,
    NamespaceError,
    delete_environment,
    load_cluster,
    load_environment,
    remove_namespace_record,
)
from utilities.logger import ThreadLogger

logger = ThreadLogger(__name__)

# How long to wait for the namespace to finish terminating before giving up.
_TERMINATION_TIMEOUT_SECONDS = 300


def _remove_environment(env):
    """Un-entitle every group and delete the pinned environment, if any."""
    if env is not None:
        set_progress(delete_environment(env))
    else:
        set_progress("No CloudBolt environment recorded for this namespace; nothing to remove.")


def run(job, resource, **kwargs):
    """Delete the EKS namespace and its CloudBolt environment."""
    set_progress("Starting AWS EKS Namespace teardown...")
    logger.info("EKS namespace teardown plugin started for job %s", job.id)

    if resource is None:
        msg = "No resource associated with this job; assuming already deleted."
        logger.warning(msg)
        return "WARNING", msg, ""

    try:
        namespace, orchestrator, cluster_name = load_cluster(resource)
    except ValueError as exc:
        msg = f"Cannot locate the namespace ({exc}); assuming already deleted."
        logger.warning(msg)
        set_progress(msg)
        _remove_environment(load_environment(resource))
        return "WARNING", msg, ""

    # ---- CloudBolt environment first -------------------------------------
    _remove_environment(load_environment(resource))

    try:
        client = NamespaceClient.from_orchestrator(orchestrator)
    except Exception as exc:
        msg = f"Could not connect to the API of cluster '{cluster_name}': {exc}"
        logger.exception(msg)
        return "FAILURE", "", msg

    # ---- Already gone? --------------------------------------------------
    try:
        current = client.get_namespace(namespace)
    except NamespaceError as exc:
        return "FAILURE", "", f"Could not read namespace '{namespace}': {exc}"

    if current is None:
        msg = f"Namespace '{namespace}' not found on '{cluster_name}'; assuming already deleted."
        logger.warning(msg)
        set_progress(msg)
        if remove_namespace_record(orchestrator, namespace):
            set_progress(f"Removed '{namespace}' from orchestrator '{orchestrator.name}'.")
        return "WARNING", msg, ""

    # ---- Inventory, then delete -----------------------------------------
    counts = client.count_workloads(namespace)
    summary = ", ".join(
        f"{count if count is not None else '?'} {kind}" for kind, count in counts.items()
    )
    set_progress(f"Namespace '{namespace}' contains {summary}; all of it will be deleted.")

    phase = (current.get("status") or {}).get("phase")
    if phase != "Terminating":
        set_progress(f"Deleting namespace '{namespace}'...")
        try:
            client.delete_namespace(namespace)
        except NamespaceError as exc:
            logger.exception("Namespace deletion failed")
            return "FAILURE", "", f"Failed to delete namespace '{namespace}': {exc}"
    else:
        set_progress(f"Namespace '{namespace}' is already terminating; waiting for it to finish.")

    if client.wait_for_namespace_deleted(namespace, timeout=_TERMINATION_TIMEOUT_SECONDS):
        set_progress(f"Namespace '{namespace}' has been removed from the cluster.")
        status = "SUCCESS"
        msg = f"Namespace '{namespace}' deleted from '{cluster_name}'."
    else:
        status = "WARNING"
        msg = (
            f"Delete of namespace '{namespace}' was accepted but it is still terminating after "
            f"{_TERMINATION_TIMEOUT_SECONDS}s (finalizers may be pending). Check the cluster."
        )
        set_progress(msg)

    # ---- Orchestrator inventory ------------------------------------------
    if remove_namespace_record(orchestrator, namespace):
        set_progress(f"Removed '{namespace}' from orchestrator '{orchestrator.name}'.")

    logger.info(msg)
    return status, msg, ""
