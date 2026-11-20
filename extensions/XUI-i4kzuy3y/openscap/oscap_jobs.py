"""
The OpenSCAP scan and readiness check as a CloudBolt job.

start() runs in the web request. It validates the request, takes the
server's start lock, makes the scan id and the upload targets
(oscap_repo.py), and starts the plugin from oscap_hooks.py with
OrchestrationHook.run_as_job, the call CloudBolt uses to run any plugin as a
job. One scan or check runs per server at a time.

The upload URLs and token are bearer secrets. They travel in the job's
context, which CloudBolt keeps in an encrypted column, and are named in
__sensitive_keys, the list CloudBolt's plug-in runner reads to print them as
<stripped>; when the job ends they are blanked in the job parameters. The
settings the job needs ride in the same context (oscap_cfg), so the job never
reads settings.json.

run_scan() runs inside that job. It builds the script (oscap_scan.sh with a
generated header of quoted VAR=value lines, never string-formatted input,
inside a POSIX sh launcher), runs it as root (Server.execute_script,
through the server's own credentials or agent channel; OpenShift Virtualization
VMs go through the kubevirt_api_tunnel shared module, see _execute), parses the
OSCAP|key|value lines it prints (bounded: known keys only, capped lists and
lengths), writes the summary into the server's index (oscap_store.py) and
adds a server event. Everything that came from the server is stripped of
URLs and tokens before it is stored or shown.

Vendor reference for the oscap options the script uses (--profile, --results,
--results-arf, --report, --skip-validation, no remote fetching by default):
    https://static.open-scap.org/openscap-1.3/oscap_user_manual.html
That page does not cover "oscap info --profiles", --thin-results or the exit
codes (0 pass, 1 error, 2 a rule failed), which the script takes from oscap's
own man page and --help output; it adds --thin-results and --skip-validation
only when --help lists them.
curl reads the upload URL and token from its config on stdin (-K -):
    https://curl.se/docs/manpage.html
"""
import math
import os
import re
import secrets
import shlex

from infrastructure.models import Server
from jobs.models import Job
from utilities.events import add_server_event
from utilities.logger import ThreadLogger

from . import oscap_hooks, oscap_repo, oscap_store as store

logger = ThreadLogger(__name__)

SCRIPT_FILE = "oscap_scan.sh"
SECRET_KEYS = ("oscap_upload_urls", "oscap_upload_token")
MODES = ("scan", "check")
COUNT_KEYS = (
    "pass", "fail", "error", "unknown", "notapplicable", "notchecked", "notselected", "informational", "fixed",
)
SEVERITIES = ("high", "medium", "low", "unknown")

# Bounds on everything parsed from the server's output.
MAX_OUTPUT = 256 * 1024
MAX_DATASTREAMS = 3
MAX_PROFILES = 60
MAX_FAILED = 100
ID_LEN = 200
TITLE_LEN = 200
MESSAGE_LEN = 300
ID_RE = re.compile(r"[A-Za-z0-9._:-]{1,200}")
KNOWN_KEYS = (
    "os_name", "oscap_version", "problem", "installed", "ready", "datastream", "profile", "scan_datastream",
    "scan_profile", "scan_profile_title", "exit_code", "failed", "stored", "duration", "summary", "done",
    "progress", "failed_total",
)
URL_RE = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://\S+")
TOKEN_RE = re.compile(r"[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}")
CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")

NO_TARGETS_PROBLEM = (
    "The result files were not stored: the default portal has no site URL, so servers cannot be told where to "
    "upload. Set the portal's domain, or switch the OpenSCAP storage to S3."
)
INDEX_ERROR = "The scan history for this server cannot be read or written; ask an administrator to check the OpenSCAP folder."


class ScanError(Exception):
    """The script cannot be built. Raised inside a running job only; never shown
    in an HTTP response."""


class Refusal:
    """A request that cannot start: the sentence to show and the HTTP status. start()
    returns one instead of raising, so no response is built from an exception."""

    def __init__(self, message, status=400):
        self.message = message
        self.status = status


# ---------------------------------------------------------------- script
def _launcher(inner):
    """Wrap the bash script in a POSIX sh launcher: channels may run a script
    with sh (SSM, dash on Debian and Ubuntu) or ignore the shebang. The launcher
    writes the script to a temp file through a single-quoted heredoc and runs it
    with bash. The heredoc delimiter is random and must not occur in the
    content; the launcher always exits 0."""
    if not inner.endswith("\n"):
        inner += "\n"
    delimiter = ""
    for _ in range(5):
        candidate = "OSCAP_END_" + secrets.token_hex(8)
        if candidate not in inner:
            delimiter = candidate
            break
    if not delimiter:
        raise ScanError("Could not build the script.")
    return (
        "#!/bin/sh\n"
        "umask 077\n"
        'f=$(mktemp "${TMPDIR:-/tmp}/oscap-run.XXXXXX" 2>/dev/null) || '
        '{ echo "OSCAP|problem|mktemp is not installed on the server"; echo "OSCAP|done|1"; exit 0; }\n'
        "trap 'rm -f \"$f\"' EXIT\n"
        "if ! command -v bash >/dev/null 2>&1; then\n"
        '    echo "OSCAP|problem|bash is not installed on the server"; echo "OSCAP|done|1"; exit 0\n'
        "fi\n"
        'cat > "$f" <<\'' + delimiter + "'\n" + inner + delimiter + "\n"
        'bash "$f" </dev/null\n'
        "exit 0\n"
    )


def build_script(mode, datastream, profile, default_profiles, allow_install, urls, insecure, token=""):
    """The script to run: an sh launcher around a header of quoted assignments
    and oscap_scan.sh."""
    if mode not in MODES:
        raise ScanError("Unknown mode.")
    if datastream and not store.DATASTREAM_RE.fullmatch(datastream):
        raise ScanError("The datastream name has characters that are not allowed.")
    if not store.PROFILE_RE.fullmatch(profile or ""):
        raise ScanError("The profile id has characters that are not allowed.")
    values = [
        ("MODE", mode),
        ("DATASTREAM", datastream or ""),
        ("PROFILE", profile or ""),
        ("DEFAULT_PROFILES", default_profiles or ""),
        ("ALLOW_INSTALL", "1" if allow_install else "0"),
        ("UP_REPORT", (urls or {}).get("report.html", "")),
        ("UP_RESULTS", (urls or {}).get("results.xml.gz", "")),
        ("UP_ARF", (urls or {}).get("arf.xml.gz", "")),
        ("UP_TOKEN", token or ""),
        ("UP_INSECURE", "1" if insecure else "0"),
    ]
    header = "".join("{}={}\n".format(name, shlex.quote(str(value))) for name, value in values)
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), SCRIPT_FILE)
    with open(path, encoding="utf-8", newline="") as handle:
        body = handle.read().replace("\r\n", "\n")
    return _launcher("#!/bin/bash\n" + header + body)


# ---------------------------------------------------------------- output
def redact(text):
    """Text safe to store or show: control characters, URLs and tokens removed."""
    text = CONTROL_RE.sub(" ", str(text or ""))
    text = URL_RE.sub("<url>", text)
    return TOKEN_RE.sub("<token>", text).strip()


def clean(text, limit):
    return redact(text)[:limit]


def _int(value, high=10 ** 7):
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return number if 0 <= number <= high else None


def _score(value):
    try:
        number = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and 0 <= number <= 100 else None


def _bounded(text):
    text = text or ""
    if len(text) <= MAX_OUTPUT:
        return text
    half = MAX_OUTPUT // 2
    return text[:half] + "\n" + text[-half:]


def _apply_summary(result, fields):
    """Fold one summary line's fields into result; a later line wins."""
    result["summary"] = True
    result["final"] = fields.get("final") == "1"
    result["ready"] = fields.get("ready") == "1"
    for key, field, limit in (("oscap_version", "ver", 40), ("os_name", "os", 120),
                              ("scan_datastream", "ds", ID_LEN), ("scan_profile", "prof", ID_LEN),
                              ("scan_profile_title", "title", TITLE_LEN)):
        if fields.get(field):
            result[key] = clean(fields[field], limit)
    code = _int(fields.get("exit"), 255)
    if code is not None:
        result["exit_code"] = code
    result["score"] = _score(fields.get("score"))
    duration = _int(fields.get("dur"), 10 ** 6)
    if duration is not None:
        result["duration"] = duration
    problem = clean(fields.get("problem", ""), MESSAGE_LEN)
    result["scan_problem" if result["ready"] else "problem"] = problem
    for key in COUNT_KEYS:
        number = _int(fields.get(key))
        if number is not None:
            result["counts"][key] = number
    for key in SEVERITIES:
        number = _int(fields.get("sev_" + key))
        if number is not None:
            result["severity"][key] = number
    rules = _int(fields.get("rules"))
    if rules is not None:
        result["rules"] = rules
    for field, name in (("st_report", "report.html"), ("st_results", "results.xml.gz"), ("st_arf", "arf.xml.gz")):
        if fields.get(field) in ("0", "1"):
            reason = result["stored"].get(name, (False, ""))[1]
            result["stored"][name] = (fields[field] == "1", reason)


def parse_output(text):
    """The OSCAP|key|value lines of the script's output as one dict. Only known
    keys are read; lists and strings are capped."""
    result = {
        "os_name": "", "oscap_version": "", "ready": False, "problem": "", "scan_problem": "", "installed": False,
        "datastreams": [], "scan_datastream": "", "scan_profile": "", "scan_profile_title": "",
        "exit_code": None, "counts": {}, "severity": {}, "score": None, "rules": None, "failed": [],
        "stored": {}, "duration": None, "done": False, "summary": False, "final": False,
    }
    by_file = {}
    for raw in _bounded(text).splitlines():
        line = raw.strip()
        if not line.startswith("OSCAP|"):
            continue
        parts = line.split("|", 2)
        if len(parts) < 3 or parts[1] not in KNOWN_KEYS:
            continue
        key, value = parts[1], parts[2]
        if key in ("os_name", "oscap_version"):
            result[key] = clean(value, 120)
        elif key == "scan_datastream":
            result[key] = clean(value, ID_LEN)
        elif key in ("scan_profile", "scan_profile_title"):
            result[key] = clean(value, ID_LEN if key == "scan_profile" else TITLE_LEN)
        elif key == "problem":
            result["scan_problem" if result["ready"] else "problem"] = clean(value, MESSAGE_LEN)
        elif key == "installed":
            result["installed"] = True
        elif key == "ready":
            result["ready"] = True
        elif key == "datastream":
            name, _, recommended = value.partition("|")
            if store.DATASTREAM_RE.fullmatch(name) and len(name) <= ID_LEN and len(result["datastreams"]) < MAX_DATASTREAMS:
                entry = {"file": name, "recommended": recommended.strip() == "1", "profiles": []}
                by_file[name] = entry
                result["datastreams"].append(entry)
        elif key == "profile":
            name, _, rest = value.partition("|")
            pid, _, title = rest.partition("|")
            entry = by_file.get(name)
            if entry is not None and ID_RE.fullmatch(pid) and len(entry["profiles"]) < MAX_PROFILES:
                entry["profiles"].append({"id": pid, "title": clean(title, TITLE_LEN)})
        elif key == "exit_code":
            code = _int(value, 255)
            if code is not None:
                result["exit_code"] = code
        elif key == "failed":
            severity, _, rule = value.partition(" ")
            rule = rule.strip()
            if severity in SEVERITIES and ID_RE.fullmatch(rule) and len(result["failed"]) < MAX_FAILED:
                result["failed"].append({"id": rule, "severity": severity})
        elif key == "stored":
            name, _, rest = value.partition("|")
            ok, _, reason = rest.partition("|")
            if name in oscap_repo.UPLOAD_NAMES:
                result["stored"][name] = (ok.strip() == "yes", clean(reason, MESSAGE_LEN))
        elif key == "duration":
            duration = _int(value, 10 ** 6)
            if duration is not None:
                result["duration"] = duration
        elif key == "summary":
            fields = {}
            for pair in value.split("~"):
                name, _, item = pair.partition("=")
                if name:
                    fields[name] = item
            _apply_summary(result, fields)
        elif key == "done":
            result["done"] = True
    if result["rules"] is None and result["counts"]:
        result["rules"] = sum(result["counts"].values())
    return result


def state_from(parsed, previous=None):
    """The server's last-discovery record. A scan prints no datastream list, so
    the previous list stays when this run found none."""
    ready = bool(parsed["ready"])
    problem = parsed["problem"]
    if not ready and not problem:
        problem = "The check did not finish."
    datastreams = parsed["datastreams"] or ((previous or {}).get("datastreams") if ready else []) or []
    return {
        "checked": store.stamp(),
        "oscap_version": parsed["oscap_version"],
        "os_name": parsed["os_name"],
        "ready": ready,
        "problem": problem,
        "datastreams": datastreams,
    }


def scan_summary(parsed, scan_id, started, job, requested_by, requested_profile, requested_datastream, location,
                 wall, uploads=True, no_target_problem="", disk_files=None):
    """The scan dict kept in the index (the views add the URLs). A scan that
    exited 0 or 2 but left no readable results is failed."""
    exited = parsed["exit_code"] in (0, 2)
    has_results = bool(parsed["rules"])
    completed = exited and has_results
    files = {name: ok for name, (ok, _reason) in parsed["stored"].items()}
    if disk_files is not None:
        files = {name: bool(disk_files.get(name)) for name in oscap_repo.UPLOAD_NAMES}
    # One line per distinct reason: the three files usually fail the same way.
    by_reason = {}
    for name, (ok, reason) in parsed["stored"].items():
        if not ok:
            by_reason.setdefault(reason, []).append(name)
    problems = ["{}: {}".format(", ".join(names), reason) for reason, names in by_reason.items()]
    if not uploads:
        problems = [no_target_problem or NO_TARGETS_PROBLEM]
    elif disk_files is None and completed and not parsed["final"]:
        problems.append("The end of the script's output was cut off, so which files were stored is unknown.")
    counts = {key: parsed["counts"].get(key, 0) for key in COUNT_KEYS}
    severity = {key: parsed["severity"].get(key, 0) for key in SEVERITIES}
    error = ""
    if not completed:
        if exited and not has_results:
            error = "oscap finished but its results could not be read."
        else:
            error = parsed["scan_problem"] or parsed["problem"] or "The scan did not finish."
    summary = {
        "id": scan_id,
        "started": started,
        "duration_s": parsed["duration"] if parsed["duration"] is not None else wall,
        "profile_id": parsed["scan_profile"] or requested_profile,
        "profile_title": parsed["scan_profile_title"],
        "datastream": parsed["scan_datastream"] or requested_datastream,
        "status": "completed" if completed else "failed",
        "error": error,
        "score": parsed["score"] if completed else None,
        "counts": counts,
        "fail_by_severity": severity,
        "failed_rules": parsed["failed"],
        "failed_rules_truncated": counts["fail"] > len(parsed["failed"]),
        "stored": any(files.values()),
        "pruned": False,
        "store_problem": "; ".join(problems)[:MESSAGE_LEN * 2],
        "files": files,
        "job_id": job.id if job is not None else None,
        "job_url": job.get_absolute_url() if job is not None else "",
        "requested_by": requested_by,
    }
    summary.update(location)
    return summary


# ---------------------------------------------------------------- active job
def active_job(server_id, index=None):
    """(marker, Job) of the scan or check in progress, or None. A marker whose
    Job is no longer active is stale and ignored. Raises store.StoreError when
    the index cannot be read."""
    index = index if index is not None else store.read_index(server_id)
    marker = index.get("active")
    if not isinstance(marker, dict) or not marker.get("job_id"):
        return None
    job = Job.objects.filter(id=marker["job_id"]).first()
    if job is None or not job.is_active():
        return None
    return marker, job


# ---------------------------------------------------------------- start
def start(profile, server, mode, datastream, profile_id, install, base_url=""):
    """Validate and start the job. base_url is the default portal's configured
    site URL (never a request's Host). Returns the Job, or a Refusal."""
    if mode not in MODES:
        return Refusal("The mode must be scan or check.")
    datastream = (datastream or "").strip()
    profile_id = (profile_id or "").strip()
    if datastream and not store.DATASTREAM_RE.fullmatch(datastream):
        return Refusal("The datastream name has characters that are not allowed.")
    if not store.PROFILE_RE.fullmatch(profile_id):
        return Refusal("The profile id has characters that are not allowed.")
    if server.status == "HISTORICAL":
        return Refusal("This server is historical.")
    if server.is_windows():
        return Refusal("OpenSCAP scans Linux servers only.")
    if server.power_status != "POWERON":
        return Refusal("The server is not powered on.", 409)

    is_admin = bool(getattr(profile, "is_cbadmin", False))
    cfg = store.get_settings()
    job_cfg = store.job_settings(cfg)
    job_cfg["allow_install"] = bool(install) and cfg["allow_install"]
    try:
        # One start at a time per server: the active check, the queueing and
        # the marker are one step, so two clicks cannot start two jobs.
        with store.server_lock(server.id):
            running = active_job(server.id)
            if running is not None:
                return Refusal(
                    "Job {} is still running on this server; wait for it to finish.".format(running[1].id), 409
                )
            created = _queue(profile, server, mode, datastream, profile_id, cfg, job_cfg, base_url, is_admin)
    except store.StoreError:
        logger.exception("OpenSCAP: index problem starting a job on server %s", server.id)
        return Refusal(INDEX_ERROR, 500)
    return created


def _queue(profile, server, mode, datastream, profile_id, cfg, job_cfg, base_url, is_admin):
    """The queued Job, or a Refusal."""
    scan_id = store.new_scan_id()
    urls, token, region = {}, "", ""
    uploads = False
    store_problem = ""
    where = {"backend": cfg["backend"], "bucket": "", "key_prefix": "", "region": ""}
    if mode == "scan":
        if cfg["backend"] == "local":
            try:
                oscap_repo.prune(server, cfg, reserve=1)
            except Exception:
                logger.exception("OpenSCAP: pruning before a scan failed for server %s", server.id)
        if cfg["backend"] == "local" and not base_url:
            store_problem = NO_TARGETS_PROBLEM
        else:
            try:
                # The links must outlive the scan itself plus the upload.
                targets = oscap_repo.upload_targets(
                    server, scan_id, base_url=base_url, minutes=cfg["scan_timeout_min"] + 5
                )
            except oscap_repo.RepoError as exc:
                logger.error("OpenSCAP: no upload targets for server %s: %s", server.id, exc.detail)
                # Administrators get the sentence for the error code (a fixed table), others the generic one.
                return Refusal(oscap_repo.admin_text(exc.code, cfg) if is_admin else oscap_repo.GENERIC_PUBLIC, 409)
            urls, token, region = targets["urls"], targets["token"], targets["region"]
            uploads = True
        where = oscap_repo.location(server, scan_id, region)

    created = oscap_hooks.scan_hook().run_as_job(
        owner=profile,
        context={
            "oscap_server_id": server.id,
            "oscap_mode": mode,
            "oscap_datastream": datastream,
            "oscap_profile": profile_id,
            "oscap_scan_id": scan_id,
            "oscap_cfg": job_cfg,
            "oscap_uploads": uploads,
            "oscap_store_problem": store_problem,
            "oscap_upload_urls": urls,
            "oscap_upload_token": token,
            "oscap_location": where,
            "oscap_started": store.stamp(),
            "oscap_requested_by": str(profile.user.username) if getattr(profile, "user", None) else str(profile),
            "__sensitive_keys": list(SECRET_KEYS),
        },
    )
    if not created:
        return Refusal("CloudBolt did not start the job; see the application log.", 500)
    job = created[0]
    # From here the job is queued: nothing below may turn into an error.
    try:
        job.label = "OpenSCAP {}: {}".format("scan" if mode == "scan" else "check", server.hostname)
        job.save(update_fields=["label"])
        # The same link run_as_job makes for the servers it is given, so the
        # job shows on the server's own Jobs tab and its group can see it.
        server.jobs.add(job)
    except Exception:
        logger.exception("OpenSCAP: could not label or link job %s", job.id)
    try:
        store.set_active(server.id, {"job_id": job.id, "kind": mode, "started": store.stamp(), "scan_id": scan_id})
    except Exception:
        logger.exception("OpenSCAP: could not record job %s as active on server %s", job.id, server.id)
    return job


def panel_active(server_id, index):
    """The panel's "active" dict, or None."""
    found = active_job(server_id, index)
    if found is None:
        return None
    marker, job = found
    progress = ""
    try:
        progress = clean(job.get_progress()[2].message or "", MESSAGE_LEN)
    except Exception:
        progress = ""
    return {
        "job_id": job.id,
        "job_url": job.get_absolute_url(),
        "kind": marker.get("kind") or "scan",
        "started": marker.get("started") or "",
        "progress": progress,
    }


# ---------------------------------------------------------------- the job
def _clear_secrets(job):
    """Blank the upload URLs and token in the job's stored context."""
    if job is None:
        return
    try:
        params = job.job_parameters.cast()
        arguments = dict(params.arguments)
        context = dict(arguments.get("context") or {})
        context["oscap_upload_urls"] = {}
        context["oscap_upload_token"] = ""
        arguments["context"] = context
        params.arguments = arguments
        params.save()
    except Exception:
        logger.exception("Could not clear the upload links from the parameters of job %s", getattr(job, "id", ""))


def _progress(job, message):
    if job is not None:
        job.set_progress(message)


def run_scan(job, oscap_server_id=None, oscap_mode="scan", oscap_datastream="", oscap_profile="",
             oscap_scan_id="", oscap_cfg=None, oscap_uploads=False, oscap_store_problem="",
             oscap_upload_urls=None, oscap_upload_token="", oscap_location=None, oscap_started="",
             oscap_requested_by="", **kwargs):
    """Plugin entry point (via oscap_job.py). Returns the 4-tuple CloudBolt
    plugins use: status, output, errors, outputs."""
    server = Server.objects.filter(id=oscap_server_id).first()
    try:
        if server is None:
            return "FAILURE", "", "The server no longer exists.", {}
        try:
            return _run(job, server, oscap_mode, oscap_datastream, oscap_profile, oscap_scan_id,
                        oscap_cfg or store.job_settings(store.DEFAULT_SETTINGS), bool(oscap_uploads),
                        oscap_store_problem, oscap_upload_urls or {}, oscap_upload_token or "",
                        oscap_location or {}, oscap_started, oscap_requested_by)
        except Exception as exc:
            logger.exception("OpenSCAP job failed on server %s", oscap_server_id)
            return "FAILURE", "", "OpenSCAP failed: {}".format(clean(str(exc), MESSAGE_LEN)), {}
    finally:
        # Always clear the marker, so a failed job never blocks the next scan.
        if oscap_server_id:
            try:
                store.set_active(oscap_server_id, None)
            except Exception:
                logger.exception("OpenSCAP: could not clear the active marker for server %s", oscap_server_id)
        _clear_secrets(job)


class TransportError(Exception):
    """The script could not be delivered to the server; the message says why."""


# Seconds of silence after which the tunnel's SSH session sends a keepalive, so a long quiet oscap
# run does not look idle to the cluster's API endpoint or a load balancer in front of it.
TUNNEL_KEEPALIVE = 30


def _is_openshift_vm(server):
    """True for a VM on an OpenShift Virtualization resource handler (resourcehandlers/ovirt). Only
    those servers have an OVirtServerInfo row, and it is what kubevirt_api_tunnel reads the
    namespace from (resourcehandlers/ovirt/models.py OVirtServerInfo)."""
    return getattr(server, "ovirtserverinfo", None) is not None


def _execute(server, script, timeout, job=None):
    """Run the script as root on the server and return its stdout text.

    Most servers use Server.execute_script. A script that exits non-zero or times
    out raises, with the output on the exception's .output. OpenShift
    Virtualization guests sit on the pod network and CloudBolt cannot reach them,
    so they run through the kubevirt_api_tunnel shared module (SSH over the
    KubeVirt port-forward websocket) and raise the same way. A tunnel that cannot
    be opened raises TransportError.
    """
    if not _is_openshift_vm(server):
        # Server.execute_script runs the script through the server's own
        # credentials or its resource handler's agent channel
        # (infrastructure/models.py Server.execute_script).
        return server.execute_script(
            script_contents=script,
            run_with_sudo=True,
            timeout=timeout,
            show_streaming_output=False,
        )

    try:
        from shared_modules import kubevirt_api_tunnel
    except ImportError:
        raise TransportError(
            "{} is an OpenShift Virtualization VM, which OpenSCAP reaches through the kubevirt_api_tunnel "
            "shared module, and that module is not on this CloudBolt. Sync server_actions/SVA-5vsp0tv6, "
            "which carries it.".format(server.hostname)
        )
    import socket
    import time
    import paramiko

    _progress(job, "Opening the API tunnel to {}".format(server.hostname))
    transport = None
    try:
        # connect_ssh logs in with the server's CloudBolt credentials (key or password). Used instead of
        # run_script because run_script waits at most its timeout between reads, sets no keepalive and
        # cannot run the command under sudo.
        transport = kubevirt_api_tunnel.connect_ssh(server)
        transport.set_keepalive(TUNNEL_KEEPALIVE)
        channel = transport.open_session()
        channel.set_combine_stderr(True)
        channel.settimeout(5)
        channel.exec_command("sudo -n sh -s")
        channel.sendall(script.replace("\r\n", "\n"))
        channel.shutdown_write()
        deadline = time.time() + timeout
        chunks = []
        while True:
            try:
                data = channel.recv(65536)
            except socket.timeout:
                if time.time() > deadline:
                    exc = TimeoutError("The script did not finish within {} minutes.".format(int(timeout // 60)))
                    exc.output = b"".join(chunks).decode(errors="replace")
                    raise exc
                continue
            if not data:
                break
            chunks.append(data)
        output = b"".join(chunks).decode(errors="replace")
        exit_code = channel.recv_exit_status()
    except kubevirt_api_tunnel.TunnelError as exc:
        raise TransportError(str(exc))
    except (paramiko.SSHException, EOFError, OSError) as exc:
        if isinstance(exc, TimeoutError):
            raise
        raise TransportError("The SSH session to {} failed: {}".format(server.hostname, exc))
    finally:
        if transport is not None:
            transport.close()
    if exit_code:
        tail = [line for line in output.strip().splitlines() if line.strip()][-1:]
        exc = RuntimeError("The script exited with code {}.{}".format(exit_code, " " + tail[0] if tail else ""))
        exc.output = output
        raise exc
    return output


def _local_files(server, scan_id):
    """Which result files the local backend holds for a scan, by looking at the disk."""
    found = {}
    for name in oscap_repo.UPLOAD_NAMES:
        found[name] = os.path.isfile(oscap_repo.local_file(server.id, scan_id, name))
    return found


def _run(job, server, mode, datastream, profile_id, scan_id, cfg, uploads, store_problem, urls, token, location,
         started, requested_by):
    if uploads and not urls:
        return "FAILURE", "", (
            "This job's upload links were cleared when it first ran, so it cannot run again. "
            "Start a new scan from the OpenSCAP tab."
        ), {}
    if server.power_status != "POWERON":
        return "FAILURE", "", "{} is not powered on.".format(server.hostname), {}

    script = build_script(
        mode, datastream, profile_id, cfg["default_profiles"], cfg["allow_install"], urls, cfg["insecure"], token,
    )
    _progress(job, "Running the OpenSCAP {} on {}".format("scan" if mode == "scan" else "check", server.hostname))
    began = store.now_utc()
    error = ""
    output = ""
    try:
        output = _execute(server, script, cfg["timeout_min"] * 60, job)
    except TransportError as exc:
        return "FAILURE", "", clean(str(exc), MESSAGE_LEN), {}
    except NotImplementedError:
        return "FAILURE", "", "This server's resource handler cannot run scripts on servers.", {}
    except Exception as exc:
        # A script that exits non-zero or times out raises; its output rides on the exception.
        output = str(getattr(exc, "output", "") or "")
        if hasattr(exc, "output"):
            # A timeout or a non-zero exit: the exception carries the script output, and its message is ours.
            error = clean(str(exc), MESSAGE_LEN)
        else:
            logger.exception("OpenSCAP: running the script on server %s failed", server.id)
            error = "The script could not be run on the server. The CloudBolt job log has the details."
    wall = int((store.now_utc() - began).total_seconds())

    parsed = parse_output(output)
    if not parsed["done"] and not parsed["summary"]:
        extra = "The script did not finish (the connection dropped, it timed out, or the output was cut off)."
        if not output.strip():
            error = error or "The server returned no output."
        if not parsed["ready"] and not parsed["problem"]:
            parsed["problem"] = error or extra
        elif parsed["ready"] and not parsed["scan_problem"]:
            parsed["scan_problem"] = error or extra

    previous = store.read_index(server.id)["state"]
    store.set_state(server.id, state_from(parsed, previous))
    outputs = {"mode": mode, "ready": parsed["ready"], "oscap_version": parsed["oscap_version"]}

    if mode == "check":
        add_server_event(
            "MODIFICATION", server,
            "OpenSCAP check: {}.".format("ready" if parsed["ready"] else parsed["problem"] or "not ready"),
            job=job,
        )
        if parsed["ready"]:
            return "SUCCESS", "OpenSCAP {} is ready on {} ({}).".format(
                parsed["oscap_version"] or "", server.hostname, parsed["os_name"]), "", outputs
        # The check itself ran; the server is just not ready to scan.
        return "WARNING", "", parsed["problem"] or "OpenSCAP is not ready.", outputs

    disk = _local_files(server, scan_id) if (uploads and cfg["backend"] == "local") else None
    summary = scan_summary(parsed, scan_id, started or store.stamp(), job, requested_by, profile_id, datastream,
                           location, wall, uploads=uploads, no_target_problem=store_problem, disk_files=disk)
    store.add_scan(server.id, summary)
    outputs.update({
        "scan_id": scan_id, "status": summary["status"], "score": summary["score"],
        "counts": summary["counts"], "stored": summary["stored"],
    })
    if cfg["backend"] == "local":
        try:
            oscap_repo.prune(server, cfg)
        except Exception:
            logger.exception("OpenSCAP: pruning failed for server %s", server.id)
    if summary["status"] != "completed":
        add_server_event("MODIFICATION", server, "OpenSCAP scan failed: {}".format(summary["error"]), job=job)
        return "FAILURE", "", summary["error"], outputs
    score = "{:.1f}%".format(summary["score"]) if summary["score"] is not None else "no score"
    message = "Scan of {} with {} finished: {}, {} failed, {} passed.".format(
        server.hostname, summary["profile_id"] or "the default profile", score,
        summary["counts"]["fail"], summary["counts"]["pass"],
    )
    add_server_event("MODIFICATION", server, "OpenSCAP scan finished: " + message, job=job)
    missing = [name for name in oscap_repo.UPLOAD_NAMES if not summary["files"].get(name)]
    if missing:
        return "WARNING", message, "The scan completed, but these files could not be stored: {}. {}".format(
            ", ".join(missing), summary["store_problem"]), outputs
    return "SUCCESS", message, "", outputs
