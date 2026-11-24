"""
Restart Web Workers (Dev Appliances Only) (MCP Tool Action plugin).

DEVELOPMENT APPLIANCES ONLY. Never deploy this to a production appliance: it
works on any appliance it is synced to, and it interrupts every in-flight web request, synchronous action, inbound webhook
and order-form call while the web tier reloads.

Why it exists: UI extension routes and views, and shared-module code, are
cached in the web workers and load only when a worker process starts. An agent
that just synced an extension therefore needs the workers recycled. The
cloudbolt account cannot restart httpd (no sudo, by design), so this signals
CloudBolt's mod_wsgi daemon processes directly and lets Apache respawn them.
That reimports Django, which is what extensions need; it does not reload
Apache's own configuration, and it does not touch the job engine.

How the workers are found: CloudBolt's Django app runs only in the mod_wsgi
daemon processes (WSGIDaemonProcess cloudbolt ...). mod_wsgi rewrites their
command line to the display name "cloudbolt" while /proc/<pid>/comm stays
"httpd", and their parent is the root-owned Apache parent. Regular Apache
worker children also run as cloudbolt and are also named httpd, but keep the
httpd binary as their command line, so they are never signalled.

The restart is rolling: it recycles one worker at a time and waits for each
new worker to finish importing Django (CPU time stops growing) before the
next, so the other workers keep serving.

Safety gates: the caller must be a CloudBolt admin, the action must run as a
job (a synchronous run would kill the request it runs in). A dry run never
signals anything.

Every declared input is read from its rendered template token in
_read_inputs(); keep each one referenced that way or CloudBolt's token scan
deletes the input when the script is saved. Output keys are camelCase so a
synchronous run and an asynchronous fetch_job read identically. The dry-run
choice is a STR input holding true or false, not BOOL: a BOOL input renders
as a select in the action's edit form and the platform then refuses it.
"""
import html
import os
import signal
import time

import requests

from utilities.helpers import get_ssl_verification
from utilities.logger import ThreadLogger

logger = ThreadLogger(__name__)

WORKER_CMDLINE_NAME = "cloudbolt"
POLL_INTERVAL_SECONDS = 2
HTTP_PROBE_TIMEOUT_SECONDS = 10
HTTP_PROBE_TOTAL_SECONDS = 180
ROLLING_TIMEOUT_SECONDS = 900
SETTLE_WAIT_SECONDS = 120
SETTLE_SAMPLE_SECONDS = 1
SETTLE_MIN_AGE_SECONDS = 3
SETTLE_MAX_CPU_DELTA = 0.05
PROC = "/proc"
CLOCK_TICKS = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100


def _text(value):
    return html.unescape(value or "").strip()


def _failure(message):
    return {
        "status": "FAILURE",
        "output_message": "",
        "error_message": message,
        "outputs": {"error": message},
    }


def _read_inputs():
    raw = {
        "dev_web_restart_dry_run": """{{ dev_web_restart_dry_run }}""",
    }
    dry_run = _text(raw["dev_web_restart_dry_run"]).lower() in ("true", "yes", "1")
    return {"dry_run": dry_run}


def _read(path):
    with open(path, "rb") as handle:
        return handle.read()


def _comm(pid):
    return _read(f"{PROC}/{pid}/comm").decode("utf-8", "replace").strip()


def _parent_pid(pid):
    # /proc/<pid>/stat is "pid (comm) state ppid ..."; comm may hold spaces or
    # parentheses, so split after the last closing parenthesis.
    stat = _read(f"{PROC}/{pid}/stat").decode("utf-8", "replace")
    return int(stat.rsplit(")", 1)[1].split()[1])


def _first_argument(pid):
    parts = _read(f"{PROC}/{pid}/cmdline").split(b"\0")
    return parts[0].decode("utf-8", "replace").strip() if parts else ""


def _real_uid(pid):
    # Read the real uid from status: the owner of /proc/<pid> turns root for a
    # process that is not dumpable (for example after a setuid), so it is not a
    # reliable uid.
    for line in _read(f"{PROC}/{pid}/status").decode("utf-8", "replace").splitlines():
        if line.startswith("Uid:"):
            return int(line.split()[1])
    raise ValueError("no Uid line")


def _worker_parent(pid, my_uid):
    """Return the Apache parent pid if pid is a mod_wsgi daemon process, else None."""
    if _real_uid(pid) != my_uid:
        return None
    if _first_argument(pid) != WORKER_CMDLINE_NAME or _comm(pid) != "httpd":
        return None
    parent = _parent_pid(pid)
    if _comm(parent) != "httpd" or _real_uid(parent) != 0:
        return None
    return parent


def _ancestors_of_self():
    pids = set()
    pid = os.getpid()
    while pid > 1 and pid not in pids:
        pids.add(pid)
        try:
            pid = _parent_pid(pid)
        except (OSError, ValueError, IndexError):
            break
    return pids


def _scan_workers():
    """Return (worker pids, Apache parent pids) for the mod_wsgi daemon processes."""
    excluded = _ancestors_of_self()
    my_uid = os.getuid()
    workers = set()
    parents = set()
    for entry in os.listdir(PROC):
        if not entry.isdigit():
            continue
        pid = int(entry)
        if pid in excluded:
            continue
        try:
            parent = _worker_parent(pid, my_uid)
            if parent is None:
                continue
        except (OSError, ValueError, IndexError):
            continue  # the process exited while it was being inspected
        workers.add(pid)
        parents.add(parent)
    return sorted(workers), sorted(parents)


def _base_url():
    try:
        from portals.models import PortalConfig

        # With no request this is the default portal (portals/models.py get_current_portal).
        portal = PortalConfig.get_current_portal()
        base = ((portal.site_url if portal else None) or "").rstrip("/")
    except Exception:
        base = ""
    return base or "https://localhost"


def _probe_http(base_url, started, deadline):
    """Poll the appliance's own site until it answers below 500 or time runs out."""
    last = "no response"
    while True:
        try:
            response = requests.get(
                base_url + "/",
                timeout=HTTP_PROBE_TIMEOUT_SECONDS,
                verify=get_ssl_verification(),
                allow_redirects=False,
            )
            if response.status_code < 500:
                return response.status_code, round(time.time() - started, 1)
            last = f"HTTP {response.status_code}"
        except Exception as exc:
            last = f"error: {exc}"
        if time.time() >= deadline:
            return last, None
        time.sleep(POLL_INTERVAL_SECONDS)


def _signal_workers(pids):
    sent = []
    my_uid = os.getuid()
    for pid in pids:
        try:
            # Re-check just before signalling, in case the pid was reused.
            if _worker_parent(pid, my_uid) is None:
                logger.warning("Process %s is no longer a web worker; not signalled", pid)
                continue
            # mod_wsgi: SIGINT on a daemon process shuts it down and the Apache
            # supervisor restarts it. Daemon processes only, never Apache children.
            # https://modwsgi.readthedocs.io/en/master/user-guides/reloading-source-code.html
            # SIGUSR1 would be graceful, but graceful-timeout here is four hours:
            # https://modwsgi.readthedocs.io/en/master/configuration-directives/WSGIDaemonProcess.html
            os.kill(pid, signal.SIGINT)
            sent.append(pid)
        except (OSError, ValueError, IndexError):
            logger.warning("Web worker %s was already gone", pid)
    return sent


def _cpu_seconds(pid):
    """utime + stime of a process, from /proc/<pid>/stat (fields 14 and 15)."""
    stat = _read(f"{PROC}/{pid}/stat").decode("utf-8", "replace")
    fields = stat.rsplit(")", 1)[1].split()
    return (int(fields[11]) + int(fields[12])) / CLOCK_TICKS


def _wait_until_settled(pid, max_wait=SETTLE_WAIT_SECONDS):
    """Wait until a new worker has finished importing Django.

    The import is CPU bound, so a worker counts as ready once it has been seen
    for SETTLE_MIN_AGE_SECONDS and used less than SETTLE_MAX_CPU_DELTA seconds
    of CPU over the last one-second sample. Returns (settled, seconds waited).
    """
    begun = time.time()
    try:
        last_cpu = _cpu_seconds(pid)
        while time.time() - begun < max_wait:
            time.sleep(SETTLE_SAMPLE_SECONDS)
            cpu = _cpu_seconds(pid)
            if (
                time.time() - begun >= SETTLE_MIN_AGE_SECONDS
                and cpu - last_cpu < SETTLE_MAX_CPU_DELTA
            ):
                return True, round(time.time() - begun, 1)
            last_cpu = cpu
    except (OSError, ValueError, IndexError):
        pass  # the new worker exited; the caller records it as not settled
    return False, round(time.time() - begun, 1)


def _finish(old_workers, new_workers, sent, per_worker, replaced_after, started, deadline):
    """Probe the site and build the SUCCESS result."""
    logger.info("Web workers replaced after %s seconds: %s", replaced_after, new_workers)
    base_url = _base_url()
    probe_status, probe_seconds = _probe_http(base_url, started, deadline)
    message = (
        f"Signalled {sent} of {len(old_workers)} web workers; all were "
        f"replaced after {replaced_after} seconds."
    )
    if probe_seconds is None:
        message += (
            f" Warning: {base_url}/ did not answer below HTTP 500 within "
            f"{HTTP_PROBE_TOTAL_SECONDS} seconds (last result: {probe_status}). The "
            "workers were replaced, so retry the MCP call or open the UI in a moment."
        )
        logger.warning("HTTP probe never succeeded: %s", probe_status)
    else:
        message += f" {base_url}/ answered HTTP {probe_status} after {probe_seconds} seconds."
    unsettled = [w["oldPid"] for w in per_worker if not w["settled"]]
    if unsettled:
        message += f" Workers that never settled or were skipped: {unsettled}."
    logger.info(message)
    return {
        "status": "SUCCESS",
        "output_message": message,
        "error_message": "",
        "outputs": {
            "oldWorkerPids": old_workers,
            "newWorkerPids": new_workers,
            "signalledCount": sent,
            "perWorker": per_worker,
            "secondsUntilWorkersReplaced": replaced_after,
            "totalSeconds": round(time.time() - started, 1),
            "httpProbeStatus": probe_status,
            "secondsUntilHttpOk": probe_seconds,
            "message": message,
        },
    }


def _restart_rolling(old_workers):
    """Recycle one worker at a time so the others keep serving."""
    started = time.time()
    deadline = started + ROLLING_TIMEOUT_SECONDS
    known = set(old_workers)  # every pid seen so far; only the originals are signalled
    per_worker = []
    sent = 0
    for old_pid in old_workers:
        if time.time() >= deadline:
            return _failure(
                f"Rolling restart ran past {ROLLING_TIMEOUT_SECONDS} seconds after "
                f"{sent} of {len(old_workers)} workers. Workers not yet recycled: "
                f"{[p for p in old_workers if p not in {w['oldPid'] for w in per_worker}]}."
            )
        current, _ = _scan_workers()
        known |= set(current)  # workers mod_wsgi recycled on its own are not ours to match
        entry = {
            "oldPid": old_pid, "newPid": None, "secondsToRespawn": None,
            "secondsToSettle": None, "settled": False,
        }
        if old_pid not in current:
            logger.info("Web worker %s already gone (recycled by mod_wsgi); skipped", old_pid)
            per_worker.append(entry)
            continue
        step = time.time()
        if _signal_workers([old_pid]):
            sent += 1
        else:
            per_worker.append(entry)
            continue
        new_pid = None
        while time.time() < deadline:
            time.sleep(POLL_INTERVAL_SECONDS)
            current, _ = _scan_workers()
            fresh = sorted(set(current) - known)
            if len(current) >= len(old_workers) and old_pid not in current and fresh:
                new_pid = fresh[0]
                break
        if new_pid is None:
            return _failure(
                f"Worker {old_pid} was signalled but no replacement appeared before the "
                f"rolling timeout. Recycled so far: {per_worker}."
            )
        known.add(new_pid)
        entry["newPid"] = new_pid
        entry["secondsToRespawn"] = round(time.time() - step, 1)
        entry["settled"], entry["secondsToSettle"] = _wait_until_settled(
            new_pid, max(SETTLE_MIN_AGE_SECONDS + 2, min(SETTLE_WAIT_SECONDS, deadline - time.time()))
        )
        per_worker.append(entry)
        logger.info(
            "Web worker %s -> %s: respawned in %s s, settle %s s (settled=%s)",
            old_pid, new_pid, entry["secondsToRespawn"], entry["secondsToSettle"], entry["settled"],
        )
    current, _ = _scan_workers()
    still_old = sorted(set(current) & set(old_workers))
    skipped = [w["oldPid"] for w in per_worker if w["newPid"] is None]
    new_workers = sorted(set(current) - set(old_workers))
    if still_old and not skipped:
        return _failure(f"Rolling restart finished but old workers are still running: {still_old}.")
    replaced_after = round(time.time() - started, 1)
    return _finish(
        old_workers, new_workers, sent, per_worker, replaced_after, started,
        time.time() + HTTP_PROBE_TOTAL_SECONDS,
    )


def _restart():
    old_workers, _ = _scan_workers()
    if not old_workers:
        return _failure(
            "No mod_wsgi web worker processes (command line 'cloudbolt', run as this "
            "user under the Apache parent) were found, so nothing was signalled. This "
            "does not look like a CloudBolt web appliance."
        )
    return _restart_rolling(old_workers)


def run(job, *args, **kwargs):
    profile = kwargs.get("profile")
    if profile is None and job is not None:
        profile = job.owner
    if profile is None:
        return _failure(
            "No calling user profile was supplied. Run this tool through the MCP "
            "server or the API as an authenticated user."
        )
    if not profile.is_cbadmin:
        return _failure("Only CloudBolt admins may restart the web workers.")
    if job is None:
        return _failure(
            "This action must run as a job, because restarting the web workers would "
            "kill the request a synchronous run executes in. Untick Synchronous Action "
            "on the MCP Tool Action."
        )

    try:
        inputs = _read_inputs()
        if inputs["dry_run"]:
            workers, parents = _scan_workers()
            message = (
                f"Dry run: {len(workers)} web workers matched, nothing was signalled."
            )
            logger.info(message)
            return {
                "status": "SUCCESS",
                "output_message": message,
                "error_message": "",
                "outputs": {
                    "dryRun": True,
                    "workerPids": workers,
                    "apacheParentPids": parents,
                    "message": message,
                },
            }
        return _restart()
    except Exception as exc:
        logger.error("Web worker restart failed: %s", exc)
        return _failure(f"Could not restart the web workers: {exc}")
