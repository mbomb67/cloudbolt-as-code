"""
CloudBolt teardown plugin: AKS Namespace.

Deletes the AKS managed namespace recorded on the Resource and removes the
CloudBolt Environment that was pinned to it.

Deleting the managed namespace deletes EVERYTHING inside it -- pods,
deployments, services, PVCs -- because CloudBolt creates it with the delete
policy Delete, under which AKS removes the Kubernetes namespace with it.
  https://learn.microsoft.com/en-us/rest/api/aks/managed-namespaces/delete
  https://learn.microsoft.com/en-us/azure/aks/concepts-managed-namespaces
So this plugin:
  1. deletes every pipeline identity CloudBolt created for the namespace
     (its role assignments, then the managed identity and its federated
     credentials), even when the cluster is already gone;
  2. deletes every role assignment CloudBolt made on the managed namespace,
     so nothing is left pointing at it; assignments made outside CloudBolt
     are reported and left alone;
  3. logs an inventory of what the namespace contains before deleting;
  4. deletes the managed namespace, waits for Azure to finish, and waits for
     the Kubernetes namespace to finish terminating; then
  5. un-entitles every group, deletes the CloudBolt Environment, and drops
     the orchestrator's inventory record for the namespace.

A managed namespace whose delete policy is Keep (one adopted by discovery
that was created that way outside CloudBolt) loses only its Azure resource;
AKS keeps the Kubernetes namespace, and the job output says so.

Idempotency: returns WARNING (not FAILURE) when the namespace, its metadata,
or its cluster is already gone, so re-runs tear down cleanly.

Returns a 3-tuple: (status, output_msg, error_msg)
"""

from common.methods import set_progress
from shared_modules.aks_namespace import (
    CF_PREFIX,
    NamespaceClient,
    delete_environment,
    delete_managed_namespace,
    ensure_custom_fields,
    get_managed_namespace,
    load_cluster_resource,
    load_environment,
    load_orchestrator,
    load_pipeline_identities,
    remove_all_namespace_access,
    remove_namespace_record,
    remove_pipeline_identity,
)
from shared_modules.azure_aks import AKSError, cluster_for_resource
from utilities.logger import ThreadLogger

logger = ThreadLogger(__name__)

# How long to wait for the Kubernetes namespace to finish terminating after
# Azure reports the managed namespace deleted.
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

    The identities live in the cluster's resource group, so they are deleted
    even when the cluster no longer exists. Returns a warning message when an
    identity could not be deleted, else "".
    """
    warnings = []
    for identity in load_pipeline_identities(resource):
        try:
            for message in remove_pipeline_identity(resource, identity):
                set_progress(message)
        except Exception as exc:
            logger.exception("Pipeline identity cleanup failed")
            warnings.append(f"Managed identity {identity.get('identity_name')} was not deleted: {exc}")
            set_progress(warnings[-1])
    return " ".join(warnings)


def _remove_access(cluster, namespace):
    """Delete CloudBolt's role assignments on the managed namespace. Never fails the teardown.

    Returns a warning message when the access could not be cleaned up, else "".
    """
    try:
        messages = remove_all_namespace_access(cluster, namespace)
    except Exception as exc:
        logger.exception("Role assignment cleanup failed")
        msg = f"Microsoft Entra access was not cleaned up: {exc}"
        set_progress(msg)
        return msg
    for message in messages:
        set_progress(message)
    if not messages:
        set_progress("No CloudBolt role assignment was left on this namespace.")
    return ""


def _log_inventory(orchestrator, namespace):
    """Log what the namespace holds before it goes. Best effort."""
    if orchestrator is None:
        return None
    try:
        client = NamespaceClient.from_orchestrator(orchestrator)
        counts = client.count_workloads(namespace)
    except Exception as exc:
        logger.warning("Could not take an inventory of %s: %s", namespace, exc)
        return None
    summary = ", ".join(f"{count if count is not None else '?'} {kind}" for kind, count in counts.items())
    set_progress(f"Namespace '{namespace}' contains {summary}; all of it will be deleted.")
    return client


def _kubernetes_namespace_gone(client, namespace):
    """Wait for the Kubernetes namespace to finish terminating; True when gone or not checkable."""
    if client is None:
        return True
    try:
        return client.wait_for_namespace_deleted(namespace, timeout=_TERMINATION_TIMEOUT_SECONDS)
    except Exception as exc:
        logger.warning("Could not check whether %s finished terminating: %s", namespace, exc)
        return True


def _gone(env, orchestrator, namespace, msg, warnings):
    """Finish a teardown whose namespace is already gone: WARNING, environment and record removed."""
    logger.warning(msg)
    set_progress(msg)
    _remove_environment(env)
    _remove_record(orchestrator, namespace)
    return "WARNING", " ".join(w for w in [msg] + warnings if w), ""


def run(job, resource=None, **kwargs):
    """Delete the AKS managed namespace and its CloudBolt environment."""
    set_progress("Starting AKS Namespace teardown...")
    logger.info("AKS namespace teardown plugin started for job %s", getattr(job, "id", None))

    if resource is None:
        msg = "No resource associated with this job; assuming already deleted."
        logger.warning(msg)
        return "WARNING", msg, ""

    ensure_custom_fields()
    namespace = resource.get_value_for_custom_field(CF_PREFIX + "name")
    env = load_environment(resource)
    orchestrator = load_orchestrator(resource.get_value_for_custom_field(CF_PREFIX + "orchestrator_id"))

    if not namespace:
        return _gone(env, orchestrator, namespace,
                     f"The resource has no '{CF_PREFIX}name' value; assuming the namespace was never created.", [])

    # ---- Locate the cluster ------------------------------------------------
    cluster_resource = load_cluster_resource(resource)
    if cluster_resource is None:
        return _gone(env, orchestrator, namespace,
                     f"The namespace records no cluster resource; assuming '{namespace}' is already deleted.", [])
    try:
        cluster = cluster_for_resource(cluster_resource)
    except AKSError as exc:
        return _gone(env, orchestrator, namespace,
                     f"Cannot reach the AKS cluster of '{namespace}' ({exc}); assuming it is already deleted.", [])

    # ---- Pipeline identities (they outlive the cluster) --------------------
    warnings = [_remove_pipeline_identities(resource)]

    try:
        cluster_json = cluster.get(allow_404=True)
    except AKSError as exc:
        return "FAILURE", "", f"Could not read AKS cluster '{cluster.name}': {exc}"
    if cluster_json is None:
        return _gone(env, orchestrator, namespace,
                     f"AKS cluster '{cluster.name}' no longer exists, so namespace '{namespace}' went with it.",
                     warnings)

    try:
        managed = get_managed_namespace(cluster, namespace)
    except AKSError as exc:
        return "FAILURE", "", f"Could not read managed namespace '{namespace}': {exc}"
    if managed is None:
        return _gone(env, orchestrator, namespace,
                     f"Managed namespace '{namespace}' not found on '{cluster.name}'; assuming already deleted.",
                     warnings)

    # ---- Role assignments ----------------------------------------------------
    warnings.append(_remove_access(cluster, namespace))

    # ---- Inventory, then delete -------------------------------------------
    keep = str((managed.get("properties") or {}).get("deletePolicy") or "").lower() == "keep"
    client = None if keep else _log_inventory(orchestrator, namespace)
    set_progress(f"Deleting managed namespace '{namespace}' from '{cluster.name}'...")
    try:
        delete_managed_namespace(cluster, namespace)
    except AKSError as exc:
        logger.exception("Managed namespace deletion failed")
        return "FAILURE", " ".join(w for w in warnings if w), f"Failed to delete managed namespace '{namespace}': {exc}"

    status = "SUCCESS"
    if keep:
        status = "WARNING"
        msg = (f"Managed namespace '{namespace}' deleted from '{cluster.name}'. Its delete policy is Keep, so AKS "
               "kept the Kubernetes namespace and everything in it; delete it on the cluster if it is not needed.")
    elif _kubernetes_namespace_gone(client, namespace):
        msg = f"Namespace '{namespace}' deleted from '{cluster.name}'."
    else:
        status = "WARNING"
        msg = (f"Managed namespace '{namespace}' was deleted but its Kubernetes namespace is still terminating "
               f"after {_TERMINATION_TIMEOUT_SECONDS}s (finalizers may be pending). Check the cluster.")
    set_progress(msg)

    # ---- CloudBolt environment and orchestrator inventory ------------------
    _remove_environment(env)
    _remove_record(orchestrator, namespace)

    warning_text = " ".join(w for w in warnings if w)
    if warning_text:
        status = "WARNING"
        msg = f"{msg} {warning_text}"
    logger.info(msg)
    return status, msg, ""
