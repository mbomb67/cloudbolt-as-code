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
  3. starts the update on the server as a background worker (vuln_patch.sh, or vuln_patch_windows.ps1 as a
     SYSTEM scheduled task through vuln_wintask.py) and polls it every POLL_S (WIN_POLL_S) seconds;
  4. when the server needs a restart and the person allowed it, restarts it and waits until the boot id
     changes, then (Linux) removes old kernels; without permission the run ends "restart_pending";
  5. scans the server again as a child job (vuln_jobs.start with handover_from: the update's marker stays and
     the scan is let in), then writes the result to the server's state file under "patch".
A step that fails ends that server only; the next server still runs. A server that the update finished but whose
rescan failed is "updated" with a warning, never a failure.

All the Linux-specific work (the guest script, the calls, the parsing) is in the functions whose names start
with _linux_, all the Windows-specific work in those that start with _windows_; _patch_one only dispatches on the
server's family. Windows Server 2022 and 2025 are supported (the builds of vuln_msrc.PRODUCTS, because the rescan
cannot match any other), and only when no Configuration Manager client (the CcmExec service) manages the server.

A cancel of the job leaves the update running on the server: a package transaction is never stopped midway.
Nothing user-controlled reaches a shell: the Linux script gets a header of shlex-quoted values (a run id checked
against a pattern, a UUID boot id, whole numbers), never string-formatted input; the Windows scripts get only a
validated run id and whole numbers.
"""
import os
import re
import shlex
import time
from datetime import datetime

from utilities.events import add_server_event
from utilities.logger import ThreadLogger

from . import vuln_db, vuln_fleet, vuln_hooks, vuln_jobs, vuln_msrc, vuln_store as store, vuln_wintask

logger = ThreadLogger(__name__)

SCRIPT_FILE = "vuln_patch.sh"
WIN_SCRIPT_FILE = "vuln_patch_windows.ps1"
PATCH_KIND = vuln_jobs.PATCH_KIND

# Seconds between two looks at a running update, and how many looks in a row may fail (a server restarting
# for the update itself, a network blip) before the update counts as lost.
POLL_S = 15
LOST_POLLS = 20
# Seconds between two looks at a server that is restarting.
PROBE_S = 15
# Seconds the server waits before it restarts itself, on both systems: AWS Systems Manager reads a command's result
# a fixed 5 seconds after it is sent, so a shorter delay can take the server down before the reply is read.
REBOOT_DELAY_S = 20
# The Windows engine: seconds between two polls of the status file and between two probes of a restarting server
# (a call through Systems Manager or Run Command adds 5 to 60 seconds), the restart delay, and how far apart two
# boot times may be and still be the same boot (the value is derived from the clock and can move by seconds).
WIN_POLL_S = 20
WIN_PROBE_S = 20
WIN_REBOOT_DELAY_S = 20
BOOT_TOLERANCE_S = 10
# A moved boot id counts as a restart only when the server also reports an uptime shorter than the time since the
# job read the old boot id plus this many seconds: a clock step moves the boot id but not the uptime.
UPTIME_SLACK_S = 120
# Successful probes in a row that still show the old boot id before a reboot call that raised is sent again, and
# before the restart is given up as not started.
RESTART_UNCHANGED_PROBES = 3
# The most a start, poll, probe or reboot call may take, and the removal of old kernels.
CALL_TIMEOUT = 60
OLD_KERNELS_TIMEOUT = 15 * 60
# Added to the scan time limit when a job waits for the rescan: queueing and the upload.
WAIT_MARGIN_S = 600
DEFAULT_PATCH_TIMEOUT_MIN = 90
DEFAULT_RESTART_WAIT_MIN = 60
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
# A Windows boot id: the last boot time in UTC, yyyyMMddHHmmss.
BOOT_RE = re.compile(r"[0-9]{14}")
BOOT_FORMAT = "%Y%m%d%H%M%S"
WIN_KEYS = ("phase", "found", "skipped", "item", "updated", "failed", "result", "hresult", "restart",
            "restart_before", "more", "failure", "reason", "note", "done")
HRESULT_RE = re.compile(r"0x[0-9A-Fa-f]{8}")
KB_RE = re.compile(r"KB[0-9]{4,9}")
MAX_ITEMS = 50
MAX_FAILED_LISTED = 5

ONLY_SUPPORTED = "only Linux and Windows Server 2022 and 2025 servers are supported"
ONLY_SERVER = "Only Windows Server 2022 and 2025 are supported."
RESTART_NOT_STARTED = "The restart could not be started."
OLD_KERNELS_KEPT = "Old kernels were kept."
GUEST_KEEPS_RUNNING = "The job was canceled. An update already started on a server keeps running there until it ends."
CALL_FAILED = "The server could not be reached"
PATCHING_OFF = "Installing updates is turned off in Vulnerability Settings."
OLDER_KERNEL = "Restarted, but the server is still running the older kernel."
CHECK_PACKAGE_MANAGER = ("Check the package manager on the server (dnf history, or dpkg --configure -a) before "
                         "trying again.")
STOPPED = "The update stopped before it finished. " + CHECK_PACKAGE_MANAGER
CHECK_WINDOWS_UPDATE = "Check Windows Update on the server before trying again."
STOPPED_WINDOWS = "The update stopped before it finished. " + CHECK_WINDOWS_UPDATE
RESTARTED_WINDOWS = "The server restarted during the update and the update reported no result."
CCM_REFUSED = "This server is patched by Configuration Manager."


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


def eligibility(server, problems=None):
    """"" when updates may be installed on the server now, else a reason code (vuln_fleet.REASONS): the codes
    of vuln_fleet.eligibility, unsupported_os for anything but Linux or Windows, and what a rescan of this kind of
    server needs (not_set_up, no_site_url, no_windows_data), because a rescan that cannot run is not allowed to
    follow an install. problems is vuln_fleet.prerequisites(), when the caller has it. No remote call: the Windows
    build and the Configuration Manager check are made on the server, when the update starts."""
    try:
        if not (vuln_jobs.is_linux(server) or vuln_jobs.is_windows(server)):
            return "unsupported_os"
        code = vuln_fleet.eligibility(server, store.get_settings()["visibility"])
        if code:
            return code
        if problems is None:
            problems = vuln_fleet.prerequisites(vuln_fleet.default_base_url())
        return vuln_fleet.prerequisite_for(server, problems)
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
    problems = vuln_fleet.prerequisites(base_url)
    for target in targets:
        code = eligibility(target, problems)
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
    {"updated", "failed", "restart_needed", "restarted", "kernels_removed", "notes" (warnings), "info" (plain
    sentences for the message), "no_updates"}; raises PatchFailed."""
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
             "restarted": False, "kernels_removed": None, "notes": notes, "info": [], "no_updates": False}
    if polled["exit"]:
        raise PatchFailed("The update failed on the server (exit code {}): {}".format(
            polled["exit"], polled["failure"] or "no reason was reported."), facts)
    needed = polled["restart"] == "1"
    facts["restart_needed"] = needed
    if polled["restart"] == "unknown":
        notes.append("Whether a restart is needed could not be told.")
    back = None
    if needed:
        if not restart:
            # A restart is pending: old kernels stay until it is done.
            return facts
        try:
            back = _linux_restart(job, server, run_id, old_boot_id,
                                  _minutes(cfg, "restart_wait_min", DEFAULT_RESTART_WAIT_MIN) * 60)
        except PatchFailed as exc:
            raise PatchFailed(str(exc), facts)
        facts["restarted"] = True
    if back is not None and _older_kernel(back):
        # Old kernels may still go: the package manager never removes the running kernel.
        notes.append(OLDER_KERNEL)
    # Old kernels go whenever the setting is on and no restart is pending (none was needed, or it is done and
    # confirmed); the script itself refuses unless the server runs its newest kernel. A failure here never fails
    # the update.
    if not _flag(cfg, "remove_old_kernels", True):
        if facts["restarted"]:
            facts["info"].append(OLD_KERNELS_KEPT)
    else:
        _linux_old_kernels(job, server, run_id, facts)
    facts["no_updates"] = bool(polled["updated"] == 0 and not polled["failed"] and not needed
                               and not facts["kernels_removed"])
    return facts


def _flag(cfg, key, default):
    """A boolean setting; a missing or unreadable one is default."""
    try:
        return parse_restart_flag(cfg.get(key), default)
    except Exception:
        return default


def _older_kernel(probed):
    return bool(probed.get("kernel_running") and probed.get("kernel_newest")
                and probed["kernel_running"] != probed["kernel_newest"])


def _linux_old_kernels(job, server, run_id, facts):
    """The old-kernel step. kernels_removed is the guest's own count; a note the guest returns when it skipped the
    cleanup goes into the message (a warning only when the server is not running its newest kernel)."""
    notes, info = facts["notes"], facts["info"]
    _progress(job, "{}: removing old kernels".format(server.hostname))
    try:
        cleaned = _linux_call(server, "oldkernels", run_id, timeout=OLD_KERNELS_TIMEOUT, job=job)
    except CallFailed as exc:
        notes.append("Old kernels were not removed: {}".format(exc))
        return
    if cleaned["exit"] or cleaned["reason"]:
        notes.append("Old kernels were not removed: {}".format(cleaned["failure"] or cleaned["reason"]
                                                              or "the package manager failed."))
        return
    facts["kernels_removed"] = cleaned["removed"]
    if _older_kernel(cleaned):
        if OLDER_KERNEL not in notes:
            notes.append(OLDER_KERNEL)
    else:
        info.extend(cleaned["notes"])


# ---------------------------------------------------------------- Windows: the scripts and the calls
# Two small scripts run in the remote session itself (the update runs in a SYSTEM scheduled task,
# vuln_patch_windows.ps1, through vuln_wintask.py). They hold no comments: every character counts against the
# WinRM command-line limit (vuln_wintask.script_fits). Nothing user-controlled goes into either of them.
#   PROBE reads the build (the registry value CurrentBuild, as vuln_scan_windows.ps1 does), the last boot time of the
#   operating system, its product type and its uptime in seconds (now minus the boot time, both UTC), and whether the
#   Configuration Manager client service CcmExec exists. The product type, the uptime and the build travel in one
#   value, "build;producttype;uptime", because vuln_wintask only passes on the transport keys it knows. The boot time is
#   Win32_OperatingSystem.LastBootUpTime, "Date and time the operating system was last restarted"
#   (https://learn.microsoft.com/en-us/windows/win32/cimwin32prov/win32-operatingsystem), printed in UTC as
#   yyyyMMddHHmmss: the boot id. It is derived from the clock, so two reads of one boot can differ by seconds
#   (BOOT_TOLERANCE_S); a real restart moves it by the uptime. Whether the platform ever moves it by more than a few
#   seconds without a restart is not documented: a first live run has to confirm it.
#   REBOOT runs shutdown.exe /r /t <delay> and waits for shutdown.exe itself, which returns at once
#   (https://learn.microsoft.com/en-us/windows-server/administration/windows-commands/shutdown: /r restarts the
#   computer after shutdown, /t is the time-out in seconds, and a time-out above 0 implies /f). Start-Process with
#   -Wait -PassThru -WindowStyle Hidden writes nothing to stderr, which VMware Tools would count as a failure.
PROBE = (
    vuln_wintask.PRELUDE
    + "try{\n"
    + "$o=Get-ItemProperty 'HKLM:\\SOFTWARE\\Microsoft\\Windows NT\\CurrentVersion'\n"
    + "$w=Get-CimInstance Win32_OperatingSystem\n"
    + "$b=$w.LastBootUpTime.ToUniversalTime()\n"
    + "'CBWT|build|'+$o.CurrentBuild+';'+$w.ProductType+';'+[int]([DateTime]::UtcNow-$b).TotalSeconds\n"
    + "'CBWT|boot|'+$b.ToString('yyyyMMddHHmmss',[Globalization.CultureInfo]::InvariantCulture)\n"
    + "'CBWT|ccm|'+[int][bool](Get-Service -Name CcmExec -ErrorAction SilentlyContinue)\n"
    + "}catch{'CBWT|terr|probe: '+$_.Exception.Message}\n"
    + "exit 0\n"
)
REBOOT = (
    vuln_wintask.PRELUDE
    + "try{\n"
    + "$p=Start-Process -FilePath \"$env:SystemRoot\\System32\\shutdown.exe\" -ArgumentList '/r','/t','@@DELAY@@' "
    + "-Wait -PassThru -WindowStyle Hidden\n"
    + "'CBWT|reboot|'+$p.ExitCode\n"
    + "}catch{'CBWT|terr|reboot: '+$_.Exception.Message}\n"
    + "exit 0\n"
)


def _reboot_script():
    return REBOOT.replace("@@DELAY@@", str(int(WIN_REBOOT_DELAY_S)))


def boot_changed(old, new):
    """True when two Windows boot ids (UTC yyyyMMddHHmmss) are more than BOOT_TOLERANCE_S seconds apart. A value
    that is not a boot id counts as unchanged."""
    try:
        before = datetime.strptime(str(old), BOOT_FORMAT)
        after = datetime.strptime(str(new), BOOT_FORMAT)
    except (TypeError, ValueError):
        return False
    return abs((after - before).total_seconds()) > BOOT_TOLERANCE_S


def _windows_call(function, *args):
    """Call one of vuln_wintask's functions, turning its errors into CallFailed (message safe to store)."""
    try:
        return function(*args)
    except vuln_wintask.WinTaskError as exc:
        raise CallFailed(vuln_jobs.clean(str(exc), MESSAGE_LEN) or CALL_FAILED)
    except NotImplementedError:
        raise CallFailed("This server's resource handler cannot run scripts on servers.")


def _windows_probe(server, job=None):
    """{"boot", "build", "ccm"} read from the server: boot is the boot id ("" when unreadable), build the Windows
    build number, ccm "1" when the Configuration Manager client service exists. Raises CallFailed."""
    reply = _windows_call(vuln_wintask.inline, server, PROBE)
    if reply.get("terr"):
        raise CallFailed(vuln_jobs.clean(reply["terr"], MESSAGE_LEN))
    return _windows_probe_reply(reply)


def _windows_probe_reply(reply):
    """The probe's transport values as {"boot", "build", "ccm", "product", "uptime"}. Raises CallFailed when the
    answer is empty."""
    boot = str(reply.get("boot", "")).strip()
    packed = [part.strip() for part in str(reply.get("build", "")).split(";")] + ["", ""]
    build, product, uptime = packed[0], packed[1], packed[2]
    if not (boot or build or reply.get("ccm")):
        raise CallFailed("The server's answer was cut off.")
    return {
        "boot": boot if BOOT_RE.fullmatch(boot) else "",
        "build": build if vuln_jobs.BUILD_RE.fullmatch(build) else "",
        "ccm": "1" if str(reply.get("ccm", "")).strip() == "1" else "0",
        "product": product if product in ("1", "2", "3") else "",
        "uptime": vuln_jobs._int(uptime, 10 ** 9) if uptime.isdigit() else None,
    }


def _restarted(old_boot, probed, since):
    """True when the probe shows the server restarted since the job read old_boot at clock time since: the boot id
    moved by more than BOOT_TOLERANCE_S and the reported uptime is shorter than the time since then plus
    UPTIME_SLACK_S. A clock step moves the boot id but not the uptime; an uptime that is missing proves nothing."""
    if not boot_changed(old_boot, probed.get("boot")):
        return False
    uptime = probed.get("uptime")
    return uptime is not None and uptime < (_clock() - since) + UPTIME_SLACK_S


def _windows_blank():
    return {"phase": "", "found": None, "skipped": None, "items": [], "updated": None, "failed": None,
            "result": None, "hresult": "", "restart": "", "restart_before": "", "more": None, "failure": "",
            "reason": "", "notes": [], "done": False}


def _windows_parse(lines, into=None):
    """The worker's CBVULN|key|value lines as one dict, added to into when it is given (each poll brings the
    lines after the last). Only known keys are read; a later line wins; text is cleaned and capped, counts are whole
    numbers within bounds (else None), restart is 0 or 1, the result a code from 0 to 5, the HRESULT 0x and eight
    hexadecimal digits, and at most MAX_ITEMS items and MAX_NOTES notes are kept."""
    result = into if into is not None else _windows_blank()
    if isinstance(lines, (str, bytes)):
        text = lines.decode("utf-8", errors="replace") if isinstance(lines, bytes) else lines
        lines = vuln_jobs._bounded(text).splitlines()
    for raw in lines:
        line = str(raw).strip()
        if not line.startswith("CBVULN|"):
            continue
        parts = line.split("|", 2)
        if len(parts) < 3 or parts[1] not in WIN_KEYS:
            continue
        key, value = parts[1], parts[2].strip()
        if key in ("found", "skipped", "updated", "failed", "more"):
            result[key] = vuln_jobs._int(value, MAX_COUNT)
        elif key == "result":
            result[key] = vuln_jobs._int(value, 5)
        elif key == "hresult":
            result[key] = value if HRESULT_RE.fullmatch(value) else ""
        elif key in ("restart", "restart_before"):
            result[key] = value if value in ("0", "1") else ""
        elif key == "item":
            bits = value.split(";", 2)
            code = vuln_jobs._int(bits[0], 5) if len(bits) == 3 else None
            if code is not None and len(result["items"]) < MAX_ITEMS:
                kb = bits[1].strip()
                result["items"].append({"result": code, "kb": kb if KB_RE.fullmatch(kb) else "",
                                        "title": vuln_jobs.clean(bits[2], 120)})
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


def _windows_cleanup(server, run_id):
    """Unregister the task and remove the run folder on the server. Best effort, and only when the task is not
    running: vuln_wintask.cleanup stops the task, and an update must never be stopped midway, so a task that is
    still running (a canceled job, a timeout) keeps its folder, and the next update removes folders older than two
    days. A server that cannot be reached keeps it too."""
    try:
        state = vuln_wintask.poll(server, run_id, 0, 200)
    except Exception:
        logger.info("Vulnerabilities: the update on server %s was not checked, so its run folder stays", server.id)
        return
    if state.get("alive"):
        logger.info("Vulnerabilities: the update on server %s is still running, so its run folder stays", server.id)
        return
    vuln_wintask.cleanup(server, run_id)


def _windows_wait_for_install(job, server, run_id, limit_s, old_boot="", since=None):
    """Poll the worker's status file until it logs done. Returns the parsed lines (_windows_parse). Raises
    PatchFailed at the limit, when CloudBolt lost contact for LOST_POLLS polls in a row, or when the task is gone
    without a done line (two polls in a row: it is not running and brought no new line); then one probe tells a
    restart (_restarted, against old_boot read at clock time since) from a task that stopped."""
    began = _clock()
    since = began if since is None else since
    lost = idle = offset = 0
    parsed = _windows_blank()
    shown = ""
    more = False
    while True:
        if not more:
            _sleep(WIN_POLL_S)
        if _clock() - began > limit_s:
            raise PatchFailed("The update did not finish within {} minutes and may still be running on the server. "
                              "{}".format(int(limit_s // 60), CHECK_WINDOWS_UPDATE), _facts_of(parsed))
        try:
            polled = _windows_call(vuln_wintask.poll, server, run_id, offset)
        except CallFailed as exc:
            lost += 1
            more = False
            if lost >= LOST_POLLS:
                raise PatchFailed("CloudBolt lost contact with the server for {} checks in a row ({}). {}".format(
                    LOST_POLLS, exc, CHECK_WINDOWS_UPDATE), _facts_of(parsed))
            continue
        lost = 0
        _windows_parse(polled["lines"], parsed)
        offset = polled["next"]
        more = bool(polled["more"])
        if parsed["phase"] and parsed["phase"] != shown:
            shown = parsed["phase"]
            _progress(job, "{}: {}".format(server.hostname, shown))
        if parsed["done"]:
            return parsed
        idle = idle + 1 if (not polled["alive"] and not polled["lines"] and not more) else 0
        if idle >= 2:
            try:
                probed = _windows_probe(server, job)
            except CallFailed:
                probed = {}
            if old_boot and _restarted(old_boot, probed, since):
                raise PatchFailed(RESTARTED_WINDOWS, _facts_of(parsed))
            raise PatchFailed(STOPPED_WINDOWS, _facts_of(parsed))


def _windows_reboot(server):
    """Send the restart. Returns True when the call went through, False when it raised (ambiguous: the server may
    be going down). Raises PatchFailed when the reply says the restart was not scheduled."""
    try:
        reply = _windows_call(vuln_wintask.inline, server, _reboot_script())
    except CallFailed:
        return False
    if reply.get("terr"):
        raise PatchFailed("The restart could not be scheduled: {}".format(vuln_jobs.clean(reply["terr"], MESSAGE_LEN)))
    code = reply.get("reboot")
    if code is not None and str(code).strip() != "0":
        raise PatchFailed("The restart could not be scheduled (exit code {}).".format(vuln_jobs.clean(code, 20)))
    return True


def _windows_restart(job, server, old_boot, wait_s, since=None):
    """Restart the server and wait until it has restarted (_restarted). Returns the new boot id. A server that
    already shows a restart (it restarted on its own after the install) is not restarted again. Raises PatchFailed
    when the restart cannot be scheduled (a reply with a non-zero exit code or an error), when the server is not
    back within wait_s seconds, or when a reboot call that raised was not followed by a restart: after
    RESTART_UNCHANGED_PROBES successful probes in a row with the old boot id the reboot is sent once more, and after
    as many again the restart is given up as not started. Any error while probing means not back yet."""
    since = _clock() if since is None else since
    _progress(job, "{}: restarting".format(server.hostname))
    try:
        now = _windows_probe(server, job)
    except CallFailed:
        now = {}
    if now.get("boot") and _restarted(old_boot, now, since):
        return now["boot"]
    ambiguous = not _windows_reboot(server)
    resent = False
    unchanged = 0
    began = _clock()
    while True:
        _sleep(WIN_PROBE_S)
        if _clock() - began > wait_s:
            raise PatchFailed("The server did not come back within {} minutes of the restart.".format(int(wait_s // 60)))
        try:
            probed = _windows_probe(server, job)
        except CallFailed:
            unchanged = 0
            continue
        if probed["boot"] and _restarted(old_boot, probed, since):
            return probed["boot"]
        if not ambiguous:
            continue
        unchanged += 1
        if unchanged >= RESTART_UNCHANGED_PROBES:
            if resent:
                raise PatchFailed(RESTART_NOT_STARTED)
            resent = True
            unchanged = 0
            _windows_reboot(server)


def _failed_note(items):
    """"These updates failed: KB1, KB2." for the items whose result is 3, 4 or 5 (at most MAX_FAILED_LISTED), or ""."""
    bad = [item for item in items if item["result"] in (3, 4, 5)]
    if not bad:
        return ""
    names = [item["kb"] or item["title"] or "an update" for item in bad[:MAX_FAILED_LISTED]]
    more = len(bad) - len(names)
    return "These updates failed: {}{}.".format(", ".join(names), " and {} more".format(more) if more > 0 else "")


def _windows_patch(job, server, run_id, restart, cfg):
    """Steps 2 to 4 on a Windows server: the probe (Configuration Manager, build, boot id), the update as a SYSTEM
    task, the restart. Returns the same facts as _linux_patch (kernels_removed is None); raises PatchFailed."""
    notes = []
    probed = _windows_probe(server, job)
    if probed["ccm"] == "1":
        raise PatchFailed(CCM_REFUSED)
    if probed["product"] not in ("2", "3"):
        # Windows 11 24H2 has build 26100 too; only a server product type is updated here. Unreadable refuses.
        raise PatchFailed(ONLY_SERVER)
    if not probed["build"]:
        raise PatchFailed("The Windows build could not be read, so nothing was changed.")
    if probed["build"] not in vuln_msrc.PRODUCTS:
        raise PatchFailed("Windows build {} is not one this extension updates (Windows Server 2022 and 2025), so "
                          "nothing was changed.".format(probed["build"]))
    old_boot = probed["boot"]
    if not old_boot:
        raise PatchFailed("The server's boot time could not be read, so nothing was changed.")
    since = _clock()
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), WIN_SCRIPT_FILE)
    worker = _windows_call(vuln_wintask.build_worker, path, {})
    limit = _minutes(cfg, "patch_timeout_min", DEFAULT_PATCH_TIMEOUT_MIN) * 60
    _progress(job, "{}: starting the update".format(server.hostname))
    try:
        _windows_call(vuln_wintask.start, server, run_id, worker, limit)
    except CallFailed as exc:
        raise PatchFailed("The update did not start: {}".format(exc))
    polled = _windows_wait_for_install(job, server, run_id, limit, old_boot, since)
    needed = polled["restart"] == "1"
    facts = {"updated": polled["updated"], "failed": polled["failed"], "restart_needed": needed,
             "restarted": False, "kernels_removed": None, "notes": notes, "info": [], "no_updates": False}
    if polled["reason"]:
        raise PatchFailed(polled["reason"], facts)
    if polled["failure"]:
        raise PatchFailed("The update failed: {}{}".format(
            polled["failure"], " ({})".format(polled["hresult"]) if polled["hresult"] else ""), facts)
    if polled["restart_before"] == "1":
        notes.append("A restart was already pending before the update started.")
    if polled["updated"] is None:
        # Nothing was installed: no updates, or none that can be installed without a person.
        facts["updated"], facts["failed"] = 0, 0
        # Nothing was offered: the outcome says so. Updates that were offered but skipped are in the skipped note.
        facts["no_updates"] = not polled["found"]
    failed_note = _failed_note(polled["items"])
    if polled["result"] in (4, 5):
        raise PatchFailed("Windows Update could not install the updates (result {}{}). {}".format(
            polled["result"], ", " + polled["hresult"] if polled["hresult"] else "", failed_note).strip(), facts)
    if failed_note:
        notes.append(failed_note)
    if polled["skipped"]:
        notes.append("{} update{} skipped because {} user input or a license agreement.".format(
            polled["skipped"], " was" if polled["skipped"] == 1 else "s were",
            "it needs" if polled["skipped"] == 1 else "they need"))
    if polled["more"]:
        notes.append("{} more update{} available; run the update again.".format(
            polled["more"], " is" if polled["more"] == 1 else "s are"))
    if not needed or not restart:
        return facts
    try:
        _windows_restart(job, server, old_boot, _minutes(cfg, "restart_wait_min", DEFAULT_RESTART_WAIT_MIN) * 60,
                         since)
    except PatchFailed as exc:
        raise PatchFailed(str(exc), facts)
    facts["restarted"] = True
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
    """The one line a server gets in the job output: the facts, leaving out what did not happen."""
    if record["status"] == "failed":
        return "{}: failed. {}".format(host, record["message"])
    if record["status"] == "no_updates":
        text = "{}: no updates were available.".format(host)
        return text + (" " + record["message"] if record["message"] else "")
    windows = record.get("family") == "windows"
    if record["updated"] is None:
        parts = ["updates installed"]
    else:
        parts = ["{} {}{} {}".format(record["updated"], "update" if windows else "package",
                                     "" if record["updated"] == 1 else "s", "installed" if windows else "updated")]
    if record["restarted"]:
        parts.append("restarted")
    elif record["restart_needed"]:
        parts.append("a restart is still needed")
    if record["kernels_removed"]:
        parts.append("{} old kernel package{} removed".format(
            record["kernels_removed"], "" if record["kernels_removed"] == 1 else "s"))
    before, after = record["before"], record["after"]
    if before is not None and after is not None:
        counts = "known vulnerabilities {} to {}".format(before["vulns"], after["vulns"])
        if before.get("vuln_fixable") is not None and after.get("vuln_fixable") is not None:
            counts += ", with a fix {} to {}".format(before["vuln_fixable"], after["vuln_fixable"])
        parts.append(counts)
    text = "{}: {}.".format(host, ", ".join(parts))
    if record["message"]:
        text += " " + record["message"]
    return text


def _record(job, server, status, facts, before, after, began, message, family="linux"):
    owner = getattr(job, "owner", None)
    return {
        "family": family,
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
    touched (neither Linux nor Windows, held by another job, no longer eligible); a failure after that is a
    record with status "failed"."""
    if vuln_jobs.is_windows(server):
        family = "windows"
    elif vuln_jobs.is_linux(server):
        family = "linux"
    else:
        raise PatchFailed(ONLY_SUPPORTED)
    patcher, cleaner = (_windows_patch, _windows_cleanup) if family == "windows" else (_linux_patch, _linux_cleanup)
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
            facts = patcher(job, server, run_id, restart, cfg)
        except (PatchFailed, CallFailed) as exc:
            facts = getattr(exc, "facts", None) or {}
            record = _record(job, server, "failed", facts, before, None, began, str(exc), family)
            _finish(job, server, record)
            return dict(record, hostname=server.hostname, warning=True)
        notes = list(facts.get("notes") or [])
        info = list(facts.get("info") or [])
        after, problem = _rescan(job, server)
        if after is None:
            warning = True
            notes.append("The updates were installed and the rescan failed: {}".format(problem))
        status = "updated"
        if facts["restart_needed"] and not facts["restarted"]:
            status = "restart_pending"
            warning = True
            notes.append("A restart is needed to finish; the server was not restarted.")
        elif facts.get("no_updates") and not facts["restarted"] and not facts.get("kernels_removed"):
            status = "no_updates"
        warning = warning or bool(notes)
        record = _record(job, server, status, facts, before, after, began, " ".join(notes + info), family)
        _finish(job, server, record)
        return dict(record, hostname=server.hostname, warning=warning)
    finally:
        try:
            store.clear_active(server.id, marker["scan_id"])
        except Exception:
            logger.exception("Vulnerabilities: could not clear the active marker for server %s", server.id)
        cleaner(server, run_id)


def _finish(job, server, record):
    """Write the record and the server's history event. Neither may end the run."""
    try:
        record_patch(server.id, record)
    except Exception:
        logger.exception("Vulnerabilities: could not record the update of server %s", server.id)
    try:
        # INFO, not MODIFICATION: add_server_event drops a MODIFICATION for a server with no creation event.
        if record["status"] == "no_updates":
            text = "Vulnerability update: no updates were available. {}".format(record["message"]).strip()
        else:
            text = "Vulnerability update {}: {}".format(record["status"].replace("_", " "),
                                                        record["message"] or "no problems.")
        add_server_event("INFO", server, text, job=job)
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
    counts = {"updated": 0, "no_updates": 0, "restart_pending": 0, "failed": 0, "skipped": 0}
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
    if counts["failed"]:
        return "FAILURE", output, problem, outputs
    if warned or counts["failed"] or counts["skipped"]:
        return "WARNING", output, problem, outputs
    return "SUCCESS", output, "", outputs
