"""
The vulnerability scan and the scanner setup as CloudBolt jobs.

start() runs in the web request. It validates the request, takes the server's
start lock, makes the scan id and the upload targets (vuln_repo.py), and starts
the plugin from vuln_hooks.py with OrchestrationHook.run_as_job, the call
CloudBolt uses to run any plugin as a job. One scan runs per server at a time.

The upload URL, the tool URLs and the token are bearer secrets. They travel in
the job's context, which CloudBolt keeps in an encrypted column, and are named
in __sensitive_keys, the list CloudBolt's plug-in runner reads to print them as
<stripped>; when the job ends they are blanked in the job parameters. The
settings the job needs ride in the same context (vuln_cfg), so the job never
reads settings.json.

run_scan() runs inside that job:
  1. it builds the guest script (vuln_scan.sh with a generated header of quoted
     VAR=value lines, never string-formatted input, inside a POSIX sh launcher)
     and runs it as root (Server.execute_script, through the server's own
     credentials or agent channel; OpenShift Virtualization VMs go through the
     kubevirt_api_tunnel shared module, see _execute). The script builds a
     Syft software list and uploads it to the appliance;
  2. the appliance matches the list against the vulnerability database with
     Grype (vuln_tools.py);
  3. the findings replace the server's rows in the findings database
     (vuln_db.py) and the scan is recorded in the server's state file.
A scan that fails leaves the findings of the previous good scan untouched.
Everything that came from the server is stripped of URLs and tokens before it
is stored or shown.

An OpenShift Virtualization VM usually has no route to CloudBolt, so it gets no
upload URL: the script leaves the file in a root-only directory (HOLD_DIR) and
the job reads it over the same API tunnel (_collect).
"""
import json
import os
import re
import secrets
import shlex
import tempfile
import time
from datetime import datetime, timezone

from infrastructure.models import Server
from jobs.models import Job
from utilities.events import add_server_event
from utilities.logger import ThreadLogger

from . import vuln_db, vuln_hooks, vuln_match, vuln_repo, vuln_store as store, vuln_tools as tools

logger = ThreadLogger(__name__)

SCRIPT_FILE = "vuln_scan.sh"
SECRET_KEYS = ("vuln_upload_url", "vuln_token", "vuln_tools")

# The Vulnerabilities tab shows on Linux servers (settings can narrow it to the
# ones that carry this label).
LINUX_FAMILY = "Linux"
LABEL = "Vulnerabilities"
SCAN_PERMISSION = "server.all_actions"

# Bounds on everything parsed from the server's output.
MAX_OUTPUT = 256 * 1024
MESSAGE_LEN = 300
# Text is cut to this before the URL and token patterns run: they are quadratic on one long line.
REDACT_INPUT = 4000
# The scanner's JSON report is read whole, so a report larger than this fails the scan.
MAX_REPORT_BYTES = 512 * 1024 * 1024
# A scan of the same server is refused for this long after one finished.
COOLDOWN_S = 60
# The most a tunnel transfer or the collection of a held file may take.
COLLECT_TIMEOUT = 10 * 60
# The token must outlive a queued job: the scan time limit plus a wait in the queue.
TOKEN_EXTRA_MIN = 65
MAX_NOTES = 5
KNOWN_KEYS = ("os", "arch", "tool_source", "packages", "sbom_bytes", "seconds", "upload", "reason", "note",
              "summary", "done", "held")
UPLOAD_STATES = ("ok", "held", "failed")
# Where a server keeps its software list for _collect; the scan id is appended.
HOLD_PREFIX = "/var/tmp/vuln-hold-"
HELD_NAME = "sbom.json.gz"
EXCLUDE_RE = re.compile(r"/[A-Za-z0-9._/@+-]{0,200}")
URL_RE = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://\S+")
TOKEN_RE = re.compile(r"[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}")
CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")

UNASSIGNED = "Servers in an Unassigned group are not scanned. Move the server to a group first."
NOT_SET_UP = ("The scanner is not set up yet. A CloudBolt admin can set it up on the Vulnerability Settings page.")
NO_SITE_URL = "The default portal has no site URL, so servers cannot be told where to send their software list."
STATE_ERROR = "The scan history for this server cannot be read or written; ask an administrator to check the Vulnerabilities folder."
COOLDOWN = "This server's software list was collected less than a minute ago. Try again shortly."
NOT_HELD = "the server could not keep the software list for CloudBolt to collect"


class ScanError(Exception):
    """The script cannot be built. Raised inside a running job only; never shown
    in an HTTP response."""


class ScanFailed(Exception):
    """A step of a running scan failed; the message is safe to store and show."""


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
        candidate = "VULN_END_" + secrets.token_hex(8)
        if candidate not in inner:
            delimiter = candidate
            break
    if not delimiter:
        raise ScanError("Could not build the script.")
    return (
        "#!/bin/sh\n"
        "umask 077\n"
        'f=$(mktemp "${TMPDIR:-/tmp}/vuln-run.XXXXXX" 2>/dev/null) || '
        '{ echo "CBVULN|reason|mktemp is not installed on the server"; echo "CBVULN|done|1"; exit 0; }\n'
        "trap 'rm -f \"$f\"' EXIT\n"
        "if ! command -v bash >/dev/null 2>&1; then\n"
        '    echo "CBVULN|reason|bash is not installed on the server"; echo "CBVULN|done|1"; exit 0\n'
        "fi\n"
        'cat > "$f" <<\'' + delimiter + "'\n" + inner + delimiter + "\n"
        'bash "$f" </dev/null\n'
        "exit 0\n"
    )


def hold_dir(scan_id):
    """The directory on the server that keeps one scan's file for collection."""
    if not store.SCAN_ID_RE.fullmatch(scan_id or ""):
        raise ScanError("The scan id has characters that are not allowed.")
    return HOLD_PREFIX + scan_id


def build_script(targets_tools, upload_url, token, cfg, hold=""):
    """The script to run: an sh launcher around a header of quoted assignments
    and vuln_scan.sh. targets_tools is vuln_repo.targets()["tools"]; hold is the
    directory to leave the file in, or empty when the server uploads it."""
    extra = []
    for path in cfg.get("excludes") or []:
        if not EXCLUDE_RE.fullmatch(str(path)):
            raise ScanError("An excluded path has characters that are not allowed.")
        extra.append(str(path))
    values = [
        ("UP_URL", upload_url or ""),
        ("UP_TOKEN", token or ""),
        ("UP_INSECURE", "1" if cfg.get("insecure") else "0"),
    ]
    for arch in tools.GUEST_ARCHES:
        item = (targets_tools or {}).get(arch) or {}
        suffix = arch.upper()
        values += [
            ("TOOL_URL_" + suffix, item.get("url", "")),
            ("TOOL_SHA_" + suffix, item.get("sha256", "")),
            ("TOOL_GH_" + suffix, item.get("github_url", "")),
        ]
    values += [
        ("ALLOW_GITHUB", "1" if cfg.get("guest_internet") else "0"),
        ("HOLD_DIR", hold or ""),
        ("EXTRA_EXCLUDES", "\n".join(extra)),
        # The guest stops its own scan two minutes before the job's limit, so the reason reaches the job.
        ("SCAN_TIMEOUT_S", str(max(240, int(cfg.get("timeout_min") or 20) * 60 - 120))),
    ]
    header = "".join("{}={}\n".format(name, shlex.quote(str(value))) for name, value in values)
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), SCRIPT_FILE)
    with open(path, encoding="utf-8", newline="") as handle:
        body = handle.read().replace("\r\n", "\n")
    return _launcher("#!/bin/bash\n" + header + body)


# ---------------------------------------------------------------- output
def redact(text):
    """Text safe to store or show: control characters, URLs and tokens removed."""
    text = CONTROL_RE.sub(" ", str(text or "")[:REDACT_INPUT])
    text = URL_RE.sub("<url>", text)
    return TOKEN_RE.sub("<token>", text).strip()


def clean(text, limit):
    return redact(text)[:limit]


def _int(value, high=10 ** 10):
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return number if 0 <= number <= high else None


def _bounded(text):
    text = text or ""
    if len(text) <= MAX_OUTPUT:
        return text
    half = MAX_OUTPUT // 2
    return text[:half] + "\n" + text[-half:]


class _Capture:
    """The first and the last half of MAX_OUTPUT bytes of a stream, like _bounded:
    a hostile guest that prints without end cannot fill memory."""

    def __init__(self):
        self.half = MAX_OUTPUT // 2
        self.head = bytearray()
        self.tail = bytearray()
        self.dropped = False

    def add(self, data):
        room = self.half - len(self.head)
        if room > 0:
            self.head += data[:room]
            data = data[room:]
        if data:
            self.tail += data
            if len(self.tail) > self.half:
                del self.tail[:len(self.tail) - self.half]
                self.dropped = True

    def text(self):
        gap = b"\n" if self.dropped else b""
        return (bytes(self.head) + gap + bytes(self.tail)).decode(errors="replace")


def _apply_summary(result, fields):
    """Fold one summary line's fields into result; a later line wins."""
    result["summary"] = True
    result["final"] = fields.get("final") == "1"
    for key, field, limit in (("os", "os", 120), ("arch", "arch", 20), ("tool_source", "tool", 20)):
        if fields.get(field):
            result[key] = clean(fields[field], limit)
    size = _int(fields.get("bytes"))
    if size is not None:
        result["sbom_bytes"] = size
    seconds = _int(fields.get("seconds"), 10 ** 6)
    if seconds is not None:
        result["seconds"] = seconds
    if fields.get("upload") in UPLOAD_STATES:
        result["upload"] = fields["upload"]
    if fields.get("reason"):
        result["reason"] = clean(fields["reason"], MESSAGE_LEN)


def parse_output(text):
    """The CBVULN|key|value lines of the script's output as one dict. Only known
    keys are read; strings are capped."""
    result = {
        "os": "", "arch": "", "tool_source": "", "packages": None, "sbom_bytes": None, "seconds": None,
        "upload": "", "reason": "", "notes": [], "held": False, "done": False, "summary": False, "final": False,
    }
    for raw in _bounded(text).splitlines():
        line = raw.strip()
        if not line.startswith("CBVULN|"):
            continue
        parts = line.split("|", 2)
        if len(parts) < 3 or parts[1] not in KNOWN_KEYS:
            continue
        key, value = parts[1], parts[2]
        if key == "os":
            result["os"] = clean(value, 120)
        elif key == "arch":
            result["arch"] = clean(value, 20)
        elif key == "tool_source":
            result["tool_source"] = clean(value, 20)
        elif key == "packages":
            result["packages"] = _int(value)
        elif key == "sbom_bytes":
            result["sbom_bytes"] = _int(value)
        elif key == "seconds":
            result["seconds"] = _int(value, 10 ** 6)
        elif key == "upload":
            if value.strip() in UPLOAD_STATES:
                result["upload"] = value.strip()
        elif key == "reason":
            result["reason"] = clean(value, MESSAGE_LEN)
        elif key == "note":
            if len(result["notes"]) < MAX_NOTES:
                result["notes"].append(clean(value, MESSAGE_LEN))
        elif key == "summary":
            fields = {}
            for pair in value.split("~"):
                name, _, item = pair.partition("=")
                if name:
                    fields[name] = item
            _apply_summary(result, fields)
        elif key == "done":
            result["done"] = True
        elif key == "held":
            result["held"] = value.strip() == "1"
    return result


# ---------------------------------------------------------------- unassigned groups
def in_unassigned_group(server):
    """True when the server is in an Unassigned group: the general one or a
    tenant's own (Group.is_unassigned in accounts/models.py, the test core uses:
    the group is named Unassigned or is a tenant's default group). A server with
    no group is not in one. A server that cannot be checked counts as in one,
    because such a server is never scanned."""
    try:
        group = server.group
        return bool(group is not None and group.is_unassigned)
    except Exception:
        logger.exception("Unassigned group check failed for server %s", getattr(server, "id", ""))
        return True


# ---------------------------------------------------------------- permissions
# Collecting the software list runs a script as root on the server, the same
# thing a Remote Script server action does. CloudBolt lets a user run any
# server action on a server when their role in the server's group (or a
# management group, or as the server's owner) has server.all_actions;
# utilities/permissions.py server_action_permitted checks that, and so does
# has_permission on a Server.
def can_scan(profile, server):
    """Whether the profile may scan the server: a CB Admin, or the
    server.all_actions permission on it. Never for a server in an Unassigned group."""
    if in_unassigned_group(server):
        return False
    if getattr(profile, "is_cbadmin", False):
        return True
    try:
        return bool(profile.has_permission(SCAN_PERMISSION, server))
    except Exception:
        logger.exception("Permission check '%s' failed for server %s", SCAN_PERMISSION, server.id)
        return False


def can_view(profile, server):
    """Whether the profile may view the server (Server.objects_for_profile)."""
    return Server.objects_for_profile(profile).filter(pk=server.id).exists()


# ---------------------------------------------------------------- which servers
def is_linux(server):
    family = server.os_family
    return bool(family and family.get_base_name() == LINUX_FAMILY)


def shows_tab(server, visibility):
    """Whether the server gets the tab: not historical, Linux, and (when the
    settings say "label") labelled Vulnerabilities. Cheap: no network, no
    remote call."""
    if server.status == "HISTORICAL" or not is_linux(server):
        return False
    if visibility != "label":
        return True
    # Not server.labels: that is a new query per server; tags.all() reads a prefetch.
    return LABEL in [tag.name for tag in server.tags.all()]


# ---------------------------------------------------------------- active job
def _active(marker):
    """(marker, Job) when the marker names a Job that is still active, else None.
    A marker whose Job is gone or finished is stale and ignored."""
    if not isinstance(marker, dict) or not marker.get("job_id"):
        return None
    job = Job.objects.filter(id=marker["job_id"]).first()
    if job is None or not job.is_active():
        return None
    return marker, job


def active_job(server_id, state=None):
    """(marker, Job) of the scan in progress, or None. Raises store.StoreError
    when the state file cannot be read."""
    state = state if state is not None else store.read_state(server_id)
    return _active(state.get("active"))


def _progress_text(job):
    try:
        return clean(job.get_progress()[2].message or "", MESSAGE_LEN)
    except Exception:
        return ""


def panel_active(server_id):
    """The panel's "active" dict, or None."""
    try:
        found = active_job(server_id)
    except store.StoreError:
        return None
    if found is None:
        return None
    marker, job = found
    return {
        "job_id": job.id,
        "job_url": job.get_absolute_url(),
        "message": _progress_text(job),
        "started": marker.get("started") or "",
    }


# ---------------------------------------------------------------- start
def _seconds_since(moment):
    """Seconds since a store.stamp() value, or None when it does not parse."""
    try:
        then = datetime.strptime(str(moment), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return (store.now_utc() - then).total_seconds()


def _in_cooldown(last):
    """True when the server's last scan completed less than COOLDOWN_S ago. A
    failed scan has no cooldown."""
    if not isinstance(last, dict) or last.get("status") != "completed":
        return False
    age = _seconds_since(last.get("finished"))
    return age is not None and age < COOLDOWN_S


def start(profile, server, base_url, parent_job=None):
    """Validate and start the scan. base_url is the default portal's configured
    site URL (never a request's Host). Returns the Job, or a Refusal."""
    if server.status == "HISTORICAL":
        return Refusal("This server is historical.")
    if in_unassigned_group(server):
        return Refusal(UNASSIGNED, 409)
    if not is_linux(server):
        return Refusal("Vulnerability scanning supports Linux servers only.")
    if server.power_status != "POWERON":
        return Refusal("The server is not powered on.", 409)
    if not tools.status()["ready"]:
        return Refusal(NOT_SET_UP, 409)
    if not base_url:
        return Refusal(NO_SITE_URL, 409)
    cfg = store.job_settings()
    try:
        # One start at a time per server: the active check, the queueing and
        # the marker are one step, so two clicks cannot start two jobs.
        with store.start_lock(server.id):
            state = store.read_state(server.id)
            running = active_job(server.id, state)
            if running is not None:
                return Refusal(
                    "Job {} is still running on this server; wait for it to finish.".format(running[1].id), 409
                )
            if _in_cooldown(state.get("last")):
                return Refusal(COOLDOWN, 429)
            return _queue(profile, server, cfg, base_url, parent_job)
    except store.StoreError:
        logger.exception("Vulnerabilities: state problem starting a job on server %s", server.id)
        return Refusal(STATE_ERROR, 500)


def _is_openshift_vm(server):
    """True for a VM on an OpenShift Virtualization resource handler (resourcehandlers/ovirt). Only
    those servers have an OVirtServerInfo row, and it is what kubevirt_api_tunnel reads the
    namespace from (resourcehandlers/ovirt/models.py OVirtServerInfo)."""
    return getattr(server, "ovirtserverinfo", None) is not None


def _requested_by(profile):
    return str(profile.user.username) if getattr(profile, "user", None) else str(profile)


def _queue(profile, server, cfg, base_url, parent_job):
    """The queued Job, or a Refusal."""
    scan_id = store.new_scan_id()
    pull = _is_openshift_vm(server)
    try:
        # The links must outlive the scan, the upload and a wait in the job queue.
        found = vuln_repo.targets(base_url, scan_id, server.id, cfg["timeout_min"] + TOKEN_EXTRA_MIN)
        vuln_repo.prepare(server.id, scan_id)
    except vuln_repo.RepoError as exc:
        logger.error("Vulnerabilities: no upload targets for server %s: %s", server.id, exc.detail)
        return Refusal(vuln_repo.BAD_SITE_URL if exc.public == vuln_repo.BAD_SITE_URL else vuln_repo.GENERIC_PUBLIC, 409)
    started = store.stamp()
    requested_by = _requested_by(profile)

    created = vuln_hooks.scan_hook().run_as_job(
        owner=profile,
        parent_job=parent_job,
        context={
            "vuln_server_id": server.id,
            "vuln_scan_id": scan_id,
            "vuln_cfg": cfg,
            # An OpenShift VM cannot reach CloudBolt: no upload URL, the job collects the file itself.
            "vuln_upload_url": "" if pull else found["upload_url"],
            "vuln_tools": found["tools"],
            "vuln_token": found["token"],
            "vuln_pull": pull,
            "vuln_started": started,
            "vuln_requested_by": requested_by,
            "__sensitive_keys": list(SECRET_KEYS),
        },
    )
    if not created:
        return Refusal("CloudBolt did not start the job; see the application log.", 500)
    job = created[0]
    # From here the job is queued: nothing below may turn into an error.
    try:
        job.label = "Vulnerability scan: {}".format(server.hostname)
        job.save(update_fields=["label"])
        # The same link run_as_job makes for the servers it is given, so the
        # job shows on the server's own Jobs tab and its group can see it.
        server.jobs.add(job)
    except Exception:
        logger.exception("Vulnerabilities: could not label or link job %s", job.id)
    try:
        store.set_active(server.id, {
            "job_id": job.id, "scan_id": scan_id, "started": started, "requested_by": requested_by,
        })
    except Exception:
        logger.exception("Vulnerabilities: could not record job %s as active on server %s", job.id, server.id)
    return job


# ---------------------------------------------------------------- scanner setup
def _setup_marker_path():
    return store.contained(store.TOOLS_DIR, "setup.json")


def _read_setup_marker():
    try:
        with open(_setup_marker_path(), encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError, RecursionError, store.StoreError):
        return None


def _write_setup_marker(marker):
    """Atomic write of the setup marker; None removes it."""
    path = _setup_marker_path()
    if marker is None:
        try:
            os.unlink(path)
        except OSError:
            pass
        return
    store.ensure_dir(store.TOOLS_DIR)
    fd, temp = tempfile.mkstemp(dir=store.TOOLS_DIR, prefix=".setup-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(marker, handle)
        store.own(temp)
        os.replace(temp, path)
    except BaseException:
        try:
            os.unlink(temp)
        except OSError:
            pass
        raise


def setup_active():
    """{"job_id","job_url","message"} while a setup job is active, else None."""
    found = _active(_read_setup_marker())
    if found is None:
        return None
    _marker, job = found
    return {"job_id": job.id, "job_url": job.get_absolute_url(), "message": _progress_text(job)}


def start_setup(profile):
    """Start the scanner setup (or update) job. One at a time. Returns the Job or a Refusal."""
    try:
        with store.setup_lock():
            running = _active(_read_setup_marker())
            if running is not None:
                return Refusal("Job {} is already setting up the scanner.".format(running[1].id), 409)
            created = vuln_hooks.setup_hook().run_as_job(
                owner=profile,
                parent_job=None,
                context={"vuln_requested_by": _requested_by(profile)},
            )
            if not created:
                return Refusal("CloudBolt did not start the job; see the application log.", 500)
            job = created[0]
            try:
                job.label = "Vulnerability scanner setup"
                job.save(update_fields=["label"])
            except Exception:
                logger.exception("Vulnerabilities: could not label job %s", job.id)
            try:
                _write_setup_marker({"job_id": job.id, "started": store.stamp()})
            except Exception:
                logger.exception("Vulnerabilities: could not record job %s as the setup job", job.id)
            return job
    except store.StoreError:
        logger.exception("Vulnerabilities: state problem starting the setup job")
        return Refusal(STATE_ERROR, 500)


def run_setup(job, vuln_requested_by="", **kwargs):
    """Plugin entry point (via vuln_setup_job.py). Returns the 4-tuple CloudBolt
    plugins use: status, output, errors, outputs."""
    def progress(message):
        _progress(job, message)

    try:
        result = tools.setup(progress)
    except tools.ToolError as exc:
        logger.error("Vulnerabilities: scanner setup failed: %s", exc.detail)
        message = clean(exc.public, MESSAGE_LEN)
        tools.record_setup_error(message)
        return "FAILURE", "", message, {}
    except Exception as exc:
        logger.exception("Vulnerabilities: scanner setup failed")
        message = "The scanner setup failed: {}".format(clean(str(exc), MESSAGE_LEN))
        tools.record_setup_error(message)
        return "FAILURE", "", message, {}
    finally:
        try:
            _write_setup_marker(None)
        except Exception:
            logger.exception("Vulnerabilities: could not clear the setup marker")
    db = result["db"]
    message = "The scanner is ready (scanner {}, software list tool {}); the vulnerability database {}{}.".format(
        result["grype"], result["syft"], "was updated" if db["updated"] else "is already current",
        ", built {}".format(db["built"]) if db["built"] else "",
    )
    return "SUCCESS", message, "", {"grype": result["grype"], "syft": result["syft"], "db_built": db["built"]}


# ---------------------------------------------------------------- the scan job
def _clear_secrets(job):
    """Blank the upload URL, the tool URLs and the token in the job's stored context."""
    if job is None:
        return
    try:
        params = job.job_parameters.cast()
        arguments = dict(params.arguments)
        context = dict(arguments.get("context") or {})
        context["vuln_upload_url"] = ""
        context["vuln_token"] = ""
        context["vuln_tools"] = {}
        arguments["context"] = context
        params.arguments = arguments
        params.save()
    except Exception:
        logger.exception("Could not clear the upload links from the parameters of job %s", getattr(job, "id", ""))


def _progress(job, message):
    if job is not None:
        job.set_progress(message)


def run_scan(job, vuln_server_id=None, vuln_scan_id="", vuln_cfg=None, vuln_upload_url="", vuln_tools=None,
             vuln_token="", vuln_pull=False, vuln_started="", vuln_requested_by="", **kwargs):
    """Plugin entry point (via vuln_job.py). Returns the 4-tuple CloudBolt
    plugins use: status, output, errors, outputs."""
    server = Server.objects.filter(id=vuln_server_id).first()
    began = store.now_utc()
    try:
        if server is None:
            return "FAILURE", "", "The server no longer exists.", {}
        if in_unassigned_group(server):
            # A rerun or a clone of an old job reaches here without passing start().
            return _fail(job, server, vuln_scan_id, vuln_started, began, UNASSIGNED)
        cfg = vuln_cfg or store.job_settings(store.DEFAULT_SETTINGS)
        try:
            return _run(job, server, vuln_scan_id, cfg, vuln_upload_url or "", vuln_tools or {}, vuln_token or "",
                        bool(vuln_pull), vuln_started or store.stamp(), vuln_requested_by, began)
        except Exception as exc:
            if isinstance(exc, (ScanFailed, tools.ToolError, store.StoreError)):
                message = exc.public if isinstance(exc, tools.ToolError) else str(exc)
                if isinstance(exc, tools.ToolError):
                    logger.error("Vulnerabilities: scan %s on server %s failed: %s", vuln_scan_id, server.id, exc.detail)
            else:
                logger.exception("Vulnerabilities: scan job failed on server %s", vuln_server_id)
                message = "The scan failed: {}".format(clean(str(exc), MESSAGE_LEN))
            return _fail(job, server, vuln_scan_id, vuln_started, began, clean(message, MESSAGE_LEN * 2))
    finally:
        # Always clear the marker, so a failed job never blocks the next scan.
        if vuln_server_id:
            try:
                # Only this scan's own marker: a rerun of an old job must not clear a newer scan's.
                store.clear_active(vuln_server_id, vuln_scan_id or None)
            except Exception:
                logger.exception("Vulnerabilities: could not clear the active marker for server %s", vuln_server_id)
        _clear_secrets(job)


def _fail(job, server, scan_id, started, began, message):
    """Record a failed scan (the server's earlier findings stay as they are)
    and return the job's FAILURE result."""
    now = store.now_utc()
    try:
        # The file of a failed scan is of no use, and the one from the last good scan is still there.
        vuln_repo.discard(server.id, scan_id)
    except Exception:
        logger.exception("Vulnerabilities: could not discard the inventory of scan %s", scan_id)
    try:
        store.record_scan(server.id, {
            "scan_id": scan_id, "started": started or store.stamp(now), "finished": store.stamp(now),
            "status": "failed", "error": message, "duration_s": int((now - began).total_seconds()),
            "packages": None, "findings": None,
        })
    except Exception:
        logger.exception("Vulnerabilities: could not record the failed scan %s", scan_id)
    try:
        add_server_event("MODIFICATION", server, "Vulnerability scan failed: {}".format(message), job=job)
    except Exception:
        logger.exception("Vulnerabilities: could not add the server event")
    return "FAILURE", "", message, {"scan_id": scan_id, "status": "failed"}


class TransportError(Exception):
    """The script could not be delivered to the server; the message says why."""


# Seconds of silence after which the tunnel's SSH session sends a keepalive, so a long quiet
# run does not look idle to the cluster's API endpoint or a load balancer in front of it.
TUNNEL_KEEPALIVE = 30


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
            "{} is an OpenShift Virtualization VM, which the scan reaches through the kubevirt_api_tunnel "
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
        capture = _Capture()
        while True:
            # Checked on every pass: a guest that keeps printing never reaches the socket timeout.
            if time.time() > deadline:
                exc = TimeoutError("The script did not finish within {} minutes.".format(int(timeout // 60)))
                exc.output = capture.text()
                raise exc
            try:
                data = channel.recv(65536)
            except socket.timeout:
                continue
            if not data:
                break
            capture.add(data)
        output = capture.text()
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


def _collect(server, scan_id):
    """Read the software list an OpenShift Virtualization VM kept in its hold
    directory, over the API tunnel, into the inventory folder, then remove the
    directory. Returns (stored, reason)."""
    import paramiko
    from shared_modules import kubevirt_api_tunnel

    remote = hold_dir(scan_id)
    transport = None
    deadline = time.time() + COLLECT_TIMEOUT

    def chunks(channel):
        # The size is capped by store_stream (MAX_UPLOAD_BYTES); the time is capped here.
        while True:
            if time.time() > deadline:
                raise TimeoutError("the transfer took longer than {} minutes".format(COLLECT_TIMEOUT // 60))
            data = channel.recv(65536)
            if not data:
                return
            yield data

    try:
        transport = kubevirt_api_tunnel.connect_ssh(server)
        transport.set_keepalive(TUNNEL_KEEPALIVE)
        channel = transport.open_session()
        channel.settimeout(120)
        # The name is this module's constant and the scan id is checked by hold_dir.
        channel.exec_command("sudo -n cat " + shlex.quote("{}/{}".format(remote, HELD_NAME)))
        channel.shutdown_write()
        try:
            vuln_repo.store_stream(server.id, scan_id, chunks(channel))
        finally:
            channel.close()
        channel = transport.open_session()
        channel.settimeout(60)
        channel.exec_command("sudo -n rm -rf " + shlex.quote(remote))
        while not channel.exit_status_ready() and time.time() < deadline + 60:
            time.sleep(0.5)
        return True, ""
    except vuln_repo.RepoError as exc:
        logger.error("Vulnerabilities: storing the list of scan %s from server %s failed: %s", scan_id, server.id,
                     exc.detail)
        return False, clean(exc.public, 150)
    except (kubevirt_api_tunnel.TunnelError, paramiko.SSHException, EOFError, OSError) as exc:
        logger.exception("Vulnerabilities: collecting the list of scan %s from server %s failed", scan_id, server.id)
        return False, "the file could not be read over the API tunnel: {}".format(
            clean(str(exc), 150) or type(exc).__name__
        )
    finally:
        if transport is not None:
            transport.close()


def _snapshot(server, parsed_output, scan_id, inventory_at, packages, matched_at):
    """What the findings database records about the server. Read-only: names
    only, nothing secret."""
    os_name = parsed_output["os"]
    if not os_name:
        try:
            os_name = server.os_build.name if server.os_build else ""
        except Exception:
            os_name = ""
    if not os_name:
        try:
            os_name = server.os_family.name if server.os_family else ""
        except Exception:
            os_name = ""
    return {
        "server_id": server.id,
        "hostname": server.hostname,
        "os_name": os_name,
        "group_name": server.group.name if server.group else "",
        "environment": server.environment.name if server.environment else "",
        "scan_id": scan_id,
        "inventory_at": inventory_at,
        "packages": packages,
        "matched_at": matched_at,
    }


def _run(job, server, scan_id, cfg, upload_url, targets_tools, token, pull, started, requested_by, began):
    if not pull and not upload_url:
        raise ScanFailed(
            "This job's upload links were cleared when it first ran, so it cannot run again. "
            "Start a new scan from the Vulnerabilities tab."
        )
    if server.power_status != "POWERON":
        raise ScanFailed("{} is not powered on.".format(server.hostname))

    script = build_script(targets_tools, upload_url, token, cfg, hold=hold_dir(scan_id) if pull else "")

    # 1. The guest script builds the software list and sends it.
    _progress(job, "Collecting the software list")
    error = ""
    output = ""
    try:
        output = _execute(server, script, cfg["timeout_min"] * 60, job)
    except TransportError as exc:
        raise ScanFailed(clean(str(exc), MESSAGE_LEN))
    except NotImplementedError:
        raise ScanFailed("This server's resource handler cannot run scripts on servers.")
    except Exception as exc:
        # A script that exits non-zero or times out raises; its output rides on the exception.
        output = str(getattr(exc, "output", "") or "")
        if hasattr(exc, "output"):
            error = clean(str(exc), MESSAGE_LEN)
        else:
            logger.exception("Vulnerabilities: running the script on server %s failed", server.id)
            error = "The script could not be run on the server. The CloudBolt job log has the details."
    parsed = parse_output(output)
    for note in parsed["notes"]:
        logger.info("Vulnerabilities: server %s: %s", server.id, note)
    inventory_at = store.stamp()

    # 2. An OpenShift VM keeps the file for the job to read through the tunnel.
    if pull and parsed["held"]:
        _progress(job, "Collecting the software list from {}".format(server.hostname))
        stored, reason = _collect(server, scan_id)
        if not stored:
            parsed["reason"] = reason or NOT_HELD

    # 3. The file must have arrived.
    path = vuln_repo.inventory_path(server.id, scan_id)
    if path is None:
        reason = parsed["reason"] or error
        if not reason and not parsed["done"] and not parsed["summary"]:
            reason = "The script did not finish (the connection dropped, it timed out, or the output was cut off)."
        if not reason and not output.strip():
            reason = "The server returned no output."
        if not reason and pull and not parsed["held"]:
            reason = NOT_HELD
        raise ScanFailed("The server did not send its software list: {}".format(
            reason or "no reason was reported."))
    try:
        facts = vuln_match.sbom_facts(path)
    except (ValueError, OSError, EOFError) as exc:
        if "larger than the limit" in str(exc):
            raise ScanFailed("The software list the server sent is larger than the limit.")
        raise ScanFailed("The software list the server sent could not be read.")

    # 4. Match it against the vulnerability database.
    _progress(job, "Matching against the vulnerability database")
    out_json = store.contained(store.WORK_DIR, "{}.grype.json".format(scan_id))
    store.ensure_dir(store.WORK_DIR)
    try:
        tools.match(path, out_json)
        if os.path.getsize(out_json) > MAX_REPORT_BYTES:
            raise ScanFailed("The scanner's report is larger than the limit.")
        with open(out_json, encoding="utf-8") as handle:
            document = json.load(handle)
    finally:
        try:
            os.unlink(out_json)
        except OSError:
            pass
    parsed_grype = vuln_match.parse_grype(document)
    document = None

    # 5. Replace the server's findings. This is the only step that changes them.
    _progress(job, "Saving the findings")
    now = store.now_utc()
    if not parsed_grype["db_built"]:
        # This Grype version keeps no build time in its report; the setup recorded the database's.
        parsed_grype["db_built"] = tools.status()["db"]["built"]
    row = vuln_db.replace_server(
        _snapshot(server, parsed, scan_id, inventory_at, facts["packages"], store.stamp(now)), parsed_grype
    )
    try:
        # Matches no server references any more would otherwise pile up.
        vuln_db.prune_orphans()
    except Exception:
        logger.exception("Vulnerabilities: pruning the findings store failed")

    # 6. Housekeeping and the record.
    try:
        vuln_repo.prune(server.id, scan_id)
    except Exception:
        logger.exception("Vulnerabilities: pruning failed for server %s", server.id)
    summary = vuln_match.summarize(parsed_grype["matches"])
    findings = row.get("findings", summary["findings"]) if isinstance(row, dict) else summary["findings"]
    finished = store.now_utc()
    store.record_scan(server.id, {
        "scan_id": scan_id, "started": started, "finished": store.stamp(finished), "status": "completed",
        "error": "", "duration_s": int((finished - began).total_seconds()), "packages": facts["packages"],
        "findings": findings,
    })
    message = "Scan of {} finished: {} packages, {} findings ({} critical, {} high).".format(
        server.hostname, facts["packages"], findings, summary["critical"], summary["high"])
    add_server_event("MODIFICATION", server, "Vulnerability scan finished: " + message, job=job)
    outputs = {"scan_id": scan_id, "status": "completed", "packages": facts["packages"], "findings": findings}
    for key in ("critical", "high", "medium", "low", "unknown", "kev", "cves"):
        outputs[key] = summary[key]
    return "SUCCESS", message, "", outputs
