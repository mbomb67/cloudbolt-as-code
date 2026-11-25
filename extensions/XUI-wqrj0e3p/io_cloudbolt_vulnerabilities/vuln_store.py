"""
Vulnerabilities XUI: settings, locks, signing key and the per-server scan state.

Everything lives under PROSERV_DIR/vulnerabilities/ (the shared proserv disk):

    settings.json              the admin page's settings
    .signing_key               random key for upload tokens (mode 0600)
    scanner.lock               shared while a match runs, exclusive while the
                               scanner or its database is installed or updated
    match-slot-<n>.lock        one per match allowed at once (match_slot): the scanner run and the load of its report
    tools/                     scanner binary, guest tarballs, status files
    grype-db/                  the scanner's vulnerability database cache
    inventories/<server>/...   the stored software list of each server
    work/                      temp files, safe to empty
    findings.db                SQLite findings store (vuln_db.py)
    servers/<server id>.json   one server's scan in progress and recent scans
    servers/<server id>.lock   lock the per-server state writes hold
    resources/<resource id>.json   the scan run in progress on a resource (a marker only)

Two processes write here: the web process (apache, as the CloudBolt service
user) and the job engine, which can run jobs as root. So every directory and
file either creates is made readable and writable by both: modes 0775 and
0664, and when running as root a chown to the CloudBolt service account, the
pattern of resourcehandlers/dynamic/loader.py _own_as_cloudbolt. The signing
key is the exception: mode 0600 and owned by the CloudBolt service account.

CloudBolt runs with USE_TZ=False, so every stamp here is made with
datetime.now(timezone.utc) rather than timezone.now().
"""
import contextlib
import json
import os
import re
import secrets
import shutil
import tempfile
import threading
import time
from datetime import datetime, timezone

from django.conf import settings

try:
    import fcntl
except ImportError:  # not on a CloudBolt appliance; tests only
    fcntl = None

ROOT = os.path.join(settings.PROSERV_DIR, "vulnerabilities")
SETTINGS_FILE = os.path.join(ROOT, "settings.json")
TOOLS_DIR = os.path.join(ROOT, "tools")
GRYPE_DB_DIR = os.path.join(ROOT, "grype-db")
INVENTORY_DIR = os.path.join(ROOT, "inventories")
WORK_DIR = os.path.join(ROOT, "work")
FINDINGS_DB = os.path.join(ROOT, "findings.db")
SERVERS_DIR = os.path.join(ROOT, "servers")
RESOURCES_DIR = os.path.join(ROOT, "resources")
KEY_FILE = os.path.join(ROOT, ".signing_key")
SCANNER_LOCK_FILE = "scanner.lock"
MATCH_SLOT_FILE = "match-slot-{}.lock"
# How long a match waits for a free slot before it gives up, and how often it looks.
MATCH_SLOT_WAIT_S = 90 * 60
MATCH_SLOT_POLL_S = 2

MAX_HISTORY = 20
MAX_EXCLUDES = 20
SCAN_ID_RE = re.compile(r"[A-Za-z0-9-]{8,64}")
EXCLUDE_RE = re.compile(r"/[A-Za-z0-9._/@+-]{0,200}")
VISIBILITIES = ("linux", "label")
# The three files a server keeps in servers/: its state, the lock for state writes and the scan-start lock.
STATE_SUFFIXES = ("json", "lock", "start")

DEFAULT_SETTINGS = {
    "scan_timeout_min": 20,
    "insecure_upload": False,
    "guest_internet_download": True,
    "visibility": "linux",
    "extra_excludes": "",
    # Scan a server as its own job after a build finishes (vuln_hooks.apply_provision_setting).
    "scan_on_provision": False,
    # Vulnerability matches at once on the appliance, for scans and the nightly re-match alike (match_slot),
    # and scans the weekly inventory keeps running at once.
    "rematch_workers": 1,
    "inventory_parallel": 4,
}
REMATCH_WORKERS_RANGE = (1, 4)
INVENTORY_PARALLEL_RANGE = (1, 10)


class StoreError(Exception):
    """State or key cannot be read or written; never swallowed into an empty state."""


def now_utc():
    return datetime.now(timezone.utc)


def stamp(moment=None):
    """A time as 2026-11-23T14:05:09Z (ISO 8601, UTC)."""
    return (moment or now_utc()).strftime("%Y-%m-%dT%H:%M:%SZ")


def new_scan_id():
    """UTC timestamp plus a short random suffix; characters [A-Za-z0-9-] only."""
    return "{}-{}".format(now_utc().strftime("%Y%m%dT%H%M%SZ"), secrets.token_hex(3))


# ---------------------------------------------------------------- ownership
def own(path, directory=False):
    """Make path usable by both the web process and the job engine: 0775 for a
    directory, 0664 for a file, and, as root, owned by the CloudBolt service
    account (resourcehandlers/dynamic/loader.py _own_as_cloudbolt)."""
    os.chmod(path, 0o775 if directory else 0o664)
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        shutil.chown(path, settings.CLOUDBOLT_OS_USER, settings.CLOUDBOLT_OS_GROUP)


def ensure_dir(path):
    """os.makedirs that also owns every directory it creates, up to PROSERV_DIR."""
    missing = []
    probe = os.path.abspath(path)
    stop = os.path.abspath(settings.PROSERV_DIR)
    while probe != stop and not os.path.isdir(probe) and len(probe) > len(stop):
        missing.append(probe)
        probe = os.path.dirname(probe)
    os.makedirs(path, exist_ok=True)
    for created in reversed(missing):
        own(created, directory=True)
    return path


def contained(base, *parts):
    """base joined with parts, normalised, and only if it is strictly inside base
    (StoreError otherwise); the returned value is the one to use. Every path
    built from an id goes through here.
    https://codeql.github.com/codeql-query-help/python/py-path-injection/"""
    base = os.path.normpath(base)
    path = os.path.normpath(os.path.join(base, *[str(part) for part in parts]))
    if not path.startswith(base + os.sep):
        raise StoreError("The path is outside the vulnerabilities folder.")
    return path


@contextlib.contextmanager
def _lock(directory, name, mode=None, wait_s=None):
    """Lock on directory/name across processes. mode is fcntl.LOCK_EX (default)
    or fcntl.LOCK_SH. Blocks until it is free, or, when wait_s is given, polls
    every 2 seconds and raises StoreError once wait_s seconds have passed."""
    if fcntl is None:
        yield
        return
    ensure_dir(directory)
    path = contained(directory, name)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o664)
    try:
        try:
            own(path)
        except OSError:
            pass
        wanted = fcntl.LOCK_EX if mode is None else mode
        if wait_s is None:
            fcntl.flock(fd, wanted)
        else:
            deadline = time.monotonic() + wait_s
            while True:
                try:
                    fcntl.flock(fd, wanted | fcntl.LOCK_NB)
                    break
                except BlockingIOError:  # held by another process
                    if time.monotonic() >= deadline:
                        raise StoreError("The scanner is busy. Try again later.")
                    time.sleep(2)
        yield
    finally:
        os.close(fd)  # closing releases the lock


def server_lock(server_id):
    """Exclusive lock for one server's state writes, across processes."""
    return _lock(SERVERS_DIR, "{}.lock".format(int(server_id)))


def start_lock(server_id):
    """Lock for the check-then-queue step of a scan start. set_active takes the
    server lock itself and flock is not re-entrant, so a start cannot hold that
    one; this is a second lock file next to it."""
    return _lock(SERVERS_DIR, "{}.start".format(int(server_id)))


def setup_lock():
    """Lock for the check-then-queue step of a scanner setup start."""
    return _lock(ROOT, "setup-start.lock")


def scanner_lock(exclusive, wait_s=None):
    """Lock on the scanner and its database. Shared for a match (many may run
    together), exclusive for setup and database updates (which replace the
    files a match reads). Waits up to wait_s seconds (default 45 minutes shared,
    30 exclusive: a database update can take that long) and then raises
    StoreError; a no-op without fcntl."""
    mode = None if fcntl is None else (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
    if wait_s is None:
        wait_s = 30 * 60 if exclusive else 45 * 60
    return _lock(ROOT, SCANNER_LOCK_FILE, mode, wait_s)


_slot_state = threading.local()


@contextlib.contextmanager
def match_slot(slots, wait_s=None, on_wait=None):
    """One of `slots` numbered slots for heavy matching work (running the scanner and loading its
    report), across every process on the appliance: slot files match-slot-0.lock ... under ROOT, each
    taken with a non-blocking flock, polled every MATCH_SLOT_POLL_S seconds. Gives up with StoreError
    ("The scanner is busy") after wait_s seconds (default MATCH_SLOT_WAIT_S). on_wait is called once, the
    first time no slot is free. A no-op without fcntl.

    Take it once per unit of work and never nested: flock on a second file descriptor of the same
    process conflicts with the first, so a thread that held a slot and asked for another would wait for
    itself (a nested attempt raises StoreError at once). Take the slot first and the shared scanner lock
    after it (tools.match does); a database update takes only the exclusive scanner lock."""
    if fcntl is None:
        yield
        return
    if getattr(_slot_state, "held", False):
        raise StoreError("A match slot is already held by this thread.")
    ensure_dir(ROOT)
    slots = max(1, int(slots))
    deadline = time.monotonic() + (MATCH_SLOT_WAIT_S if wait_s is None else wait_s)
    fd = None
    called = False
    while fd is None:
        for number in range(slots):
            path = contained(ROOT, MATCH_SLOT_FILE.format(number))
            candidate = os.open(path, os.O_RDWR | os.O_CREAT, 0o664)
            try:
                try:
                    own(path)
                except OSError:
                    pass
                fcntl.flock(candidate, fcntl.LOCK_EX | fcntl.LOCK_NB)
                fd = candidate
                break
            except BlockingIOError:  # held by another process or thread
                os.close(candidate)
            except BaseException:
                os.close(candidate)
                raise
        if fd is not None:
            break
        if time.monotonic() >= deadline:
            raise StoreError("The scanner is busy. Try again later.")
        if not called and on_wait is not None:
            called = True
            on_wait()
        time.sleep(MATCH_SLOT_POLL_S)
    _slot_state.held = True
    try:
        yield
    finally:
        _slot_state.held = False
        os.close(fd)  # closing releases the lock


# ---------------------------------------------------------------- files
def _read_json(path, default=None):
    """Parsed JSON; default when the file is missing or damaged. A file that
    exists but cannot be read raises StoreError."""
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError:
        return default
    except (ValueError, RecursionError):  # damaged, or nested too deeply to parse
        return default
    except OSError as exc:
        raise StoreError("{} cannot be read: {}".format(os.path.basename(path), exc.strerror or exc))


def _write_json(path, data):
    directory = os.path.dirname(path)
    ensure_dir(directory)
    fd, temp = tempfile.mkstemp(dir=directory, prefix=".tmp-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, sort_keys=True)
        own(temp)
        os.replace(temp, path)
    except Exception:
        try:
            os.unlink(temp)
        except OSError:
            pass
        raise


def signing_key():
    """The per-install random key. Created atomically with mode 0600: written
    to a temp file and linked into place, so no reader ever sees a partial
    key; a loser of the race reads the winner's. A job running as root that
    creates it hands it to the CloudBolt service account, so the web process
    can still read it."""
    try:
        return _valid_key(_read_key())
    except FileNotFoundError:
        pass
    ensure_dir(ROOT)
    fd, temp = tempfile.mkstemp(dir=ROOT, prefix=".key-")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(secrets.token_bytes(32))
        os.chmod(temp, 0o600)
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            shutil.chown(temp, settings.CLOUDBOLT_OS_USER, settings.CLOUDBOLT_OS_GROUP)
        try:
            os.link(temp, KEY_FILE)
        except FileExistsError:
            pass
    finally:
        try:
            os.unlink(temp)
        except OSError:
            pass
    return _valid_key(_read_key())


def _read_key():
    with open(KEY_FILE, "rb") as handle:
        return handle.read()


def _valid_key(key):
    if len(key) < 32:
        raise StoreError("The signing key file is shorter than 32 bytes; delete it to make a new one.")
    return key


# ---------------------------------------------------------------- settings
def _clamp(value, low, high, default):
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        return default
    return max(low, min(high, number))


def _whole(raw, low, high):
    """The int in raw when it is ASCII digits only and within low..high, else None. str.isdigit() is
    True for characters such as a superscript two that int() refuses, so it is not used here."""
    raw = str(raw).strip()
    if not (raw.isascii() and raw.isdigit()):
        return None
    try:
        number = int(raw)
    except ValueError:
        return None
    return number if low <= number <= high else None


def _flag(value):
    return str(value).strip().lower() in ("1", "true", "on", "yes")


def _excludes(text):
    """(list of paths, error message or "") from newline-separated text."""
    paths = []
    for line in str(text or "").replace("\r", "").split("\n"):
        line = line.strip()
        if not line:
            continue
        if not EXCLUDE_RE.fullmatch(line) or ".." in line.split("/"):
            return paths, "Each excluded folder must be an absolute path such as /srv/data, using letters, digits and . _ / @ + - only."
        path = line.rstrip("/") or "/"
        if path == "/":
            return paths, "Excluding / would skip the whole server."
        if path not in paths:
            paths.append(path)
    if len(paths) > MAX_EXCLUDES:
        return paths[:MAX_EXCLUDES], "At most {} folders can be excluded.".format(MAX_EXCLUDES)
    return paths, ""


def get_settings():
    """The saved settings over the defaults; always a complete dict. An
    unreadable settings file gives the defaults."""
    try:
        saved = _read_json(SETTINGS_FILE, {})
    except StoreError:
        saved = {}
    result = dict(DEFAULT_SETTINGS)
    if isinstance(saved, dict):
        for key in DEFAULT_SETTINGS:
            if key in saved:
                result[key] = saved[key]
    result["scan_timeout_min"] = _clamp(result["scan_timeout_min"], 5, 120, DEFAULT_SETTINGS["scan_timeout_min"])
    if result["visibility"] not in VISIBILITIES:
        result["visibility"] = "linux"
    for key in ("insecure_upload", "guest_internet_download", "scan_on_provision"):
        result[key] = bool(result[key])
    result["rematch_workers"] = _clamp(
        result["rematch_workers"], *REMATCH_WORKERS_RANGE, DEFAULT_SETTINGS["rematch_workers"])
    result["inventory_parallel"] = _clamp(
        result["inventory_parallel"], *INVENTORY_PARALLEL_RANGE, DEFAULT_SETTINGS["inventory_parallel"])
    if not isinstance(result["extra_excludes"], str):
        result["extra_excludes"] = ""
    return result


def validate_settings(data):
    """(clean settings, errors) from posted values; keys equal the settings keys.
    errors is a dict keyed by setting name."""
    errors = {}
    clean = dict(DEFAULT_SETTINGS)

    raw = str(data.get("scan_timeout_min", DEFAULT_SETTINGS["scan_timeout_min"])).strip()
    if _whole(raw, 5, 120) is None:
        errors["scan_timeout_min"] = "Scan time limit must be a whole number of minutes from 5 to 120."
    clean["scan_timeout_min"] = _clamp(raw, 5, 120, DEFAULT_SETTINGS["scan_timeout_min"])

    clean["insecure_upload"] = _flag(data.get("insecure_upload", ""))
    clean["guest_internet_download"] = _flag(data.get("guest_internet_download", ""))

    visibility = str(data.get("visibility", "linux")).strip()
    if visibility not in VISIBILITIES:
        errors["visibility"] = "Show the tab on must be every Linux and Windows server or servers labelled {}.".format("Vulnerabilities")
        visibility = "linux"
    clean["visibility"] = visibility

    clean["scan_on_provision"] = _flag(data.get("scan_on_provision", ""))

    for key, (low, high), label in (
        ("rematch_workers", REMATCH_WORKERS_RANGE, "Vulnerability matches at once"),
        ("inventory_parallel", INVENTORY_PARALLEL_RANGE, "Servers scanned at once by the weekly inventory"),
    ):
        raw = str(data.get(key, DEFAULT_SETTINGS[key])).strip()
        if _whole(raw, low, high) is None:
            errors[key] = "{} must be a whole number from {} to {}.".format(label, low, high)
        clean[key] = _clamp(raw, low, high, DEFAULT_SETTINGS[key])

    paths, problem = _excludes(data.get("extra_excludes", ""))
    if problem:
        errors["extra_excludes"] = problem
    clean["extra_excludes"] = "\n".join(paths)
    return clean, errors


def save_settings(clean):
    _write_json(SETTINGS_FILE, clean)


def job_settings(cfg=None):
    """The few settings a job needs, as a dict that rides in the job context so
    the job never reads settings.json."""
    cfg = cfg or get_settings()
    return {
        "timeout_min": cfg["scan_timeout_min"],
        "insecure": bool(cfg["insecure_upload"]),
        "guest_internet": bool(cfg["guest_internet_download"]),
        "excludes": _excludes(cfg["extra_excludes"])[0],
    }


# ---------------------------------------------------------------- per-server state
def _server_path(server_id):
    return contained(SERVERS_DIR, "{}.json".format(int(server_id)))


def _normalise(data):
    if not isinstance(data, dict):
        data = {}
    if not isinstance(data.get("active"), dict):
        data["active"] = None
    if not isinstance(data.get("last"), dict):
        data["last"] = None
    if not isinstance(data.get("history"), list):
        data["history"] = []
    return data


def read_state(server_id):
    """The server's state. A missing or damaged file is the empty state; a file
    that exists but cannot be read raises StoreError, so it is never treated
    as empty and overwritten."""
    return _normalise(_read_json(_server_path(server_id), {}))


def _update(server_id, change):
    """Read, let change(state) alter it, write back atomically, all under the
    server lock so the web process and a job cannot lose each other's write."""
    with server_lock(server_id):
        state = read_state(server_id)
        change(state)
        state["history"] = state["history"][:MAX_HISTORY]
        _write_json(_server_path(server_id), state)
    return state


def set_active(server_id, marker):
    """Record the scan in progress: {"job_id","scan_id","started","requested_by"}."""
    _update(server_id, lambda state: state.__setitem__("active", marker))


def clear_active(server_id, scan_id=None):
    """Clear the active marker. With scan_id, only when the marker is that
    scan's: a rerun of an old job must not clear a newer scan's marker."""

    def change(state):
        active = state.get("active")
        if scan_id is None or (isinstance(active, dict) and active.get("scan_id") == scan_id):
            state["active"] = None

    _update(server_id, change)


def record_scan(server_id, entry):
    """Set "last" and put the entry first in the history (newest first),
    replacing one with the same scan id; keeps the newest 20."""

    def change(state):
        state["last"] = entry
        state["history"] = [entry] + [item for item in state["history"] if item.get("scan_id") != entry.get("scan_id")]

    _update(server_id, change)


def forget_state(server_id):
    """Remove the per-server state file and its two lock files (a deleted server's
    leftovers). Returns True when any of them existed. The caller makes sure no scan is
    running on the server."""
    found = False
    for suffix in STATE_SUFFIXES:
        path = contained(SERVERS_DIR, "{}.{}".format(int(server_id), suffix))
        try:
            os.unlink(path)
            found = True
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise StoreError("The scan history of server {} cannot be removed: {}".format(int(server_id),
                                                                                        exc.strerror or exc))
    return found


def known_state_ids():
    """The ids of every server that has a state file or a lock file, as a sorted list."""
    try:
        names = os.listdir(SERVERS_DIR)
    except FileNotFoundError:
        return []
    except OSError as exc:
        raise StoreError("The scan history folder cannot be read: {}".format(exc.strerror or exc))
    ids = set()
    for name in names:
        number, _, suffix = name.partition(".")
        if number.isascii() and number.isdigit() and suffix in STATE_SUFFIXES:
            ids.add(int(number))
    return sorted(ids)


# ---------------------------------------------------------------- per-resource scan run
# One small marker per resource: the "scan all servers" job in progress. No history is kept.
def resource_lock(resource_id):
    """Exclusive lock for the check-then-queue step of a resource scan start (the same split as
    start_lock: the marker writes take their own lock, so a start cannot hold this one and call them)."""
    return _lock(RESOURCES_DIR, "{}.start".format(int(resource_id)))


def _resource_data_lock(resource_id):
    return _lock(RESOURCES_DIR, "{}.lock".format(int(resource_id)))


def _resource_path(resource_id):
    return contained(RESOURCES_DIR, "{}.json".format(int(resource_id)))


def read_resource_run(resource_id):
    """The marker {"job_id","started","requested_by","server_ids"} or None when there is none or it is
    damaged. A file that exists but cannot be read raises StoreError."""
    found = _read_json(_resource_path(resource_id), None)
    return found if isinstance(found, dict) else None


def set_resource_run(resource_id, marker):
    """Record the scan run in progress (atomic write)."""
    with _resource_data_lock(resource_id):
        _write_json(_resource_path(resource_id), marker)


def clear_resource_run(resource_id, job_id=None):
    """Remove the marker. With job_id, only when the marker is that job's: a rerun of an old job must
    not clear the marker of a newer run. Returns True when a marker was removed."""
    path = _resource_path(resource_id)
    with _resource_data_lock(resource_id):
        if job_id is not None:
            found = _read_json(path, None)
            if not isinstance(found, dict) or found.get("job_id") != job_id:
                return False
        try:
            os.unlink(path)
            return True
        except FileNotFoundError:
            return False
        except OSError as exc:
            raise StoreError("The scan run of resource {} cannot be cleared: {}".format(
                int(resource_id), exc.strerror or exc))
