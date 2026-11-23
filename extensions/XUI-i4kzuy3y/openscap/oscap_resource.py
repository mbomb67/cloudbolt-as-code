"""
OpenSCAP scans of the servers of one resource, as one CloudBolt job.

start() runs in the web request. It validates the request, takes the
resource's start lock and starts the plugin from oscap_hooks.py (the parent
job) with OrchestrationHook.run_as_job. One run per resource at a time.

run_resource_scan() runs inside the parent job. It starts one OpenSCAP Scan
job per server (oscap_jobs.start, the same call the server tab makes), a few at
a time (MAX_ACTIVE), and polls them. Each child is the server's own scan or
check job: it keeps its own result files and its own entry in the server's
index, and the parent job is the child's parent_job. A server that is refused
or whose job fails, or that takes too long, does not stop the others; the run
record says why. The roll-up across servers is never stored: the resource tab
computes it from the servers' indexes (oscap_rollup.py). What the run stores
(oscap_store.py) is the run record, for the History table and the trend.

The parent job holds one job engine worker while it waits and the children run
on the others, so a run moves at full width only when the engine has more than
MAX_ACTIVE + 1 workers free; with fewer it is slower.
"""
import time

from infrastructure.models import Server
from jobs.models import Job
from resources.models import Resource
from utilities.logger import ThreadLogger

from . import oscap_hooks, oscap_jobs, oscap_rollup, oscap_store as store

logger = ThreadLogger(__name__)

# Children running at once, how often they are polled, and how long past the
# configured scan timeout one child may take before the run moves on.
MAX_ACTIVE = 5
POLL_SECONDS = 5
TIMEOUT_MARGIN_MIN = 5
REASON_LEN = 200
# How long a child may wait in the job engine's queue before the run cancels it.
QUEUE_CAP_MIN = 60

RESOURCE_INDEX_ERROR = (
    "The scan history for this resource cannot be read or written; ask an administrator to check the OpenSCAP folder."
)


# ---------------------------------------------------------------- which servers
def live_servers(resource):
    """The resource's servers that are not historical, in blueprint deploy order.
    Only resource.server_set: the servers of a sub-resource belong to that
    sub-resource's own tab."""
    return list(
        resource.server_set.exclude(status="HISTORICAL")
        .select_related("os_family", "service_item")
        .prefetch_related("tags")
        .order_by("service_item__deploy_seq", "id")
    )


def eligible(server, visibility):
    """Whether the server is part of the resource tab: it would get the server
    tab, and it is not Windows (the scan runs a shell script)."""
    return oscap_jobs.shows_tab(server, visibility) and not server.is_windows()


def summary_input(server_id, hostname, scan):
    """One server's scan summary as oscap_rollup.rollup takes it."""
    return {
        "id": server_id,
        "hostname": hostname,
        "score": scan.get("score"),
        "fail": (scan.get("counts") or {}).get("fail", 0),
        "failed_rules": scan.get("failed_rules") or [],
        "truncated": bool(scan.get("failed_rules_truncated")),
    }


# ---------------------------------------------------------------- active run
def active_run(resource_id, index=None):
    """(marker, Job) of the run in progress, or None. A marker whose Job is no
    longer active is stale and ignored. Raises store.StoreError when the index
    cannot be read."""
    index = index if index is not None else store.read_resource_index(resource_id)
    marker = index.get("active")
    if not isinstance(marker, dict) or not marker.get("job_id"):
        return None
    job = Job.objects.filter(id=marker["job_id"]).first()
    if job is None or not job.is_active():
        return None
    return marker, job


def panel_active(resource_id, index):
    """The panel's "active" dict for the resource run, or None."""
    found = active_run(resource_id, index)
    if found is None:
        return None
    marker, job = found
    try:
        done, total, _message = job.get_progress()
    except Exception:
        done = total = 0
    server_ids = [item for item in marker.get("server_ids") or [] if isinstance(item, int)]
    total = int(total or 0) or len(server_ids)
    done = min(int(done or 0), total)
    return {
        "job_id": job.id,
        "job_url": job.get_absolute_url(),
        "kind": marker.get("kind") or "scan",
        "started": marker.get("started") or "",
        "done": done,
        "total": total,
        "percent": int(100 * done / total) if total else 0,
        "server_ids": server_ids,
        "finished_ids": [item for item in marker.get("done") or [] if isinstance(item, int)],
    }


# ---------------------------------------------------------------- start
def start(profile, resource, servers, mode, datastream, profile_id, install, base_url=""):
    """Validate and start the parent job for these servers (already checked to
    belong to the resource, to be shown the tab and to be scannable by the
    profile). base_url is the default portal's configured site URL (never a
    request's Host); the children build their upload links from it. Returns the
    Job, or a Refusal."""
    if mode not in oscap_jobs.MODES:
        return oscap_jobs.Refusal("The mode must be scan or check.")
    datastream = (datastream or "").strip()
    profile_id = (profile_id or "").strip()
    if datastream and not store.DATASTREAM_RE.fullmatch(datastream):
        return oscap_jobs.Refusal("The datastream name has characters that are not allowed.")
    if not store.PROFILE_RE.fullmatch(profile_id):
        return oscap_jobs.Refusal("The profile id has characters that are not allowed.")
    if not servers:
        return oscap_jobs.Refusal("Choose at least one server.")

    cfg = store.get_settings()
    job_cfg = store.job_settings(cfg)
    try:
        # One start at a time per resource: the active check, the queueing and
        # the marker are one step, so two clicks cannot start two runs.
        with store.resource_lock(resource.id):
            running = active_run(resource.id)
            if running is not None:
                return oscap_jobs.Refusal(
                    "Job {} is still running on this resource; wait for it to finish.".format(running[1].id), 409
                )
            return _queue(profile, resource, servers, mode, datastream, profile_id,
                          bool(install) and cfg["allow_install"], job_cfg, base_url)
    except store.StoreError:
        logger.exception("OpenSCAP: index problem starting a run on resource %s", resource.id)
        return oscap_jobs.Refusal(RESOURCE_INDEX_ERROR, 500)


def _queue(profile, resource, servers, mode, datastream, profile_id, install, job_cfg, base_url):
    """The queued parent Job, or a Refusal."""
    run_id = store.new_scan_id()
    started = store.stamp()
    server_ids = [server.id for server in servers]
    created = oscap_hooks.resource_scan_hook().run_as_job(
        owner=profile,
        context={
            "oscap_resource_id": resource.id,
            "oscap_mode": mode,
            "oscap_datastream": datastream,
            "oscap_profile": profile_id,
            "oscap_install": install,
            "oscap_server_ids": server_ids,
            "oscap_cfg": job_cfg,
            "oscap_base_url": base_url,
            "oscap_run_id": run_id,
            "oscap_started": started,
            "oscap_requested_by": str(profile.user.username) if getattr(profile, "user", None) else str(profile),
        },
    )
    if not created:
        return oscap_jobs.Refusal("CloudBolt did not start the job; see the application log.", 500)
    job = created[0]
    # From here the job is queued: nothing below may turn into an error.
    try:
        job.label = "OpenSCAP {}: {}".format("scan" if mode == "scan" else "check", resource.name)
        job.save(update_fields=["label"])
        # So the job shows on the resource's own Jobs tab and its group can see it.
        resource.jobs.add(job)
    except Exception:
        logger.exception("OpenSCAP: could not label or link job %s", job.id)
    try:
        store.set_resource_active(
            resource.id,
            {"job_id": job.id, "kind": mode, "started": started, "run_id": run_id, "server_ids": server_ids, "done": []},
        )
    except Exception:
        logger.exception("OpenSCAP: could not record job %s as active on resource %s", job.id, resource.id)
    return job


# ---------------------------------------------------------------- the job
def _progress(job, finished, total):
    if job is not None:
        job.set_progress("{} of {} servers complete".format(finished, total), tasks_done=finished, total_tasks=total)


def _canceled(job):
    return job is not None and Job.objects.filter(id=job.id, status__in=("TO_CANCEL", "CANCELED")).exists()


def _entry(server_id, hostname="", status="failed", reason="", job_id=None, scan_id=""):
    """One server's line of a run record."""
    return {
        "server_id": server_id,
        "hostname": hostname,
        "job_id": job_id,
        "scan_id": scan_id,
        "status": status,
        "reason": oscap_jobs.clean(reason, REASON_LEN),
    }


def _start_child(job, resource, server_id, mode, datastream, profile_id, install, base_url):
    """(entry, child Job or None). The entry is final when there is no Job: the
    server was refused. Everything that can change between the click and now is
    checked again."""
    server = Server.objects.filter(id=server_id).first()
    if server is None:
        return _entry(server_id, reason="The server no longer exists."), None
    hostname = server.hostname
    if server.resource_id != resource.id:
        return _entry(server_id, hostname, reason="The server is no longer part of this resource."), None
    if server.status == "HISTORICAL":
        return _entry(server_id, hostname, reason="The server is historical."), None
    if server.is_windows():
        return _entry(server_id, hostname, reason="OpenSCAP scans Linux servers only."), None
    if job is None or job.owner is None:
        return _entry(server_id, hostname, reason="The run has no owner to start the scan as."), None
    # The permissions are checked again as the user the run belongs to: they may have changed since the click.
    if not oscap_jobs.can_view(job.owner, server) or not oscap_jobs.can_scan(job.owner, server):
        return _entry(server_id, hostname, status="skipped",
                      reason="You no longer have permission to scan this server."), None
    child = oscap_jobs.start(
        job.owner, server, mode, datastream, profile_id, install, base_url=base_url, parent_job=job
    )
    if isinstance(child, oscap_jobs.Refusal):
        return _entry(server_id, hostname, status="skipped", reason=child.message), None
    return _entry(server_id, hostname, status="running", job_id=child.id,
                  scan_id=str(getattr(child, "oscap_scan_id", "") or "")), child


def _settle(entry, child, mode):
    """Fill the entry of a child that has ended, from the child's status and the
    record the child left in the server's index."""
    ran = child.status in ("SUCCESS", "WARNING")
    entry["status"] = "completed" if ran else "failed"
    reason = ""
    try:
        if mode == "scan":
            scan = store.find_scan(entry["server_id"], entry["scan_id"]) if entry["scan_id"] else None
            if scan is not None:
                reason = scan.get("error") or ("" if scan.get("stored") else scan.get("store_problem") or "")
        elif ran:
            # A check that ran can still find the server not ready (a WARNING job): ready is what counts.
            state = store.read_index(entry["server_id"])["state"]
            if not state.get("ready"):
                entry["status"] = "not_ready"
                reason = state.get("problem") or "OpenSCAP is not ready."
    except store.StoreError:
        pass
    if not reason and entry["status"] == "failed":
        reason = "The job did not finish; see job {}.".format(child.id)
    entry["reason"] = oscap_jobs.clean(reason, REASON_LEN)


def _mark_done(resource_id, server_id, job_id):
    try:
        with store.resource_lock(resource_id):
            store.mark_resource_server_done(resource_id, server_id, job_id)
    except Exception:
        logger.exception("OpenSCAP: could not mark server %s done on resource %s", server_id, resource_id)


def run_resource_scan(job, oscap_resource_id=None, oscap_mode="scan", oscap_datastream="", oscap_profile="",
                      oscap_install=False, oscap_server_ids=None, oscap_cfg=None, oscap_base_url="",
                      oscap_run_id="", oscap_started="", oscap_requested_by="", **kwargs):
    """Plugin entry point (via oscap_resource_job.py). Returns the 4-tuple
    CloudBolt plugins use: status, output, errors, outputs."""
    resource = Resource.objects.filter(id=oscap_resource_id).first()
    order = [int(item) for item in oscap_server_ids or []]
    entries = {}
    began = store.now_utc()
    try:
        if resource is None:
            return "FAILURE", "", "The resource no longer exists.", {}
        try:
            return _run(job, resource, order, entries, oscap_mode, oscap_datastream, oscap_profile,
                        bool(oscap_install), oscap_cfg or store.job_settings(store.DEFAULT_SETTINGS), oscap_base_url)
        except Exception as exc:
            logger.exception("OpenSCAP resource job failed on resource %s", oscap_resource_id)
            return "FAILURE", "", "OpenSCAP failed: {}".format(oscap_jobs.clean(str(exc), oscap_jobs.MESSAGE_LEN)), {}
    finally:
        # Always clear the marker and write the record, so a failed run never blocks the next one.
        if oscap_resource_id and resource is not None:
            _finish(job, resource, order, entries, oscap_mode, oscap_datastream, oscap_profile, oscap_run_id,
                    oscap_started, oscap_requested_by, int((store.now_utc() - began).total_seconds()))


def _cancel_child(child):
    """Cancel a child the way the Cancel Job button does: jobs.forms.JobCancelForm.save
    marks the job (and any sub-jobs) TO_CANCEL, and the job engine ends it."""
    from jobs.forms import JobCancelForm

    try:
        JobCancelForm().save(child)
    except Exception:
        logger.exception("OpenSCAP: could not cancel job %s", child.id)


def _run(job, resource, order, entries, mode, datastream, profile_id, install, cfg, base_url):
    total = len(order)
    limit = (int(cfg.get("timeout_min") or 30) + TIMEOUT_MARGIN_MIN) * 60
    job_id = job.id if job is not None else None
    pending = list(order)
    active = {}
    finished = 0
    _progress(job, finished, total)
    while pending or active:
        if _canceled(job):
            for server_id in pending:
                entries[server_id] = _entry(server_id, status="skipped",
                                            reason="The run was canceled before this server started.")
            for running in active.values():
                _cancel_child(running["child"])
                running["entry"].update(
                    status="failed",
                    reason="The run was canceled while this server was scanning; job {} was canceled.".format(
                        running["child"].id),
                )
            break
        while pending and len(active) < MAX_ACTIVE:
            server_id = pending.pop(0)
            try:
                entry, child = _start_child(job, resource, server_id, mode, datastream, profile_id, install, base_url)
            except Exception:
                logger.exception("OpenSCAP: could not start server %s in a run of resource %s", server_id, resource.id)
                entry, child = _entry(server_id, reason="The scan could not be started; see the application log."), None
            entries[server_id] = entry
            if child is None:
                finished += 1
                _mark_done(resource.id, server_id, job_id)
                _progress(job, finished, total)
            else:
                active[server_id] = {"child": child, "entry": entry, "queued_at": time.time(), "deadline": None}
        if not active:
            continue
        # The children are polled together, never one at a time.
        time.sleep(POLL_SECONDS)
        for server_id, running in list(active.items()):
            child = Job.objects.filter(id=running["child"].id).first()
            if child is not None and child.is_active():
                now = time.time()
                # The scan timeout counts from the moment the child leaves the engine's queue.
                if running["deadline"] is None and child.status in Job.RUNNING_STATUSES:
                    running["deadline"] = now + limit
                if running["deadline"] is None:
                    if now - running["queued_at"] <= QUEUE_CAP_MIN * 60:
                        continue
                    reason = "Job {} waited more than {} minutes in the queue and was canceled.".format(
                        child.id, QUEUE_CAP_MIN)
                elif now <= running["deadline"]:
                    continue
                else:
                    reason = "Job {} did not finish in time and was canceled.".format(child.id)
                _cancel_child(child)
                running["entry"].update(status="failed", reason=oscap_jobs.clean(reason, REASON_LEN))
            elif child is None:
                running["entry"].update(status="failed", reason="The job no longer exists.")
            else:
                _settle(running["entry"], child, mode)
            del active[server_id]
            finished += 1
            _mark_done(resource.id, server_id, job_id)
            _progress(job, finished, total)

    results = [entries[server_id] for server_id in order if server_id in entries]
    completed = sum(1 for entry in results if entry["status"] == "completed")
    lines = []
    for entry in results:
        detail = " ({})".format(entry["reason"]) if entry["reason"] else ""
        lines.append("{}: {}{}".format(entry["hostname"] or entry["server_id"], entry["status"].replace("_", " "), detail))
    if mode == "scan":
        headline = "{} of {} servers completed the scan.".format(completed, total)
    else:
        headline = "{} of {} servers ready.".format(completed, total)
    output = "\n".join([headline] + lines)
    outputs = {"mode": mode, "servers_completed": completed, "servers_attempted": total}
    if total and completed == total:
        return "SUCCESS", output, "", outputs
    problems = "\n".join(line for entry, line in zip(results, lines) if entry["status"] != "completed")
    if completed:
        return "WARNING", output, problems, outputs
    return "FAILURE", output, problems or "No server was scanned.", outputs


def _snapshot(entries, mode):
    """(average score, unique failed rules, profile id, profile title, mixed) of
    the servers that completed a scan in this run, from the scans they left."""
    if mode != "scan":
        return None, None, "", "", False
    servers, profiles = [], set()
    dropped = False
    for entry in entries:
        if entry["status"] != "completed" or not entry["scan_id"]:
            continue
        scan = store.find_scan(entry["server_id"], entry["scan_id"])
        if scan is None or scan.get("status") != "completed":
            continue
        if scan.get("failed_rules_dropped"):
            # A newer scan of this server has already replaced the list: the rule count would be short.
            dropped = True
        servers.append(summary_input(entry["server_id"], entry["hostname"], scan))
        profiles.add((scan.get("profile_id") or "", scan.get("profile_title") or ""))
    if not servers:
        return None, None, "", "", False
    rolled = oscap_rollup.rollup(servers)
    unique = None if dropped else rolled["unique_failed"]
    if len(profiles) == 1:
        found = next(iter(profiles))
        return rolled["average"], unique, found[0], found[1], False
    return rolled["average"], unique, "", "", True


def _finish(job, resource, order, entries, mode, datastream, profile_id, run_id, started, requested_by, wall):
    """Clear the active marker and write the run record in one step."""
    try:
        results = [
            entries.get(server_id) or _entry(server_id, reason="The run stopped before this server was scanned.")
            for server_id in order
        ]
        for entry in results:
            if entry["status"] == "running":
                entry["status"] = "failed"
                entry["reason"] = oscap_jobs.clean(
                    "The run stopped before this server finished; see job {}.".format(entry["job_id"]), REASON_LEN)
        try:
            average, unique, found_id, found_title, mixed = _snapshot(results, mode)
        except Exception:
            logger.exception("OpenSCAP: could not summarise run %s", run_id)
            average, unique, found_id, found_title, mixed = None, None, "", "", False
        job_id = job.id if job is not None else None
        run = {
            "run_id": run_id,
            "started": started or store.stamp(),
            "finished": store.stamp(),
            "duration_s": wall,
            "mode": mode,
            "profile_id": profile_id or found_id,
            "profile_title": found_title,
            "profile_mixed": mixed and not profile_id,
            "datastream": datastream,
            "requested_by": requested_by,
            "job_id": job_id,
            "servers": results,
            "servers_completed": sum(1 for entry in results if entry["status"] == "completed"),
            "servers_attempted": len(results),
            "average_score": average,
            "unique_failed": unique,
        }

        def change(index):
            marker = index.get("active")
            if isinstance(marker, dict) and marker.get("job_id") == job_id:
                index.pop("active", None)
            index["runs"] = [run] + [r for r in index["runs"] if r.get("run_id") != run_id]

        with store.resource_lock(resource.id):
            store.update_resource_index(resource.id, change)
    except Exception:
        logger.exception("OpenSCAP: could not clear the active marker or write the run of resource %s", resource.id)
