"""
CloudBolt teardown plugin: GKE Namespace.

Deletes the namespace recorded on the Resource from its GKE cluster and
removes the CloudBolt Environment that was pinned to it.

Deleting a namespace deletes EVERYTHING inside it -- pods, deployments,
services, PVCs -- and Kubernetes finishes it asynchronously.
  https://kubernetes.io/docs/concepts/overview/working-with-objects/namespaces/
So this plugin:
  1. deletes every pipeline identity CloudBolt created for the namespace
     (its Workload Identity Federation provider lives in the Google Cloud
     project, not the cluster), even when the cluster is already gone;
  2. deletes CloudBolt's RoleBindings in the namespace, so the job log
     records who lost access; RoleBindings made outside CloudBolt go with
     the namespace;
  3. logs an inventory of what the namespace contains before deleting;
  4. deletes the namespace and waits for it to finish terminating; then
  5. drops the orchestrator's inventory record for the namespace, un-entitles
     every group, and deletes the CloudBolt Environment.

Idempotency: returns WARNING (not FAILURE) when the namespace, its metadata,
or its cluster is already gone, so re-runs tear down cleanly.

Returns a 3-tuple: (status, output_msg, error_msg)
"""

from common.methods import set_progress
from shared_modules.gcp_gke import GKEError, cluster_for_resource
from shared_modules.gke_namespace import (
    CF_PREFIX,
    NamespaceClient,
    NamespaceError,
    delete_environment,
    ensure_custom_fields,
    load_cluster_resource,
    load_environment,
    load_orchestrator,
    remove_all_namespace_access,
    remove_all_pipeline_identities,
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


def _remove_record(orchestrator, namespace):
    if orchestrator is not None and namespace and remove_namespace_record(orchestrator, namespace):
        set_progress(f"Removed '{namespace}' from orchestrator '{orchestrator.name}'.")


def _remove_pipeline_identities(resource):
    """Delete every pipeline identity CloudBolt created for the namespace. Never fails the teardown.

    Returns a warning message when an identity could not be deleted, else "".
    """
    try:
        messages = remove_all_pipeline_identities(resource)
    except Exception as exc:
        logger.exception("Pipeline identity cleanup failed")
        msg = f"Pipeline identities were not deleted: {exc}"
        set_progress(msg)
        return msg
    for message in messages:
        set_progress(message)
    return ""


def _remove_access(client, namespace):
    """Delete CloudBolt's RoleBindings in the namespace. Never fails the teardown.

    Returns a warning message when the access could not be cleaned up, else "".
    """
    try:
        messages = remove_all_namespace_access(client, namespace)
    except Exception as exc:
        logger.exception("RoleBinding cleanup failed")
        msg = f"Google access was not cleaned up before the delete: {exc}"
        set_progress(msg)
        return msg
    for message in messages:
        set_progress(message)
    if not messages:
        set_progress("No CloudBolt RoleBinding was left in this namespace.")
    return ""


def _cluster_gone(cluster_resource):
    """Why the namespace's GKE cluster no longer exists, or "" when it exists or GKE cannot tell.

    clusters.get answers 404 for a deleted cluster:
    https://cloud.google.com/kubernetes-engine/docs/reference/rest/v1/projects.locations.clusters/get
    """
    if cluster_resource is None:
        return "The namespace records no cluster resource."
    values = cluster_resource.get_cf_values_as_dict()
    name = values.get("gke_cluster_name")
    if not (name and values.get("gke_project_id") and values.get("gke_location")):
        return f"Cluster resource '{cluster_resource.name}' records no GKE cluster."
    try:
        cluster_json = cluster_for_resource(cluster_resource).get(allow_404=True)
    except GKEError as exc:
        logger.warning("Could not tell whether GKE cluster %s exists: %s", name, exc)
        set_progress(f"Could not tell whether GKE cluster '{name}' still exists: {exc}")
        return ""
    if cluster_json is None:
        return f"GKE cluster '{name}' no longer exists."
    return ""


def _gone(env, orchestrator, namespace, msg, warnings):
    """Finish a teardown whose namespace is already gone: WARNING, record and environment removed."""
    logger.warning(msg)
    set_progress(msg)
    _remove_record(orchestrator, namespace)
    _remove_environment(env)
    return "WARNING", " ".join(w for w in [msg] + warnings if w), ""


def run(job, resource=None, **kwargs):
    """Delete the GKE namespace and its CloudBolt environment."""
    set_progress("Starting GKE Namespace teardown...")
    logger.info("GKE namespace teardown plugin started for job %s", getattr(job, "id", None))

    if resource is None:
        msg = "No resource associated with this job; assuming already deleted."
        logger.warning(msg)
        return "WARNING", msg, ""

    ensure_custom_fields()
    namespace = resource.get_value_for_custom_field(CF_PREFIX + "name")
    env = load_environment(resource)
    orchestrator = load_orchestrator(resource.get_value_for_custom_field(CF_PREFIX + "orchestrator_id"))

    # ---- Pipeline identities (they live in the project, not the cluster) ----
    warnings = [_remove_pipeline_identities(resource)]

    if not namespace:
        return _gone(env, orchestrator, namespace,
                     f"The resource has no '{CF_PREFIX}name' value; assuming the namespace was never created.",
                     warnings)

    # ---- Reach the namespace through the cluster's orchestrator ------------
    cluster_resource = load_cluster_resource(resource)
    cluster_name = resource.get_value_for_custom_field(CF_PREFIX + "cluster") or \
        (cluster_resource.name if cluster_resource is not None else "its cluster")
    client = current = None
    api_error = "the namespace records no container orchestrator"
    if orchestrator is not None:
        try:
            client = NamespaceClient.from_orchestrator(orchestrator)
            current = client.get_namespace(namespace)
            api_error = ""
        except NamespaceError as exc:
            client, api_error = None, str(exc)

    if client is None:
        # The cluster API is out of reach: the namespace is gone only when its cluster is.
        gone = _cluster_gone(cluster_resource)
        if gone:
            return _gone(env, orchestrator, namespace, f"{gone} Namespace '{namespace}' went with it.", warnings)
        return ("FAILURE", " ".join(w for w in warnings if w),
                f"Could not reach namespace '{namespace}' on '{cluster_name}' ({api_error}), and GKE does not "
                "report the cluster deleted. Run the Google Kubernetes Engine blueprint's discovery to refresh its "
                "container orchestrator, then try again.")
    if current is None:
        return _gone(env, orchestrator, namespace,
                     f"Namespace '{namespace}' not found on '{cluster_name}'; assuming already deleted.", warnings)

    # ---- RoleBindings ------------------------------------------------------
    warnings.append(_remove_access(client, namespace))

    # ---- Inventory, then delete -------------------------------------------
    counts = client.count_workloads(namespace)
    summary = ", ".join(f"{count if count is not None else '?'} {kind}" for kind, count in counts.items())
    set_progress(f"Namespace '{namespace}' contains {summary}; all of it will be deleted.")

    if (current.get("status") or {}).get("phase") != "Terminating":
        set_progress(f"Deleting namespace '{namespace}' from '{cluster_name}'...")
        try:
            client.delete_namespace(namespace)
        except NamespaceError as exc:
            logger.exception("Namespace deletion failed")
            return ("FAILURE", " ".join(w for w in warnings if w),
                    f"Failed to delete namespace '{namespace}': {exc}")
    else:
        set_progress(f"Namespace '{namespace}' is already terminating; waiting for it to finish.")

    try:
        deleted = client.wait_for_namespace_deleted(namespace, timeout=_TERMINATION_TIMEOUT_SECONDS)
    except NamespaceError as exc:
        logger.warning("Could not check whether %s finished terminating: %s", namespace, exc)
        deleted = True
    if deleted:
        status = "SUCCESS"
        msg = f"Namespace '{namespace}' deleted from '{cluster_name}'."
    else:
        status = "WARNING"
        msg = (f"Delete of namespace '{namespace}' was accepted but it is still terminating after "
               f"{_TERMINATION_TIMEOUT_SECONDS}s (finalizers may be pending). Check the cluster.")
    set_progress(msg)

    # ---- Orchestrator inventory and CloudBolt environment ----------------
    _remove_record(orchestrator, namespace)
    _remove_environment(env)

    warning_text = " ".join(w for w in warnings if w)
    if warning_text:
        status = "WARNING"
        msg = f"{msg} {warning_text}"
    logger.info(msg)
    return status, msg, ""
