"""
Vulnerabilities XUI: the scanner on the appliance.

This module owns three things under PROSERV_DIR/vulnerabilities:

* the Grype binary for the appliance's own architecture (TOOLS_DIR),
* the Syft tarballs the servers download to build their software list
  (TOOLS_DIR; they are kept as tarballs and served by vuln_repo.serve_tool),
* the Grype vulnerability database (GRYPE_DB_DIR, Grype's own cache).

Releases are pinned and every download is checked against the sha256 the
publisher lists, before anything is installed or run. Calls to the scanner use
a list argv (never a shell), a timeout, and a minimal environment.

status() is cheap on purpose: it reads TOOLS_DIR/status.json (written by
setup() and db_update()) and a few stat calls, so a web request can call it.

Vendor references (each call site names its page):
    https://oss.anchore.com/docs/reference/grype/configuration/
    https://oss.anchore.com/docs/reference/syft/configuration/
    https://oss.anchore.com/docs/guides/sbom/catalogers/
    https://github.com/anchore/grype/releases
    https://github.com/anchore/syft/releases
"""
import contextlib
import gzip
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import tarfile
import tempfile
import zlib

from utilities.logger import ThreadLogger

from . import vuln_match
from . import vuln_msrc
from . import vuln_store as store

logger = ThreadLogger(__name__)

GRYPE_VERSION = "0.122.1"
SYFT_VERSION = "1.57.2"
GUEST_ARCHES = ("amd64", "arm64")

# sha256 of each pinned asset, copied from the publishers' checksum files.
ASSETS = {
    "grype_{}_linux_amd64.tar.gz".format(GRYPE_VERSION):
        "f1ba7e2917ee1395618de20a98da0aa7c0fb1788927797651047c50502a37538",
    "grype_{}_linux_arm64.tar.gz".format(GRYPE_VERSION):
        "819e5be3d9516432fedb7b6b56df7306c3a16607606dfc05826eefbb00e471da",
    "syft_{}_linux_amd64.tar.gz".format(SYFT_VERSION):
        "ad92bd3ec1b94a3df01d4d0aec199afa9c42b72e86da436ff5f837914d32706b",
    "syft_{}_linux_arm64.tar.gz".format(SYFT_VERSION):
        "8efe605309c98798e1ee0cffdcf8c05ca26819b4cd44c0b03650f1e963e9bdb6",
}
GRYPE_URL = "https://github.com/anchore/grype/releases/download/v" + GRYPE_VERSION + "/"  # + asset name
SYFT_URL = "https://github.com/anchore/syft/releases/download/v" + SYFT_VERSION + "/"  # + asset name
DB_LISTING_URL = "https://grype.anchore.io/databases/v6/latest.json"

GRYPE_BINARY = "grype-" + GRYPE_VERSION  # the version is in the name, so an upgrade never reuses a stale file
STATUS_FILE = "status.json"

# The database unpacks to about 3.6 GB and the old and new copies exist side
# by side while it updates, so refuse with less than this free.
MIN_FREE_BYTES = 9 * 1024 ** 3
# The scanner program and the guest tarballs are small; this is only a sanity check before downloading them.
LIGHT_FREE_BYTES = 1024 ** 3
DB_UPDATE_TIMEOUT = 30 * 60
MATCH_TIMEOUT = 15 * 60
QUICK_TIMEOUT = 60
DOWNLOAD_TIMEOUT = (10, 300)  # connect, read (seconds)
MAX_DOWNLOAD_BYTES = 200 * 1024 * 1024

URL_RE = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://\S+")
CONTROL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
SECRET_RE = re.compile(r"[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}")
DETAIL_TAIL = 800
# The URL and token patterns are quadratic on a long single line, so text is cut to this before they run.
REDACT_INPUT = 4000


# Names the match in its error messages; _run words a kill by signal 9 for it as an out-of-memory stop.
MATCH_WHAT = "The vulnerability match"
OUT_OF_MEMORY = ("The appliance ran out of memory while matching the software list. Lower 'Vulnerability matches at "
                 "once' in the Vulnerability Settings, or give the appliance more memory.")


class ToolError(Exception):
    """public is a one-line message safe to show; detail is for the log only."""

    def __init__(self, public, detail=""):
        super().__init__(public)
        self.public = public
        self.detail = detail or public
        # Set by setup() when the Microsoft update ran next to a failed scanner setup: {"updated", "built"[, "error"]}.
        self.msrc = None


# ---------------------------------------------------------------- small helpers
def _tail(text):
    """The last part of a tool's output with URLs (a proxy URL can carry a
    password) and token-like strings removed."""
    text = CONTROL_RE.sub(" ", str(text or "")[-REDACT_INPUT:])
    text = URL_RE.sub("<url>", text)
    text = SECRET_RE.sub("<token>", text)
    return text.strip()[-DETAIL_TAIL:]


def machine_arch():
    """amd64 or arm64 for this appliance (platform.machine())."""
    machine = platform.machine().lower()
    if machine in ("x86_64", "amd64"):
        return "amd64"
    if machine in ("aarch64", "arm64"):
        return "arm64"
    raise ToolError("This appliance's processor type ({}) is not supported by the scanner.".format(machine or "unknown"))


def _tool_path(name):
    return store.contained(store.TOOLS_DIR, name)


def grype_path():
    return _tool_path(GRYPE_BINARY)


def _asset_name(tool, arch):
    version = GRYPE_VERSION if tool == "grype" else SYFT_VERSION
    return "{}_{}_linux_{}.tar.gz".format(tool, version, arch)


def syft_asset(arch):
    """{"path", "name", "sha256", "url"} of the Syft tarball a server of this
    architecture downloads."""
    if arch not in GUEST_ARCHES:
        raise ToolError("Unknown server architecture.", "architecture {!r}".format(arch))
    name = _asset_name("syft", arch)
    return {"path": _tool_path(name), "name": name, "sha256": ASSETS[name], "url": SYFT_URL + name}


def _read_status_file():
    try:
        with open(_tool_path(STATUS_FILE), encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError, RecursionError):
        return {}


def _write_status_file(data):
    """Atomic write: the web process reads this file while a job writes it."""
    store.ensure_dir(store.TOOLS_DIR)
    path = _tool_path(STATUS_FILE)
    fd, temp = tempfile.mkstemp(dir=store.TOOLS_DIR, prefix=".status-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, sort_keys=True)
        store.own(temp)
        os.replace(temp, path)
    except BaseException:
        try:
            os.unlink(temp)
        except OSError:
            pass
        raise


def _update_status(**sections):
    data = _read_status_file()
    data.update(sections)
    _write_status_file(data)


def _free_bytes():
    """Free space on the filesystem that holds (or would hold) ROOT."""
    probe = os.path.abspath(store.ROOT)
    while probe and not os.path.exists(probe):
        parent = os.path.dirname(probe)
        if parent == probe:
            break
        probe = parent
    try:
        return int(shutil.disk_usage(probe).free)
    except OSError:
        return 0


def _database_file():
    """(path, bytes) of the unpacked database, or ("", 0). Grype keeps it at
    <cache>/<schema>/<model>/vulnerability.db; the layout is searched, not
    assumed, and only to a shallow depth."""
    base = store.GRYPE_DB_DIR
    if not os.path.isdir(base):
        return "", 0
    base_depth = base.rstrip(os.sep).count(os.sep)
    for current, dirs, files in os.walk(base):
        if current.count(os.sep) - base_depth >= 4:
            dirs[:] = []
        if "vulnerability.db" in files:
            path = os.path.join(current, "vulnerability.db")
            try:
                return path, os.stat(path).st_size
            except OSError:
                return "", 0
    return "", 0


# ---------------------------------------------------------------- status
def status():
    """What the settings page and the tab show. No subprocess, no network."""
    data = _read_status_file()
    grype_there = os.path.isfile(grype_path())
    db_path, db_bytes = _database_file()
    db_info = data.get("db") if isinstance(data.get("db"), dict) else {}
    present = bool(db_path)
    return {
        # The last setup failure, shown on the settings page; empty when the last setup worked.
        "last_error": str(data.get("last_error") or ""),
        "last_error_at": str(data.get("last_error_at") or ""),
        "ready": bool(grype_there and present),  # the Linux scanner; Windows needs only the Microsoft data
        "ready_windows": vuln_msrc.ready(),
        "msrc": vuln_msrc.status(),
        "grype": {"installed": grype_there, "version": GRYPE_VERSION if grype_there else ""},
        "syft": {
            "amd64": os.path.isfile(syft_asset("amd64")["path"]),
            "arm64": os.path.isfile(syft_asset("arm64")["path"]),
            "version": SYFT_VERSION,
        },
        "db": {
            "present": present,
            "built": str(db_info.get("built") or "") if present else "",
            "checked": str(db_info.get("checked") or "") if present else "",
            "bytes": db_bytes,
        },
        "free_bytes": _free_bytes(),
    }


# ---------------------------------------------------------------- environment and processes
def _proxy_and_cert_env(url):
    """HTTPS_PROXY/HTTP_PROXY and SSL_CERT_FILE the way the rest of CloudBolt
    reaches the internet (common.methods.get_proxies, utilities.helpers.get_ssl_verification)."""
    from common.methods import get_proxies
    from utilities.helpers import get_ssl_verification

    env = {}
    proxies = get_proxies(url) or {}
    if proxies.get("https"):
        env["HTTPS_PROXY"] = proxies["https"]
    if proxies.get("http"):
        env["HTTP_PROXY"] = proxies["http"]
    verify = get_ssl_verification()
    if isinstance(verify, str) and verify:
        env["SSL_CERT_FILE"] = verify
    return env


def _grype_env(match):
    """The environment of every Grype call. Names are Grype's own, from its
    configuration reference:
        https://oss.anchore.com/docs/reference/grype/configuration/
    The environment is built from scratch, so nothing CloudBolt's job engine
    holds (database passwords, cloud keys) reaches the scanner."""
    env = {
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "LANG": "C.UTF-8",
        "HOME": store.WORK_DIR,  # Grype wants a writable home
        "GRYPE_DB_CACHE_DIR": store.GRYPE_DB_DIR,
        "GRYPE_CHECK_FOR_APP_UPDATE": "false",
    }
    if match:
        env.update({
            "GRYPE_DB_AUTO_UPDATE": "false",
            # A stale database must still answer; the UI shows its age.
            "GRYPE_DB_VALIDATE_AGE": "false",
            # Otherwise every run hashes the 3.6 GB database.
            "GRYPE_DB_VALIDATE_BY_HASH_ON_START": "false",
        })
    else:
        # Grype's own default for the download is 5 minutes, too short for the 207 MB archive on a slow link.
        # The environment name is derived from the config key db.update-download-timeout:
        # https://oss.anchore.com/docs/reference/grype/configuration/
        env["GRYPE_DB_UPDATE_DOWNLOAD_TIMEOUT"] = "30m"
        env.update(_proxy_and_cert_env(DB_LISTING_URL))
    _prepare_dirs()
    return env


def _is_root():
    return hasattr(os, "geteuid") and os.geteuid() == 0


_identity_warned = False


def _service_ids():
    """(uid, gid) of the CloudBolt service account, or None when it cannot be
    looked up (a warning is logged once)."""
    global _identity_warned
    try:
        import grp  # not on Windows; imported here so the module still imports there
        import pwd
        from django.conf import settings

        return pwd.getpwnam(settings.CLOUDBOLT_OS_USER).pw_uid, grp.getgrnam(settings.CLOUDBOLT_OS_GROUP).gr_gid
    except (ImportError, KeyError, AttributeError) as exc:
        if not _identity_warned:
            _identity_warned = True
            logger.warning("Vulnerabilities: cannot look up the CloudBolt service account (%s); "
                           "the scanner runs as the current user", exc)
        return None


def _own_tree_once(path):
    """Hand an existing tree to the service account once (a database cache made
    by an earlier run as root). A marker file records that it was done; errors
    are tolerated, the scanner then simply fails to write and says so."""
    from django.conf import settings

    marker = os.path.join(path, ".owned")
    if os.path.exists(marker):
        return
    for current, dirs, files in os.walk(path):
        for name in dirs + files:
            try:
                shutil.chown(os.path.join(current, name), settings.CLOUDBOLT_OS_USER, settings.CLOUDBOLT_OS_GROUP)
            except (OSError, LookupError):
                pass
    try:
        with open(marker, "wb"):
            pass
        store.own(marker)
    except OSError:
        pass


def _prepare_dirs():
    """The folders the scanner writes, made and (when this runs as root, where
    the scanner is not) owned by the service account first."""
    store.ensure_dir(store.WORK_DIR)
    store.ensure_dir(store.GRYPE_DB_DIR)
    if _is_root() and _service_ids() is not None:
        for path in (store.WORK_DIR, store.GRYPE_DB_DIR):
            try:
                store.own(path, directory=True)
            except (OSError, LookupError):
                pass
        _own_tree_once(store.GRYPE_DB_DIR)


def _run(argv, env, timeout, what, stdout=None):
    """Run argv (a list, no shell) and return its stdout text, or raise
    ToolError with a redacted tail of stderr. stdout may be an open binary file.
    As root (the job engine can run jobs as root) the scanner is run as the
    CloudBolt service account instead: it parses files a scanned server sent."""
    identity = {}
    if _is_root():
        ids = _service_ids()
        if ids is not None:
            identity = {"user": ids[0], "group": ids[1], "extra_groups": []}
    try:
        done = subprocess.run(
            argv, env=env, stdin=subprocess.DEVNULL, stdout=stdout if stdout is not None else subprocess.PIPE,
            stderr=subprocess.PIPE, timeout=timeout, check=False, **identity,
        )
    except subprocess.TimeoutExpired:
        raise ToolError("{} took longer than {} minutes and was stopped.".format(what, max(timeout // 60, 1)),
                        "timeout after {}s: {}".format(timeout, " ".join(argv[:3])))
    except OSError as exc:
        raise ToolError("{} could not be started.".format(what), "{}: {}".format(type(exc).__name__, exc))
    err = _tail(done.stderr.decode("utf-8", "replace") if done.stderr else "")
    if done.returncode < 0:
        # subprocess reports a process the kernel or a person stopped with a signal as -signal. Signal 9 during
        # the match is almost always the kernel's out-of-memory killer.
        signal_number = -done.returncode
        if what == MATCH_WHAT:
            public = OUT_OF_MEMORY if signal_number == 9 else "The scanner was stopped by signal {}.".format(signal_number)
        else:
            public = "{} was stopped by signal {}.".format(what, signal_number)
        raise ToolError(public, "signal {}: {}".format(signal_number, err))
    if done.returncode != 0:
        raise ToolError("{} failed (exit code {}).".format(what, done.returncode),
                        "exit {}: {}".format(done.returncode, err))
    if stdout is not None:
        return ""
    return done.stdout.decode("utf-8", "replace") if done.stdout else ""


# ---------------------------------------------------------------- downloads
def _download(url, wanted_sha, dest, progress=None, label=""):
    """Download url into dest only when its sha256 is wanted_sha. The file is
    hashed while it streams into a temp file and moved into place afterwards,
    so a bad or partial download never sits at dest."""
    import requests
    from common.methods import get_proxies
    from utilities.helpers import get_ssl_verification

    store.ensure_dir(os.path.dirname(dest))
    fd, temp = tempfile.mkstemp(dir=os.path.dirname(dest), prefix=".download-")
    try:
        # The file object owns the descriptor from here, so it is closed on every path.
        try:
            handle = os.fdopen(fd, "wb")
        except BaseException:
            os.close(fd)
            raise
        digest = hashlib.sha256()
        total = 0
        try:
            with handle:
                with requests.get(url, stream=True, verify=get_ssl_verification(), proxies=get_proxies(url),
                                  timeout=DOWNLOAD_TIMEOUT) as response:
                    response.raise_for_status()
                    for chunk in response.iter_content(chunk_size=256 * 1024):
                        if not chunk:
                            continue
                        total += len(chunk)
                        if total > MAX_DOWNLOAD_BYTES:
                            raise ToolError("The download of {} is larger than expected.".format(label or "a file"))
                        digest.update(chunk)
                        handle.write(chunk)
        except requests.RequestException as exc:
            raise ToolError(
                "CloudBolt could not download {}. The appliance needs outbound access to github.com and "
                "grype.anchore.io (check the proxy settings).".format(label or "a file"),
                "{}: {}".format(type(exc).__name__, _tail(str(exc))),
            )
        if digest.hexdigest() != wanted_sha:
            raise ToolError(
                "The download of {} does not match its published checksum, so it was discarded.".format(label or "a file"),
                "sha256 {} expected {}".format(digest.hexdigest(), wanted_sha),
            )
        store.own(temp)
        os.replace(temp, dest)
    finally:
        try:
            os.unlink(temp)
        except OSError:
            pass


def _file_sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _has_good_file(path, wanted_sha):
    try:
        return os.path.isfile(path) and _file_sha256(path) == wanted_sha
    except OSError:
        return False


def _extract_member(tarball, member_name, dest):
    """Copy only the regular file named member_name out of the tarball into
    dest, mode 0755. Nothing else is extracted and no member path is used for
    a destination, so a crafted archive cannot write elsewhere."""
    fd, temp = tempfile.mkstemp(dir=os.path.dirname(dest), prefix=".extract-")
    try:
        with os.fdopen(fd, "wb") as handle, tarfile.open(tarball, "r:gz") as archive:
            found = None
            for member in archive:
                if member.isfile() and os.path.basename(member.name) == member_name and member.size > 0:
                    found = member
                    break
            if found is None:
                raise ToolError("The downloaded archive does not contain {}.".format(member_name))
            shutil.copyfileobj(archive.extractfile(found), handle)
        if _is_root():
            store.own(temp)
        os.chmod(temp, 0o755)  # nobody but the owner may change the scanner program
        os.replace(temp, dest)
    except (tarfile.TarError, OSError, EOFError) as exc:
        raise ToolError("The downloaded archive could not be unpacked.", "{}: {}".format(type(exc).__name__, exc))
    finally:
        try:
            os.unlink(temp)
        except OSError:
            pass


# ---------------------------------------------------------------- setup
@contextlib.contextmanager
def _locked(exclusive):
    """store.scanner_lock, with a lock that is not freed in time turned into a
    ToolError whose message is safe to show."""
    with contextlib.ExitStack() as stack:
        try:
            stack.enter_context(store.scanner_lock(exclusive))
        except store.StoreError as exc:
            raise ToolError(str(exc), "scanner lock: {}".format(exc))
        yield


def _say(progress, message):
    if progress is not None:
        try:
            progress(message)
        except Exception:
            logger.exception("Vulnerabilities: progress callback failed")


def _check_disk(need=MIN_FREE_BYTES, what="the vulnerability database"):
    free = _free_bytes()
    if free < need:
        raise ToolError(
            "The appliance has {:.1f} GB free and {} needs {:.0f} GB. "
            "Free some disk space and try again.".format(free / 1024 ** 3, what, need / 1024 ** 3),
            "free {} bytes, need {}".format(free, need),
        )


def _install_grype(progress):
    """Install the scanner program; True when this run installed a new copy."""
    dest = grype_path()
    if os.path.isfile(dest):
        _say(progress, "The scanner program is already installed")
        return False
    arch = machine_arch()
    name = _asset_name("grype", arch)
    tarball = _tool_path(name)
    _say(progress, "Downloading the scanner program")
    if not _has_good_file(tarball, ASSETS[name]):
        _check_disk(LIGHT_FREE_BYTES, "the scanner program")
        _download(GRYPE_URL + name, ASSETS[name], tarball, label="the scanner program")
    # Only a tarball whose hash matched gets unpacked, and only the binary named grype comes out of it.
    _extract_member(tarball, "grype", dest)
    # An older version's binary is of no use any more.
    for entry in os.scandir(store.TOOLS_DIR):
        if entry.is_file() and entry.name.startswith("grype-") and entry.name != GRYPE_BINARY and not entry.name.endswith(".tar.gz"):
            try:
                os.unlink(entry.path)
            except OSError:
                pass
    try:
        os.unlink(tarball)  # the extracted binary is all the appliance needs
    except OSError:
        pass
    version = _run([dest, "version"], _grype_env(True), QUICK_TIMEOUT, "The scanner program")
    logger.info("Vulnerabilities: installed grype: %s", _tail(version)[:200])
    return True


def _install_syft(progress):
    for arch in GUEST_ARCHES:
        asset = syft_asset(arch)
        if _has_good_file(asset["path"], asset["sha256"]):
            continue
        _say(progress, "Downloading the software list tool for {} servers".format(arch))
        _check_disk(LIGHT_FREE_BYTES, "the software list tool")
        _download(asset["url"], asset["sha256"], asset["path"], label="the software list tool")


def _setup_grype(progress):
    """The Linux part of setup(), under the exclusive scanner lock."""
    with _locked(True):
        store.ensure_dir(store.TOOLS_DIR)
        store.ensure_dir(store.GRYPE_DB_DIR)
        store.ensure_dir(store.WORK_DIR)
        installed = _install_grype(progress)
        _install_syft(progress)
        _update_status(
            grype={"version": GRYPE_VERSION, "installed_at": store.stamp()},
            syft={"version": SYFT_VERSION},
        )
        # A new Grype version may need to rebuild the database's indexes. That has to happen here, under the
        # exclusive lock, and not during a match, so a new program forces the update even when the listing
        # says the database is unchanged.
        result = db_update(progress, force=installed)
        _update_status(last_error="", last_error_at="")
    return result


def _setup_microsoft(progress):
    """The Microsoft part of setup(): {"updated", "built"} or, on failure (recorded for the settings page by
    vuln_msrc.update), {"updated": False, "built": "", "error": public text}. Never raises."""
    try:
        return update_microsoft(progress)
    except vuln_msrc.MsrcError as exc:
        logger.warning("Vulnerabilities: Microsoft data was not downloaded during setup: %s", exc.detail)
        return {"updated": False, "built": "", "error": exc.public}
    except Exception:
        logger.exception("Vulnerabilities: Microsoft data was not downloaded during setup")
        return {"updated": False, "built": "", "error": "The Microsoft security data update failed unexpectedly."}


def setup(progress=None):
    """Install the scanner program, the guest tarballs and the database (under the exclusive scanner lock), then
    download the Microsoft security data. Safe to run again: files that are present with the right hash are kept.

    The two parts are independent: an appliance that cannot reach github.com or grype.anchore.io still gets the
    Microsoft data, and one that cannot reach Microsoft still gets the scanner. The Microsoft update runs after
    the scanner lock is released and takes its own lock.

    Contract:
    * Success: returns a dict with keys grype, syft, db ({"updated", "built"}) and msrc, which is
      {"updated", "built"} or, when the Microsoft update failed, {"updated": False, "built": "", "error": text}.
      A Microsoft failure never fails the Linux setup.
    * The scanner part fails: the Microsoft update is still attempted, then the original exception (a ToolError
      for the expected failures) is raised with the attribute .msrc set to the same dict as above, so the caller
      can report both."""
    try:
        result = _setup_grype(progress)
    except Exception as exc:
        exc.msrc = _setup_microsoft(progress)
        raise
    return {"grype": GRYPE_VERSION, "syft": SYFT_VERSION, "db": result, "msrc": _setup_microsoft(progress)}


def record_setup_error(message):
    """Remember why the last setup failed (a short redacted sentence), so the
    settings page can show it. A successful setup clears it."""
    try:
        _update_status(last_error=_tail(message)[:300], last_error_at=store.stamp())
    except Exception:
        logger.exception("Vulnerabilities: could not record the setup error")


# ---------------------------------------------------------------- database
def _fetch_listing():
    """The parsed latest.json of the database listing:
    {"status","schemaVersion","built","path","checksum"}."""
    import requests
    from common.methods import get_proxies
    from utilities.helpers import get_ssl_verification

    try:
        response = requests.get(DB_LISTING_URL, verify=get_ssl_verification(), proxies=get_proxies(DB_LISTING_URL),
                                timeout=(10, 60))
        response.raise_for_status()
        data = response.json()
    except (requests.RequestException, ValueError) as exc:
        raise ToolError(
            "CloudBolt could not read the vulnerability database listing. The appliance needs outbound "
            "access to grype.anchore.io (check the proxy settings).",
            "{}: {}".format(type(exc).__name__, _tail(str(exc))),
        )
    if not isinstance(data, dict):
        raise ToolError("The vulnerability database listing is not in the expected format.")
    return data


def _built_from_status(doc):
    """The build time out of `grype db status -o json`. The key names vary by
    Grype version (built, buildTime, ...), so look for a few and take the
    first string that is present."""
    if not isinstance(doc, dict):
        return ""
    for key in ("built", "buildTime", "build_time", "builtAt"):
        if isinstance(doc.get(key), str) and doc[key]:
            return doc[key]
    return ""


def db_update(progress=None, force=False):
    """Update the database. The caller holds the exclusive scanner lock
    (setup() does); update_database() is the entry that takes it. force skips
    the "already current" short-circuit. Returns {"updated": bool, "built": str}."""
    listing = _fetch_listing()
    recorded = _read_status_file().get("db")
    recorded = recorded if isinstance(recorded, dict) else {}
    checksum = str(listing.get("checksum") or "")
    present = bool(_database_file()[0])
    if not force and present and checksum and checksum == recorded.get("checksum"):
        _update_status(db=dict(recorded, checked=store.stamp()))
        return {"updated": False, "built": str(recorded.get("built") or "")}
    if not os.path.isfile(grype_path()):
        raise ToolError("The scanner program is not installed. Run the scanner setup first.")
    _check_disk()
    _say(progress, "Downloading the vulnerability database (about 300 MB, it unpacks to 3.6 GB)")
    # `grype db update` is Grype's own command for this; it verifies the archive's checksum itself.
    # https://oss.anchore.com/docs/reference/grype/configuration/
    _run([grype_path(), "db", "update"], _grype_env(False), DB_UPDATE_TIMEOUT, "The vulnerability database update")
    _say(progress, "Checking the new vulnerability database")
    built, db_status = "", {}
    try:
        # `grype db status -o json` prints the installed database's metadata.
        raw = _run([grype_path(), "db", "status", "-o", "json"], _grype_env(True), QUICK_TIMEOUT,
                   "The vulnerability database check")
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            db_status = parsed
            built = _built_from_status(parsed)
    except (ValueError, ToolError) as exc:
        # The update itself worked; the status text is only for display.
        logger.warning("Vulnerabilities: could not read the database status: %s", _tail(str(exc)))
    built = built or str(listing.get("built") or "")
    if not _database_file()[0]:
        raise ToolError("The vulnerability database update finished but no database file was found.")
    _update_status(db={"built": built, "checked": store.stamp(), "checksum": checksum, "status": db_status})
    return {"updated": True, "built": built}


def update_microsoft(progress=None):
    """Download Microsoft's security data for Windows Server (vuln_msrc.update). It takes its own lock, not the
    scanner lock. Returns {"updated": bool, "built": stamp}; raises vuln_msrc.MsrcError, whose public text is
    safe to show."""
    return vuln_msrc.update(progress)


def update_database(progress=None):
    """db_update() under the exclusive scanner lock."""
    with _locked(True):
        store.ensure_dir(store.GRYPE_DB_DIR)
        return db_update(progress)


# ---------------------------------------------------------------- match
def match(sbom_gz_path, out_json_path):
    """Run Grype on a stored software list (a gzip syft-json file) and write
    its JSON report to out_json_path. The shared scanner lock keeps a database
    update from replacing the database mid-run."""
    _prepare_dirs()
    fd, plain = tempfile.mkstemp(dir=store.WORK_DIR, prefix="sbom-", suffix=".json")
    out_fd, out_temp = tempfile.mkstemp(dir=store.WORK_DIR, prefix="grype-", suffix=".out")
    os.close(out_fd)
    try:
        try:
            # The scanner runs as the service account, so it must be able to read the copy and write the report.
            if _is_root():
                store.own(plain)
                store.own(out_temp)
            with os.fdopen(fd, "wb") as target, gzip.open(sbom_gz_path, "rb") as source:
                copied = 0
                while True:
                    chunk = source.read(1024 * 1024)
                    if not chunk:
                        break
                    copied += len(chunk)
                    if copied > vuln_match.MAX_SBOM_BYTES:
                        raise ToolError("The software list is larger than the limit.",
                                        "inflated past {} bytes".format(vuln_match.MAX_SBOM_BYTES))
                    target.write(chunk)
        except (OSError, EOFError, zlib.error) as exc:
            raise ToolError("The stored software list could not be read.", "{}: {}".format(type(exc).__name__, exc))
        with _locked(False):
            if not os.path.isfile(grype_path()):
                raise ToolError("The scanner program is not installed. A CloudBolt admin can set it up on the "
                                "Vulnerability Settings page.")
            # `sbom:<file>` is Grype's input scheme for a Syft SBOM. The report goes to stdout (-o json) and is
            # written to the file here, which works on every Grype version.
            # https://oss.anchore.com/docs/reference/grype/configuration/
            with open(out_temp, "wb") as handle:
                _run([grype_path(), "sbom:" + plain, "-o", "json", "-q"], _grype_env(True), MATCH_TIMEOUT,
                     MATCH_WHAT, stdout=handle)
        if os.path.getsize(out_temp) == 0:
            raise ToolError("The scanner produced no report.")
        store.own(out_temp)
        os.replace(out_temp, out_json_path)
    finally:
        for path in (plain, out_temp):
            try:
                os.unlink(path)
            except OSError:
                pass
