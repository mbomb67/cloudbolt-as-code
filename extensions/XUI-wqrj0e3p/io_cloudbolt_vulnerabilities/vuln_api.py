"""
Vulnerability scans and updates for other content: start them from a plug-in, and wait for the result or not.

    from xui.io_cloudbolt_vulnerabilities import vuln_api

    result = vuln_api.scan_and_wait(server, job.owner)
    result = vuln_api.install_updates(server, job.owner, restart_if_needed=True)
    result["status"]    "completed", "restart_pending" (updates installed, restart still needed), "failed",
                        "refused" or "timeout"
    result["message"]   one sentence for the job output
    result["job"]       the scan's or the update's own Job, or None
    result["scan"]      the server's last scan entry (scan_and_wait), or None
    result["patch"]     the server's last update record (install_updates), or None

    result = vuln_api.install_updates_on_resource(resource, job.owner, restart_if_needed=True)
    result["counts"]    {"updated", "restart_pending", "failed", "skipped"} servers (install_updates_on_resource)

Both start the same jobs the Vulnerabilities tab starts, so they take the same lock and land in the server's
history. install_updates installs the updates the server's package manager (Linux) or Windows Update (Windows Server
2022 and 2025) offers, restarts it
when that is needed and restart_if_needed is true, and scans it again.

    result = vuln_api.start_scan(server, job.owner, parent_job=job)
    result["status"]    "started" or "refused"

start_scan returns at once. With parent_job the scan is a sub-job of that job: shown under it and cancelled with it.

    ok, reason = eligible(server)   (True, "") or (False, one sentence why not)

install_updates_on_resource installs the updates on every server of one resource, one server at a time, and stops at the
first server that fails. Its status is "completed", "restart_pending" (nothing failed, at least one server still needs a
restart), "failed", "refused" or "timeout".

eligible says whether content may scan a server now: it is not historical, is not in an Unassigned group, has a
script channel, gets the tab under the "Show the tab on" setting, and the scanner is set up.

Nothing here checks a permission: the caller is content an administrator put in place, running as a job. Do not call
it from a web request.
"""
from utilities.logger import ThreadLogger

from . import vuln_fleet, vuln_jobs, vuln_patch, vuln_patch_resource, vuln_store as store

logger = ThreadLogger(__name__)

# Added to the time limits of a job when waiting for it: queueing and the upload of the software list.
WAIT_MARGIN_SECONDS = 600
# The longest install_updates_on_resource waits by default: 7.5 hours.
MAX_RESOURCE_WAIT_SECONDS = 27000


def site_url():
    """The default portal's configured site URL, or "" when it has none."""
    return vuln_fleet.default_base_url()


def _result(status, message, job=None, scan=None, patch=None):
    return {"status": status, "message": message, "job": job, "scan": scan, "patch": patch}


def eligible(server):
    """(True, "") when content may scan this server now, else (False, one sentence why not)."""
    code = vuln_fleet.eligibility(server, store.get_settings()["visibility"])
    if not code:
        code = vuln_fleet.prerequisite_for(server, vuln_fleet.prerequisites(site_url()))
    if code:
        return False, "the server is {}.".format(vuln_fleet.REASONS.get(code, code))
    return True, ""


def start_scan(server, owner, parent_job=None):
    """Start a scan of one server and return without waiting for it. owner is the UserProfile the scan job belongs to
    (a plug-in passes job.owner)."""
    started = vuln_jobs.start(owner, server, site_url(), parent_job=parent_job)
    if vuln_fleet._started_id(started) is None:
        return _result("refused", started.message)
    return _result("started", "The scan of {} started as job {}.".format(server.hostname, started.id), job=started)


def _wait(started, timeout):
    """The ended Job, or None when it did not end within timeout seconds (Job.wait_for_completion cancels it)."""
    from utilities.exceptions import TimeoutException
    try:
        return started.wait_for_completion(timeout=timeout)
    except TimeoutException:
        return None


def scan_and_wait(server, owner, timeout=None, parent_job=None):
    """Scan one server and wait for the scan's job to end. timeout is in seconds; the default is the scan time
    limit setting plus ten minutes."""
    started = vuln_jobs.start(owner, server, site_url(), parent_job=parent_job)
    if vuln_fleet._started_id(started) is None:
        return _result("refused", started.message)
    wait = timeout or store.job_settings()["timeout_min"] * 60 + WAIT_MARGIN_SECONDS
    job = _wait(started, wait)
    if job is None:
        return _result("timeout", "The scan of {} did not finish within {} minutes (job {}).".format(
            server.hostname, wait // 60, started.id), job=started)
    last = _state(server.id).get("last")
    scan = last if isinstance(last, dict) and last.get("scan_id") and job.status in ("SUCCESS", "WARNING") else None
    if scan is None or scan.get("status") != "completed":
        detail = scan.get("error") if scan else vuln_jobs.clean(job.errors or "", vuln_jobs.MESSAGE_LEN)
        return _result("failed", "The scan of {} failed (job {}). {}".format(server.hostname, job.id, detail or "").strip(),
                       job=job, scan=scan)
    return _result("completed", "{} scanned: {} known vulnerabilities.".format(
        server.hostname, _count(server.id)), job=job, scan=scan)


def _state(server_id):
    try:
        return store.read_state(server_id)
    except Exception:
        logger.exception("Vulnerabilities: could not read the state of server %s", server_id)
        return {}


def _count(server_id):
    summary = vuln_patch._summary(server_id)
    return summary["vulns"] if summary and summary.get("vulns") is not None else "no"


def install_updates(server, owner, restart_if_needed=True, timeout=None, parent_job=None):
    """Install the updates a Linux or Windows Server 2022/2025 server offers, restart it when needed (restart_if_needed), scan it again, and
    wait for the job to end. timeout is in seconds; the default covers the update, the restart wait and the rescan
    time limits of the settings."""
    started = vuln_patch.start_job(owner, server=server, restart_if_needed=restart_if_needed, parent_job=parent_job)
    if vuln_fleet._started_id(started) is None:
        return _result("refused", started.message)
    cfg = store.get_settings()
    wait = timeout or (vuln_patch._minutes(cfg, "patch_timeout_min", vuln_patch.DEFAULT_PATCH_TIMEOUT_MIN)
                       + vuln_patch._minutes(cfg, "restart_wait_min", vuln_patch.DEFAULT_RESTART_WAIT_MIN)
                       + cfg["scan_timeout_min"]) * 60 + WAIT_MARGIN_SECONDS
    job = _wait(started, wait)
    if job is None:
        return _result("timeout", "The update of {} did not finish within {} minutes (job {}).".format(
            server.hostname, wait // 60, started.id), job=started)
    patch = _state(server.id).get("patch")
    if not isinstance(patch, dict) or patch.get("job_id") != job.id:
        detail = vuln_jobs.clean(job.errors or "", vuln_jobs.MESSAGE_LEN)
        return _result("failed", "The update of {} recorded no result (job {} ended as {}). {}".format(
            server.hostname, job.id, job.status, detail).strip(), job=job)
    status = patch.get("status")
    if status == "failed":
        return _result("failed", "The update of {} failed: {}".format(server.hostname, patch.get("message") or
                                                                      "no reason given"), job=job, patch=patch)
    if status == "restart_pending":
        return _result("restart_pending", "Updates were installed on {}; a restart is still needed.".format(
            server.hostname), job=job, patch=patch)
    if status == "no_updates":
        return _result("completed", "No updates were available on {}.".format(server.hostname), job=job, patch=patch)
    return _result("completed", "Updates were installed on {}{}.".format(
        server.hostname, "; it restarted" if patch.get("restarted") else ""), job=job, patch=patch)


def install_updates_on_resource(resource, owner, restart_if_needed=True, timeout=None, parent_job=None):
    """Install the updates on every server of one resource, one server at a time, and wait for the job to end.
    owner is the UserProfile the job belongs to (a plug-in passes job.owner). timeout is in seconds; the default is
    the worst case of one server (update, restart wait and rescan limits of the settings) times the servers the
    owner may update now, at most 7.5 hours.

    This does no permission check of its own before it starts: the caller is content an administrator put in
    place. The job still checks every server as owner (the Install Updates server action must be permitted for
    that user on it, and the server must be eligible), skips what fails the checks with a reason, and stops at
    the first server whose update fails. The patching setting is enforced by the job. The result is the usual
    dict plus counts, from the resource's last update record: {"updated", "restart_pending", "failed", "skipped"}.
    Status "completed" does not mean every server was updated: the caller must read counts (updated, no_updates,
    restart_pending, failed, skipped) to see what happened to each server.
    The status is "restart_pending" when nothing failed and at least one server still needs a restart."""
    started = vuln_patch_resource.start_job(owner, resource, restart_if_needed=restart_if_needed, parent_job=parent_job)
    if vuln_fleet._started_id(started) is None:
        return dict(_result("refused", started.message), counts=None)
    cfg = store.get_settings()
    if timeout:
        wait = timeout
    else:
        try:
            servers = max(1, len(vuln_patch_resource.patchable_ids(owner, resource, cfg["visibility"])))
        except Exception:
            logger.exception("Vulnerabilities: could not count the servers of resource %s", getattr(resource, "id", ""))
            servers = 1
        worst = (vuln_patch._minutes(cfg, "patch_timeout_min", vuln_patch.DEFAULT_PATCH_TIMEOUT_MIN)
                 + vuln_patch._minutes(cfg, "restart_wait_min", vuln_patch.DEFAULT_RESTART_WAIT_MIN)
                 + cfg["scan_timeout_min"]) * 60
        wait = min(servers * worst + WAIT_MARGIN_SECONDS, MAX_RESOURCE_WAIT_SECONDS)
    name = getattr(resource, "name", "") or "the resource"
    job = _wait(started, wait)
    if job is None:
        return dict(_result("timeout", "The update of {} did not finish within {} minutes (job {}).".format(
            name, wait // 60, started.id), job=started), counts=None)
    record = _resource_patch(resource.id)
    if not isinstance(record, dict) or record.get("job_id") != job.id:
        detail = vuln_jobs.clean(job.errors or "", vuln_jobs.MESSAGE_LEN)
        return dict(_result("failed", "The update of {} recorded no result (job {} ended as {}). {}".format(
            name, job.id, job.status, detail).strip(), job=job), counts=None)
    counts = record.get("counts") if isinstance(record.get("counts"), dict) else {}
    counts = {key: _whole(counts.get(key)) for key in ("updated", "no_updates", "restart_pending", "failed", "skipped")}
    note = record.get("message") or "no reason given"
    if record.get("status") == "failure" or counts["failed"] > 0:
        return dict(_result("failed", "The update of {} failed: {}".format(name, note), job=job), counts=counts)
    if counts["restart_pending"] > 0:
        return dict(_result("restart_pending", "Updates were installed on the servers of {}; {} still need a "
                            "restart.".format(name, counts["restart_pending"]), job=job), counts=counts)
    return dict(_result("completed", "Updates were installed on the servers of {}: {}".format(name, note), job=job),
                counts=counts)


def _resource_patch(resource_id):
    try:
        return store.read_resource_patch(resource_id)
    except Exception:
        logger.exception("Vulnerabilities: could not read the update record of resource %s", resource_id)
        return None


def _whole(value):
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0
