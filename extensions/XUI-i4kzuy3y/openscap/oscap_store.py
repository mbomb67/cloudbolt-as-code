"""
OpenSCAP XUI: settings and the per-server index.

Everything lives under PROSERV_DIR/openscap/ (the shared proserv disk, as the
File Archives shared module does):

    settings.json            the admin page's settings
    .signing_key             random key for local-upload tokens (mode 0600)
    servers/<server id>.json one server's last discovery ("state") and the
                             summaries of its latest scans ("scans")
    servers/<server id>.lock lock a scan start holds
    reports/<server id>/...  result files of the local backend (oscap_repo.py)

The index holds summaries only; the full result files never go into the
CloudBolt database. Writes are atomic (temp file, then rename).

Two processes write here: the web process (apache, as the CloudBolt service
user) and the job engine, which can run jobs as root. So every directory and
file either creates is made readable and writable by both: modes 0775 and
0664, and when running as root a chown to the CloudBolt service account, the
pattern of resourcehandlers/dynamic/loader.py _own_as_cloudbolt. The signing
key is the exception: mode 0600, created and read only by the web process
(tokens are made when a scan starts and checked on upload, both web
requests; the job never touches it).

CloudBolt runs with USE_TZ=False, so every stamp here is made with
datetime.now(timezone.utc) rather than timezone.now().
"""
import contextlib
import errno
import json
import os
import re
import secrets
import shutil
import tempfile
from datetime import datetime, timezone

from django.conf import settings

try:
    import fcntl
except ImportError:  # not on a CloudBolt appliance; tests only
    fcntl = None

ROOT = os.path.join(settings.PROSERV_DIR, "openscap")
SETTINGS_FILE = os.path.join(ROOT, "settings.json")
SERVERS_DIR = os.path.join(ROOT, "servers")
REPORTS_DIR = os.path.join(ROOT, "reports")
KEY_FILE = os.path.join(ROOT, ".signing_key")

MAX_SCANS = 50
SCAN_ID_RE = re.compile(r"[A-Za-z0-9-]{8,64}")
DATASTREAM_RE = re.compile(r"[A-Za-z0-9._-]+")
PROFILE_RE = re.compile(r"[A-Za-z0-9._:-]*")

DEFAULT_SETTINGS = {
    "backend": "local",
    "aws_handler_id": "",
    "s3_bucket": "",
    "s3_region": "",
    "s3_prefix": "openscap",
    "link_minutes": 15,
    "local_keep": 10,
    "local_max_gb": 5,
    "local_insecure_upload": False,
    "default_profiles": "cis_server_l1,cis,stig,standard",
    "scan_timeout_min": 30,
    "allow_install": False,
    "visibility": "linux",
}
BACKENDS = ("local", "s3")
VISIBILITIES = ("linux", "label")
S3_BUCKET_RE = re.compile(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]")
S3_PREFIX_RE = re.compile(r"[A-Za-z0-9._/-]*")
REGION_RE = re.compile(r"[a-z]{2}(-[a-z]+)+-[0-9]+")
DEFAULT_PROFILES_RE = re.compile(r"[A-Za-z0-9._-]+(,[ ]*[A-Za-z0-9._-]+)*")


class StoreError(Exception):
    """The index or key cannot be read or written; never swallowed into an empty index."""


def now_utc():
    return datetime.now(timezone.utc)


def stamp(moment=None):
    """A time as 2026-11-20 14:05 UTC."""
    return (moment or now_utc()).strftime("%Y-%m-%d %H:%M UTC")


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
    """os.makedirs that also owns every directory it creates, up to ROOT's parent."""
    missing = []
    probe = os.path.abspath(path)
    stop = os.path.abspath(settings.PROSERV_DIR)
    while probe != stop and not os.path.isdir(probe) and len(probe) > len(stop):
        missing.append(probe)
        probe = os.path.dirname(probe)
    os.makedirs(path, exist_ok=True)
    for created in reversed(missing):
        own(created, directory=True)


@contextlib.contextmanager
def server_lock(server_id):
    """Exclusive lock for one server's scan starts, across processes."""
    if fcntl is None:
        yield
        return
    ensure_dir(SERVERS_DIR)
    path = os.path.join(SERVERS_DIR, "{}.lock".format(int(server_id)))
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o664)
    try:
        try:
            own(path)
        except OSError:
            pass
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
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
    """The per-install random key. Web process only. Created atomically with
    mode 0600: written to a temp file and linked into place, so no reader ever
    sees a partial key; a loser of the race reads the winner's."""
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
    result["link_minutes"] = _clamp(result["link_minutes"], 1, 60, 15)
    result["local_keep"] = _clamp(result["local_keep"], 1, 100, 10)
    result["local_max_gb"] = _clamp(result["local_max_gb"], 1, 100, 5)
    result["scan_timeout_min"] = _clamp(result["scan_timeout_min"], 5, 180, 30)
    if result["backend"] not in BACKENDS:
        result["backend"] = "local"
    if result["visibility"] not in VISIBILITIES:
        result["visibility"] = "linux"
    for key in ("local_insecure_upload", "allow_install"):
        result[key] = bool(result[key])
    return result


def job_settings(cfg=None):
    """The few settings a job needs, as a dict that rides in the job context so
    the job never reads settings.json."""
    cfg = cfg or get_settings()
    return {
        "timeout_min": cfg["scan_timeout_min"],
        "default_profiles": cfg["default_profiles"],
        "insecure": bool(cfg["local_insecure_upload"] and cfg["backend"] == "local"),
        "allow_install": cfg["allow_install"],
        "backend": cfg["backend"],
        "local_keep": cfg["local_keep"],
        "local_max_gb": cfg["local_max_gb"],
    }


def _flag(value):
    return str(value).strip().lower() in ("1", "true", "on", "yes")


def validate_settings(data):
    """(clean settings, errors) from posted values; keys equal the settings keys."""
    errors = []
    clean = dict(DEFAULT_SETTINGS)

    backend = str(data.get("backend", "local")).strip()
    if backend not in BACKENDS:
        errors.append("Storage must be the CloudBolt appliance or an S3 bucket.")
        backend = "local"
    clean["backend"] = backend

    clean["aws_handler_id"] = str(data.get("aws_handler_id", "")).strip()
    bucket = str(data.get("s3_bucket", "")).strip()
    region = str(data.get("s3_region", "")).strip()
    prefix = str(data.get("s3_prefix", "openscap")).strip().strip("/")
    if backend == "s3":
        if not clean["aws_handler_id"].isdigit():
            errors.append("Choose the AWS resource handler whose credentials reach the bucket.")
        if not S3_BUCKET_RE.fullmatch(bucket):
            errors.append("Enter the S3 bucket name (3-63 lowercase letters, digits, dots and dashes).")
    elif bucket and not S3_BUCKET_RE.fullmatch(bucket):
        errors.append("The S3 bucket name is not valid.")
    if region and not REGION_RE.fullmatch(region):
        errors.append("The S3 region must look like us-east-1, or be empty to look it up.")
    if not S3_PREFIX_RE.fullmatch(prefix) or ".." in prefix:
        errors.append("The S3 key prefix may use letters, digits, dots, dashes, underscores and slashes.")
        prefix = "openscap"
    clean["s3_bucket"], clean["s3_region"], clean["s3_prefix"] = bucket, region, prefix

    clean["link_minutes"] = _clamp(data.get("link_minutes", 15), 1, 60, 15)
    clean["local_keep"] = _clamp(data.get("local_keep", 10), 1, 100, 10)
    clean["local_max_gb"] = _clamp(data.get("local_max_gb", 5), 1, 100, 5)
    clean["scan_timeout_min"] = _clamp(data.get("scan_timeout_min", 30), 5, 180, 30)
    clean["local_insecure_upload"] = _flag(data.get("local_insecure_upload", ""))
    clean["allow_install"] = _flag(data.get("allow_install", ""))

    profiles = str(data.get("default_profiles", "")).strip() or DEFAULT_SETTINGS["default_profiles"]
    if not DEFAULT_PROFILES_RE.fullmatch(profiles):
        errors.append("Default profiles is a comma-separated list of profile-id endings, such as cis_server_l1,cis,stig.")
        profiles = DEFAULT_SETTINGS["default_profiles"]
    clean["default_profiles"] = ",".join(p.strip() for p in profiles.split(","))

    visibility = str(data.get("visibility", "linux")).strip()
    if visibility not in VISIBILITIES:
        errors.append("Show the tab on must be Linux servers or servers labelled OpenScap.")
        visibility = "linux"
    clean["visibility"] = visibility
    return clean, errors


def save_settings(clean):
    _write_json(SETTINGS_FILE, clean)


# ---------------------------------------------------------------- per-server index
def _server_path(server_id):
    return os.path.join(SERVERS_DIR, "{}.json".format(int(server_id)))


def _normalise(data):
    if not isinstance(data, dict):
        data = {}
    if not isinstance(data.get("state"), dict):
        data["state"] = {}
    if not isinstance(data.get("scans"), list):
        data["scans"] = []
    return data


def read_index(server_id):
    """The server's index. A missing or damaged file is an empty index; a file
    that exists but cannot be read raises StoreError, so it is never treated
    as empty and overwritten."""
    return _normalise(_read_json(_server_path(server_id), {}))


def update_index(server_id, change):
    """Read the index, let change(index) alter it, write it back atomically.
    Refuses (StoreError) when the existing file is unreadable."""
    index = read_index(server_id)
    change(index)
    index["scans"] = index["scans"][:MAX_SCANS]
    _write_json(_server_path(server_id), index)
    return index


def set_active(server_id, active):
    """Record (dict) or clear (None) the scan or check job in progress."""

    def change(index):
        if active:
            index["active"] = active
        else:
            index.pop("active", None)

    update_index(server_id, change)


def set_state(server_id, state):
    update_index(server_id, lambda index: index.__setitem__("state", state))


def add_scan(server_id, summary):
    """Put a scan summary first (newest first), replacing one with the same id."""

    def change(index):
        index["scans"] = [summary] + [s for s in index["scans"] if s.get("id") != summary.get("id")]

    update_index(server_id, change)


def find_scan(server_id, scan_id):
    for scan in read_index(server_id)["scans"]:
        if scan.get("id") == scan_id:
            return scan
    return None


def drop_scans(server_id, scan_ids):
    """Mark scans whose files retention removed: they stay listed as pruned."""
    wanted = set(scan_ids)

    def change(index):
        for scan in index["scans"]:
            if scan.get("id") in wanted:
                scan["stored"] = False
                scan["pruned"] = True
                scan["files"] = {}
                scan["store_problem"] = ""

    if wanted:
        try:
            update_index(server_id, change)
        except (StoreError, OSError):
            # The files are gone either way; a server's index that cannot be
            # written is reported by its own panel.
            pass
