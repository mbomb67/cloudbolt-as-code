"""
Vulnerabilities XUI: Microsoft's security data for Windows Server, and the match against a server's build.

A Windows server is matched from its build number, not from a software list. This module keeps a compact index
of what Microsoft publishes for the server products this extension covers (PRODUCTS) and answers, for one
server's facts, which vulnerabilities its build is still missing the fix for.

Sources (all free, fetched from the appliance, never from a server):

* the Microsoft Security Update Guide API v2.0, the table of which update fixes which CVE in which product
  (affectedProduct) and which CVEs Microsoft rates as exploited (vulnerability, exploited eq 'Yes'):
      https://api.msrc.microsoft.com/sug/v2.0/en-US/
* the CISA Known Exploited Vulnerabilities catalog:
      https://www.cisa.gov/known-exploited-vulnerabilities-catalog

Traps of the API, verified on 2026-11-24: spaces in a query must be %20 (quote once, never twice); $select and
form-encoded queries are refused by a firewall in front of it (HTTP 403); send Accept: application/json.

Windows findings list only CVEs for which Microsoft publishes a fixing update for the server's product; a CVE
with no fixing update is never listed, because no update could clear it.

The index is one gzipped JSON file, ROOT/msrc/index.json.gz, swapped in with os.replace so a reader never sees
a half-written file and a failed update leaves the previous index in place. It does not use the Grype scanner
lock: Linux scans and Microsoft updates never wait for each other.

This module is imported by job code and by the web process, so it imports no Django at module level beyond the
extension's own store (which reads settings) and never raises from status(), ready() or load_index().
"""
import contextlib
import gzip
import hashlib
import io
import json
import os
import re
import tempfile
import threading
import time
import urllib.parse
import zlib

from . import vuln_match
from . import vuln_store as store

try:
    from utilities.logger import ThreadLogger

    logger = ThreadLogger(__name__)
except ImportError:  # only where CloudBolt is absent
    import logging

    logger = logging.getLogger(__name__)

# CurrentBuild -> (product name, desktop product id, Server Core product id). One line per Windows Server version.
PRODUCTS = {
    "20348": ("Windows Server 2022", 11923, 11924),
    "26100": ("Windows Server 2025", 12436, 12437),
}
CORE_SUFFIX = " (Server Core installation)"
HOTPATCH_SUFFIX = " (hotpatch)"

SUG_URL = "https://api.msrc.microsoft.com/sug/v2.0/en-US/"
KEV_URL = "https://www.cisa.gov/sites/default/files/feeds/known_exploited_vulnerabilities.json"
ALLOWED_HOSTS = ("api.msrc.microsoft.com", "www.cisa.gov")
GUIDE_URL = "https://msrc.microsoft.com/update-guide/vulnerability/"
PAGE_SIZE = 1000

MSRC_DIR = os.path.join(store.ROOT, "msrc")
INDEX_FILE = "index.json.gz"
STATUS_FILE = "status.json"
LOCK_FILE = "msrc.lock"
INDEX_VERSION = 1

# Limits: one response, everything of one update, the time of one update, and the pages of one listing.
MAX_RESPONSE_BYTES = 64 * 1024 * 1024
MAX_TOTAL_BYTES = 512 * 1024 * 1024
MAX_INDEX_BYTES = 256 * 1024 * 1024  # what the index may inflate to
TOTAL_TIMEOUT_S = 20 * 60
REQUEST_TIMEOUT = (10, 120)  # connect, read (seconds)
MAX_PAGES = 200
PAUSE_S = 0.25  # between requests: be polite to a public API
LOCK_WAIT_S = 30 * 60

# Microsoft's rating -> the severity names the rest of the extension uses.
SEVERITIES = {"critical": "Critical", "important": "High", "moderate": "Medium", "low": "Low"}

URL_RE = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://\S+")
CONTROL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
SECRET_RE = re.compile(r"[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}")
KB_RE = re.compile(r"(?:KB)?([0-9]{5,9})", re.IGNORECASE)
BUILD_RE = re.compile(r"[0-9]{1,10}(?:\.[0-9]{1,10}){0,5}")
CVE_RE = re.compile(r"CVE-[0-9]{4}-[0-9]{4,9}", re.IGNORECASE)
DETAIL_TAIL = 800
REDACT_INPUT = 4000

NOT_READY = ("The Microsoft security data is not on this appliance yet. A CloudBolt admin can download it on the "
             "Vulnerability Settings page.")


class MsrcError(Exception):
    """public is a one-line message safe to show; detail is for the log only; reason is a short code
    ("unsupported_windows", "not_ready", "bad_facts") when the caller acts on the kind of failure."""

    def __init__(self, public, detail="", reason=""):
        super().__init__(public)
        self.public = public
        self.detail = detail or public
        self.reason = reason


# ---------------------------------------------------------------- small helpers
def _tail(text):
    """Text with control characters, URLs (a proxy URL can carry a password) and token-like strings removed,
    cut to the last DETAIL_TAIL characters; the same redaction vuln_tools applies to scanner output."""
    text = CONTROL_RE.sub(" ", str(text or "")[-REDACT_INPUT:])
    text = URL_RE.sub("<url>", text)
    text = SECRET_RE.sub("<token>", text)
    return text.strip()[-DETAIL_TAIL:]


def _say(progress, message):
    if progress is not None:
        try:
            progress(message)
        except Exception:
            logger.exception("Vulnerabilities: progress callback failed")


def build_tuple(text):
    """"10.0.26100.33650" -> (10, 0, 26100, 33650); None when the text is not dot-separated whole numbers.
    Tuples compare component by component, so a 5-digit update number sorts above a 4-digit one."""
    text = str(text or "").strip()
    if not BUILD_RE.fullmatch(text):
        return None
    return tuple(int(part) for part in text.split("."))


def normalise_kb(text):
    """"KB5131077", "kb5131077" and "5131077" -> "KB5131077"; "" for anything else."""
    found = KB_RE.fullmatch(str(text or "").strip())
    return "KB" + found.group(1) if found else ""


def severity_for(rating):
    """Microsoft's rating (Critical, Important, Moderate, Low) as the extension's severity name; Unknown otherwise."""
    return SEVERITIES.get(str(rating or "").strip().lower(), "Unknown")


STAMP_DATE_RE = re.compile(r"([0-9]{4}-[0-9]{2}-[0-9]{2})(?:\.[0-9a-f]+)?")


def stamp_date(stamp):
    """The ISO date part of an index stamp ("2026-11-10.26edfc27ff" -> "2026-11-10"); "" for anything else,
    including the placeholder date of an index without release dates."""
    found = STAMP_DATE_RE.fullmatch(str(stamp or "").strip())
    if not found or found.group(1) == "0000-00-00":
        return ""
    return found.group(1)


def _is_hotpatch(sub_type):
    return "hotpatch" in str(sub_type or "").lower().replace(" ", "")


def _product_name(pid):
    for name, desktop, core in PRODUCTS.values():
        if pid == desktop:
            return name
        if pid == core:
            return name + CORE_SUFFIX
    return ""


def _build_of_product(pid):
    for build, (_name, desktop, core) in PRODUCTS.items():
        if pid in (desktop, core):
            return build
    return ""


def product_ids():
    """Every product id the index covers, sorted."""
    return sorted(pid for _name, desktop, core in PRODUCTS.values() for pid in (desktop, core))


def product_for(facts):
    """(product id, product name) for a server's facts, or None for a Windows version this module does not
    cover (Server 2019 and older, a client build, damaged facts). Server Core and the desktop installation are
    different products with different CVE lists."""
    if not isinstance(facts, dict):
        return None
    entry = PRODUCTS.get(str(facts.get("current_build") or "").strip())
    if entry is None:
        return None
    name, desktop, core = entry
    if str(facts.get("installation_type") or "").strip().lower() == "server core":
        return core, name + CORE_SUFFIX
    return desktop, name


def _server_build(facts):
    """The server's build as a tuple, checked against its CurrentBuild; built from CurrentBuild and UBR when the
    stored string is missing or does not belong to it. None when neither works."""
    current = str(facts.get("current_build") or "").strip()
    wanted = build_tuple("10.0." + current)
    if wanted is None:
        return None
    stored = build_tuple(facts.get("build"))
    if stored is not None and len(stored) == 4 and stored[:3] == wanted:
        return stored
    try:
        ubr = int(str(facts.get("ubr")).strip())
    except (TypeError, ValueError):
        return None
    return wanted + (ubr,) if ubr >= 0 else None


# ---------------------------------------------------------------- the index file
def _path(name):
    return store.contained(MSRC_DIR, name)


def _atomic_write(path, data):
    """Write data (bytes) to path through a temp file in the same folder, flushed to disk first, then os.replace;
    the old file stays whole if anything fails."""
    store.ensure_dir(os.path.dirname(path))
    fd, temp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".msrc-")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        store.own(temp)
        os.replace(temp, path)
    except BaseException:
        try:
            os.unlink(temp)
        except OSError:
            pass
        raise


def _read_status_file():
    try:
        with open(_path(STATUS_FILE), encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError, RecursionError, store.StoreError):
        return {}


def _update_status(**values):
    data = _read_status_file()
    data.update(values)
    _atomic_write(_path(STATUS_FILE), json.dumps(data, sort_keys=True, indent=2).encode("utf-8"))


def _inflate(raw):
    """Parsed JSON of gzip bytes, refusing more than MAX_INDEX_BYTES inflated."""
    with gzip.GzipFile(fileobj=io.BytesIO(raw)) as handle:
        body = handle.read(MAX_INDEX_BYTES + 1)
    if len(body) > MAX_INDEX_BYTES:
        raise ValueError("index too large")
    return json.loads(body)


def _valid_index(data):
    return (isinstance(data, dict) and data.get("version") == INDEX_VERSION and isinstance(data.get("stamp"), str)
            and data.get("stamp") and isinstance(data.get("products"), dict))


_cache = {"key": None, "index": None}
_cache_lock = threading.Lock()


def load_index():
    """The parsed index, cached per process until the file changes; None when it is absent or unreadable."""
    try:
        path = _path(INDEX_FILE)
        info = os.stat(path)
    except (OSError, store.StoreError):
        return None
    key = (path, info.st_mtime_ns, info.st_size)
    with _cache_lock:
        if _cache["key"] == key:
            return _cache["index"]
    try:
        with open(path, "rb") as handle:
            data = _inflate(handle.read())
    except (OSError, ValueError, EOFError, RecursionError, MemoryError, zlib.error):
        return None
    if not _valid_index(data):
        return None
    with _cache_lock:
        _cache["key"], _cache["index"] = key, data
    return data


def ready():
    """True when the index file exists and the status file says an update wrote it successfully. Cheap: it does
    not parse the index (the web process asks on every panel load). An index without a status file (copied in by
    hand) is parsed once instead. Never raises."""
    try:
        path = _path(INDEX_FILE)
        if not os.path.isfile(path) or os.path.getsize(path) == 0:
            return False
        if _read_status_file().get("built"):
            return True
        return load_index() is not None
    except Exception:
        return False


def status():
    """What the settings page and the tab show. No network. Never raises."""
    result = {"present": False, "built": "", "checked": "", "cves": 0, "bytes": 0, "kev_entries": 0,
              "last_error": "", "last_error_at": ""}
    try:
        data = _read_status_file()
        result["last_error"] = str(data.get("last_error") or "")
        result["last_error_at"] = str(data.get("last_error_at") or "")
        path = _path(INDEX_FILE)
        if not os.path.isfile(path):
            return result
        result["present"] = True
        info = os.stat(path)
        result["bytes"] = info.st_size
        key = [info.st_mtime_ns, info.st_size]
        # The status counts only when it was written for this very index file. A status write that failed after
        # the index was replaced leaves an older record behind, and the index itself then tells the truth.
        if data.get("built") and data.get("cves") is not None and data.get("index_key") == key:
            result["built"] = str(data["built"])
            result["checked"] = str(data.get("checked") or "")
            result["cves"] = int(data.get("cves") or 0)
            result["kev_entries"] = int(data.get("kev_entries") or 0)
        else:  # no (or an older) status record: read what the index itself says (parsed once, then cached)
            index = load_index()
            if index is None:
                result["present"] = False
            else:
                result["built"] = index["stamp"]
                result["checked"] = str(index.get("fetched_at") or "")
                result["cves"] = int(index.get("cve_count") or 0)
                result["kev_entries"] = len(index.get("kev") or [])
    except Exception:
        logger.exception("Vulnerabilities: could not read the Microsoft data status")
    return result


# ---------------------------------------------------------------- building the index
def _new_acc():
    return {pid: {} for pid in product_ids()}


def reduce_rows(rows, acc):
    """Fold affectedProduct rows into acc (product id, then CVE id, to an entry). Only the fields the match needs are kept.
    A row of a product this module does not cover, or whose id is not a CVE id, is skipped (advisories carry
    no fixing update and never reach the index)."""
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        try:
            pid = int(row.get("productId"))
        except (TypeError, ValueError):
            continue
        cve = str(row.get("cveNumber") or "").strip().upper()
        build = _build_of_product(pid)
        if pid not in acc or not CVE_RE.fullmatch(cve) or not build:
            continue
        entry = acc[pid].setdefault(cve, {"sev": "", "std": set(), "hp": set(), "fixes": False, "bad": False,
                                         "released": ""})
        rating = str(row.get("severity") or "").strip()
        if severity_for(rating) != "Unknown" and (
                severity_for(entry["sev"]) == "Unknown"
                or vuln_match.sev_rank(severity_for(rating)) > vuln_match.sev_rank(severity_for(entry["sev"]))):
            entry["sev"] = rating
        entry["released"] = max(entry["released"], str(row.get("releaseDate") or "")[:10])
        prefix = (10, 0, int(build))
        for article in row.get("kbArticles") or []:
            if not isinstance(article, dict):
                continue
            fixed = str(article.get("fixedBuildNumber") or "").strip()
            if not fixed:
                continue
            entry["fixes"] = True
            parsed = build_tuple(fixed)
            kb = normalise_kb(article.get("articleName"))
            # A build of another Windows version (a 26200 build listed for 26100) or a malformed one is not a
            # fix for this product.
            if parsed is None or len(parsed) != 4 or parsed[:3] != prefix or not kb:
                entry["bad"] = True
                continue
            entry["hp" if _is_hotpatch(article.get("downloadName")) else "std"].add((parsed, kb))


def build_index(acc, exploited, kev_all, fetched_at, kev_etag=""):
    """The index dict from the reduced rows, the Microsoft exploited CVE ids and every CISA KEV CVE id.

    A CVE enters a product's list only when Microsoft publishes at least one usable fixing update for that
    product. One with no fix rows (an open or configuration-only issue, an advisory), or whose fix data is all
    unusable (a build of another Windows version), is left out: no update could ever clear it, so it would be
    noise on every server.

    The stamp is a hash of what the match depends on (the fixes, the ratings, and the exploited and KEV flags of
    those CVEs) plus the newest release date in the data, so it changes exactly when a server's result could
    change and not because the download ran again."""
    products = {}
    all_cves = set()
    newest = ""
    for pid in sorted(acc):
        cves = {}
        hotpatch_kbs, hotpatch_builds, standard_builds = set(), set(), set()
        for cve in sorted(acc[pid]):
            entry = acc[pid][cve]
            if not entry["std"] and not entry["hp"]:
                continue
            cves[cve] = {
                "sev": entry["sev"],
                "std": [[".".join(str(n) for n in b), kb] for b, kb in sorted(entry["std"])],
                "hp": [[".".join(str(n) for n in b), kb] for b, kb in sorted(entry["hp"])],
            }
            hotpatch_kbs.update(kb for _b, kb in entry["hp"])
            hotpatch_builds.update(b for b, _kb in entry["hp"])
            standard_builds.update(b for b, _kb in entry["std"])
            newest = max(newest, entry["released"])
        all_cves.update(cves)
        # A build that some CVE lists as a standard fix too is a baseline build, which both tracks run, so it
        # says nothing about the track and is not kept.
        products[str(pid)] = {"cves": cves, "hotpatch_kbs": sorted(hotpatch_kbs),
                              "hotpatch_builds": [".".join(str(n) for n in b)
                                                  for b in sorted(hotpatch_builds - standard_builds)]}
    exploited_here = sorted(cve for cve in {str(c).strip().upper() for c in exploited} if cve in all_cves)
    kev_ids = sorted({str(c).strip().upper() for c in kev_all if CVE_RE.fullmatch(str(c).strip())})
    kev_here = sorted(cve for cve in kev_ids if cve in all_cves)
    content = {"products": products, "exploited": exploited_here, "kev": kev_here}
    digest = hashlib.sha256(json.dumps(content, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    return {
        "version": INDEX_VERSION,
        "stamp": "{}.{}".format(newest or "0000-00-00", digest[:10]),
        "fetched_at": fetched_at,
        "cve_count": len(all_cves),
        "products": products,
        "exploited": exploited_here,
        "kev": kev_ids,  # the whole CISA catalog, so a not-modified answer needs no second download
        "kev_etag": kev_etag,
    }


def dump_index(index):
    return gzip.compress(json.dumps(index, sort_keys=True, separators=(",", ":")).encode("utf-8"), 9, mtime=0)


# ---------------------------------------------------------------- downloads
def _session_args(url):
    """Proxy and certificate settings the way the rest of CloudBolt reaches the internet."""
    from common.methods import get_proxies
    from utilities.helpers import get_ssl_verification

    return {"verify": get_ssl_verification(), "proxies": get_proxies(url)}


def _check_url(url):
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != "https" or parts.hostname not in ALLOWED_HOSTS:
        raise MsrcError("The Microsoft security data answered with a link to an unexpected place, so it was not followed.",
                        "link host {!r}".format(parts.hostname))


def _get(url, headers=None, timeout=REQUEST_TIMEOUT, max_bytes=MAX_RESPONSE_BYTES):
    """GET url; returns (status code, body bytes, response headers). 200 and 304 are answers; any other status,
    a network error, or a body larger than max_bytes raises MsrcError (with the response's first bytes redacted
    in the detail)."""
    import requests

    _check_url(url)
    head = {"Accept": "application/json", "User-Agent": "cloudbolt-vulnerabilities"}
    head.update(headers or {})
    try:
        # allow_redirects=False: a redirect could lead outside ALLOWED_HOSTS, so it is an error, never followed.
        with requests.get(url, headers=head, stream=True, timeout=timeout, allow_redirects=False,
                          **_session_args(url)) as response:
            code = int(response.status_code)
            if code == 304:
                return code, b"", dict(response.headers)
            chunks, total = [], 0
            for chunk in response.iter_content(chunk_size=256 * 1024):
                if not chunk:
                    continue
                total += len(chunk)
                if total > max_bytes:
                    raise MsrcError("An answer of the Microsoft security data was larger than expected, so it was discarded.",
                                    "over {} bytes".format(max_bytes))
                chunks.append(chunk)
            body = b"".join(chunks)
            headers_out = dict(response.headers)
    except requests.RequestException as exc:
        raise MsrcError("CloudBolt could not download the Microsoft security data. The appliance needs outbound "
                        "access to api.msrc.microsoft.com and www.cisa.gov (check the proxy settings).",
                        "{}: {}".format(type(exc).__name__, _tail(str(exc))))
    if code != 200:
        if 300 <= code < 400:
            public = "The Microsoft security data service tried to redirect the request, so it was not followed."
        elif code == 403:
            public = ("Microsoft's security data service refused the request (HTTP 403). A firewall in front of it "
                      "blocks some callers; check the proxy settings or try again later.")
        else:
            public = "The Microsoft security data service answered with HTTP {}.".format(code)
        raise MsrcError(public, "HTTP {} from {}: {}".format(code, urllib.parse.urlsplit(url).path,
                                                              _tail(body[:400].decode("utf-8", "replace"))))
    return code, body, headers_out


def _json(body, what):
    try:
        data = json.loads(body)
    except (ValueError, RecursionError):
        raise MsrcError("The Microsoft security data ({}) is not in the expected format.".format(what),
                        "not JSON: {}".format(_tail(body[:200].decode("utf-8", "replace"))))
    if not isinstance(data, dict):
        raise MsrcError("The Microsoft security data ({}) is not in the expected format.".format(what), "not an object")
    return data


def _filter_url(table, expression):
    """A Security Update Guide query. quote() encodes the spaces once (%20); the quote characters stay."""
    return "{}{}?$filter={}&$top={}".format(SUG_URL, table, urllib.parse.quote(expression, safe="'"), PAGE_SIZE)


def _next_link(link):
    link = str(link or "")
    if " " in link:
        # The server's own link is normally encoded. A raw one is encoded once: % is a safe character, so an
        # escape already in the link is kept as it is and only the raw characters change (space -> %20).
        link = urllib.parse.quote(link, safe="$=?&',()/:-.@%")
    return link


class _Budget:
    """The time and size one update may use in all."""

    def __init__(self):
        self.deadline = time.monotonic() + TOTAL_TIMEOUT_S
        self.bytes = 0
        self.requests = 0

    def get(self, url, headers=None):
        if time.monotonic() > self.deadline:
            raise MsrcError("Downloading the Microsoft security data took longer than {} minutes and was stopped."
                            .format(TOTAL_TIMEOUT_S // 60), "deadline passed")
        if self.requests:
            time.sleep(PAUSE_S)
        self.requests += 1
        remaining = max(5.0, self.deadline - time.monotonic())
        code, body, response_headers = _get(url, headers, timeout=(REQUEST_TIMEOUT[0], min(REQUEST_TIMEOUT[1], remaining)))
        self.bytes += len(body)
        if self.bytes > MAX_TOTAL_BYTES:
            raise MsrcError("The Microsoft security data download was larger than expected and was stopped.",
                            "total over {} bytes".format(MAX_TOTAL_BYTES))
        return code, body, response_headers

    def pages(self, first_url, what):
        """Every row of a listing, one page at a time, following @odata.nextLink."""
        url, seen = first_url, 0
        while url:
            seen += 1
            if seen > MAX_PAGES:
                raise MsrcError("The Microsoft security data listing ({}) has more pages than expected.".format(what),
                                "over {} pages".format(MAX_PAGES))
            _code, body, _headers = self.get(url)
            doc = _json(body, what)
            rows = doc.get("value")
            if not isinstance(rows, list):
                raise MsrcError("The Microsoft security data ({}) is not in the expected format.".format(what),
                                "no value list")
            yield rows
            url = _next_link(doc.get("@odata.nextLink"))


def _fetch_kev(budget, previous):
    """(all KEV CVE ids, etag). A conditional request with the previous index's etag; a not-modified answer
    reuses that index's list."""
    headers = {}
    if previous and previous.get("kev_etag") and previous.get("kev"):
        headers["If-None-Match"] = previous["kev_etag"]
    code, body, response_headers = budget.get(KEV_URL, headers)
    if code == 304:
        return list(previous.get("kev") or []), str(previous.get("kev_etag") or "")
    doc = _json(body, "CISA known exploited list")
    items = doc.get("vulnerabilities")
    if not isinstance(items, list):
        raise MsrcError("The CISA known exploited list is not in the expected format.", "no vulnerabilities list")
    ids = [str(item.get("cveID") or "") for item in items if isinstance(item, dict)]
    etag = ""
    for name, value in response_headers.items():
        if str(name).lower() == "etag":
            etag = str(value)
    return ids, etag


def fetch_all(progress=None, previous=None):
    """Download everything and return the index dict; nothing is written. Raises MsrcError."""
    budget = _Budget()
    acc = _new_acc()
    for pid in product_ids():
        _say(progress, "Downloading the Microsoft security updates for {}".format(_product_name(pid)))
        count = 0
        for rows in budget.pages(_filter_url("affectedProduct", "productId eq {}".format(pid)),
                                 "updates for {}".format(_product_name(pid))):
            reduce_rows(rows, acc)
            count += len(rows)
        if count == 0:
            raise MsrcError("Microsoft's security data had no updates for {}.".format(_product_name(pid)),
                            "empty listing for product {}".format(pid))
    _say(progress, "Downloading the list of vulnerabilities Microsoft rates as exploited")
    exploited = []
    for rows in budget.pages(_filter_url("vulnerability", "exploited eq 'Yes'"), "exploited vulnerabilities"):
        exploited.extend(str(row.get("cveNumber") or "") for row in rows if isinstance(row, dict))
    _say(progress, "Downloading the CISA known exploited vulnerabilities list")
    kev_ids, etag = _fetch_kev(budget, previous)
    index = build_index(acc, exploited, kev_ids, store.stamp(), etag)
    logger.info("Vulnerabilities: downloaded Microsoft security data: %s requests, %s bytes, %s CVEs",
                budget.requests, budget.bytes, index["cve_count"])
    return index


# ---------------------------------------------------------------- update
@contextlib.contextmanager
def _locked():
    """Exclusive lock on ROOT/msrc.lock (not the scanner lock), a lock held too long turned into an MsrcError."""
    try:
        with store._lock(MSRC_DIR, LOCK_FILE, None, LOCK_WAIT_S):
            yield
    except store.StoreError as exc:
        raise MsrcError(str(exc), "msrc lock: {}".format(exc))


TRUNCATION_LIMIT = 0.8  # a product's CVE list may not shrink below this share of the current index's


def _refuse_truncated(previous, index):
    """Raise when a product's CVE count fell by more than 20% against the current index: a cut-off listing
    would otherwise replace a good index and hide real findings. No current index, nothing to compare."""
    if not previous:
        return
    for pid, product in (index.get("products") or {}).items():
        old = len(((previous.get("products") or {}).get(pid) or {}).get("cves") or {})
        new = len(product.get("cves") or {})
        if old and new < old * TRUNCATION_LIMIT:
            raise MsrcError(
                "Microsoft's security data for {} lists {} vulnerabilities where the data on this appliance has {}, "
                "so the download looks incomplete and was not used. The previous data stays in place.".format(
                    _product_name(int(pid)), new, old),
                "product {}: {} CVEs against {}".format(pid, new, old))


def record_error(message):
    """Remember why the last update failed (a short redacted sentence) so the settings page can show it."""
    try:
        _update_status(last_error=_tail(message)[:300], last_error_at=store.stamp())
    except Exception:
        logger.exception("Vulnerabilities: could not record the Microsoft data error")


def update(progress=None):
    """Download Microsoft's data and replace the index when it changed. Returns {"updated": bool, "built": stamp}.
    On any failure the previous index stays as it was, the reason is recorded for the settings page, and an
    MsrcError is raised."""
    try:
        with _locked():
            store.ensure_dir(MSRC_DIR)
            previous = load_index()
            index = fetch_all(progress, previous)
            _refuse_truncated(previous, index)
            raw = dump_index(index)
            # Read the bytes back before they replace anything: a file this module cannot read must never go live.
            if not _valid_index(_inflate(raw)):
                raise MsrcError("The new Microsoft security data could not be verified.", "round trip failed")
            changed = previous is None or previous.get("stamp") != index["stamp"]
            # Same content with a new download etag is rewritten too, so the next run can ask "not modified".
            if changed or previous.get("kev_etag") != index.get("kev_etag"):
                _atomic_write(_path(INDEX_FILE), raw)
            info = os.stat(_path(INDEX_FILE))
            try:
                _update_status(built=index["stamp"], checked=store.stamp(), cves=index["cve_count"], bytes=info.st_size,
                               kev_entries=len(index["kev"]), last_error="", last_error_at="",
                               index_key=[info.st_mtime_ns, info.st_size])
            except Exception:
                # The index is already live; status() reads its stamp from the file when the record is missing.
                logger.exception("Vulnerabilities: could not record the Microsoft data status")
            return {"updated": changed, "built": index["stamp"]}
    except MsrcError as exc:
        logger.warning("Vulnerabilities: Microsoft data update failed: %s", exc.detail)
        record_error(exc.public)
        raise
    except Exception as exc:
        logger.exception("Vulnerabilities: Microsoft data update failed")
        record_error("The Microsoft security data update failed unexpectedly.")
        raise MsrcError("The Microsoft security data update failed unexpectedly.",
                        "{}: {}".format(type(exc).__name__, _tail(str(exc))))


# ---------------------------------------------------------------- match
def _finding(cve, entry, pkg_name, build, fix, flagged):
    kb, fixed_build = (fix[1], fix[0]) if fix else ("", "")
    severity = severity_for(entry.get("sev"))
    return {
        "pkg_name": pkg_name,
        "pkg_type": "windows",
        "pkg_version": ".".join(str(n) for n in build),
        "path": kb,
        "vuln_id": cve,
        "cves": [{"cve": cve, "kev": flagged}],
        "severity": severity,
        "sev_rank": vuln_match.sev_rank(severity),
        "fix_state": "fixed" if fix else "not-fixed",
        "fix_version": fixed_build,
        "kev": flagged,
        "epss": None,
        "risk": None,
        "data_source": GUIDE_URL + cve,
    }


def match(facts, index=None):
    """The vulnerabilities a Windows server's build is still missing the fix for.

    Returns {"matches": [finding dicts, sorted by vuln_id], "db_built": index stamp, "track": "standard" or
    "hotpatch", "product": product name}. Raises MsrcError when there is no index (reason "not_ready"), when the
    Windows version is not covered (reason "unsupported_windows") or the build cannot be read (reason
    "bad_facts"). The result depends on nothing but the facts and the index, so the store's hash of it is stable.

    A server is on the hotpatch track when either signal says so:
    1. one of its installed updates (facts["hotfixes"], from Get-HotFix) is a hotpatch update of its product
       (the index's hotpatch_kbs); or
    2. its exact build equals a hotpatch fixed build of its product (the index's hotpatch_builds), because
       Get-HotFix can leave the hotpatch update out. Baseline builds are run by both tracks, so a build that a
       standard update also lists never counts, and equality with a standard build says nothing.
    Otherwise it is on the standard track. The shared findings store keys a finding by package name, version,
    type and vulnerability id and keeps one fix version per key, so the hotpatch track reports its findings
    under the product name plus " (hotpatch)": two servers at one build on different tracks need different
    fix updates and must not overwrite each other."""
    if index is None:
        index = load_index()
    if index is None:
        raise MsrcError(NOT_READY, "no Microsoft index", "not_ready")
    found = product_for(facts)
    if found is None:
        current = str((facts or {}).get("current_build") or "") if isinstance(facts, dict) else ""
        raise MsrcError("This Windows version has no published server updates in this extension (Windows Server 2022 "
                        "and 2025 are covered).", "current_build {!r}".format(current[:20]), "unsupported_windows")
    pid, product_name = found
    build = _server_build(facts)
    if build is None:
        raise MsrcError("The Windows build number of this server could not be read.", "facts without a usable build",
                        "bad_facts")
    product = (index.get("products") or {}).get(str(pid))
    if not isinstance(product, dict):
        raise MsrcError("The Microsoft security data on this appliance does not cover {} yet. A CloudBolt admin can "
                        "update it on the Vulnerability Settings page.".format(product_name),
                        "product {} missing from the index".format(pid), "not_ready")
    installed = {normalise_kb(item) for item in (facts.get("hotfixes") or []) if isinstance(item, str)}
    installed.discard("")
    own_build = ".".join(str(n) for n in build)
    hotpatch = bool(installed & set(product.get("hotpatch_kbs") or [])) or own_build in set(
        product.get("hotpatch_builds") or [])
    pkg_name = product_name + HOTPATCH_SUFFIX if hotpatch else product_name
    flagged_ids = set(index.get("exploited") or []) | set(index.get("kev") or [])
    matches = []
    for cve, entry in (product.get("cves") or {}).items():
        fixes = []
        for key in (("std", "hp") if hotpatch else ("std",)):
            fixes.extend((build_tuple(b), kb) for b, kb in entry.get(key) or [])
        if not fixes and not hotpatch and entry.get("hp"):
            # Only hotpatch updates are listed for it; a server on the standard track gets the fix with the
            # month's regular update, whose build is the same or later than the hotpatch build.
            fixes = [(build_tuple(b), kb) for b, kb in entry["hp"]]
        fixes = sorted((b, kb) for b, kb in fixes if b is not None)
        if fixes and build >= fixes[0][0]:
            continue
        fix = (".".join(str(n) for n in fixes[0][0]), fixes[0][1]) if fixes else None
        matches.append(_finding(cve, entry, pkg_name, build, fix, cve in flagged_ids))
    matches.sort(key=lambda item: item["vuln_id"])
    return {"matches": matches, "db_built": str(index.get("stamp") or ""), "track": "hotpatch" if hotpatch else "standard",
            "product": product_name}
