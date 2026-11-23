"""
OpenSCAP XUI: where the result files of a scan are kept.

The full results (the HTML report, the XCCDF results and the ARF file) never
go into the CloudBolt database. The target server uploads them itself, with
curl, to one of two backends that share this small interface. The exception
is an OpenShift Virtualization VM with the local backend: such a guest
usually has no route to CloudBolt, so the job reads the files over the API
tunnel it ran the scan through and stores them with store_stream.

    upload_targets(server, scan_id, base_url, minutes) -> urls, token, region
    location(server, scan_id, region)                  -> where the files will be
    link_or_response(server, scan, kind, request)      -> redirect or stream
    prune(server, cfg)                                 -> scan ids removed

S3: the file goes to a presigned PUT URL minted from the credentials of a
CloudBolt AWS resource handler; viewing redirects to a short-lived presigned
GET URL. Nothing is pruned: use a bucket lifecycle rule.

Local: files live under PROSERV_DIR/openscap/reports/<server id>/<scan id>/.
The target PUTs them to the openscap_upload route with an HMAC-SHA256 token in
the X-Oscap-Token header (never in the URL, so access logs do not hold it).
The per-install random key is oscap_store.signing_key; the token binds the
scan id, the server id, the allowed file names and an expiry. An upload is
accepted once per file name, only while the scan's directory exists (start
creates it, retention removes it), and only while the backend stays under the
space limit. Viewing needs a CloudBolt login and the view permission on the
server. The report is an HTML page with its own script, so it is served with
a sandbox Content-Security-Policy that keeps that script away from the
CloudBolt session.

Vendor API references (each call site cites its own page):
    https://docs.aws.amazon.com/boto3/latest/reference/services/s3/client/generate_presigned_url.html
    https://docs.aws.amazon.com/boto3/latest/reference/services/s3/client/head_bucket.html
    https://docs.aws.amazon.com/boto3/latest/guide/s3-presigned-urls.html
    https://docs.aws.amazon.com/botocore/latest/reference/config.html
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

from django.http import FileResponse, Http404, HttpResponseRedirect
from django.urls import reverse

from utilities.logger import ThreadLogger

from . import oscap_store as store

logger = ThreadLogger(__name__)

# Result file names on the target and in the repository, by the kind the
# artifact route takes.
FILES = {
    "report": "report.html",
    "results": "results.xml.gz",
    "arf": "arf.xml.gz",
}
UPLOAD_NAMES = tuple(FILES.values())
MAX_UPLOAD_BYTES = 50 * 1024 * 1024
# Space kept free for one scan's three files when making room.
HEADROOM_BYTES = len(UPLOAD_NAMES) * MAX_UPLOAD_BYTES
HOST_RE = re.compile(r"[^A-Za-z0-9._-]+")
GENERIC_PUBLIC = "The stored result is not available right now. Ask an administrator to check the result storage."
TOKEN_HEADER = "HTTP_X_OSCAP_TOKEN"


# What an administrator is told, by error code. The text never comes from an
# exception: a response body is built from this table, the log keeps the detail.
ADMIN_TEXT = {
    "handler_missing": "The AWS resource handler chosen for the S3 bucket no longer exists.",
    "s3_client": "CloudBolt could not create an S3 client from the chosen AWS resource handler.",
    "bucket_read": (
        "CloudBolt could not read bucket {bucket}. Check the bucket name, the handler's "
        "s3:ListBucket permission, or set the bucket region. The CloudBolt application log has the error."
    ),
    "bucket_region": "S3 did not report the region of bucket {bucket}; set it on the settings page.",
    "s3_unconfigured": "S3 storage is not configured. An administrator must finish the OpenSCAP settings page.",
    "no_site_url": "The default portal has no site URL.",
}


def admin_text(code, cfg):
    """The sentence an administrator sees for an error code, or the generic one."""
    template = ADMIN_TEXT.get(code)
    if template is None:
        return GENERIC_PUBLIC
    return template.format(bucket=cfg.get("s3_bucket", ""))


class RepoError(Exception):
    """A problem with the result repository. str() is a generic sentence safe
    for any viewer; detail is for the log; code picks the administrator's
    sentence from ADMIN_TEXT."""

    def __init__(self, public=GENERIC_PUBLIC, detail="", code=""):
        super().__init__(public)
        self.detail = detail or public
        self.code = code


def _safe_host(server):
    return HOST_RE.sub("-", server.hostname or "server")[:60] or "server"


def kind_name(kind):
    name = FILES.get(kind)
    if name is None:
        raise Http404
    return name


# ---------------------------------------------------------------- tokens (local backend)
def _b64(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(text):
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def make_token(scan_id, server_id, names, minutes, key=None, now=None):
    """A signed upload token: scan id, server id, allowed file names, expiry."""
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
    is for this scan and it allows file name; else None."""
    try:
        body, signature = token.split(".")
        expected = _b64(hmac.new(key or store.signing_key(), body.encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(signature, expected):
            return None
        payload = json.loads(_unb64(body))
    except (ValueError, TypeError, AttributeError):
        return None
    if not isinstance(payload, dict):
        return None
    if payload.get("e", 0) < (now if now is not None else time.time()):
        return None
    if payload.get("s") != scan_id or name not in (payload.get("n") or []) or name not in UPLOAD_NAMES:
        return None
    if not store.SCAN_ID_RE.fullmatch(str(payload.get("s", ""))) or not isinstance(payload.get("v"), int):
        return None
    return payload


# ---------------------------------------------------------------- local backend
def _contained(base, *parts):
    """base joined with parts, normalised, and only if it is strictly inside
    base (Http404 otherwise). Every path built from a scan id, a file name or a
    server id goes through here, and the returned value is the one to use.
    https://codeql.github.com/codeql-query-help/python/py-path-injection/"""
    base = os.path.normpath(base)
    path = os.path.normpath(os.path.join(base, *parts))
    if not path.startswith(base + os.sep):
        raise Http404
    return path


def _server_dir(server_id):
    return _contained(store.REPORTS_DIR, str(int(server_id)))


def local_dir(server_id, scan_id):
    if not store.SCAN_ID_RE.fullmatch(scan_id or ""):
        raise Http404
    return _contained(_server_dir(server_id), scan_id)


def local_file(server_id, scan_id, name):
    """Path of one stored file of a scan (Http404 if the name leaves the scan's directory)."""
    return _contained(local_dir(server_id, scan_id), name)


def local_usage():
    """{"scans": count, "bytes": total} of the local backend's files."""
    scans = size = 0
    for _server_id, _scan_id, scan_bytes in _local_scans():
        scans += 1
        size += scan_bytes
    return {"scans": scans, "bytes": size}


def _local_scans():
    """(server id, scan id, bytes) of every stored local scan."""
    found = []
    if not os.path.isdir(store.REPORTS_DIR):
        return found
    for server_dir in os.scandir(store.REPORTS_DIR):
        if not server_dir.is_dir() or not server_dir.name.isdigit():
            continue
        for scan_dir in os.scandir(server_dir.path):
            if not scan_dir.is_dir():
                continue
            scan_bytes = 0
            for entry in os.scandir(scan_dir.path):
                if entry.is_file():
                    scan_bytes += entry.stat().st_size
            found.append((int(server_dir.name), scan_dir.name, scan_bytes))
    return found


def prepare_local(server_id, scan_id):
    """Create the scan's directory; uploads are refused without it."""
    store.ensure_dir(local_dir(server_id, scan_id))


def receive_upload(request, scan_id, name):
    """Store a PUT body for a valid token (X-Oscap-Token header). Raises
    Http404 on any failure, so a caller learns nothing."""
    token = request.META.get(TOKEN_HEADER, "")
    payload = read_token(token, scan_id, name)
    if payload is None or request.method != "PUT":
        raise Http404
    try:
        declared = int(request.META.get("CONTENT_LENGTH") or 0)
    except ValueError:
        raise Http404
    if declared <= 0 or declared > MAX_UPLOAD_BYTES:
        raise Http404
    directory = local_dir(payload["v"], payload["s"])
    final = _contained(directory, name)
    # Only into a scan directory a scan start made (retention removes it), and
    # only once per file name.
    if not os.path.isdir(directory) or os.path.exists(final):
        raise Http404
    cfg = store.get_settings()
    if local_usage()["bytes"] + declared > cfg["local_max_gb"] * 1024 ** 3:
        raise Http404
    fd, temp = tempfile.mkstemp(dir=directory, prefix=".upload-")
    total = 0
    try:
        with os.fdopen(fd, "wb") as handle:
            while True:
                chunk = request.read(64 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_UPLOAD_BYTES or total > declared:
                    raise Http404
                handle.write(chunk)
        if total == 0:
            raise Http404
        store.own(temp)
        # link, not replace: a second upload of the same name cannot win a race.
        os.link(temp, final)
    except Exception:
        raise Http404
    finally:
        try:
            os.unlink(temp)
        except OSError:
            pass
    return total


def store_stream(server_id, scan_id, name, chunks):
    """Store one result file the job read from the server itself; chunks is an
    iterable of bytes. The rules of an upload apply: a known file name, into
    the directory the scan start made, once per name, capped in size and by
    the space limit. Returns (stored, reason)."""
    if name not in UPLOAD_NAMES:
        return False, "not a result file"
    try:
        directory = local_dir(server_id, scan_id)
        final = _contained(directory, name)
    except Http404:
        return False, "the scan id is not valid"
    if not os.path.isdir(directory):
        return False, "the scan's folder on the appliance is gone"
    if os.path.exists(final):
        return True, ""
    room = store.get_settings()["local_max_gb"] * 1024 ** 3 - local_usage()["bytes"]
    fd, temp = tempfile.mkstemp(dir=directory, prefix=".collect-")
    total = 0
    reason = ""
    try:
        with os.fdopen(fd, "wb") as handle:
            for chunk in chunks:
                total += len(chunk)
                if total > MAX_UPLOAD_BYTES:
                    reason = "the file is larger than {} MB".format(MAX_UPLOAD_BYTES // 1024 ** 2)
                    break
                if total > room:
                    reason = "the OpenSCAP storage on the appliance is full"
                    break
                handle.write(chunk)
        if not reason and total == 0:
            reason = "the file was not produced"
        if reason:
            return False, reason
        store.own(temp)
        try:
            os.link(temp, final)
        except FileExistsError:
            pass
        return True, ""
    finally:
        try:
            os.unlink(temp)
        except OSError:
            pass


def _local_response(server, scan, kind):
    name = kind_name(kind)
    path = local_file(server.id, scan["id"], name)
    if not os.path.isfile(path):
        raise Http404
    if kind == "report":
        response = FileResponse(open(path, "rb"), content_type="text/html; charset=utf-8")
        # The report carries its own inline script and styles. Sandboxed
        # without allow-same-origin, that script gets an opaque origin and
        # cannot reach the CloudBolt session, and the policy blocks every
        # connection, form and external resource.
        response["Content-Security-Policy"] = (
            "sandbox allow-scripts; default-src 'none'; script-src 'unsafe-inline'; "
            "style-src 'unsafe-inline'; img-src data:; font-src data:; connect-src 'none'; "
            "form-action 'none'; base-uri 'none'"
        )
    else:
        response = FileResponse(
            open(path, "rb"),
            as_attachment=True,
            filename="{}-{}-{}".format(_safe_host(server), scan["id"], name),
            content_type="application/gzip",
        )
    response["X-Content-Type-Options"] = "nosniff"
    response["Cache-Control"] = "private, no-store"
    return response


def _remove_scans(victims):
    """Delete (server id, scan id) pairs from disk and mark them pruned."""
    by_server = {}
    for server_id, scan_id in victims:
        try:
            shutil.rmtree(_contained(_server_dir(server_id), scan_id), ignore_errors=True)
        except Http404:
            pass  # a name that is not inside the server's folder is not ours to remove
        by_server.setdefault(server_id, []).append(scan_id)
    for server_id, scan_ids in by_server.items():
        store.drop_scans(server_id, scan_ids)
    return [scan_id for ids in by_server.values() for scan_id in ids]


def prune(server, cfg, reserve=0):
    """Keep the newest local_keep - reserve scans of the server, then, when the
    backend is over its space limit (less room for one more scan), remove the
    oldest scans across all servers. Returns the removed scan ids."""
    victims = []
    base = _server_dir(server.id)
    if os.path.isdir(base):
        scan_ids = sorted(
            e.name for e in os.scandir(base) if e.is_dir() and store.SCAN_ID_RE.fullmatch(e.name)
        )
        keep = max(cfg["local_keep"] - reserve, 0)
        victims += [(server.id, s) for s in scan_ids[: max(len(scan_ids) - keep, 0)]]
    removed = _remove_scans(victims) if victims else []
    limit = cfg["local_max_gb"] * 1024 ** 3 - (HEADROOM_BYTES if reserve else 0)
    scans = sorted(_local_scans(), key=lambda row: row[1])  # scan ids start with a UTC timestamp
    total = sum(row[2] for row in scans)
    over = []
    for server_id, scan_id, scan_bytes in scans:
        if total <= limit:
            break
        over.append((server_id, scan_id))
        total -= scan_bytes
    if over:
        removed += _remove_scans(over)
    return removed


# ---------------------------------------------------------------- S3 backend
_REGION_CACHE = {}


def _s3_handler(cfg):
    from resourcehandlers.aws.models import AWSHandler

    try:
        handler = AWSHandler.objects.filter(id=int(cfg["aws_handler_id"])).first()
    except (TypeError, ValueError):
        handler = None
    if handler is None:
        raise RepoError(detail="The AWS resource handler chosen for the S3 bucket no longer exists.", code="handler_missing")
    return handler


def _s3_clients(cfg, region=""):
    """(client for the presigned URLs, bucket region). A known region (the
    setting, or the one recorded when the scan started) needs no lookup."""
    handler = _s3_handler(cfg)
    region = region or cfg["s3_region"]
    # CloudBolt's own wrapper builds the boto3 client from the handler's
    # credentials (resourcehandlers/aws/models.py AWSHandler.get_boto3_client).
    lookup = handler.get_boto3_client(region or "us-east-1", "s3")
    if lookup is None:
        raise RepoError(
            detail="CloudBolt could not create an S3 client from the chosen AWS resource handler.", code="s3_client"
        )
    cache_key = (cfg["aws_handler_id"], cfg["s3_bucket"])
    if not region:
        region = _REGION_CACHE.get(cache_key)
    if not region:
        try:
            # https://docs.aws.amazon.com/boto3/latest/reference/services/s3/client/head_bucket.html
            # (response key BucketRegion)
            region = lookup.head_bucket(Bucket=cfg["s3_bucket"]).get("BucketRegion") or ""
        except Exception as exc:
            raise RepoError(
                detail="CloudBolt could not read bucket {}: {}".format(cfg["s3_bucket"], _short(exc)),
                code="bucket_read",
            )
        if not region:
            raise RepoError(
                detail="S3 did not report the region of bucket {}.".format(cfg["s3_bucket"]), code="bucket_region"
            )
        _REGION_CACHE[cache_key] = region
    return _presign_client(lookup, region), region


def _signing_config():
    """A botocore Config for presigning: Signature Version 4, virtual-hosted
    addressing, short timeouts, one retry, and request checksums only when an
    operation requires them. Newer botocore adds a default checksum parameter
    to a presigned put_object URL, which a plain curl -T upload would not
    satisfy; request_checksum_calculation="when_required" stops that. Older
    botocore does not know the option and raises TypeError, so it is left out.
    https://docs.aws.amazon.com/botocore/latest/reference/config.html
    https://docs.aws.amazon.com/boto3/latest/guide/s3-presigned-urls.html
    """
    from botocore.config import Config

    options = {
        "signature_version": "s3v4",
        "s3": {"addressing_style": "virtual"},
        "connect_timeout": 3,
        "read_timeout": 10,
        "retries": {"max_attempts": 1},
    }
    try:
        return Config(request_checksum_calculation="when_required", **options)
    except TypeError:
        return Config(**options)


def _presign_client(lookup, region):
    """A client that signs with Signature Version 4 for the bucket's region.

    The AWS docs recommend s3v4, the bucket's region and virtual-hosted
    addressing for presigned URLs, and warn that the default may be Signature
    Version 2, which leaves some parameters unsigned:
    https://docs.aws.amazon.com/boto3/latest/guide/s3-presigned-urls.html
    https://docs.aws.amazon.com/boto3/latest/reference/services/s3/client/generate_presigned_url.html
    The CloudBolt wrapper does not take a Config, so the credentials of its
    client (private botocore accessor _get_credentials) go into a second
    client; signing makes no network call. If the accessor is missing the
    wrapper's own client is used.
    """
    import boto3

    try:
        frozen = lookup._get_credentials().get_frozen_credentials()
    except Exception:
        logger.exception("OpenSCAP: using the handler's own S3 client for presigned URLs")
        return lookup
    return boto3.client(
        "s3",
        region_name=region,
        aws_access_key_id=frozen.access_key,
        aws_secret_access_key=frozen.secret_key,
        aws_session_token=frozen.token,
        config=_signing_config(),
    )


def _short(exc):
    return str(exc)[:200]


def s3_key(cfg, server, scan_id, name):
    prefix = cfg["s3_prefix"].strip("/")
    parts = [prefix] if prefix else []
    parts += ["{}-{}".format(_safe_host(server), server.id), scan_id, name]
    return "/".join(parts)


def _s3_upload_targets(cfg, server, scan_id, minutes):
    client, region = _s3_clients(cfg)
    urls = {}
    for name in UPLOAD_NAMES:
        # No ContentType in Params, so the signature does not require a
        # Content-Type header and a plain "curl -T" upload works.
        # https://docs.aws.amazon.com/boto3/latest/reference/services/s3/client/generate_presigned_url.html
        urls[name] = client.generate_presigned_url(
            ClientMethod="put_object",
            Params={"Bucket": cfg["s3_bucket"], "Key": s3_key(cfg, server, scan_id, name)},
            ExpiresIn=int(minutes) * 60,
            HttpMethod="PUT",
        )
    return urls, region


def _s3_response(server, scan, kind):
    cfg = store.get_settings()
    name = kind_name(kind)
    bucket = scan.get("bucket") or cfg["s3_bucket"]
    key = scan.get("key_prefix")
    key = "{}/{}".format(key, name) if key else s3_key(cfg, server, scan["id"], name)
    params = {"Bucket": bucket, "Key": key}
    if kind == "report":
        params["ResponseContentType"] = "text/html; charset=utf-8"
    else:
        params["ResponseContentType"] = "application/gzip"
        params["ResponseContentDisposition"] = 'attachment; filename="{}-{}-{}"'.format(
            _safe_host(server), scan["id"], name
        )
    try:
        # The region recorded when the scan started: no head_bucket here.
        client, _region = _s3_clients(dict(cfg, s3_bucket=bucket), region=scan.get("region") or "")
        # https://docs.aws.amazon.com/boto3/latest/reference/services/s3/client/generate_presigned_url.html
        url = client.generate_presigned_url(
            ClientMethod="get_object", Params=params, ExpiresIn=cfg["link_minutes"] * 60
        )
    except RepoError:
        raise
    except Exception as exc:
        logger.exception("OpenSCAP: could not presign a link for scan %s", scan.get("id"))
        raise RepoError(detail="Could not create a link to the stored file: {}".format(_short(exc)))
    response = HttpResponseRedirect(url)
    response["Cache-Control"] = "private, no-store"
    return response


# ---------------------------------------------------------------- shared interface
def describe(cfg=None):
    cfg = cfg or store.get_settings()
    if cfg["backend"] == "s3":
        label = "S3 bucket {}".format(cfg["s3_bucket"])
        if cfg["s3_prefix"]:
            label += "/{}".format(cfg["s3_prefix"].strip("/"))
        return {"kind": "s3", "label": label}
    return {
        "kind": "local",
        "label": "CloudBolt appliance (keeps last {} scans per server)".format(cfg["local_keep"]),
    }


def upload_targets(server, scan_id, base_url="", minutes=35):
    """What the target uploads to: {"urls": {file name: URL}, "token": str,
    "region": str}. The token is for the local backend only. base_url must be
    the portal's configured site URL, never a request's Host."""
    cfg = store.get_settings()
    if cfg["backend"] == "s3":
        if not cfg["s3_bucket"] or not cfg["aws_handler_id"]:
            raise RepoError(
                detail="S3 storage is not configured. An administrator must finish the OpenSCAP settings page.",
                code="s3_unconfigured",
            )
        urls, region = _s3_upload_targets(cfg, server, scan_id, minutes)
        return {"urls": urls, "token": "", "region": region}
    if not base_url:
        raise RepoError(detail="The default portal has no site URL.", code="no_site_url")
    prepare_local(server.id, scan_id)
    token = make_token(scan_id, server.id, UPLOAD_NAMES, minutes)
    return {
        "urls": {
            name: "{}{}".format(base_url.rstrip("/"), reverse("openscap_upload", args=[scan_id, name]))
            for name in UPLOAD_NAMES
        },
        "token": token,
        "region": "",
    }


def location(server, scan_id, region=""):
    """What the scan summary records about where its files will be."""
    cfg = store.get_settings()
    if cfg["backend"] == "s3":
        key = s3_key(cfg, server, scan_id, "x").rsplit("/", 1)[0]
        return {"backend": "s3", "bucket": cfg["s3_bucket"], "key_prefix": key, "region": region}
    return {"backend": "local", "bucket": "", "key_prefix": "", "region": ""}


def exists(server, scan, kind):
    """Whether the file is stored, from the summary (S3) or the disk (local); no network."""
    if scan.get("backend") == "s3":
        return bool((scan.get("files") or {}).get(kind_name(kind)))
    try:
        return os.path.isfile(local_file(server.id, scan["id"], kind_name(kind)))
    except Http404:
        return False


def link_or_response(server, scan, kind, request=None):
    """A redirect to a short-lived S3 link, or the local file as a response."""
    if not scan or not (scan.get("files") or {}).get(kind_name(kind)):
        raise Http404
    if scan.get("backend") == "s3":
        return _s3_response(server, scan, kind)
    return _local_response(server, scan, kind)
