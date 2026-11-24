"""
Installing updates on a server, then scanning it again.

start_job() runs in the web request or in another job. It checks every server and starts the plugin from
vuln_hooks.py (vuln_patch_job.py) with OrchestrationHook.run_as_job. CloudBolt itself starts the same plugin when a
person chooses the Install Updates server action; the dialog's "Restart if needed" input reaches run_patch as the
keyword restart_if_needed.

run_patch() runs inside that job. Servers go one at a time. For each one, _patch_one:
  1. takes the server's start lock and refuses when a scan or another update holds the server (the active
     marker, kind "patch"), then writes its own marker, so the tab shows the update's progress;
  2. remembers the vulnerability counts of the last scan (None when the server was never scanned) and the
     server's boot id;
  3. starts the update on the server as a background worker (vuln_patch.sh) and polls it every POLL_S seconds;
  4. when the server needs a restart and the person allowed it, restarts it and waits until the boot id
     changes, then removes old kernels; without permission the run ends "restart_pending";
  5. scans the server again as a child job (vuln_jobs.start with handover_from: the update's marker stays and
     the scan is let in), then writes the result to the server's state file under "patch".
A step that fails ends that server only; the next server still runs. A server that the update finished but whose
rescan failed is "updated" with a warning, never a failure.

All the Linux-specific work (the guest script, the calls, the parsing) is in the functions whose names start
with _linux_; _patch_one only dispatches. A Windows runner is added the same way later.

A cancel of the job leaves the update running on the server: a package transaction is never stopped midway.
Nothing user-controlled reaches a shell: the script gets a header of shlex-quoted values (a run id checked
against a pattern, a UUID boot id, whole numbers), never string-formatted input.
"""
import os
import re
import shlex
import time

from utilities.events import add_server_event
from utilities.logger import ThreadLogger

from . import vuln_db, vuln_fleet, vuln_hooks, vuln_jobs, vuln_store as store

logger = ThreadLogger(__name__)

SCRIPT_FILE = "vuln_patch.sh"
PATCH_KIND = vuln_jobs.PATCH_KIND

# Seconds between two looks at a running update, and how many looks in a row may fail (a server restarting
# for the update itself, a network blip) before the update counts as lost.
POLL_S = 15
LOST_POLLS = 20
# Seconds between two looks at a server that is restarting.
PROBE_S = 15
REBOOT_DELAY_S = 5
# The most a start, poll, probe or reboot call may take, and the removal of old kernels.
CALL_TIMEOUT = 60
OLD_KERNELS_TIMEOUT = 15 * 60
# Added to the scan time limit when a job waits for the rescan: queueing and the upload.
WAIT_MARGIN_S = 600
DEFAULT_PATCH_TIMEOUT_MIN = 90
DEFAULT_RESTART_WAIT_MIN = 30
MESSAGE_LEN = 300
# CloudBolt cancels any job that runs 8 hours (vuln_fleet.JOB_BUDGET_S); a server is started only when its
# worst case (the update time limit, the restart wait and this margin for the rescan) still ends before 7.5 hours.
JOB_LIMIT_S = int(7.5 * 3600)
RESCAN_MARGIN_MIN = 30

MODES = ("start", "poll", "probe", "reboot", "oldkernels", "cleanup")
KNOWN_KEYS = ("os", "distro", "state", "phase", "updated", "failed", "exit", "removed", "failure", "reason", "note",
              "restart", "restart_reason", "boot_id", "kernel_running", "kernel_newest", "done")
STATES = ("running", "done", "died", "missing")
DISTROS = ("rhel", "ol", "amzn2023", "ubuntu")
MAX_NOTES = 5
MAX_COUNT = 10 ** 6
RUN_ID_RE = re.compile(r"[A-Za-z0-9-]{8,64}")
BOOT_ID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")

ONLY_LINUX = "only Linux servers are supported in this version"
GUEST_KEEPS_RUNNING = "The job was canceled. An update already started on a server keeps running there until it ends."
CALL_FAILED = "The server could not be reached"
PATCHING_OFF = "Installing updates is turned off in Vulnerability Settings."
OLDER_KERNEL = "Restarted, but the server is still running the older kernel."
CHECK_PACKAGE_MANAGER = ("Check the package manager on the server (dnf history, or dpkg --configure -a) before "
                         "trying again.")
STOPPED = "The update stopped before it finished. " + CHECK_PACKAGE_MANAGER


class PatchFailed(Exception):
    """A step of an update failed; the message is safe to store and show. facts carries what was learned before
    the failure (the install's counts), for the record."""

    def __init__(self, message="", facts=None):
        super().__init__(message)
        self.facts = facts or {}


class CallFailed(Exception):
    """A call to the server did not go through (the transport, a timeout, the script could not run); the message
    says why and is safe to store."""


# ---------------------------------------------------------------- small helpers
def parse_restart_flag(value, default=True):
    """The "Restart if needed" input as a bool. It arrives as a bool from the dialog and as text from a default
    set through the API or MCP, or from a dropdown ("True", "false", "0"); anything else is the default."""
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    text = str(value).strip().lower()
    if text in ("true", "1", "yes", "y", "on", "t"):
        return True
    if text in ("false", "0", "no", "n", "off", "f"):
        return False
    return default


def _sleep(seconds):
    time.sleep(seconds)


def _clock():
    """Seconds on a clock that only moves forward; the job measures its time limits with it."""
    return time.monotonic()


def _progress(job, message):
    vuln_fleet._progress(job, message)


def _why(code):
    """One phrase for an eligibility code."""
    return vuln_fleet.REASONS.get(code, code)


def _minutes(cfg, key, default):
    try:
        return max(1, int(cfg.get(key) or default))
    except (TypeError, ValueError):
        return default


def eligibility(server):
    """"" when updates may be installed on the server now, else a reason code (vuln_fleet.REASONS): the codes
    of vuln_fleet.eligibility, unsupported_os for anything but Linux, and the scanner's own (not_set_up,
    no_site_url), because a rescan that cannot run is not allowed to follow an install. No remote call."""
    try:
        if vuln_jobs.is_windows(server) or not vuln_jobs.is_linux(server):
            return "unsupported_os"
        code = vuln_fleet.eligibility(server, store.get_settings()["visibility"])
        if code:
            return code
        return vuln_fleet.prerequisites(vuln_fleet.default_base_url())[0]
    except Exception:
        logger.exception("Vulnerabilities: could not check whether server %s may be updated", getattr(server, "id", ""))
        return "unknown"


def patching_enabled():
    """True when the "patching" setting is on. An unreadable setting counts as off."""
    try:
        return bool(store.get_settings().get("patching"))
    except Exception:
        logger.exception("Vulnerabilities: could not read the patching setting")
        return False


def record_patch(server_id, record):
    """Write the server's last update (the shape in the module docstring) to its state file, next to the last
    scan. vuln_store keeps keys it does not know."""
    store._update(server_id, lambda state: state.__setitem__("patch", record))


def _summary(server_id):
    """{"vulns", "vuln_fixable"} from the findings store, or None when the server has no scan."""
    try:
        row = vuln_db.server_summary(server_id)
    except Exception:
        logger.exception("Vulnerabilities: could not read the summary of server %s", server_id)
        return None
    if not isinstance(row, dict):
        return None
    return {"vulns": row.get("vulns"), "vuln_fixable": row.get("vuln_fixable")}


# ---------------------------------------------------------------- start
def start_job(owner, server=None, restart_if_needed=True, parent_job=None, servers=None):
    """Check the server (or the list given as servers) and start the update job. owner is the requesting
    UserProfile. Returns the Job, or a vuln_jobs.Refusal for the first server that cannot be updated. No permission
    check: the caller has made it."""
    if not patching_enabled():
        return vuln_jobs.Refusal(PATCHING_OFF, 403)
    targets = list(servers) if servers is not None else ([server] if server is not None else [])
    if not targets:
        return vuln_jobs.Refusal("No server was given.")
    base_url = vuln_fleet.default_base_url()
    for target in targets:
        code = eligibility(target)
        if code:
            return vuln_jobs.Refusal("{} cannot be updated: {}.".format(target.hostname, _why(code)), 409)
        refusal = vuln_jobs.precheck(target, base_url)
        if refusal is not None:
            return refusal
    created = vuln_hooks.patch_hook().run_as_job(
        owner=owner,
        servers=targets,
        parent_job=parent_job,
        context={"restart_if_needed": bool(parse_restart_flag(restart_if_needed))},
    )
    if not created:
        return vuln_jobs.Refusal("CloudBolt did not start the job; see the application log.", 500)
    job = created[0]
    # From here the job is queued: nothing below may turn into an error.
    try:
        job.label = "Install updates: {}".format(targets[0].hostname if len(targets) == 1
                                                 else "{} servers".format(len(targets)))
        job.save(update_fields=["label"])
        for target in targets:
            target.jobs.add(job)
    except Exception:
        logger.exception("Vulnerabilities: could not label or link job %s", job.id)
    return job


# ---------------------------------------------------------------- the guest script
def _linux_build_script(mode, run_id, boot_id=""):
    """The script to run: the sh launcher around a header of quoted assignments and vuln_patch.sh. The run id
    and the boot id are checked here; the script checks them again."""
    if mode not in MODES:
        raise PatchFailed("The update script has no mode {}.".format(mode))
    if not RUN_ID_RE.fullmatch(str(run_id or "")):
        raise PatchFailed("The run id has characters that are not allowed.")
    if boot_id and not BOOT_ID_RE.fullmatch(str(boot_id)):
        raise PatchFailed("The boot id has characters that are not allowed.")
    values = [("MODE", mode), ("RUN_ID", run_id), ("BOOT_ID", boot_id or ""), ("REBOOT_DELAY_S", str(REBOOT_DELAY_S))]
    header = "".join("{}={}\n".format(name, shlex.quote(str(value))) for name, value in values)
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), SCRIPT_FILE)
    with open(path, encoding="utf-8", newline="") as handle:
        body = handle.read().replace("\r\n", "\n")
    return vuln_jobs._launcher("#!/bin/bash\n" + header + body)


def _linux_parse(text):
    """The CBVULN|key|value lines of the script's output as one dict. Only known keys are read; a later line
    wins; text is cleaned and capped, counts are whole numbers within bounds (else None), state, distro and
    restart must be one of their listed values, the boot id a UUID."""
    result = {
        "os": "", "distro": "", "state": "", "phase": "", "updated": None, "failed": None, "exit": None,
        "removed": None, "failure": "", "reason": "", "notes": [], "restart": "", "restart_reason": "",
        "boot_id": "", "kernel_running": "", "kernel_newest": "", "done": False,
    }
    for raw in vuln_jobs._bounded(text).splitlines():
        line = raw.strip()
        if not line.startswith("CBVULN|"):
            continue
        parts = line.split("|", 2)
        if len(parts) < 3 or parts[1] not in KNOWN_KEYS:
            continue
        key, value = parts[1], parts[2].strip()
        if key in ("updated", "failed", "removed"):
            result[key] = vuln_jobs._int(value, MAX_COUNT)
        elif key == "exit":
            result[key] = vuln_jobs._int(value, 1000)
        elif key == "state":
            result[key] = value if value in STATES else ""
        elif key == "distro":
            result[key] = value if value in DISTROS else ""
        elif key == "restart":
            result[key] = value if value in ("0", "1", "unknown") else ""
        elif key == "boot_id":
            result[key] = value if BOOT_ID_RE.fullmatch(value) else ""
        elif key == "note":
            if len(result["notes"]) < MAX_NOTES:
                result["notes"].append(vuln_jobs.clean(value, MESSAGE_LEN))
        elif key == "done":
            result["done"] = True
        elif key in ("failure", "reason"):
            result[key] = vuln_jobs.clean(value, MESSAGE_LEN)
        else:
            result[key] = vuln_jobs.clean(value, 120)
    return result


parse_patch_output = _linux_parse


def _linux_call(server, mode, run_id, boot_id="", timeout=CALL_TIMEOUT, job=None):
    """Run one mode of the script on the server and return the parsed output. Raises CallFailed when the call
    does not go through or the output is cut off before the script's last line."""
    script = _linux_build_script(mode, run_id, boot_id)
    try:
        output = vuln_jobs._execute(server, script, timeout, job)
    except vuln_jobs.TransportError as exc:
        raise CallFailed(vuln_jobs.clean(str(exc), MESSAGE_LEN))
    except NotImplementedError:
        raise CallFailed("This server's resource handler cannot run scripts on servers.")
    except Exception as exc:
        # A script that exits non-zero or times out raises, with its output on the exception.
        if hasattr(exc, "output"):
            raise CallFailed(vuln_jobs.clean(str(exc), MESSAGE_LEN) or CALL_FAILED)
        logger.exception("Vulnerabilities: calling the update script on server %s failed", server.id)
        raise CallFailed("The script could not be run on the server. The CloudBolt job log has the details.")
    if isinstance(output, bytes):
        output = output.decode("utf-8", errors="replace")
    parsed = _linux_parse(str(output or ""))
    if not parsed["done"] and not parsed["reason"]:
        raise CallFailed("The server's answer was cut off.")
    return parsed


def _linux_cleanup(server, run_id):
    """Remove the run folder on the server. Best effort: a server that cannot be reached keeps it, and the next
    update removes folders older than three days."""
    try:
        _linux_call(server, "cleanup", run_id)
    except Exception:
        logger.info("Vulnerabilities: the run folder of the update on server %s was not removed", server.id)


# ---------------------------------------------------------------- the update on one server
def _linux_wait_for_install(job, server, run_id, limit_s):
    """Poll the update until the worker is done. Returns the last poll's parsed output. Raises PatchFailed when
    the worker died or is gone, when CloudBolt lost contact for LOST_POLLS looks in a row, or at the limit."""
    began = _clock()
    lost = 0
    while True:
        _sleep(POLL_S)
        if _clock() - began > limit_s:
            raise PatchFailed("The update did not finish within {} minutes and may still be running on the server; "
                              "another update there is refused until it ends. {}".format(
                                  int(limit_s // 60), CHECK_PACKAGE_MANAGER))
        try:
            polled = _linux_call(server, "poll", run_id, job=job)
        except CallFailed as exc:
            polled, problem = None, str(exc)
        else:
            problem = ""
            if polled["state"] not in STATES:
                polled, problem = None, "the server's answer had no state"
        if polled is None:
            lost += 1
            if lost >= LOST_POLLS:
                raise PatchFailed("CloudBolt lost contact with the server for {} checks in a row ({}). {}".format(
                    LOST_POLLS, problem, CHECK_PACKAGE_MANAGER))
            continue
        lost = 0
        if polled["phase"]:
            _progress(job, "{}: {}".format(server.hostname, polled["phase"]))
        if polled["state"] == "done":
            return polled
        if polled["state"] in ("died", "missing"):
            raise PatchFailed(STOPPED, _facts_of(polled))


def _facts_of(polled):
    return {"updated": polled["updated"], "failed": polled["failed"]}


def _linux_restart(job, server, run_id, old_boot_id, wait_s):
    """Restart the server and wait until it is back on a new boot id. Returns the probe that showed the new boot
    id (it carries the running and the newest kernel). Raises PatchFailed when the restart cannot be scheduled or
    the server is not back within wait_s seconds. Any error while probing means not back yet."""
    _progress(job, "{}: restarting".format(server.hostname))
    try:
        answer = _linux_call(server, "reboot", run_id, boot_id=old_boot_id, job=job)
    except CallFailed as exc:
        raise PatchFailed("The restart could not be scheduled: {}".format(exc))
    if answer["reason"]:
        raise PatchFailed("The restart could not be scheduled: {}".format(answer["reason"]))
    if answer["boot_id"] and answer["boot_id"] != old_boot_id:
        # Already restarted: one more look for the kernels.
        try:
            return _linux_call(server, "probe", run_id, job=job)
        except CallFailed:
            return answer
    began = _clock()
    while True:
        _sleep(PROBE_S)
        if _clock() - began > wait_s:
            raise PatchFailed("The server did not come back within {} minutes of the restart.".format(int(wait_s // 60)))
        try:
            probed = _linux_call(server, "probe", run_id, job=job)
        except Exception:
            continue
        if probed["boot_id"] and probed["boot_id"] != old_boot_id:
            return probed


def _linux_patch(job, server, run_id, restart, cfg):
    """Steps 2 to 4 on a Linux server: the boot id, the install, the restart, the old kernels. Returns the facts
    {"updated", "failed", "restart_needed", "restarted", "kernels_removed", "notes"}; raises PatchFailed."""
    notes = []
    probed = _linux_call(server, "probe", run_id, job=job)
    old_boot_id = probed["boot_id"]
    if not old_boot_id:
        raise PatchFailed("The server's boot id could not be read, so nothing was changed.")
    _progress(job, "{}: starting the update".format(server.hostname))
    started = _linux_call(server, "start", run_id, job=job)
    if started["reason"]:
        raise PatchFailed("The update did not start: {}".format(started["reason"]))
    if started["state"] != "running":
        raise PatchFailed("The update did not start on the server.")
    limit = _minutes(cfg, "patch_timeout_min", DEFAULT_PATCH_TIMEOUT_MIN) * 60
    polled = _linux_wait_for_install(job, server, run_id, limit)
    facts = {"updated": polled["updated"], "failed": polled["failed"], "restart_needed": False,
             "restarted": False, "kernels_removed": None, "notes": notes}
    if polled["exit"]:
        raise PatchFailed("The update failed on the server (exit code {}): {}".format(
            polled["exit"], polled["failure"] or "no reason was reported."), facts)
    needed = polled["restart"] == "1"
    facts["restart_needed"] = needed
    if polled["restart"] == "unknown":
        notes.append("Whether a restart is needed could not be told.")
    if not needed:
        return facts
    if not restart:
        return facts
    try:
        back = _linux_restart(job, server, run_id, old_boot_id,
                              _minutes(cfg, "restart_wait_min", DEFAULT_RESTART_WAIT_MIN) * 60)
    except PatchFailed as exc:
        raise PatchFailed(str(exc), facts)
    facts["restarted"] = True
    if back["kernel_running"] and back["kernel_newest"] and back["kernel_running"] != back["kernel_newest"]:
        # Old kernels may still go: the package manager never removes the running kernel.
        notes.append(OLDER_KERNEL)
    # Old kernels go only now, with the new one proven to boot. A failure here never fails the update.
    _progress(job, "{}: removing old kernels".format(server.hostname))
    try:
        cleaned = _linux_call(server, "oldkernels", run_id, timeout=OLD_KERNELS_TIMEOUT, job=job)
        if cleaned["exit"] or cleaned["reason"]:
            notes.append("Old kernels were not removed: {}".format(cleaned["failure"] or cleaned["reason"]
                                                                  or "the package manager failed."))
        else:
            facts["kernels_removed"] = cleaned["removed"]
    except CallFailed as exc:
        notes.append("Old kernels were not removed: {}".format(exc))
    return facts


def _rescan(job, server):
    """Scan the server again as a child of the update job, which holds the active marker. Returns (summary, note):
    the vulnerability counts after the scan, or None and a sentence on why there are none."""
    base_url = vuln_fleet.default_base_url()
    _progress(job, "{}: scanning again".format(server.hostname))
    try:
        started = vuln_jobs.start(getattr(job, "owner", None), server, base_url, parent_job=job, handover_from=job.id)
    except Exception:
        logger.exception("Vulnerabilities: the rescan of server %s could not start", server.id)
        return None, "The rescan could not start."
    if vuln_fleet._started_id(started) is None:
        return None, "The rescan did not start: {}".format(vuln_jobs.clean(getattr(started, "message", ""), 200))
    wait = _int_setting("scan_timeout_min", 20) * 60 + WAIT_MARGIN_S
    # Imported here, not at the top: views.py imports this module for the tab, and a page load needs no job code.
    from utilities.exceptions import TimeoutException
    try:
        # Polls the job every five seconds; cancels it and raises when the time is up (jobs/models.py).
        ended = started.wait_for_completion(timeout=wait)
    except TimeoutException:
        return None, "The rescan did not finish within {} minutes.".format(wait // 60)
    if getattr(ended, "status", "") not in ("SUCCESS", "WARNING"):
        return None, "The rescan failed (job {}).".format(started.id)
    summary = _summary(server.id)
    if summary is None:
        return None, "The rescan finished but its result could not be read."
    return summary, ""


def _int_setting(key, default):
    try:
        return max(1, int(store.get_settings().get(key) or default))
    except Exception:
        return default


def _sentence(host, record):
    """The one line a server gets in the job output."""
    if record["status"] == "failed":
        return "{}: failed. {}".format(host, record["message"])
    parts = ["{} package{} installed or upgraded".format(
        record["updated"], "" if record["updated"] == 1 else "s") if record["updated"] is not None
        else "updates installed"]
    if record["restarted"]:
        parts.append("restarted")
    elif record["restart_needed"]:
        parts.append("a restart is still needed")
    if record["kernels_removed"]:
        parts.append("{} old kernel package{} removed".format(
            record["kernels_removed"], "" if record["kernels_removed"] == 1 else "s"))
    before, after = record["before"], record["after"]
    if before is not None and after is not None:
        parts.append("known vulnerabilities {} to {}".format(before["vulns"], after["vulns"]))
    text = "{}: {}.".format(host, ", ".join(parts))
    if record["message"]:
        text += " " + record["message"]
    return text


def _record(job, server, status, facts, before, after, began, message):
    owner = getattr(job, "owner", None)
    return {
        "at": store.stamp(),
        "by": vuln_jobs._requested_by(owner),
        "job_id": job.id,
        "status": status,
        "before": before,
        "after": after,
        "updated": facts.get("updated"),
        "failed": facts.get("failed"),
        "restarted": bool(facts.get("restarted")),
        "restart_needed": bool(facts.get("restart_needed")),
        "kernels_removed": facts.get("kernels_removed"),
        "duration_s": max(0, int(_clock() - began)),
        "message": vuln_jobs.clean(message, MESSAGE_LEN),
    }


def _patch_one(job, server, restart, cfg):
    """Update one server and scan it again. Returns the record written to the server's state ("patch") plus
    "hostname" and "warning" (the run should end with a warning). Raises PatchFailed only when the server was not
    touched (not Linux, held by another job, no longer eligible); a failure after that is a record with
    status "failed"."""
    if not vuln_jobs.is_linux(server):
        raise PatchFailed(ONLY_LINUX)
    began = _clock()
    marker = {
        "job_id": job.id, "scan_id": "patch-" + store.new_scan_id(), "started": store.stamp(),
        "requested_by": vuln_jobs._requested_by(getattr(job, "owner", None)), "kind": PATCH_KIND,
    }
    run_id = marker["scan_id"]
    try:
        with store.start_lock(server.id):
            running = vuln_jobs.active_job(server.id)
            if running is not None:
                raise PatchFailed("Job {} is still running on this server; wait for it to finish.".format(running[1].id))
            code = eligibility(server)
            if code:
                raise PatchFailed("The server cannot be updated: {}.".format(_why(code)))
            store.set_active(server.id, marker)
    except store.StoreError:
        logger.exception("Vulnerabilities: state problem starting an update on server %s", server.id)
        raise PatchFailed(vuln_jobs.STATE_ERROR)
    try:
        before = _summary(server.id)
        warning = False
        facts = {}
        after = None
        try:
            facts = _linux_patch(job, server, run_id, restart, cfg)
        except (PatchFailed, CallFailed) as exc:
            facts = getattr(exc, "facts", None) or {}
            record = _record(job, server, "failed", facts, before, None, began, str(exc))
            _finish(job, server, record)
            return dict(record, hostname=server.hostname, warning=True)
        notes = list(facts.get("notes") or [])
        after, problem = _rescan(job, server)
        if after is None:
            warning = True
            notes.append("The updates were installed and the rescan failed: {}".format(problem))
        status = "updated"
        if facts["restart_needed"] and not facts["restarted"]:
            status = "restart_pending"
            warning = True
            notes.append("A restart is needed to finish; the server was not restarted.")
        warning = warning or bool(notes)
        record = _record(job, server, status, facts, before, after, began, " ".join(notes))
        _finish(job, server, record)
        return dict(record, hostname=server.hostname, warning=warning)
    finally:
        try:
            store.clear_active(server.id, marker["scan_id"])
        except Exception:
            logger.exception("Vulnerabilities: could not clear the active marker for server %s", server.id)
        _linux_cleanup(server, run_id)


def _finish(job, server, record):
    """Write the record and the server's history event. Neither may end the run."""
    try:
        record_patch(server.id, record)
    except Exception:
        logger.exception("Vulnerabilities: could not record the update of server %s", server.id)
    try:
        # INFO, not MODIFICATION: add_server_event drops a MODIFICATION for a server with no creation event.
        add_server_event("INFO", server, "Vulnerability update {}: {}".format(
            record["status"].replace("_", " "), record["message"] or "no problems."), job=job)
    except Exception:
        logger.exception("Vulnerabilities: could not add the server event")


# ---------------------------------------------------------------- the job
def run_patch(job, servers=None, server=None, restart_if_needed=None, profile=None, **kwargs):
    """Plugin entry point (via vuln_patch_job.py). Returns the 4-tuple CloudBolt plugins use: status, output,
    errors, outputs. The servers come from the job (one or more). A server that fails does not stop the others;
    no server is started whose worst case would not end before JOB_LIMIT_S, or once the job was asked to stop."""
    if not patching_enabled():
        return "FAILURE", "", PATCHING_OFF, {}
    targets = list(servers) if servers else ([server] if server is not None else [])
    if not targets:
        return "FAILURE", "", "No server was given.", {}
    restart = parse_restart_flag(restart_if_needed)
    try:
        cfg = store.get_settings()
    except Exception:
        cfg = {}
    counts = {"updated": 0, "restart_pending": 0, "failed": 0, "skipped": 0}
    results = {}
    lines = []
    warned = False
    began = _clock()
    stopped = out_of_time = False
    worst_s = (_minutes(cfg, "patch_timeout_min", DEFAULT_PATCH_TIMEOUT_MIN)
               + _minutes(cfg, "restart_wait_min", DEFAULT_RESTART_WAIT_MIN) + RESCAN_MARGIN_MIN) * 60
    try:
        for position, target in enumerate(targets):
            if vuln_fleet._cancelled(job):
                stopped = True
            elif _clock() - began + worst_s > JOB_LIMIT_S:
                out_of_time = True
            if stopped or out_of_time:
                counts["skipped"] += len(targets) - position
                break
            try:
                done = _patch_one(job, target, restart, cfg)
            except PatchFailed as exc:
                done = {"status": "failed", "message": vuln_jobs.clean(str(exc), MESSAGE_LEN), "hostname": target.hostname,
                        "warning": True, "updated": None, "failed": None, "restarted": False, "restart_needed": False,
                        "kernels_removed": None, "before": None, "after": None}
            except Exception as exc:
                logger.exception("Vulnerabilities: the update of server %s failed", getattr(target, "id", ""))
                done = {"status": "failed", "message": "The update failed: {}".format(vuln_jobs.clean(str(exc), 200)),
                        "hostname": target.hostname, "warning": True, "updated": None, "failed": None,
                        "restarted": False, "restart_needed": False, "kernels_removed": None, "before": None,
                        "after": None}
            warned = warned or bool(done.pop("warning", False))
            counts[done["status"]] += 1
            lines.append(_sentence(target.hostname, done))
            results[str(target.id)] = done
    except BaseException:
        # A cancel (CancelJobException) ends the loop here; the marker was cleared by _patch_one's own finally.
        _progress(job, GUEST_KEEPS_RUNNING)
        raise
    if stopped:
        lines.append("Stopped early because the job was canceled. {} not started. {}".format(
            counts["skipped"], GUEST_KEEPS_RUNNING))
    if out_of_time:
        lines.append("{} server{} not started: one more update could take up to {} minutes, and the job has to end "
                     "before CloudBolt's 8 hour limit. Run the update again for them.".format(
                         counts["skipped"], " was" if counts["skipped"] == 1 else "s were", worst_s // 60))
    outputs = {"servers": results, "counts": counts}
    output = "\n".join(lines)
    problem = ""
    if counts["failed"]:
        first = next(item for item in results.values() if item["status"] == "failed")
        problem = first["message"] if len(targets) == 1 else "{} of {} servers failed. First failure ({}): {}".format(
            counts["failed"], len(targets), first["hostname"], first["message"])
    if counts["failed"] and not (counts["updated"] or counts["restart_pending"]):
        return "FAILURE", output, problem, outputs
    if warned or counts["failed"] or counts["skipped"]:
        return "WARNING", output, problem, outputs
    return "SUCCESS", output, "", outputs
