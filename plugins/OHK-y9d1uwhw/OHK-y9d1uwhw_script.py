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
     fails fast if ANY live (RUNNING/PAUSED) CloudBolt job outside this
     teardown's own job tree (the Delete Resource parent and its children)
     is attached to the resource -- that is the authoritative "a
     provision/day-2 is mid-flight, do not tear down under it" signal (plan
     R9). If no live job owns the resource, every non-final run is an orphan
     (e.g. a jobengine restart killed a paused approval) and is discarded so
     it cannot block the destroy run.

  2. Missing-workspace-ID fallback (plan R9): a crash between the no-code
     create and the resource save can leave a workspace with no stored
     tfc_workspace_id. Before returning the "nothing to clean" WARNING, this
     plugin looks the workspace up by name -- the stored tfc_workspace_name,
     the resource name (the build sets it to the orderer's Workspace Name
     before the create), then the legacy deterministic cb-nc-<global_id>
     name -- and adopts a match only when it is tagged cmp:resource-id for
     this resource (the tag is applied best-effort after the create, which
     ignores tag-bindings -- U1), or, for the legacy name alone (it embeds
     this resource's global_id), when it is not tagged for a different one.

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

import re
import time

from common.methods import set_progress
from jobs.models import Job
from utilities.logger import ThreadLogger

from shared_modules.vm_adoption import retire_servers, retired_note
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
    WORKSPACE_NAME_MAX_LENGTH,
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

# TFC's workspace-name charset; a candidate name outside it (e.g. a resource
# renamed with spaces) cannot be a workspace and is not looked up.
WORKSPACE_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,%d}$" % WORKSPACE_NAME_MAX_LENGTH)

SAFE_DELETE_MAX_ATTEMPTS = 6
SAFE_DELETE_RETRY_INTERVAL_SECONDS = 10
DISCARD_MAX_ATTEMPTS = 5

_DISCARD_COMMENT = (
    "Discarded by CloudBolt teardown job {} -- orphaned run (no live CloudBolt "
    "job owns this deployment)."
)


def _own_job_tree_ids(job):
    """Return the IDs of every job in this teardown's own job tree: the
    top-level job (e.g. the Delete Resource job), its ancestors, and all of
    their descendants (this teardown and any sibling teardown items). These
    are always live while the teardown runs, so they can never be the
    competing provision/day-2 job the guard is looking for."""
    root = job.top_level_job
    tree_ids = {root.id}
    frontier = [root.id]
    while frontier:
        frontier = list(
            Job.objects.filter(parent_job_id__in=frontier)
            .exclude(id__in=tree_ids)
            .values_list("id", flat=True)
        )
        tree_ids.update(frontier)
    return tree_ids


def _guard_live_resource_jobs(job, resource, workspace_name):
    """Fail fast if a live (RUNNING/PAUSED) CloudBolt job OUTSIDE this
    teardown's own job tree is attached to the resource. This is the no-code
    attribution signal: the auto-queued run's TFC-authored message is not
    parseable, so a live provision/day-2 job -- not a run message -- is the
    authority on 'something is mid-flight; do not tear down under it'. The
    teardown's parent (the Delete Resource job) is itself RUNNING and attached
    to the resource for the whole teardown, so the exclusion must cover the
    tree, not just this job.
    """
    other_live = (
        Job.objects.filter(resource=resource, status__in=LIVE_JOB_STATUSES)
        .exclude(id__in=_own_job_tree_ids(job))
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
    nothing TFC-side exists. A stored ID is used first. A blank ID (a crash
    between the no-code create and the resource save, R9) falls back to a
    name lookup: the stored tfc_workspace_name, the resource name (set to the
    orderer's Workspace Name before the create), then the legacy
    cb-nc-<global_id> name. A match is adopted only when it carries this
    resource's cmp:resource-id tag, or, for the legacy name alone, when it is
    not tagged for a different resource; an orderer-chosen name without the
    tag could be anyone's workspace and is left alone."""
    if workspace_id:
        try:
            return workspace_id, client.get_workspace(workspace_id)
        except TFCNotFoundError:
            return None, None
    legacy_name = no_code_workspace_name_for_resource(resource.global_id)
    candidates = []
    for name in (
        (resource.get_value_for_custom_field("tfc_workspace_name") or "").strip(),
        (resource.name or "").strip(),
        legacy_name,
    ):
        if name and name not in candidates and WORKSPACE_NAME_PATTERN.match(name):
            candidates.append(name)
    for name in candidates:
        try:
            workspace = client.get_workspace_by_name(name)
        except TFCNotFoundError:
            continue
        if client.workspace_has_resource_tag(workspace["id"], resource.global_id):
            owned = True
        elif name == legacy_name:
            owned = not client.workspace_resource_tag_conflicts(
                workspace["id"], resource.global_id
            )
        else:
            owned = False
        if not owned:
            logger.warning(
                "Workspace '%s' exists but is not tagged for resource %s; not "
                "adopting it for teardown.", name, resource.global_id,
            )
            continue
        set_progress(
            "No stored workspace ID; adopted workspace '{}' by name for teardown.".format(name)
        )
        return workspace["id"], workspace
    return None, None


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
                "ID and none tagged for it found by name); nothing to do. "
                "(Expected for resources that failed before workspace "
                "creation.)".format(resource.name)
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

        # ---- Retire the child Server records the destroy removed ----------
        # Delete Resource runs this teardown item first (deploy_seq -1), then
        # spawns decommission jobs for every non-historical child server --
        # jobs that would try to power off and delete, through the handler,
        # VMs that no longer exist. Marking the created_by_terraform records
        # HISTORICAL here (vm_adoption) leaves those jobs nothing to do.
        retired = retire_servers(
            resource,
            "Terraform destroy run {} {}.".format(result["run_id"], destroy_summary),
            progress=set_progress,
        )
        set_progress(
            "TFC destroy run {} {}; deleting workspace '{}'...".format(
                result["run_id"], destroy_summary, workspace_name
            )
        )

        _safe_delete_with_retries(
            client, workspace_id, workspace_name, result["run_url"]
        )

        msg = (
            "TFC destroy run {} {} and workspace '{}' ({}) was safe-deleted.{} Run "
            "URL: {}".format(
                result["run_id"], destroy_summary, workspace_name, workspace_id,
                retired_note(retired), result["run_url"],
            )
        )
        logger.info(msg)
        return "SUCCESS", msg, ""

    except TFCError as exc:
        logger.exception("HCP Terraform No-Code Module teardown failed")
        return "FAILURE", "", str(exc)
