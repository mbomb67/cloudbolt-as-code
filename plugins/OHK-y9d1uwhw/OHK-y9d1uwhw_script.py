"""
CloudBolt teardown plugin: HCP Terraform No-Code Module.

Decommissions a deployment of the "HCP Terraform No-Code Module" blueprint
(BP-00meiwwz): clears orphaned non-final TFC runs, runs an auto-confirmed
destroy run against the deployment's dedicated workspace, then deletes the
workspace via the SAFE-delete endpoint only -- never force-delete. All TFC REST
access goes through the tfc_api shared module (shared_modules/SHM-jlguerjr).

Sibling of the VCS teardown (OHK-2b9qu490); the workspace is a normal TFC
workspace once created, so the destroy + safe-delete machinery is identical.
Two no-code-specific deltas:

  1. Attribution is RESOURCE-LEVEL, not run-message-based. The run HCP
     auto-queues on a no-code create carries a TFC-authored message
     ("Triggered via no-code provision"), which parse_job_id_from_run_message
     cannot attribute. So instead of parsing each run's message, this plugin
     fails fast if ANY live (RUNNING/PAUSED) CloudBolt job other than this
     teardown is attached to the resource -- that is the authoritative "a
     provision/day-2 is mid-flight, do not tear down under it" signal (plan
     R9). If no live job owns the resource, every non-final run is an orphan
     (e.g. a jobengine restart killed a paused approval) and is discarded so
     it cannot block the destroy run.

  2. Missing-workspace-ID fallback (plan R9): a crash between the no-code
     create and the resource save can leave a workspace with no stored
     tfc_workspace_id. Before returning the "nothing to clean" WARNING, this
     plugin looks the workspace up by its deterministic cb-nc-<global_id> name
     (the ownership signal -- the no-code create ignores tag-bindings, U1) and
     adopts it if present and not tagged for a different resource.

Idempotency / PROVFAILED tolerance (teardown convention -- WARNING, not
FAILURE, when already gone): no resource, no workspace (by ID or name), or an
already-deleted workspace (404) -> WARNING; a destroy that errors -> FAILURE
(resource retained, second delete retries); planned_and_finished (empty state)
-> success, proceed to safe-delete.

CancelJobException ownership: this plugin NEVER catches CancelJobException; the
shared engine discards the destroy run and re-raises on cancellation.

Returns a 3-tuple: (status, output_msg, error_msg)
  status: "SUCCESS" | "WARNING" | "FAILURE"
"""

import time

from common.methods import set_progress
from jobs.models import Job
from utilities.logger import ThreadLogger

from shared_modules.tfc_api import (
    APPLY_PHASE_TIMEOUT_SECONDS,
    RUN_CLASS_APPLIED,
    RUN_CLASS_CONFIRMABLE,
    RUN_CLASS_IN_FLIGHT,
    RUN_CLASS_POLICY_OVERRIDE,
    TFCConflictError,
    TFCError,
    TFCNotFoundError,
    TFCRunFailedError,
    build_run_message,
    classify_run,
    get_client,
    no_code_workspace_name_for_resource,
    run_with_plan_approval,
)

logger = ThreadLogger(__name__)

BLUEPRINT_NAME = "HCP Terraform No-Code Module"

# Job.status values meaning a CloudBolt job is still LIVE (owns a TFC run via a
# paused approval gate or an active poll loop). Any other status -- SUCCESS,
# FAILURE, WARNING, CANCELED, TO_CANCEL -- means no code is left to manage its
# run: safe to discard. Mirrors the VCS teardown by deliberate copy.
LIVE_JOB_STATUSES = ("RUNNING", "PAUSED")

SAFE_DELETE_MAX_ATTEMPTS = 6
SAFE_DELETE_RETRY_INTERVAL_SECONDS = 10
DISCARD_MAX_ATTEMPTS = 5

_DISCARD_COMMENT = (
    "Discarded by CloudBolt teardown job {} -- orphaned run (no live CloudBolt "
    "job owns this deployment)."
)


def _guard_live_resource_jobs(job, resource, workspace_name):
    """Fail fast if a live (RUNNING/PAUSED) CloudBolt job OTHER than this
    teardown is attached to the resource. This is the no-code attribution
    signal: the auto-queued run's TFC-authored message is not parseable, so a
    live provision/day-2 job -- not a run message -- is the authority on
    'something is mid-flight; do not tear down under it'.
    """
    other_live = (
        Job.objects.filter(resource_set=resource, status__in=LIVE_JOB_STATUSES)
        .exclude(id=job.id)
    )
    live = list(other_live[:5])
    if live:
        ids = ", ".join(str(j.id) for j in live)
        raise TFCError(
            "Teardown blocked: CloudBolt job(s) {} are still active on this "
            "deployment (workspace '{}') and may own an in-flight HCP Terraform "
            "run. Wait for them (or cancel them), then delete this resource "
            "again.".format(ids, workspace_name)
        )


def _wait_out_progress(run_id):
    def _callback(status, elapsed_seconds):
        set_progress(
            "Waiting out non-discardable TFC run {} (status '{}', {}s "
            "elapsed)...".format(run_id, status, int(elapsed_seconds))
        )
    return _callback


def _wait_out_and_discard(job, client, run_id, workspace_name):
    """Clear one orphaned run that refused an immediate discard: read -> branch
    loop; a terminal run needs nothing, a confirmable/policy-override run is
    discarded (409 -> re-read), an in-flight run is waited out (bounded) then
    re-branched. Identical to the VCS teardown."""
    for _attempt in range(DISCARD_MAX_ATTEMPTS):
        run = client.get_run(run_id)
        classification = classify_run(run)
        if classification not in (
            RUN_CLASS_IN_FLIGHT, RUN_CLASS_CONFIRMABLE, RUN_CLASS_POLICY_OVERRIDE
        ):
            set_progress(
                "TFC run {} reached a terminal state on its own; nothing to "
                "discard.".format(run_id)
            )
            return
        if classification == RUN_CLASS_IN_FLIGHT:
            status = (run.get("attributes", {}) or {}).get("status", "unknown")
            set_progress(
                "TFC run {} is '{}' and cannot be discarded; waiting it out "
                "(bounded)...".format(run_id, status)
            )
            client.poll_run(
                run_id,
                timeout_seconds=APPLY_PHASE_TIMEOUT_SECONDS,
                progress_callback=_wait_out_progress(run_id),
            )
            continue
        try:
            client.discard_run(run_id, comment=_DISCARD_COMMENT.format(job.id))
        except TFCConflictError:
            logger.info(
                "Discard of TFC run %s returned 409; re-reading run state.", run_id
            )
            continue
        set_progress("Discarded orphaned TFC run {}.".format(run_id))
        return
    raise TFCError(
        "Could not clear TFC run {} on workspace '{}' after {} attempts; it "
        "kept changing state. Inspect the run in TFC, then delete this "
        "resource again.".format(run_id, workspace_name, DISCARD_MAX_ATTEMPTS)
    )


def _clear_non_final_runs(job, client, resource, workspace_id, workspace_name):
    """Resource-level fail-fast (live jobs), then discard every orphaned
    non-final run so it cannot block the destroy run."""
    _guard_live_resource_jobs(job, resource, workspace_name)

    runs = client.list_non_final_runs(workspace_id)
    if not runs:
        return
    set_progress(
        "Clearing {} orphaned non-final TFC run(s) on workspace '{}' before the "
        "destroy run.".format(len(runs), workspace_name)
    )
    deferred_run_ids = []
    for run_info in runs:
        run_id = run_info["id"]
        try:
            client.discard_run(run_id, comment=_DISCARD_COMMENT.format(job.id))
        except TFCConflictError:
            deferred_run_ids.append(run_id)
        else:
            set_progress("Discarded orphaned TFC run {}.".format(run_id))
    for run_id in deferred_run_ids:
        _wait_out_and_discard(job, client, run_id, workspace_name)


def _safe_delete_with_retries(client, workspace_id, workspace_name, run_url):
    """Safe-delete the workspace, retrying briefly on the classified 409.
    SAFE-delete only -- never force-delete. Identical to the VCS teardown."""
    last_error = None
    for attempt in range(1, SAFE_DELETE_MAX_ATTEMPTS + 1):
        try:
            client.safe_delete_workspace(workspace_id)
        except TFCNotFoundError:
            set_progress(
                "TFC workspace '{}' ({}) is already gone.".format(
                    workspace_name, workspace_id
                )
            )
            return
        except TFCConflictError as exc:
            last_error = exc
            if attempt < SAFE_DELETE_MAX_ATTEMPTS:
                set_progress(
                    "TFC declined to safe-delete workspace '{}' (attempt {}/{}); "
                    "retrying in {}s...".format(
                        workspace_name, attempt, SAFE_DELETE_MAX_ATTEMPTS,
                        SAFE_DELETE_RETRY_INTERVAL_SECONDS,
                    )
                )
                time.sleep(SAFE_DELETE_RETRY_INTERVAL_SECONDS)
        else:
            set_progress(
                "TFC workspace '{}' ({}) safe-deleted.".format(
                    workspace_name, workspace_id
                )
            )
            return
    raise TFCError(
        "TFC still refuses to safe-delete workspace '{}' ({}) after {} attempts. "
        "Do NOT force-delete it -- that orphans whatever the workspace still "
        "manages. Check the workspace in TFC (destroy run: {}); resolve, then "
        "delete this resource again. Last TFC response: {}".format(
            workspace_name, workspace_id, SAFE_DELETE_MAX_ATTEMPTS, run_url, last_error
        )
    )


def _resolve_workspace(client, resource, workspace_id):
    """Return (workspace_id, workspace) for the deployment, or (None, None) when
    nothing TFC-side exists. A stored ID is used first; a blank ID falls back to
    the deterministic cb-nc-<global_id> name (ownership signal), adopting it
    unless it is tagged for a different resource -- closing the crash-before-save
    leak (R9)."""
    if workspace_id:
        try:
            return workspace_id, client.get_workspace(workspace_id)
        except TFCNotFoundError:
            return None, None
    name = no_code_workspace_name_for_resource(resource.global_id)
    try:
        workspace = client.get_workspace_by_name(name)
    except TFCNotFoundError:
        return None, None
    if client.workspace_resource_tag_conflicts(workspace["id"], resource.global_id):
        logger.warning(
            "Workspace '%s' exists but is tagged for a different resource; not "
            "adopting it for teardown.", name,
        )
        return None, None
    set_progress(
        "No stored workspace ID; adopted workspace '{}' by name for teardown.".format(name)
    )
    return workspace["id"], workspace


def run(job, **kwargs):
    """Destroy the deployment's TFC-managed infrastructure, then its workspace."""
    set_progress("Starting HCP Terraform No-Code Module teardown...")
    logger.info("HCP Terraform No-Code teardown plugin started for job %s", job.id)

    resource = job.resource_set.first()
    if resource is None:
        msg = "No resource is associated with this job; nothing to clean up."
        logger.warning(msg)
        return "WARNING", msg, ""

    stored_workspace_id = (
        resource.get_value_for_custom_field("tfc_workspace_id") or ""
    ).strip()
    tfc_organization = (
        resource.get_value_for_custom_field("tfc_organization") or ""
    ).strip()
    tfc_connection_info = (
        resource.get_value_for_custom_field("tfc_connection_info") or ""
    ).strip()

    try:
        client = get_client(tfc_organization, tfc_connection_info)

        workspace_id, workspace = _resolve_workspace(
            client, resource, stored_workspace_id
        )
        if workspace_id is None:
            msg = (
                "Resource '{}' has no HCP Terraform workspace to clean (no stored "
                "ID and none found by name); nothing to do. (Expected for "
                "resources that failed before workspace creation.)".format(resource.name)
            )
            logger.warning(msg)
            set_progress(msg)
            return "WARNING", msg, ""
        workspace_name = (workspace.get("attributes", {}) or {}).get("name", workspace_id)

        # ---- Clear orphaned non-final runs (resource-level attribution) ---
        _clear_non_final_runs(job, client, resource, workspace_id, workspace_name)

        # ---- Destroy run, auto-confirmed (no pause -- deletion is confirmed) --
        message = build_run_message(job.id, resource.global_id, BLUEPRINT_NAME)
        set_progress(
            "Creating TFC destroy run on workspace '{}'...".format(workspace_name)
        )
        try:
            result = run_with_plan_approval(
                job, client, workspace_id, message,
                auto_confirm=True, is_destroy=True,
            )
        except TFCRunFailedError as exc:
            if exc.run_url:
                resource.set_value_for_custom_field("tfc_run_url", exc.run_url)
                resource.save()
            raise

        resource.set_value_for_custom_field("tfc_run_url", result["run_url"])
        resource.save()

        if result["status"] == RUN_CLASS_APPLIED:
            destroy_summary = "applied (managed infrastructure destroyed)"
        else:
            destroy_summary = "found no changes (empty state -- nothing was ever applied)"
        set_progress(
            "TFC destroy run {} {}; deleting workspace '{}'...".format(
                result["run_id"], destroy_summary, workspace_name
            )
        )

        _safe_delete_with_retries(
            client, workspace_id, workspace_name, result["run_url"]
        )

        msg = (
            "TFC destroy run {} {} and workspace '{}' ({}) was safe-deleted. Run "
            "URL: {}".format(
                result["run_id"], destroy_summary, workspace_name, workspace_id,
                result["run_url"],
            )
        )
        logger.info(msg)
        return "SUCCESS", msg, ""

    except TFCError as exc:
        logger.exception("HCP Terraform No-Code Module teardown failed")
        return "FAILURE", "", str(exc)
