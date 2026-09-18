"""
CloudBolt teardown plugin: OpenShift Project Landing Zone.

Deletes the OpenShift project (namespace) recorded on the Resource and removes
the CloudBolt Environment that was pinned to it.

Deleting a namespace deletes EVERYTHING inside it -- pods, VMs, PVCs. So this
plugin:
  1. refuses to run while CloudBolt still tracks active servers in the
     project's Environment (decommission them first, so their lifecycle and
     history are handled properly instead of vanishing under CloudBolt);
  2. logs an inventory of what the namespace contains before deleting;
  3. waits for the namespace to finish terminating; then
  4. un-entitles groups and deletes the CloudBolt Environment.

Idempotency: returns WARNING (not FAILURE) when the namespace, metadata, or
handler is already gone, so re-runs tear down cleanly.

Returns a 3-tuple: (status, output_msg, error_msg)
"""

from common.methods import set_progress
from shared_modules.openshift_landing_zone import (
    LandingZoneError,
    OpenShiftLandingZoneClient,
    entitled_groups,
    load_environment,
    load_handler,
    revoke_group,
)
from utilities.logger import ThreadLogger

logger = ThreadLogger(__name__)

# How long to wait for the namespace to finish terminating before giving up.
_TERMINATION_TIMEOUT_SECONDS = 300


def _active_servers(env):
    """CloudBolt servers still tracked in the environment (historical ones excluded)."""
    if env is None:
        return []
    return list(env.server_set.exclude(status="HISTORICAL"))


def _delete_environment(env):
    """Un-entitle every group and delete the environment. Returns a message."""
    for group in list(entitled_groups(env)):
        revoke_group(env, group)
    if hasattr(env, "can_be_deleted") and not env.can_be_deleted():
        return f"Environment '{env.name}' still has dependents and was left in place."
    name = env.name
    env.delete()
    return f"Deleted CloudBolt environment '{name}'."


def run(job, **kwargs):
    """Delete the OpenShift project and its CloudBolt environment."""
    set_progress("Starting OpenShift Project Landing Zone teardown...")
    logger.info("Landing zone teardown plugin started for job %s", job.id)

    resource = job.resource_set.first()
    if resource is None:
        msg = "No resource associated with this job; assuming already deleted."
        logger.warning(msg)
        return "WARNING", msg, ""

    try:
        namespace, rh = load_handler(resource)
    except ValueError as exc:
        msg = f"Cannot locate the project ({exc}); assuming already deleted."
        logger.warning(msg)
        set_progress(msg)
        return "WARNING", msg, ""

    env = load_environment(resource)

    # ---- Refuse while CloudBolt-managed servers are inside ---------------
    servers = _active_servers(env)
    if servers:
        names = ", ".join(sorted(s.hostname for s in servers)[:10])
        more = f" (+{len(servers) - 10} more)" if len(servers) > 10 else ""
        return (
            "FAILURE",
            "",
            f"Project '{namespace}' still contains {len(servers)} CloudBolt-managed server(s): "
            f"{names}{more}. Decommission them first, then retry the delete.",
        )

    try:
        client = OpenShiftLandingZoneClient.from_handler(rh)
    except Exception as exc:
        msg = f"Could not connect to the OpenShift API for handler '{rh.name}': {exc}"
        logger.exception(msg)
        return "FAILURE", "", msg

    # ---- Already gone? --------------------------------------------------
    try:
        current = client.get_namespace(namespace)
    except LandingZoneError as exc:
        return "FAILURE", "", f"Could not read project '{namespace}': {exc}"

    if current is None:
        msg = f"Project '{namespace}' not found on '{rh.name}'; assuming already deleted."
        logger.warning(msg)
        set_progress(msg)
        if env is not None:
            set_progress(_delete_environment(env))
        return "WARNING", msg, ""

    # ---- Inventory, then delete -----------------------------------------
    counts = client.count_workloads(namespace)
    summary = ", ".join(
        f"{count if count is not None else '?'} {kind}" for kind, count in counts.items()
    )
    set_progress(f"Project '{namespace}' contains {summary}; all of it will be deleted.")

    phase = (current.get("status") or {}).get("phase")
    if phase != "Terminating":
        set_progress(f"Deleting project '{namespace}'...")
        try:
            client.delete_namespace(namespace)
        except LandingZoneError as exc:
            logger.exception("Namespace deletion failed")
            return "FAILURE", "", f"Failed to delete project '{namespace}': {exc}"
    else:
        set_progress(f"Project '{namespace}' is already terminating; waiting for it to finish.")

    if client.wait_for_namespace_deleted(namespace, timeout=_TERMINATION_TIMEOUT_SECONDS):
        set_progress(f"Project '{namespace}' has been removed from the cluster.")
        status = "SUCCESS"
        msg = f"OpenShift project '{namespace}' deleted from '{rh.name}'."
    else:
        status = "WARNING"
        msg = (
            f"Delete of project '{namespace}' was accepted but it is still terminating after "
            f"{_TERMINATION_TIMEOUT_SECONDS}s (finalizers may be pending). Check the cluster."
        )
        set_progress(msg)

    # ---- CloudBolt side --------------------------------------------------
    if env is not None:
        set_progress(_delete_environment(env))
    else:
        set_progress("No CloudBolt environment recorded for this project; nothing to remove.")

    logger.info(msg)
    return status, msg, ""
