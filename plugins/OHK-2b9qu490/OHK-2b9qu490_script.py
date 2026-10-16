"""
CloudBolt teardown plugin: HCP Terraform VM.

Decommissions a deployment of the "HCP Terraform VM" blueprint (BP-b0qm83lh):
runs a TFC destroy run against the deployment's dedicated workspace
(auto-confirmed -- deletion is already an explicit, confirmed user action in
CloudBolt, so there is no plan-approval pause), waits for completion, then
deletes the workspace via the SAFE-delete endpoint only -- never
force-delete. All TFC REST access goes through the tfc_api shared module
(shared_modules/SHM-jlguerjr); no vendor API call is made directly here.

Idempotency / PROVFAILED tolerance (R9; teardown convention per
docs/agents/plugin-templates.md -- WARNING, not FAILURE, when already gone):
  - No resource, or missing/empty tfc_workspace_id custom field -> WARNING
    ("nothing to clean"): resources that PROVFAILED before workspace
    creation land here.
  - Workspace already deleted in TFC (404) -> WARNING; CloudBolt still
    removes the resource. A second delete after a successful teardown lands
    here too.
  - Destroy run errored -> FAILURE with the run URL; the resource is
    retained, so a second delete retries the destroy.
  - Destroy run "planned_and_finished" (empty state -- e.g. a PROVFAILED
    resource whose workspace never applied anything) is success: proceed
    straight to safe-delete.

Attribution-aware discard of non-final runs (the jobengine-restart recovery
path): a jobengine restart kills a paused approval frame with NO cleanup
code running, so the orphaned unconfirmed run would block every later run
on the workspace -- including this destroy. Before creating the destroy
run, this plugin lists the workspace's non-final runs and, for each, parses
the owning CloudBolt job ID out of the run message (build_run_message
format):
  - Owning job no longer RUNNING/PAUSED -- or no parseable job ID, or job
    not found -> the run is orphaned: discard it.
  - Owning job IS RUNNING or PAUSED -> fail fast, naming that job: cancel
    it (or let it finish) before deleting this resource. This is
    deliberately asymmetric with the day-2 plugins (which always fail fast
    on a non-final run): deletion is the designated recovery path for
    orphaned runs, so only teardown discards them.
  - A run mid-apply (e.g. 'applying') is not discardable; wait it out with
    the client's bounded poll rather than treating its discard 409 as
    failure.

CancelJobException ownership: this plugin NEVER catches CancelJobException
(a BaseException subclass -- the TFCError handlers below cannot see it).
The shared engine owns the reject path: on cancellation it discards the
destroy run and re-raises.

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
    parse_job_id_from_run_message,
    run_with_plan_approval,
)

logger = ThreadLogger(__name__)

# Blueprint name for TFC run-message attribution. tfc_api is blueprint-agnostic
# (run messages take the name as an argument), so the name lives with the
# plugin -- mirrored across this blueprint's four plugins by deliberate copy.
BLUEPRINT_NAME = "HCP Terraform VM"

# Job.status values meaning the run's owning CloudBolt job is still LIVE: it
# owns its TFC run (a paused approval gate or an active poll loop) and will
# apply or discard it itself, so teardown must not pull the run out from
# under it. Every other status -- SUCCESS, FAILURE, WARNING, CANCELED, and
# TO_CANCEL (its engine-side cancel path discards best-effort and tolerates
# us racing it) -- means no code is left to clean the run up: orphaned, safe
# to discard. Status strings per typings/jobs/models.pyi (jobs.models.Job;
# see get_jobs_status_counts and Job.pause docstrings).
LIVE_JOB_STATUSES = ("RUNNING", "PAUSED")

# Bounded budgets -- no loop in this plugin may run unbounded (tfc_api house
# rule). The workspace can briefly report busy (409) on safe-delete right
# after the destroy while TFC settles its state and releases the run lock.
SAFE_DELETE_MAX_ATTEMPTS = 6
SAFE_DELETE_RETRY_INTERVAL_SECONDS = 10
# Per-run cap on discard -> 409 -> re-read/wait-out cycles.
DISCARD_MAX_ATTEMPTS = 5

_DISCARD_COMMENT = (
    "Discarded by CloudBolt teardown job {} -- orphaned run (owning "
    "CloudBolt job is no longer active)."
)


def _find_owning_job(current_job, run_message):
    """Resolve the CloudBolt Job that created a TFC run, from its message.

    Returns None when the run is unattributed (no parseable job ID: manual
    TFC runs or other tooling) or -- defensively -- when the message names
    the current teardown job itself (failing fast on our own RUNNING job
    would deadlock the delete; treat as orphaned instead). Job lookup is by
    primary key: jobs.models.Job (typings/jobs/models.pyi).
    """
    owning_job_id = parse_job_id_from_run_message(run_message)
    if owning_job_id is None or owning_job_id == current_job.id:
        return None
    return Job.objects.filter(id=owning_job_id).first()


def _wait_out_progress(run_id):
    """progress_callback for waiting out a non-discardable run."""
    def _callback(status, elapsed_seconds):
        set_progress(
            "Waiting out non-discardable TFC run {} (status '{}', {}s "
            "elapsed)...".format(run_id, status, int(elapsed_seconds))
        )
    return _callback


def _wait_out_and_discard(job, client, run_id, workspace_name):
    """Clear one orphaned run that refused an immediate discard.

    Bounded read -> branch loop: a terminal run needs nothing; a run awaiting
    confirmation (or a policy override) is discarded, with a 409 triggering a
    re-read rather than failure; an in-flight run (e.g. 'applying' -- not
    discardable) is waited out with the client's bounded poll, then
    re-branched on whatever state it lands in.
    """
    for _attempt in range(DISCARD_MAX_ATTEMPTS):
        run = client.get_run(run_id)
        classification = classify_run(run)
        if classification not in (
            RUN_CLASS_IN_FLIGHT, RUN_CLASS_CONFIRMABLE, RUN_CLASS_POLICY_OVERRIDE
        ):
            # Terminal (applied / planned_and_finished / discarded /
            # canceled / errored): it no longer blocks the workspace queue.
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
            # Bounded: raises TFCTimeoutError at the deadline (-> FAILURE).
            client.poll_run(
                run_id,
                timeout_seconds=APPLY_PHASE_TIMEOUT_SECONDS,
                progress_callback=_wait_out_progress(run_id),
            )
            continue  # re-read and re-branch on the post-wait state
        # Awaiting confirmation (or a policy override): discardable.
        try:
            client.discard_run(run_id, comment=_DISCARD_COMMENT.format(job.id))
        except TFCConflictError:
            # The run moved between our read and the discard; re-branch.
            logger.info(
                "Discard of TFC run %s returned 409; re-reading run state.",
                run_id,
            )
            continue
        set_progress("Discarded orphaned TFC run {}.".format(run_id))
        return
    raise TFCError(
        "Could not clear TFC run {} on workspace '{}' after {} attempts; it "
        "kept changing state. Inspect the run in TFC, then delete this "
        "resource again.".format(run_id, workspace_name, DISCARD_MAX_ATTEMPTS)
    )


def _clear_non_final_runs(job, client, workspace_id, workspace_name):
    """Attribution-aware clearing of the workspace's non-final runs.

    Fail-fast pre-pass first (no discards happen if any run belongs to a
    live job), then discard the orphans -- immediately where TFC allows it,
    deferring 409s (e.g. a run mid-apply) to the bounded wait-out path.
    """
    runs = client.list_non_final_runs(workspace_id)
    if not runs:
        return

    # ---- Pre-pass: fail fast on live owners BEFORE mutating anything -----
    for run_info in runs:
        owning_job = _find_owning_job(job, run_info["message"])
        if owning_job is not None and owning_job.status in LIVE_JOB_STATUSES:
            run_url = client.run_app_url(run_info["id"], workspace_name)
            raise TFCError(
                "Teardown blocked: TFC run {} on workspace '{}' belongs to "
                "CloudBolt job {} which is still {}. Cancel job {} (or let "
                "it finish), then delete this resource again. Run URL: "
                "{}".format(
                    run_info["id"], workspace_name, owning_job.id,
                    owning_job.status, owning_job.id, run_url,
                )
            )

    set_progress(
        "Clearing {} orphaned non-final TFC run(s) on workspace '{}' before "
        "the destroy run.".format(len(runs), workspace_name)
    )

    # ---- Phase 1: discard everything TFC lets us discard right now -------
    deferred_run_ids = []
    for run_info in runs:
        run_id = run_info["id"]
        try:
            client.discard_run(run_id, comment=_DISCARD_COMMENT.format(job.id))
        except TFCConflictError:
            # Not discardable right now (mid-apply, still planning, or
            # already terminal); resolved by the bounded wait-out below.
            deferred_run_ids.append(run_id)
        else:
            set_progress("Discarded orphaned TFC run {}.".format(run_id))

    # ---- Phase 2: wait out the stragglers (bounded), then discard --------
    for run_id in deferred_run_ids:
        _wait_out_and_discard(job, client, run_id, workspace_name)


def _safe_delete_with_retries(client, workspace_id, workspace_name, run_url):
    """Safe-delete the workspace, retrying briefly on the classified 409.

    SAFE-delete only -- force-delete is never used: it would orphan any
    infrastructure still recorded in the workspace's state. The client
    pre-classifies the 409 into locked vs still-managing-resources guidance
    (TFCConflictError); both can be transient immediately after the destroy
    run, so retry a few times before surfacing the failure.
    """
    last_error = None
    for attempt in range(1, SAFE_DELETE_MAX_ATTEMPTS + 1):
        try:
            client.safe_delete_workspace(workspace_id)
        except TFCNotFoundError:
            # Deleted out-of-band between the destroy and this call: the
            # desired end state, not an error.
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
                    "TFC declined to safe-delete workspace '{}' (attempt "
                    "{}/{}); retrying in {}s...".format(
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
        "TFC still refuses to safe-delete workspace '{}' ({}) after {} "
        "attempts. Do NOT force-delete it -- that orphans whatever the "
        "workspace still manages. Check the workspace in TFC: if it still "
        "lists managed resources, the destroy did not remove everything "
        "(destroy run: {}); if it is locked, discard or unlock the holding "
        "run. Then delete this resource again. Last TFC response: {}".format(
            workspace_name, workspace_id, SAFE_DELETE_MAX_ATTEMPTS,
            run_url, last_error,
        )
    )


def run(job, **kwargs):
    """Destroy the deployment's TFC-managed infrastructure, then its workspace."""
    set_progress("Starting HCP Terraform VM teardown...")
    logger.info("HCP Terraform VM teardown plugin started for job %s", job.id)

    resource = job.resource_set.first()
    if resource is None:
        msg = "No resource is associated with this job; nothing to clean up."
        logger.warning(msg)
        return "WARNING", msg, ""

    workspace_id = (
        resource.get_value_for_custom_field("tfc_workspace_id") or ""
    ).strip()
    if not workspace_id:
        # R9: resources that PROVFAILED before workspace creation (or were
        # never provisioned through TFC) have nothing TFC-side to clean.
        msg = (
            "Resource '{}' has no TFC workspace ID; nothing to clean. "
            "(Expected for resources that failed before workspace "
            "creation.)".format(resource.name)
        )
        logger.warning(msg)
        set_progress(msg)
        return "WARNING", msg, ""

    # TFC coordinates seeded on the resource by the build plugin: the
    # organization (run URLs / API paths) and the 'tf-cloud'-labeled
    # ConnectionInfo the deployment provisioned through (blank on legacy
    # resources -- get_client then resolves the single labeled connection).
    tfc_organization = (
        resource.get_value_for_custom_field("tfc_organization") or ""
    ).strip()
    tfc_connection_info = (
        resource.get_value_for_custom_field("tfc_connection_info") or ""
    ).strip()

    try:
        client = get_client(tfc_organization, tfc_connection_info)

        # ---- Already gone? (idempotent re-delete / manual TFC cleanup) ---
        try:
            workspace = client.get_workspace(workspace_id)
        except TFCNotFoundError:
            msg = (
                "TFC workspace {} is already deleted; nothing to clean. The "
                "resource will still be removed.".format(workspace_id)
            )
            logger.warning(msg)
            set_progress(msg)
            return "WARNING", msg, ""
        workspace_name = (workspace.get("attributes", {}) or {}).get(
            "name", workspace_id
        )

        # ---- Clear orphaned non-final runs (attribution-aware, R9) -------
        # A leftover unconfirmed run (jobengine restart killed its paused
        # job) would otherwise queue the destroy run forever.
        _clear_non_final_runs(job, client, workspace_id, workspace_name)

        # ---- Destroy run, auto-confirmed (R8) -----------------------------
        # No pause: deletion is already a confirmed user action, so the
        # engine confirms the destroy plan the moment it is confirmable. On
        # job cancellation the engine discards the run and re-raises
        # CancelJobException -- never caught here.
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
            # Destroy errored: keep the run URL on the (retained) resource so
            # a second delete retries the destroy with the evidence at hand.
            if exc.run_url:
                resource.set_value_for_custom_field("tfc_run_url", exc.run_url)
                resource.save()
            raise

        resource.set_value_for_custom_field("tfc_run_url", result["run_url"])
        resource.save()

        # Both terminal-success states proceed to safe-delete: "applied"
        # (infrastructure destroyed) and "planned_and_finished" (empty state
        # -- nothing was ever applied, e.g. a PROVFAILED resource).
        if result["status"] == RUN_CLASS_APPLIED:
            destroy_summary = "applied (managed infrastructure destroyed)"
        else:
            destroy_summary = (
                "found no changes (empty state -- nothing was ever applied)"
            )
        set_progress(
            "TFC destroy run {} {}; deleting workspace '{}'...".format(
                result["run_id"], destroy_summary, workspace_name
            )
        )

        # ---- Safe-delete the workspace (never force-delete; R8) ----------
        _safe_delete_with_retries(
            client, workspace_id, workspace_name, result["run_url"]
        )

        msg = (
            "TFC destroy run {} {} and workspace '{}' ({}) was safe-deleted. "
            "Run URL: {}".format(
                result["run_id"], destroy_summary, workspace_name,
                workspace_id, result["run_url"],
            )
        )
        logger.info(msg)
        return "SUCCESS", msg, ""

    except TFCError as exc:
        # Every shared-module failure (config, auth, conflict, timeout,
        # failed destroy run) converts to the exemplar failure-return
        # convention; run failures carry the run URL in their message and
        # the resource stays retained, so a second delete retries.
        # CancelJobException is NOT a TFCError and passes through.
        logger.exception("HCP Terraform VM teardown failed")
        return "FAILURE", "", str(exc)
