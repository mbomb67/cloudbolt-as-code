"""
Vulnerabilities XUI: turn the scanner's JSON into rows.

Pure standard library: no Django, no package imports, so it can be tested
anywhere. The Grype field names are those of grype/presenter/models, see
https://oss.anchore.com/docs/reference/grype/ ; any field may be missing or
null, and the build-time key of the database status varies by Grype version,
so everything here is read defensively.
"""
import gzip
import hashlib
import json
import re
import zlib

SEVERITIES = ("Critical", "High", "Medium", "Low", "Unknown")
_RANKS = {"Critical": 4, "High": 3, "Medium": 2, "Low": 1, "Unknown": 0}
_FIX_STATES = ("unknown", "fixed", "not-fixed", "wont-fix")
_DB_BUILT_KEYS = ("built", "buildtimestamp", "build_timestamp", "builttime", "built_at", "builtat")
MAX_LIST = 5
# A scanned server controls the uploaded gzip, so what it inflates to is capped.
MAX_SBOM_BYTES = 512 * 1024 * 1024


class _TooLarge(ValueError):
    pass


class _LimitedReader:
    """File-like over a gzip stream that raises once more than MAX_SBOM_BYTES
    have been read, so a decompression bomb never fills memory."""

    def __init__(self, handle):
        self._handle = handle
        self._total = 0

    def read(self, size=-1):
        whole = size is None or size < 0
        chunks = []
        while True:
            chunk = self._handle.read(1024 * 1024 if whole else size)
            self._total += len(chunk)
            if self._total > MAX_SBOM_BYTES:
                raise _TooLarge("The software list is larger than the limit.")
            chunks.append(chunk)
            if not chunk or not whole:
                return b"".join(chunks)


def sev_rank(severity):
    return _RANKS.get(normalise_severity(severity), 0)


def normalise_severity(severity):
    """One of SEVERITIES; Negligible is folded into Low, anything else is Unknown."""
    text = str(severity or "").strip().lower()
    if text == "negligible":
        return "Low"
    for name in SEVERITIES:
        if name.lower() == text:
            return name
    return "Unknown"


# ---------------------------------------------------------------- versions
def _split_epoch(version):
    head, sep, tail = version.partition(":")
    if sep and head.isdigit():
        return int(head), tail
    return 0, version


def _is_alnum(ch):
    return ch.isascii() and ch.isalnum()


def _rpmvercmp(a, b):
    """rpm's segment compare (rpmvercmp), including ~ (sorts before) and ^."""
    if a == b:
        return 0
    i = j = 0
    la, lb = len(a), len(b)
    while i < la or j < lb:
        while i < la and not _is_alnum(a[i]) and a[i] not in "~^":
            i += 1
        while j < lb and not _is_alnum(b[j]) and b[j] not in "~^":
            j += 1
        a_tilde = i < la and a[i] == "~"
        b_tilde = j < lb and b[j] == "~"
        if a_tilde or b_tilde:
            if not a_tilde:
                return 1
            if not b_tilde:
                return -1
            i += 1
            j += 1
            continue
        a_caret = i < la and a[i] == "^"
        b_caret = j < lb and b[j] == "^"
        if a_caret or b_caret:
            if i >= la:
                return -1
            if j >= lb:
                return 1
            if not a_caret:
                return 1
            if not b_caret:
                return -1
            i += 1
            j += 1
            continue
        if not (i < la and j < lb):
            break
        numeric = a[i].isdigit()
        si, sj = i, j
        if numeric:
            while i < la and a[i].isascii() and a[i].isdigit():
                i += 1
            while j < lb and b[j].isascii() and b[j].isdigit():
                j += 1
        else:
            while i < la and _is_alnum(a[i]) and not a[i].isdigit():
                i += 1
            while j < lb and _is_alnum(b[j]) and not b[j].isdigit():
                j += 1
        seg_a, seg_b = a[si:i], b[sj:j]
        if not seg_b:
            return 1 if numeric else -1
        if numeric:
            seg_a, seg_b = seg_a.lstrip("0"), seg_b.lstrip("0")
            if len(seg_a) != len(seg_b):
                return 1 if len(seg_a) > len(seg_b) else -1
        if seg_a != seg_b:
            return 1 if seg_a > seg_b else -1
    if i >= la and j >= lb:
        return 0
    return 1 if i < la else -1


def compare_versions(a, b):
    """-1, 0 or 1. rpm-style, with an epoch ("1:2.3-4"); good enough to pick
    the highest fix, not a full ecosystem-aware comparison."""
    epoch_a, rest_a = _split_epoch(str(a or "").strip())
    epoch_b, rest_b = _split_epoch(str(b or "").strip())
    if epoch_a != epoch_b:
        return 1 if epoch_a > epoch_b else -1
    return _rpmvercmp(rest_a, rest_b)


def _highest(versions):
    best = ""
    for version in versions:
        if version and (not best or compare_versions(version, best) > 0):
            best = version
    return best


# ---------------------------------------------------------------- grype
def _dict(value):
    return value if isinstance(value, dict) else {}


def _list(value):
    return value if isinstance(value, list) else []


def _text(value):
    return value.strip() if isinstance(value, str) else ""


def _number(value):
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number else None  # NaN is no number


def _max(values):
    values = [v for v in values if v is not None]
    return max(values) if values else None


def _exploited_cves(vuln):
    """CVE ids a vulnerability record lists as known exploited."""
    found = []
    for item in _list(vuln.get("knownExploited")):
        cve = _text(_dict(item).get("cve")).upper()
        if cve:
            found.append(cve)
    return found


def _db_built(doc):
    """Build time of the vulnerability database, wherever this Grype version
    keeps it under descriptor.db."""
    queue = [(_dict(doc.get("descriptor")).get("db"), 0)]
    while queue:
        node, depth = queue.pop(0)
        if not isinstance(node, dict) or depth > 4:
            continue
        for key, value in node.items():
            if isinstance(value, str) and str(key).lower() in _DB_BUILT_KEYS and value.strip():
                return value.strip()
        queue.extend((value, depth + 1) for value in node.values() if isinstance(value, dict))
    return ""


def _distro(doc):
    distro = _dict(doc.get("distro"))
    return " ".join(part for part in (_text(distro.get("name")), _text(distro.get("version"))) if part)


def _one_match(raw):
    """A MATCH dict from one Grype match, or None when it has no package or id."""
    vuln = _dict(raw.get("vulnerability"))
    artifact = _dict(raw.get("artifact"))
    name, vuln_id = _text(artifact.get("name")), _text(vuln.get("id"))
    if not name or not vuln_id:
        return None
    pkg_type = _text(artifact.get("type")).lower()
    path = ""
    for location in _list(artifact.get("locations")):
        path = _text(_dict(location).get("path"))
        if path:
            break
    if pkg_type in ("rpm", "deb", "apk"):
        path = ""  # an OS package is one package however many files it owns

    records = [vuln] + [_dict(item) for item in _list(raw.get("relatedVulnerabilities"))]
    cves = {}
    for record in records:
        exploited = _exploited_cves(record)
        record_id = _text(record.get("id")).upper()
        if record_id.startswith("CVE-"):
            cves[record_id] = cves.get(record_id, False) or bool(exploited)
        for cve in exploited:
            cves[cve] = True
    kev = any(_exploited_cves(record) for record in records)

    fix = _dict(vuln.get("fix"))
    fix_state = _text(fix.get("state")).lower()
    severity = normalise_severity(vuln.get("severity"))
    epss_values = []
    for record in records:
        for item in _list(record.get("epss")):
            epss_values.append(_number(_dict(item).get("epss")))
    source = _text(vuln.get("dataSource"))
    return {
        "pkg_name": name,
        "pkg_version": _text(artifact.get("version")),
        "pkg_type": pkg_type,
        "path": path,
        "vuln_id": vuln_id,
        "severity": severity,
        "sev_rank": _RANKS[severity],
        "fix_state": fix_state if fix_state in _FIX_STATES else "unknown",
        "fix_version": _highest([_text(v) for v in _list(fix.get("versions"))]),
        "kev": kev,
        "epss": _max(epss_values),
        "risk": _max([_number(record.get("risk")) for record in records]),
        "data_source": source if source.startswith(("http://", "https://")) else "",
        "cves": [{"cve": cve, "kev": cves[cve]} for cve in sorted(cves)],
    }


def merge_matches(into, other):
    """Fold a duplicate match into the first one seen."""
    if other["sev_rank"] > into["sev_rank"]:
        into["severity"], into["sev_rank"] = other["severity"], other["sev_rank"]
    into["fix_version"] = _highest([into["fix_version"], other["fix_version"]])
    if into["fix_state"] == "unknown":
        into["fix_state"] = other["fix_state"]
    into["kev"] = into["kev"] or other["kev"]
    into["epss"] = _max([into["epss"], other["epss"]])
    into["risk"] = _max([into["risk"], other["risk"]])
    into["data_source"] = into["data_source"] or other["data_source"]
    merged = {item["cve"]: item["kev"] for item in into["cves"]}
    for item in other["cves"]:
        merged[item["cve"]] = merged.get(item["cve"], False) or item["kev"]
    into["cves"] = [{"cve": cve, "kev": merged[cve]} for cve in sorted(merged)]


def parse_grype(doc):
    """Grype's JSON document -> {"matches", "distro", "db_built"}."""
    doc = _dict(doc)
    merged = {}
    for raw in _list(doc.get("matches")):
        match = _one_match(_dict(raw))
        if match is None:
            continue
        key = (match["pkg_name"], match["pkg_version"], match["pkg_type"], match["path"], match["vuln_id"])
        if key in merged:
            merge_matches(merged[key], match)
        else:
            merged[key] = match
    return {"matches": list(merged.values()), "distro": _distro(doc), "db_built": _db_built(doc)}


def summarize(matches):
    """Counts for the servers row; counts of matches, distinct CVE ids."""
    counts = {name.lower(): 0 for name in SEVERITIES}
    cves, exploited = set(), set()
    for match in matches:
        counts[normalise_severity(match.get("severity")).lower()] += 1
        for item in match.get("cves") or []:
            cves.add(item["cve"])
            if item.get("kev"):
                exploited.add(item["cve"])
    counts["findings"] = len(matches)
    counts["kev"] = len(exploited)
    counts["cves"] = len(cves)
    return counts


def package_rows(matches):
    """One row per package (name, version, type, path) with its worst severity."""
    groups = {}
    for match in matches:
        key = (match["pkg_name"], match["pkg_version"], match["pkg_type"], match["path"])
        groups.setdefault(key, []).append(match)
    rows = []
    for (name, version, pkg_type, path), items in groups.items():
        worst = max(item["sev_rank"] for item in items)
        # A match with no CVE (an advisory only) counts as its own entry.
        entries = set()
        exploited, cve_ids = set(), set()
        for item in items:
            ids = [c["cve"] for c in item["cves"]] or [item["vuln_id"]]
            entries.update(ids)
            for c in item["cves"]:
                cve_ids.add(c["cve"])
                if c["kev"]:
                    exploited.add(c["cve"])
        ordered = sorted(items, key=lambda m: (-m["sev_rank"], not m["kev"], m["vuln_id"]))
        advisories = []
        for item in ordered:
            if item["vuln_id"] not in advisories:
                advisories.append(item["vuln_id"])
        cves = sorted(cve_ids, key=lambda c: (c not in exploited, c))
        rows.append(
            {
                "pkg_name": name,
                "pkg_version": version,
                "pkg_type": pkg_type,
                "path": path,
                "severity": next(s for s in SEVERITIES if _RANKS[s] == worst),
                "sev_rank": worst,
                "cve_count": len(entries),
                "kev": any(item["kev"] for item in items),
                "fix_version": _highest([item["fix_version"] for item in items]),
                "risk": _max([item["risk"] for item in items]),
                "advisories": advisories[:MAX_LIST],
                "advisory_count": len(advisories),
                "cves": cves[:MAX_LIST],
            }
        )
    return rows


# ---------------------------------------------------------------- software list
def sbom_facts(path):
    """Package count, distro and file hash of a gzip syft-json file. Raises
    ValueError when the file is not one."""
    digest = hashlib.sha256()
    try:
        with open(path, "rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        with gzip.open(path, "rb") as handle:
            doc = json.load(_LimitedReader(handle))
    except _TooLarge:
        raise
    except (OSError, EOFError, zlib.error, UnicodeDecodeError, RecursionError) as exc:
        raise ValueError("The software list is not a readable gzip file: {}".format(exc))
    except ValueError as exc:
        raise ValueError("The software list is not valid JSON: {}".format(exc))
    if not isinstance(doc, dict) or not isinstance(doc.get("artifacts"), list):
        raise ValueError("The software list is not a Syft JSON document.")
    distro = _dict(doc.get("distro"))
    label = _text(distro.get("prettyName")) or " ".join(
        part for part in (_text(distro.get("name")), _text(distro.get("version"))) if part
    )
    return {"packages": len(doc["artifacts"]), "distro": label, "sha256": digest.hexdigest()}
