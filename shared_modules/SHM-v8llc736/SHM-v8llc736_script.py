"""
File archives: versioned files kept on the CloudBolt appliance that scripts
running on servers declare and download.

Admins manage the files on the File Archives admin page
(extensions/XUI-0hv6t61x). A script names the files it needs in header
comment lines:

    # cb-archive: installers/app.tar.gz extract
    # cb-archive: configs/app.conf@4

"installers" is the archive, "app.tar.gz" the file, and "@4" pins a version
(no version, or "@latest", means the newest). "extract" unpacks a zip or tar
file after the download.

Run Task (shared_modules/SHM-571l7249, run_task) calls prepare() on the
rendered script. prepare() issues one short-lived signed link per file and
puts a small download step at the top of the script (after a shebang line).
On the server the step downloads each file into a fresh working directory,
checks its SHA-256 against the store, extracts it if asked, exports
CB_ARCHIVE_DIR and changes into that directory before the script body runs.
Any failure prints an ERROR line and exits 1 before the body starts.

The links point at the extension's download view,
/xui/file_archives/dl/<token>/<file name>, which needs no CloudBolt login: the
token is an HMAC-SHA256 signature over the archive, file, version, server and
expiry, made with a random key kept in the store. CloudBolt's own SECRET_KEY
is a constant in its source, so it is not used. Rotating the key revokes every
link.

Store layout, under /var/opt/cloudbolt/proserv/file_archives (shared between
the nodes of an HA install):

    settings.json                     page settings
    .signing_key                      link signing key (mode 0600)
    <archive>/archive.json            description, creator
    <archive>/<file>/<version>/<file> the file
    <archive>/<file>/<version>/meta.json   sha256, size, uploader, time, note

Every upload is a new version; the newest is what a bare name resolves to.
Restoring an old version copies it to a new version, so history only grows
until the retention count prunes it. Writes go to a temporary directory and
are renamed into place, so a download never sees a partial file.

Other content depends on these names; keep their signatures stable:
prepare, output_lines and link_lifetime (server_tasks.run_task), and
validate_entry, file_choices and ArchiveError (the workflow builder).

The activity log (links issued, downloads, refusals, uploads) is kept in the
database as JSON values of one CustomField in the file_archives namespace, one
value per file, so every node of an HA install sees it.
"""
import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import tempfile
import time
from datetime import datetime, timezone

from django.conf import settings
from django.db import transaction

from infrastructure.models import CustomField, Namespace
from orders.models import CustomFieldValue
from utilities.logger import ThreadLogger

logger = ThreadLogger(__name__)

ROOT = os.path.join(settings.PROSERV_DIR, "file_archives")
KEY_FILE = os.path.join(ROOT, ".signing_key")
SETTINGS_FILE = os.path.join(ROOT, "settings.json")
DEFAULT_SETTINGS = {
    # Base URL servers use to reach CloudBolt, e.g. https://cloudbolt.example.com
    # Empty means the default portal's domain.
    "base_url": "",
    # Have servers verify CloudBolt's TLS certificate. The SHA-256 check guards
    # the file's integrity either way.
    "verify_tls": False,
    "keep_versions": 10,
    # Link lifetime before Run Task's retries and Azure busy waits are added.
    "link_minutes": 15,
    # Largest upload accepted, in MB; the store is on the shared proserv disk.
    "max_upload_mb": 200,
}
MAX_LINK_MINUTES = 60
MAX_UPLOAD_MB = 4096
ACTIVITY_LIMIT = 100

NAME_PATTERN = r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}"
NAME = re.compile(r"^%s$" % NAME_PATTERN)
REF = re.compile(
    r"^(?P<archive>%s)/(?P<file>%s)(?:@(?P<version>latest|[0-9]+))?$" % (NAME_PATTERN, NAME_PATTERN)
)
HEADER = re.compile(r"^[ \t]*#[ \t]*cb-archive:[ \t]*(\S+)[ \t]*(.*?)[ \t]*$", re.MULTILINE)
INLINE = re.compile(r"cb-archive-link:([A-Za-z0-9._@/-]+)")
OPTIONS = {"extract"}
EXTRACTABLE = (".zip", ".tar", ".tar.gz", ".tgz", ".tar.bz2", ".tbz2", ".tar.xz", ".txz")
DOWNLOAD_PATH = "xui/file_archives/dl"


class ArchiveError(Exception):
    """A user-facing problem with an archive, file, version or link."""


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _check_name(value, what):
    if not NAME.match(value or ""):
        raise ArchiveError(
            f"{what} '{value}' must start with a letter or digit and use only letters, "
            "digits, dots, dashes and underscores (100 characters at most)."
        )
    return value


def _read_json(path, default=None):
    try:
        with open(path) as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return default


def _write_json(path, data):
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    fd, temp = tempfile.mkstemp(dir=directory, prefix=".tmp-")
    with os.fdopen(fd, "w") as handle:
        json.dump(data, handle, indent=2, sort_keys=True)
    os.replace(temp, path)


# ---------------------------------------------------------------- settings


def get_settings():
    values = dict(DEFAULT_SETTINGS)
    values.update(_read_json(SETTINGS_FILE, {}) or {})
    return values


def save_settings(base_url, verify_tls, keep_versions, link_minutes, max_upload_mb=None):
    base_url = (base_url or "").strip().rstrip("/")
    if base_url and not re.match(r"^https?://[A-Za-z0-9.:\[\]-]+(/[A-Za-z0-9._~/-]*)?$", base_url):
        raise ArchiveError("The download base URL must look like https://cloudbolt.example.com.")
    if max_upload_mb in (None, ""):
        max_upload_mb = DEFAULT_SETTINGS["max_upload_mb"]
    try:
        keep_versions = int(keep_versions)
        link_minutes = int(link_minutes)
        max_upload_mb = int(max_upload_mb)
    except (TypeError, ValueError):
        raise ArchiveError("Versions to keep, link minutes and the upload limit must be whole numbers.")
    if not 1 <= keep_versions <= 1000:
        raise ArchiveError("Versions to keep must be between 1 and 1000.")
    if not 1 <= link_minutes <= MAX_LINK_MINUTES:
        raise ArchiveError(f"Link minutes must be between 1 and {MAX_LINK_MINUTES}.")
    if not 1 <= max_upload_mb <= MAX_UPLOAD_MB:
        raise ArchiveError(f"The upload limit must be between 1 and {MAX_UPLOAD_MB} MB.")
    values = {
        "base_url": base_url,
        "verify_tls": bool(verify_tls),
        "keep_versions": keep_versions,
        "link_minutes": link_minutes,
        "max_upload_mb": max_upload_mb,
    }
    _write_json(SETTINGS_FILE, values)
    return values


def default_base_url():
    """The default portal's https://<domain>, or '' when its domain is not set."""
    from portals.models import PortalConfig

    portal = PortalConfig.get_current_portal()
    url = portal.site_url if portal else None
    return (url or "").rstrip("/")


def base_url():
    url = get_settings()["base_url"] or default_base_url()
    if not url:
        raise ArchiveError(
            "CloudBolt does not know the URL servers use to reach it. Set the download "
            "base URL on the File Archives admin page, or the default portal's domain."
        )
    return url


# ---------------------------------------------------------------- store


def _archive_dir(archive):
    return os.path.join(ROOT, _check_name(archive, "Archive name"))


def _file_dir(archive, name):
    return os.path.join(_archive_dir(archive), _check_name(name, "File name"))


def versions(archive, name):
    directory = _file_dir(archive, name)
    try:
        entries = os.listdir(directory)
    except OSError:
        return []
    found = []
    for entry in entries:
        if entry.isdigit():
            meta = _read_json(os.path.join(directory, entry, "meta.json"))
            if meta and os.path.isfile(os.path.join(directory, entry, name)):
                found.append(meta)
    return sorted(found, key=lambda meta: meta["version"])


def list_archives():
    """Every archive with its files, each file with its versions (oldest first)."""
    archives = []
    try:
        names = sorted(os.listdir(ROOT), key=str.lower)
    except OSError:
        return archives
    for archive in names:
        info = archive_info(archive)
        if info is not None:
            archives.append(info)
    return archives


def archive_info(archive):
    """One archive as list_archives() returns it, or None when it does not exist."""
    if not NAME.match(archive or ""):
        return None
    info = _read_json(os.path.join(ROOT, archive, "archive.json"))
    if info is None:
        return None
    files = []
    for name in sorted(os.listdir(os.path.join(ROOT, archive)), key=str.lower):
        if NAME.match(name) and os.path.isdir(os.path.join(ROOT, archive, name)):
            history = versions(archive, name)
            if history:
                files.append({"name": name, "versions": history, "latest": history[-1]})
    return dict(info, name=archive, files=files)


def create_archive(archive, description, username):
    if archive.lower() == "add":
        raise ArchiveError("'add' is reserved by the File Archives page; choose another archive name.")
    directory = _archive_dir(archive)
    if os.path.exists(directory):
        raise ArchiveError(f"Archive '{archive}' already exists.")
    os.makedirs(directory)
    _write_json(
        os.path.join(directory, "archive.json"),
        {"description": (description or "").strip()[:500], "created_by": username, "created_at": _now()},
    )


def update_archive(archive, description):
    path = os.path.join(_archive_dir(archive), "archive.json")
    info = _read_json(path)
    if info is None:
        raise ArchiveError(f"Archive '{archive}' does not exist.")
    info["description"] = (description or "").strip()[:500]
    _write_json(path, info)


def delete_archive(archive):
    directory = _archive_dir(archive)
    if not os.path.isfile(os.path.join(directory, "archive.json")):
        raise ArchiveError(f"Archive '{archive}' does not exist.")
    shutil.rmtree(directory)


def add_version(archive, name, chunks, username, note="", restored_from=None):
    """
    Store the bytes from chunks (an iterable of bytes) as the next version of
    archive/name and return its metadata. Prunes versions beyond the retention
    count, never the new one. Refuses more than the max_upload_mb setting
    (restores copy a stored version, so they are not limited).
    """
    if not os.path.isfile(os.path.join(_archive_dir(archive), "archive.json")):
        raise ArchiveError(f"Archive '{archive}' does not exist.")
    limit_mb = get_settings()["max_upload_mb"]
    limit = None if restored_from else limit_mb * 1024 * 1024
    directory = _file_dir(archive, name)
    os.makedirs(directory, exist_ok=True)
    staging = tempfile.mkdtemp(dir=directory, prefix=".upload-")
    try:
        digest, size = hashlib.sha256(), 0
        with open(os.path.join(staging, name), "wb") as handle:
            for chunk in chunks:
                digest.update(chunk)
                size += len(chunk)
                if limit and size > limit:
                    raise ArchiveError(f"The file is larger than the {limit_mb} MB upload limit in Settings.")
                handle.write(chunk)
        if not size:
            raise ArchiveError("The file is empty.")
        # A concurrent upload can take the same number; retry with the next one.
        for _ in range(5):
            history = versions(archive, name)
            version = (history[-1]["version"] + 1) if history else 1
            meta = {
                "version": version,
                "sha256": digest.hexdigest(),
                "size": size,
                "uploaded_by": username,
                "uploaded_at": _now(),
                "note": (note or "").strip()[:500],
            }
            if restored_from:
                meta["restored_from"] = restored_from
            _write_json(os.path.join(staging, "meta.json"), meta)
            try:
                os.rename(staging, os.path.join(directory, str(version)))
                break
            except OSError:
                if not os.path.exists(os.path.join(directory, str(version))):
                    raise
        else:
            raise ArchiveError("Could not store the file; try again.")
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        if not versions(archive, name):
            shutil.rmtree(directory, ignore_errors=True)
        raise
    _prune(archive, name, keep_version=version)
    return meta


def _prune(archive, name, keep_version):
    keep = get_settings()["keep_versions"]
    history = versions(archive, name)
    for meta in history[:-keep] if len(history) > keep else []:
        if meta["version"] != keep_version:
            shutil.rmtree(os.path.join(_file_dir(archive, name), str(meta["version"])), ignore_errors=True)


def restore_version(archive, name, version, username):
    """Copy an old version to a new, latest version."""
    path = _version_path(archive, name, version)

    def chunks():
        with open(path, "rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                yield chunk

    return add_version(archive, name, chunks(), username, note=f"Restored from version {version}", restored_from=version)


def delete_version(archive, name, version):
    _version_path(archive, name, version)
    directory = _file_dir(archive, name)
    shutil.rmtree(os.path.join(directory, str(int(version))))
    if not versions(archive, name):
        shutil.rmtree(directory, ignore_errors=True)


def delete_file(archive, name):
    directory = _file_dir(archive, name)
    if not os.path.isdir(directory):
        raise ArchiveError(f"File '{archive}/{name}' does not exist.")
    shutil.rmtree(directory)


def _version_path(archive, name, version):
    path = os.path.join(_file_dir(archive, name), str(int(version)), name)
    if not os.path.isfile(path):
        raise ArchiveError(f"Version {version} of '{archive}/{name}' does not exist.")
    return path


def resolve(ref):
    """
    Resolve "archive/file", "archive/file@latest" or "archive/file@N" to a
    dict with ref, archive, file, version, sha256, size and path.
    """
    match = REF.match((ref or "").strip())
    if not match:
        raise ArchiveError(
            f"'{ref}' is not a file reference. Use archive/file, archive/file@latest or archive/file@3."
        )
    archive, name, wanted = match.group("archive"), match.group("file"), match.group("version")
    history = versions(archive, name)
    if not history:
        raise ArchiveError(f"File '{archive}/{name}' does not exist in File Archives.")
    if wanted in (None, "latest"):
        meta = history[-1]
    else:
        meta = next((m for m in history if m["version"] == int(wanted)), None)
        if meta is None:
            raise ArchiveError(f"Version {wanted} of '{archive}/{name}' does not exist (or was pruned).")
    return {
        "ref": f"{archive}/{name}",
        "archive": archive,
        "file": name,
        "version": meta["version"],
        "sha256": meta["sha256"],
        "size": meta["size"],
        "path": os.path.join(_file_dir(archive, name), str(meta["version"]), name),
    }


# ---------------------------------------------------------------- links


def _key():
    try:
        with open(KEY_FILE, "rb") as handle:
            key = handle.read()
        if len(key) >= 32:
            return key
    except OSError:
        pass
    os.makedirs(ROOT, exist_ok=True)
    try:
        fd = os.open(KEY_FILE, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        # Another process created it first; use theirs.
        with open(KEY_FILE, "rb") as handle:
            return handle.read()
    with os.fdopen(fd, "wb") as handle:
        key = secrets.token_bytes(32)
        handle.write(key)
    return key


def rotate_key():
    """Replace the signing key: every link issued so far stops working."""
    os.makedirs(ROOT, exist_ok=True)
    fd, temp = tempfile.mkstemp(dir=ROOT, prefix=".key-")
    os.chmod(temp, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(secrets.token_bytes(32))
    os.replace(temp, KEY_FILE)


def _b64(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(text):
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def make_token(resolved, minutes, server=None, job=None):
    payload = {
        "r": resolved["ref"],
        "v": resolved["version"],
        "e": int(time.time() + 60 * min(max(int(minutes), 1), MAX_LINK_MINUTES)),
        "n": secrets.token_hex(4),
    }
    if server is not None:
        payload["s"] = server.global_id
    if job is not None:
        payload["j"] = job.id
    body = _b64(json.dumps(payload, separators=(",", ":")).encode())
    signature = _b64(hmac.new(_key(), body.encode(), hashlib.sha256).digest())
    return f"{body}.{signature}"


def read_token(token):
    """The token's payload if its signature is good and it has not expired, else None."""
    try:
        body, signature = token.split(".")
        expected = _b64(hmac.new(_key(), body.encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(signature, expected):
            return None
        payload = json.loads(_unb64(body))
    except (ValueError, TypeError):
        return None
    if not isinstance(payload, dict) or payload.get("e", 0) < time.time():
        return None
    return payload


def link_lifetime(execution_timeout=None, attempts=1):
    """
    Minutes a link must live when it is issued once before a run: the base
    lifetime plus, per attempt, the script's timeout and Azure's 5-minute
    busy wait. Capped at an hour.
    """
    per_attempt = int(execution_timeout or 300) / 60 + 5
    minutes = get_settings()["link_minutes"] + max(int(attempts or 1), 1) * per_attempt
    return int(min(minutes, MAX_LINK_MINUTES))


def issue_link(ref, minutes=None, server=None, job=None):
    """A signed download URL for ref, and the resolved file it names."""
    resolved = resolve(ref)
    minutes = minutes or get_settings()["link_minutes"]
    token = make_token(resolved, minutes, server=server, job=job)
    url = f"{base_url()}/{DOWNLOAD_PATH}/{token}/{resolved['file']}"
    detail = f"version {resolved['version']}, valid {int(minutes)} min"
    if server is not None:
        detail += f", for {server.hostname}"
    if job is not None:
        detail += f", job {job.id}"
    record(resolved["ref"], "link issued", detail)
    return url, resolved


# ---------------------------------------------------------------- activity log


def _activity_field():
    """The activity field, created on first use in its own Namespace so it stays off Parameters."""
    namespace, _ = Namespace.objects.get_or_create(name="file_archives")
    field, _ = CustomField.objects.get_or_create(
        name="file_archives_activity",
        defaults={
            "label": "File archives activity",
            "description": "Activity log of the File Archives admin page, one JSON value per file.",
            "type": "TXT",
            "namespace": namespace,
        },
    )
    if field.namespace_id != namespace.id:
        field.namespace = namespace
        field.save()
    return field


def record(ref, event, detail="", username=None):
    """Append one event to ref's activity log (newest first, capped)."""
    entry = {"at": _now(), "event": event, "detail": detail}
    if username:
        entry["by"] = username
    try:
        field = _activity_field()
        with transaction.atomic():
            list(CustomField.objects.select_for_update().filter(pk=field.pk))
            row, data = None, None
            for cfv in CustomFieldValue.objects.filter(field=field).order_by("id"):
                value = _read_value(cfv)
                if value.get("ref") == ref:
                    row, data = cfv, value
                    break
            data = data or {"ref": ref, "events": []}
            data["events"] = ([entry] + data.get("events", []))[:ACTIVITY_LIMIT]
            # Set through the typed value property and save, so the row is
            # updated in place and attached to no object.
            row = row or CustomFieldValue(field=field)
            row.value = json.dumps(data)
            row.save()
    except Exception:
        # The log must never stop a download or a run.
        logger.exception("Could not record file archive activity for %s", ref)


def _read_value(cfv):
    try:
        value = json.loads(cfv.txt_value or "{}")
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def activity(ref=None, limit=50, archive=None):
    """Recent events for ref, for every file of archive, or for all files; newest first."""
    events = []
    field = CustomField.objects.filter(name="file_archives_activity").first()
    if field is None:
        return events
    for cfv in CustomFieldValue.objects.filter(field=field):
        value = _read_value(cfv)
        found = value.get("ref", "")
        if archive is not None and not found.startswith(archive + "/"):
            continue
        if ref is None or found == ref:
            events.extend(dict(entry, ref=value.get("ref")) for entry in value.get("events", []))
    return sorted(events, key=lambda entry: entry["at"], reverse=True)[:limit]


def forget(prefix):
    """Drop the activity of a deleted file ("archive/file") or archive ("archive/")."""
    field = CustomField.objects.filter(name="file_archives_activity").first()
    if field is None:
        return
    for cfv in CustomFieldValue.objects.filter(field=field):
        ref = _read_value(cfv).get("ref", "")
        if ref == prefix or (prefix.endswith("/") and ref.startswith(prefix)):
            cfv.delete()


# ---------------------------------------------------------------- scripts


def parse_header(script):
    """
    The files a script declares with "# cb-archive: archive/file[@version] [extract]"
    lines, as a list of (ref, options) in order. Raises ArchiveError on an
    unknown option.
    """
    found = []
    for ref, rest in HEADER.findall(script or ""):
        options = set(rest.split())
        unknown = options - OPTIONS
        if unknown:
            raise ArchiveError(f"Unknown cb-archive option(s) for {ref}: {', '.join(sorted(unknown))}.")
        found.append((ref, options))
    return found


def file_choices():
    """
    Every stored file, for pickers (the workflow builder's file list): dicts of
    ref ("archive/file"), archive, file, latest, versions and extractable.
    """
    choices = []
    for archive in list_archives():
        for item in archive["files"]:
            choices.append(
                {
                    "ref": f"{archive['name']}/{item['name']}",
                    "archive": archive["name"],
                    "file": item["name"],
                    "latest": item["latest"]["version"],
                    "versions": [meta["version"] for meta in item["versions"]],
                    "extractable": item["name"].lower().endswith(EXTRACTABLE),
                }
            )
    return choices


def validate_entry(entry, must_exist=True):
    """
    Check one entry written like a header ("archive/file[@latest|@N] [extract]"),
    as run_task's archive_refs takes them. Raises ArchiveError; with
    must_exist, also when the file or version is not stored.
    """
    found = parse_header(f"# cb-archive: {(entry or '').strip()}")
    if len(found) != 1:
        raise ArchiveError(f"'{entry}' is not a file reference. Use archive/file, archive/file@latest or archive/file@3.")
    ref, _options = found[0]
    if must_exist:
        resolve(ref)
    elif not REF.match(ref):
        raise ArchiveError(f"'{ref}' is not a file reference. Use archive/file, archive/file@latest or archive/file@3.")


def output_name(file_name):
    return "archive_%s_version" % re.sub(r"[^A-Za-z0-9_]", "_", file_name)


def output_lines(outputs):
    """CB_OUTPUT lines for prepare()'s outputs, for run_task to append to a script's output."""
    return "".join(f"\nCB_OUTPUT {name}={value}" for name, value in sorted((outputs or {}).items()))


def prepare(script, server, job=None, extra_refs=None, minutes=None):
    """
    Return (script, outputs). script gets a download step for every file it
    declares in a cb-archive header and every entry in extra_refs (files a
    workflow step attached, written like a header: "archive/file[@version]
    [extract]"), and every cb-archive-link:<ref> marker replaced by
    a fresh link. outputs maps archive_<file>_version to the version each file
    resolved to, known here before the script runs. A script that uses
    neither comes back unchanged with empty outputs.
    """
    wanted = parse_header(script) + parse_header(
        "\n".join(f"# cb-archive: {ref}" for ref in (extra_refs or []))
    )
    markers = INLINE.findall(script or "")
    if not wanted and not markers:
        return script, {}
    minutes = minutes or link_lifetime()
    outputs, items, names = {}, [], set()
    for ref, options in wanted:
        url, resolved = issue_link(ref, minutes=minutes, server=server, job=job)
        if resolved["file"] in names:
            raise ArchiveError(f"Two declared files are named {resolved['file']}; they would overwrite each other.")
        names.add(resolved["file"])
        # "extract" unpacks zip and tar files; any other file stays as downloaded.
        extract = "extract" in options and resolved["file"].lower().endswith(EXTRACTABLE)
        items.append((url, resolved, extract))
        outputs[output_name(resolved["file"])] = resolved["version"]

    def replace(match):
        url, resolved = issue_link(match.group(1), minutes=minutes, server=server, job=job)
        outputs[output_name(resolved["file"])] = resolved["version"]
        return url

    script = INLINE.sub(replace, script)
    if items:
        tag = f"{job.id if job is not None else 'run'}-{secrets.token_hex(3)}"
        step = download_step(server.is_windows(), items, tag, get_settings()["verify_tls"])
        script = _insert(script, step, server.is_windows())
    files = ", ".join(f"{r['ref']} v{r['version']}" for _, r, _ in items)
    if files:
        logger.info("File archives for %s: %s", server.hostname, files)
    return script, outputs


def _insert(script, step, windows):
    """Put step after a leading shebang (Linux) or at the top (Windows)."""
    if windows:
        first = next((line.strip().lower() for line in script.splitlines()
                      if line.strip() and not line.strip().startswith("#")), "")
        if first.startswith(("param", "[cmdletbinding", "using ")):
            raise ArchiveError(
                "A PowerShell script that declares archives cannot start with param(), "
                "[CmdletBinding()] or using: the download step has to run first."
            )
        return step + "\n" + script
    if script.startswith("#!"):
        shebang, _, rest = script.partition("\n")
        return shebang + "\n" + step + "\n" + rest
    return step + "\n" + script


def download_step(windows, items, tag, verify_tls=False):
    """
    The shell text that fetches items, a list of (url, resolved, extract), on
    the server. URLs, names and digests only use characters that need no
    quoting beyond single quotes. Kept small: Azure Run Command passes Windows
    scripts on the command line (about 9,000 characters for the whole script).
    """
    if windows:
        return _powershell_step(items, tag, verify_tls)
    return _sh_step(items, tag, verify_tls)


def _sh_step(items, tag, verify_tls):
    curl_k = "" if verify_tls else " -k"
    wget_k = "" if verify_tls else " --no-check-certificate"
    lines = [
        "# ---- CloudBolt file archives: download declared files ----",
        'cbfa_fail() { echo "ERROR: $*"; echo "ERROR: $*" >&2; exit 1; }',
        'cbfa_tmp="${TMPDIR:-/tmp}"',
        "find \"$cbfa_tmp\" -maxdepth 1 -name 'cb-archives-*' -mtime +0 -exec rm -rf {} + 2>/dev/null",
        'CB_ARCHIVE_DIR="$cbfa_tmp/cb-archives-%s"' % tag,
        "export CB_ARCHIVE_DIR",
        '(umask 077; mkdir -p "$CB_ARCHIVE_DIR") && cd "$CB_ARCHIVE_DIR" || cbfa_fail "Cannot use $CB_ARCHIVE_DIR"',
        "cbfa_get() {",
        "  if command -v curl >/dev/null 2>&1; then curl -fsSL --retry 3%s -o \"$2\" \"$1\"" % curl_k,
        "  elif command -v wget >/dev/null 2>&1; then wget -q%s -O \"$2\" \"$1\"" % wget_k,
        '  else cbfa_fail "curl or wget is needed to download $2"; fi || cbfa_fail "Download of $2 failed"',
        "  cbfa_sum=$( (sha256sum \"$2\" 2>/dev/null || shasum -a 256 \"$2\") | cut -d' ' -f1)",
        '  [ "$cbfa_sum" = "$3" ] || cbfa_fail "Checksum mismatch for $2"',
        '  case "$4:$2" in',
        '    1:*.zip) unzip -qo "$2" >/dev/null 2>&1 || python3 -m zipfile -e "$2" . || cbfa_fail "Cannot unzip $2 (install unzip)" ;;',
        '    1:*) tar -xf "$2" || cbfa_fail "Cannot extract $2" ;;',
        "  esac",
        '  echo "Downloaded $2"',
        "}",
    ]
    for url, resolved, extract in items:
        lines.append("cbfa_get '%s' '%s' '%s' %d" % (url, resolved["file"], resolved["sha256"], 1 if extract else 0))
    lines.append("# ---- end of file archives ----")
    return "\n".join(lines)


def _powershell_step(items, tag, verify_tls):
    lines = [
        "# ---- CloudBolt file archives: download declared files ----",
        "$ProgressPreference = 'SilentlyContinue'",
        "function CbfaFail([string]$m) { Write-Output \"ERROR: $m\"; [Console]::Error.WriteLine(\"ERROR: $m\"); exit 1 }",
        "function CbfaGet([string]$u, [string]$n, [string]$h, [int]$x) {",
        "  try {",
        "    $f = Join-Path $env:CB_ARCHIVE_DIR $n",
        "    (New-Object Net.WebClient).DownloadFile($u, $f)",
        "    if ((Get-FileHash -Algorithm SHA256 -LiteralPath $f).Hash -ne $h) { throw \"Checksum mismatch for $n\" }",
        "    if ($x -and $n -like '*.zip') { Expand-Archive -LiteralPath $f -DestinationPath $env:CB_ARCHIVE_DIR -Force }",
        "    elseif ($x) { tar.exe -xf $f -C $env:CB_ARCHIVE_DIR; if ($LASTEXITCODE) { throw \"tar could not extract $n\" } }",
        "    Write-Output \"Downloaded $n\"",
        "  } catch { CbfaFail \"File archive $n`: $($_.Exception.Message)\" }",
        "}",
        "[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor 3072",
    ]
    if not verify_tls:
        lines.append("[Net.ServicePointManager]::ServerCertificateValidationCallback = { $true }")
    lines += [
        "$cbfaTmp = [IO.Path]::GetTempPath()",
        "Get-ChildItem -LiteralPath $cbfaTmp -Directory -Filter 'cb-archives-*' -ErrorAction SilentlyContinue |"
        " Where-Object { $_.LastWriteTime -lt (Get-Date).AddDays(-1) } |"
        " Remove-Item -Recurse -Force -ErrorAction SilentlyContinue",
        "$env:CB_ARCHIVE_DIR = Join-Path $cbfaTmp 'cb-archives-%s'" % tag,
        "try { New-Item -ItemType Directory -Force -Path $env:CB_ARCHIVE_DIR | Out-Null; Set-Location -LiteralPath $env:CB_ARCHIVE_DIR }",
        "catch { CbfaFail \"Cannot use $env:CB_ARCHIVE_DIR`: $($_.Exception.Message)\" }",
    ]
    for url, resolved, extract in items:
        lines.append("CbfaGet '%s' '%s' '%s' %d" % (url, resolved["file"], resolved["sha256"], 1 if extract else 0))
    lines.append("# ---- end of file archives ----")
    return "\n".join(lines)
