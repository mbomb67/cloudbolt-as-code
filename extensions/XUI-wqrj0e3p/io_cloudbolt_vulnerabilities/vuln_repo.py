"""
Vulnerabilities XUI: where a server's software list is kept, and the two
routes that carry it.

The server builds its software list (a Syft SBOM) itself and uploads it to the
vuln_upload route with curl, gzipped. The token travels in the X-Vuln-Token
header (never in the URL, so access logs do not hold it). The same token lets
the server download the Syft tarball from the vuln_tool route. The
per-install key is vuln_store.signing_key; the token binds the scan id, the
server id, the file names it allows and an expiry.

An upload is accepted once per scan, only while the scan's pending marker
exists (the scan start makes it, the scan end removes it), and only while the
appliance has room. An OpenShift Virtualization VM usually cannot reach
CloudBolt, so the job reads its file over the API tunnel and stores it with
store_stream.

Stored file: INVENTORY_DIR/<server id>/<scan id>.sbom.json.gz.
"""
import base64
import hashlib
import hmac
import json
import os
import re
import shutil
import tempfile
import time

from django.http import FileResponse, Http404, HttpResponse
from django.urls import reverse

from utilities.logger import ThreadLogger

from . import vuln_store as store
from . import vuln_tools as tools

logger = ThreadLogger(__name__)

UPLOAD_NAME = "sbom.json.gz"
MAX_UPLOAD_BYTES = 100 * 1024 * 1024
# Free space that must remain on the appliance after an upload is written.
RESERVE_BYTES = 1024 ** 3
TOKEN_HEADER = "HTTP_X_VULN_TOKEN"
GZIP_MAGIC = b"\x1f\x8b"
GENERIC_PUBLIC = "The software list could not be stored. Ask an administrator to check the Vulnerabilities folder."
BAD_SITE_URL = "The portal's site URL is not usable. Ask a CloudBolt admin to check it in the portal settings."
# The URL goes into the guest script and into curl: no whitespace, quotes, backslashes or control characters.
SITE_URL_RE = re.compile(r'https?://[^\s"\\\x00-\x1f\x7f]+')
PENDING_SUFFIX = ".pending"
STORED_SUFFIX = ".sbom.json.gz"


class RepoError(Exception):
    """public is safe for any viewer; detail is for the log."""

    def __init__(self, public=GENERIC_PUBLIC, detail=""):
        super().__init__(public)
        self.public = public
        self.detail = detail or public


# ---------------------------------------------------------------- tokens
def _b64(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(text):
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _tool_names():
    return [tools.syft_asset(arch)["name"] for arch in tools.GUEST_ARCHES]


def make_token(scan_id, server_id, names, minutes, key=None, now=None):
    """A signed token: scan id, server id, allowed file names, expiry."""
    payload = {
        "s": scan_id,
        "v": int(server_id),
        "n": sorted(names),
        "e": int((now if now is not None else time.time()) + 60 * int(minutes)),
    }
    body = _b64(json.dumps(payload, separators=(",", ":")).encode())
    signature = _b64(hmac.new(key or store.signing_key(), body.encode(), hashlib.sha256).digest())
    return "{}.{}".format(body, signature)


def read_token(token, scan_id, name, key=None, now=None):
    """The token's payload when its signature is good, it has not expired, it
    is for this scan and it allows the file name. Http404 on anything else, so
    a caller learns nothing."""
    try:
        body, signature = (token or "").split(".")
        expected = _b64(hmac.new(key or store.signing_key(), body.encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(signature, expected):
            raise Http404
        payload = json.loads(_unb64(body))
    except (ValueError, TypeError, AttributeError):
        raise Http404
    if not isinstance(payload, dict):
        raise Http404
    if payload.get("e", 0) < (now if now is not None else time.time()):
        raise Http404
    if payload.get("s") != scan_id or name not in (payload.get("n") or []):
        raise Http404
    if name != UPLOAD_NAME and name not in _tool_names():
        raise Http404
    if not store.SCAN_ID_RE.fullmatch(str(payload.get("s", ""))) or not isinstance(payload.get("v"), int):
        raise Http404
    return payload


# ---------------------------------------------------------------- files
def _contained(*parts):
    """A path inside INVENTORY_DIR; Http404 when it would leave it."""
    try:
        return store.contained(store.INVENTORY_DIR, *parts)
    except store.StoreError:
        raise Http404


def _server_dir(server_id):
    return _contained(str(int(server_id)))


def _pending_path(server_id, scan_id):
    if not store.SCAN_ID_RE.fullmatch(scan_id or ""):
        raise Http404
    return _contained(str(int(server_id)), scan_id + PENDING_SUFFIX)


def _final_path(server_id, scan_id):
    if not store.SCAN_ID_RE.fullmatch(scan_id or ""):
        raise Http404
    return _contained(str(int(server_id)), scan_id + STORED_SUFFIX)


def prepare(server_id, scan_id):
    """Make the pending upload slot of this scan; uploads are refused without it."""
    try:
        store.ensure_dir(_server_dir(server_id))
        pending = _pending_path(server_id, scan_id)
        with open(pending, "wb"):
            pass
        store.own(pending)
    except (OSError, Http404, store.StoreError) as exc:
        raise RepoError(detail="Could not prepare the upload slot of scan {}: {}".format(scan_id, exc))


def inventory_path(server_id, scan_id):
    """The stored file of the scan, or None when it never arrived."""
    try:
        path = _final_path(server_id, scan_id)
    except Http404:
        return None
    return path if os.path.isfile(path) else None


def discard(server_id, scan_id):
    """Remove one scan's stored file and pending marker (a scan that failed)."""
    for path in (_final_path(server_id, scan_id), _pending_path(server_id, scan_id)):
        try:
            os.unlink(path)
        except OSError:
            pass


def prune(server_id, keep_scan_id):
    """Keep only that scan's stored file for the server; the next match works
    from it and an older one is of no use."""
    base = _server_dir(server_id)
    if not os.path.isdir(base):
        return
    keep = keep_scan_id + STORED_SUFFIX
    for entry in os.scandir(base):
        if entry.name == keep:
            continue
        try:
            if entry.is_dir(follow_symlinks=False):
                shutil.rmtree(entry.path, ignore_errors=True)
            else:
                os.unlink(entry.path)
        except OSError:
            logger.warning("Vulnerabilities: could not remove old inventory file %s", entry.name)


def forget(server_id):
    """Remove a deleted server's whole inventories/<id>/ folder (its stored software list, any
    pending marker). Returns True when the folder existed. The caller makes sure no scan is
    running on the server."""
    try:
        base = _server_dir(server_id)
    except Http404:
        return False
    if not os.path.isdir(base):
        return False
    shutil.rmtree(base, ignore_errors=True)
    return True


def known_ids():
    """The ids of every server that has an inventories/<id>/ folder, as a sorted list."""
    try:
        names = os.listdir(store.INVENTORY_DIR)
    except OSError:
        return []
    return sorted(int(name) for name in names if name.isascii() and name.isdigit())


def _room_for(size):
    try:
        return shutil.disk_usage(store.ROOT if os.path.isdir(store.ROOT) else os.path.dirname(store.ROOT)).free \
            >= size + RESERVE_BYTES
    except OSError:
        return False


def _write_stream(server_id, scan_id, chunks, limit):
    """Write chunks to a temp file in the server's folder, check it is gzip and
    link it to its final name (link, not replace: a second upload cannot win a
    race). Returns the final path. Raises ValueError with a reason."""
    directory = _server_dir(server_id)
    final = _final_path(server_id, scan_id)
    pending = _pending_path(server_id, scan_id)
    if not os.path.isfile(pending):
        raise ValueError("the scan's upload slot is gone")
    if os.path.exists(final):
        raise ValueError("a file was already stored for this scan")
    fd, temp = tempfile.mkstemp(dir=directory, prefix=".upload-")
    total = 0
    head = b""  # bytes held back until there are two to check for the gzip magic
    checked = False
    try:
        with os.fdopen(fd, "wb") as handle:
            for chunk in chunks:
                if not chunk:
                    continue
                if not checked:
                    head += chunk
                    if len(head) < 2:
                        continue
                    if head[:2] != GZIP_MAGIC:
                        raise ValueError("the file is not gzip")
                    checked = True
                    chunk, head = head, b""
                total += len(chunk)
                if total > limit:
                    raise ValueError("the file is too large")
                handle.write(chunk)
        if not checked and head:
            raise ValueError("the file is not gzip")
        if total == 0:
            raise ValueError("the file is empty")
        store.own(temp)
        os.link(temp, final)
        try:
            os.unlink(pending)
        except OSError:
            pass
        return final
    finally:
        try:
            os.unlink(temp)
        except OSError:
            pass


def receive_upload(request, scan_id, name):
    """Store a PUT body for a valid token. Http404 on any failure."""
    payload = read_token(request.META.get(TOKEN_HEADER, ""), scan_id, name)
    if request.method != "PUT" or name != UPLOAD_NAME:
        raise Http404
    try:
        declared = int(request.META.get("CONTENT_LENGTH") or 0)
    except ValueError:
        raise Http404
    if declared <= 0 or declared > MAX_UPLOAD_BYTES or not _room_for(declared):
        raise Http404

    def chunks():
        while True:
            data = request.read(64 * 1024)
            if not data:
                return
            yield data

    try:
        _write_stream(payload["v"], payload["s"], chunks(), min(declared, MAX_UPLOAD_BYTES))
    except (ValueError, OSError, Http404, store.StoreError):
        raise Http404
    response = HttpResponse(status=204)
    response["Cache-Control"] = "no-store"
    return response


def store_stream(server_id, scan_id, chunks):
    """Store the file a job read from the server itself; chunks is an iterable
    of bytes. The upload rules apply. Returns the final path."""
    if not _room_for(MAX_UPLOAD_BYTES):
        raise RepoError("The appliance does not have room for the software list.", "no free space")
    try:
        return _write_stream(server_id, scan_id, chunks, MAX_UPLOAD_BYTES)
    except (ValueError, OSError, Http404, store.StoreError) as exc:
        raise RepoError("The software list could not be read from the server: {}.".format(exc), repr(exc))


def serve_tool(request, scan_id, name):
    """Stream a Syft tarball to a server that holds a valid token. GET only."""
    read_token(request.META.get(TOKEN_HEADER, ""), scan_id, name)
    if request.method != "GET" or name not in _tool_names():
        raise Http404
    for arch in tools.GUEST_ARCHES:
        asset = tools.syft_asset(arch)
        if asset["name"] == name:
            if not os.path.isfile(asset["path"]):
                raise Http404
            response = FileResponse(open(asset["path"], "rb"), as_attachment=True, filename=name,
                                    content_type="application/gzip")
            response["X-Content-Type-Options"] = "nosniff"
            response["Cache-Control"] = "private, no-store"
            return response
    raise Http404


def targets(base_url, scan_id, server_id, minutes):
    """What the server uses: a dict with "token", "upload_url" and "tools" (one
    entry per architecture with "url", "sha256", "github_url" and "name").
    base_url must be the portal's configured site URL, never a request's Host."""
    if not base_url:
        raise RepoError(detail="The default portal has no site URL.")
    if not SITE_URL_RE.fullmatch(base_url):
        raise RepoError(BAD_SITE_URL, "The portal's site URL has characters that are not allowed.")
    base = base_url.rstrip("/")
    names = [UPLOAD_NAME] + _tool_names()
    found = {}
    for arch in tools.GUEST_ARCHES:
        asset = tools.syft_asset(arch)
        found[arch] = {
            "url": "{}{}".format(base, reverse("vuln_tool", args=[scan_id, asset["name"]])),
            "sha256": asset["sha256"],
            "github_url": asset["url"],
            "name": asset["name"],
        }
    return {
        "token": make_token(scan_id, server_id, names, minutes),
        "upload_url": "{}{}".format(base, reverse("vuln_upload", args=[scan_id, UPLOAD_NAME])),
        "tools": found,
    }
