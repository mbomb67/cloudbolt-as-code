"""
Vulnerabilities XUI: the findings store, a SQLite file.

Jobs write it (one short transaction per server); the web process only reads
it, through a read-only connection. Every public function opens its own
connection and closes it, so no connection is shared between a web worker and
a job thread. The journal is SQLite's default rollback journal, not WAL: the
web process and a possibly-root job engine share this file, and WAL's side
files make ownership fragile.

A reader treats a missing file or a missing table as an empty store.
All SQL is parameterized; the only text put into a statement is fixed
fragments chosen here (ORDER BY, WHERE clauses), never user input. A
search term only ever travels as a parameter, with LIKE wildcards escaped.

https://www.sqlite.org/lang_upsert.html
"""
import contextlib
import json
import logging
import math
import os
import pathlib
import sqlite3

from . import vuln_match, vuln_store

logger = logging.getLogger(__name__)  # stdlib only, so the module imports anywhere

SCHEMA_VERSION = 1
BUSY_MS = 30000
MAX_SEARCH = 100
PAGE_MIN, PAGE_MAX = 10, 100

_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS servers(
        server_id INTEGER PRIMARY KEY, hostname TEXT, os_name TEXT, group_name TEXT, environment TEXT,
        scan_id TEXT, inventory_at TEXT, packages INTEGER, matched_at TEXT, db_built TEXT,
        findings INTEGER, critical INTEGER, high INTEGER, medium INTEGER, low INTEGER, unknown INTEGER,
        kev INTEGER, cves INTEGER, vulns INTEGER, vuln_critical INTEGER, vuln_high INTEGER,
        vuln_medium INTEGER, vuln_low INTEGER, vuln_unknown INTEGER)""",
    """CREATE TABLE IF NOT EXISTS matches(
        id INTEGER PRIMARY KEY, pkg_name TEXT, pkg_version TEXT, pkg_type TEXT, vuln_id TEXT,
        severity TEXT, sev_rank INTEGER, fix_state TEXT, fix_version TEXT, kev INTEGER, epss REAL,
        risk REAL, data_source TEXT, UNIQUE(pkg_name, pkg_version, pkg_type, vuln_id))""",
    """CREATE TABLE IF NOT EXISTS match_cves(
        match_id INTEGER, cve TEXT, kev INTEGER, PRIMARY KEY(match_id, cve)) WITHOUT ROWID""",
    "CREATE INDEX IF NOT EXISTS match_cves_cve ON match_cves(cve)",
    """CREATE TABLE IF NOT EXISTS server_matches(
        server_id INTEGER, match_id INTEGER, path TEXT, PRIMARY KEY(server_id, match_id, path)) WITHOUT ROWID""",
    "CREATE INDEX IF NOT EXISTS server_matches_match ON server_matches(match_id)",
    """CREATE TABLE IF NOT EXISTS server_packages(
        server_id INTEGER, pkg_name TEXT, pkg_version TEXT, pkg_type TEXT, path TEXT,
        severity TEXT, sev_rank INTEGER, cve_count INTEGER, kev INTEGER, fix_version TEXT, risk REAL,
        advisories TEXT, advisory_count INTEGER, cves TEXT,
        PRIMARY KEY(server_id, pkg_name, pkg_version, pkg_type, path)) WITHOUT ROWID""",
)

_SERVER_COLUMNS = (
    "server_id", "hostname", "os_name", "group_name", "environment", "scan_id", "inventory_at", "packages",
    "matched_at", "db_built", "findings", "critical", "high", "medium", "low", "unknown", "kev", "cves",
    "vulns", "vuln_critical", "vuln_high", "vuln_medium", "vuln_low", "vuln_unknown",
)
# Columns added after the first release; an older file gets them in ensure_schema.
_ADDED_SERVER_COLUMNS = _SERVER_COLUMNS[18:]
_PACKAGE_COLUMNS = (
    "pkg_name", "pkg_version", "pkg_type", "path", "severity", "sev_rank", "cve_count", "kev", "fix_version",
    "risk", "advisories", "advisory_count", "cves",
)


# ---------------------------------------------------------------- connections
def ensure_schema(conn):
    """Create the tables if missing and stamp the schema version."""
    for statement in _SCHEMA:
        conn.execute(statement)
    have = {row[1] for row in conn.execute("PRAGMA table_info(servers)")}
    for column in _ADDED_SERVER_COLUMNS:
        if column not in have:
            conn.execute("ALTER TABLE servers ADD COLUMN {} INTEGER".format(column))
    if conn.execute("PRAGMA user_version").fetchone()[0] == 0:
        conn.execute("PRAGMA user_version = {}".format(SCHEMA_VERSION))


def _own_files():
    """Hand the database file (and a journal, if one is present) to both the
    web process and the job engine; a failure here never fails a scan."""
    for path in (vuln_store.FINDINGS_DB, vuln_store.FINDINGS_DB + "-journal"):
        if os.path.exists(path):
            try:
                vuln_store.own(path)
            except OSError:
                pass


@contextlib.contextmanager
def _writer():
    """A read-write connection in manual-transaction mode; closed on exit."""
    vuln_store.ensure_dir(vuln_store.ROOT)
    conn = sqlite3.connect(vuln_store.FINDINGS_DB, timeout=BUSY_MS / 1000.0, isolation_level=None)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = {}".format(BUSY_MS))
        ensure_schema(conn)
        _own_files()
        yield conn
    finally:
        conn.close()
        _own_files()


@contextlib.contextmanager
def _transaction(conn):
    """BEGIN IMMEDIATE .. COMMIT, or ROLLBACK when the body raises."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


@contextlib.contextmanager
def _reader():
    """A read-only connection, or None when the store does not exist yet."""
    path = vuln_store.FINDINGS_DB
    if not os.path.exists(path):
        yield None
        return
    try:
        conn = sqlite3.connect(
            pathlib.Path(path).resolve().as_uri() + "?mode=ro", uri=True, timeout=BUSY_MS / 1000.0
        )
    except sqlite3.OperationalError:
        yield None  # vanished between the check and the open
        return
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = {}".format(BUSY_MS))
        yield conn
    finally:
        conn.close()


def _missing_table(exc):
    return "no such table" in str(exc)


def _empty_page(page, page_size, unavailable=False):
    return {"total": 0, "page": page, "page_size": page_size, "pages": 1, "rows": [], "unavailable": unavailable}


# ---------------------------------------------------------------- writes
def _server_row(conn, server_id):
    row = conn.execute("SELECT * FROM servers WHERE server_id = ?", (server_id,)).fetchone()
    return dict(row) if row else None


def replace_server(server, parsed):
    """Store one server's matches, replacing what it had: one transaction."""
    server_id = int(server["server_id"])
    matches = parsed.get("matches") or []
    totals = vuln_match.summarize(matches)

    # One row per shared match: the same package, version and advisory found at
    # two paths is one matches row and two server_matches rows.
    shared = {}
    for match in matches:
        key = (match["pkg_name"], match["pkg_version"], match["pkg_type"], match["vuln_id"])
        if key in shared:
            vuln_match.merge_matches(shared[key], dict(match, cves=list(match["cves"])))
        else:
            shared[key] = dict(match, cves=list(match["cves"]))
    upserts = [
        (
            m["pkg_name"], m["pkg_version"], m["pkg_type"], m["vuln_id"], m["severity"], m["sev_rank"],
            m["fix_state"], m["fix_version"], int(bool(m["kev"])), m["epss"], m["risk"], m["data_source"],
        )
        for m in shared.values()
    ]
    servers_row = (
        server_id, server.get("hostname"), server.get("os_name"), server.get("group_name"),
        server.get("environment"), server.get("scan_id"), server.get("inventory_at"), server.get("packages"),
        server.get("matched_at"), parsed.get("db_built") or "", totals["findings"], totals["critical"],
        totals["high"], totals["medium"], totals["low"], totals["unknown"], totals["kev"], totals["cves"],
        totals["vulns"], totals["vuln_critical"], totals["vuln_high"], totals["vuln_medium"], totals["vuln_low"],
        totals["vuln_unknown"],
    )
    packages = [
        (
            server_id, p["pkg_name"], p["pkg_version"], p["pkg_type"], p["path"], p["severity"], p["sev_rank"],
            p["cve_count"], int(bool(p["kev"])), p["fix_version"], p["risk"], json.dumps(p["advisories"]),
            p["advisory_count"], json.dumps(p["cves"]),
        )
        for p in vuln_match.package_rows(matches)
    ]

    with _writer() as conn:
        with _transaction(conn):
            conn.executemany(
                """INSERT INTO matches(pkg_name, pkg_version, pkg_type, vuln_id, severity, sev_rank, fix_state,
                       fix_version, kev, epss, risk, data_source)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(pkg_name, pkg_version, pkg_type, vuln_id) DO UPDATE SET
                       severity = excluded.severity, sev_rank = excluded.sev_rank,
                       fix_state = excluded.fix_state, fix_version = excluded.fix_version,
                       kev = excluded.kev, epss = excluded.epss, risk = excluded.risk,
                       data_source = excluded.data_source""",
                upserts,
            )
            ids = {}
            for key in shared:
                ids[key] = conn.execute(
                    "SELECT id FROM matches WHERE pkg_name = ? AND pkg_version = ? AND pkg_type = ? AND vuln_id = ?",
                    key,
                ).fetchone()[0]
            conn.executemany("DELETE FROM match_cves WHERE match_id = ?", [(i,) for i in ids.values()])
            conn.executemany(
                "INSERT OR REPLACE INTO match_cves(match_id, cve, kev) VALUES (?, ?, ?)",
                [(ids[key], c["cve"], int(bool(c["kev"]))) for key, m in shared.items() for c in m["cves"]],
            )
            conn.execute("DELETE FROM server_matches WHERE server_id = ?", (server_id,))
            conn.executemany(
                "INSERT OR IGNORE INTO server_matches(server_id, match_id, path) VALUES (?, ?, ?)",
                [
                    (server_id, ids[(m["pkg_name"], m["pkg_version"], m["pkg_type"], m["vuln_id"])], m["path"])
                    for m in matches
                ],
            )
            conn.execute("DELETE FROM server_packages WHERE server_id = ?", (server_id,))
            conn.executemany(
                """INSERT OR REPLACE INTO server_packages(server_id, pkg_name, pkg_version, pkg_type, path,
                       severity, sev_rank, cve_count, kev, fix_version, risk, advisories, advisory_count, cves)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                packages,
            )
            conn.execute(
                "INSERT INTO servers({}) VALUES ({}) ON CONFLICT(server_id) DO UPDATE SET {}".format(
                    ", ".join(_SERVER_COLUMNS),
                    ", ".join("?" for _ in _SERVER_COLUMNS),
                    ", ".join("{0} = excluded.{0}".format(c) for c in _SERVER_COLUMNS[1:]),
                ),
                servers_row,
            )
        return _server_row(conn, server_id)


def forget_server(server_id):
    """Drop a server's rows. Shared matches stay until prune_orphans."""
    server_id = int(server_id)
    if not os.path.exists(vuln_store.FINDINGS_DB):
        return
    with _writer() as conn:
        with _transaction(conn):
            for table in ("server_packages", "server_matches", "servers"):
                conn.execute("DELETE FROM {} WHERE server_id = ?".format(table), (server_id,))


def prune_orphans():
    """Delete matches (and their CVE links) no server references; returns how many matches."""
    if not os.path.exists(vuln_store.FINDINGS_DB):
        return 0
    with _writer() as conn:
        with _transaction(conn):
            orphans = "SELECT id FROM matches m WHERE NOT EXISTS (SELECT 1 FROM server_matches s WHERE s.match_id = m.id)"
            conn.execute("DELETE FROM match_cves WHERE match_id IN ({})".format(orphans))
            return conn.execute("DELETE FROM matches WHERE id IN ({})".format(orphans)).rowcount


# ---------------------------------------------------------------- reads
def _busy(exc, what):
    """A reader that cannot get at the file (database locked, or a leftover
    journal a read-only connection cannot roll back) answers empty and says why
    in the log; a missing table is the empty store and is not worth a warning."""
    if not _missing_table(exc):
        logger.warning("Vulnerabilities: findings store not readable for %s: %s", what, exc)


def server_summary(server_id):
    try:
        with _reader() as conn:
            if conn is None:
                return None
            return _server_row(conn, int(server_id))
    except sqlite3.OperationalError as exc:
        _busy(exc, "the summary")
        return None


def stats():
    size = 0
    try:
        size = os.path.getsize(vuln_store.FINDINGS_DB)
    except OSError:
        pass
    result = {"servers": 0, "matches": 0, "bytes": size}
    try:
        with _reader() as conn:
            if conn is None:
                return result
            result["servers"] = conn.execute("SELECT COUNT(*) FROM servers").fetchone()[0]
            result["matches"] = conn.execute("SELECT COUNT(*) FROM matches").fetchone()[0]
    except sqlite3.OperationalError as exc:
        _busy(exc, "the statistics")
        return {"servers": 0, "matches": 0, "bytes": size}
    return result


def _int(value, default):
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _paging(page, page_size):
    return max(1, _int(page, 1)), max(PAGE_MIN, min(PAGE_MAX, _int(page_size, 25)))


def _like(search):
    """A LIKE pattern for a substring match, wildcards in the term escaped
    (used with ESCAPE '\\'); None for an empty term."""
    term = str(search or "").strip()[:MAX_SEARCH]
    if not term:
        return None
    return "%" + term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def _rank_filter(severity):
    """A sev_rank for one of SEVERITIES (any case), else None."""
    text = str(severity or "").strip().lower()
    for name in vuln_match.SEVERITIES:
        if name.lower() == text:
            return vuln_match.sev_rank(name)
    return None


def _page_of(conn, where, params, order, columns, page, page_size, source, convert):
    """Count, clamp the page, fetch it. where/order/source are fixed text built
    in this module; every value travels in params."""
    total = conn.execute("SELECT COUNT(*) FROM {} WHERE {}".format(source, where), params).fetchone()[0]
    pages = max(1, int(math.ceil(total / float(page_size))))
    page = min(page, pages)
    cursor = conn.execute(
        "SELECT {} FROM {} WHERE {} ORDER BY {} LIMIT ? OFFSET ?".format(columns, source, where, order),
        list(params) + [page_size, (page - 1) * page_size],
    )
    return {"total": total, "page": page, "page_size": page_size, "pages": pages,
            "rows": [convert(r) for r in cursor], "unavailable": False}


def _package_row(row):
    data = {key: row[key] for key in _PACKAGE_COLUMNS}
    data["kev"] = bool(data["kev"])
    for key in ("advisories", "cves"):
        try:
            value = json.loads(data[key] or "[]")
        except ValueError:
            value = []
        data[key] = value if isinstance(value, list) else []
    return data


def server_packages(server_id, search="", severity="", kev_only=False, page=1, page_size=25):
    page, page_size = _paging(page, page_size)
    where, params = ["sp.server_id = ?"], [int(server_id)]
    rank = _rank_filter(severity)
    if rank is not None:
        where.append("sp.sev_rank = ?")
        params.append(rank)
    if kev_only:
        where.append("sp.kev = 1")
    pattern = _like(search)
    if pattern:
        # Search the package name, and every advisory and CVE of its matches
        # (the row's own lists are capped at five).
        where.append(
            """(sp.pkg_name LIKE ? ESCAPE '\\' OR EXISTS (
                SELECT 1 FROM server_matches sm JOIN matches m ON m.id = sm.match_id
                LEFT JOIN match_cves c ON c.match_id = m.id
                WHERE sm.server_id = sp.server_id AND sm.path = sp.path AND m.pkg_name = sp.pkg_name
                  AND m.pkg_version = sp.pkg_version AND m.pkg_type = sp.pkg_type
                  AND (m.vuln_id LIKE ? ESCAPE '\\' OR c.cve LIKE ? ESCAPE '\\')))"""
        )
        params += [pattern, pattern, pattern]
    order = "sp.kev DESC, sp.sev_rank DESC, sp.risk DESC, sp.pkg_name, sp.pkg_version, sp.path"
    columns = ", ".join("sp." + c for c in _PACKAGE_COLUMNS)
    try:
        with _reader() as conn:
            if conn is None:
                return _empty_page(page, page_size)
            return _page_of(conn, " AND ".join(where), params, order, columns, page, page_size,
                            "server_packages sp", _package_row)
    except sqlite3.OperationalError as exc:
        _busy(exc, "the package list")
        return _empty_page(page, page_size, unavailable=not _missing_table(exc))


# One row per (CVE, package): an advisory with two CVEs is two rows, and the
# same CVE reached through two advisories of one package collapses to the
# worst of them. A match with no CVE shows under its advisory id.
_CVE_SOURCE = """(
    SELECT * FROM (
        SELECT COALESCE(c.cve, m.vuln_id) AS cve, m.vuln_id AS vuln_id, m.severity AS severity,
               m.sev_rank AS sev_rank, COALESCE(c.kev, m.kev) AS kev, m.epss AS epss, m.risk AS risk,
               m.pkg_name AS pkg_name, m.pkg_version AS pkg_version, m.pkg_type AS pkg_type, sm.path AS path,
               m.fix_version AS fix_version, m.fix_state AS fix_state, m.data_source AS data_source,
               ROW_NUMBER() OVER (
                   PARTITION BY COALESCE(c.cve, m.vuln_id), m.pkg_name, m.pkg_version, m.pkg_type, sm.path
                   ORDER BY m.sev_rank DESC, COALESCE(c.kev, m.kev) DESC, m.id) AS rn
        FROM server_matches sm
        JOIN matches m ON m.id = sm.match_id
        LEFT JOIN match_cves c ON c.match_id = m.id
        WHERE sm.server_id = ?
    ) WHERE rn = 1
) AS v"""
_CVE_COLUMNS = (
    "cve", "vuln_id", "severity", "kev", "epss", "pkg_name", "pkg_version", "pkg_type", "path", "fix_version",
    "fix_state", "data_source",
)


def _cve_row(row):
    data = {key: row[key] for key in _CVE_COLUMNS}
    data["kev"] = bool(data["kev"])
    return data


def server_cves(server_id, search="", severity="", kev_only=False, page=1, page_size=25):
    page, page_size = _paging(page, page_size)
    where, params = ["1 = 1"], []
    rank = _rank_filter(severity)
    if rank is not None:
        where.append("v.sev_rank = ?")
        params.append(rank)
    if kev_only:
        where.append("v.kev = 1")
    pattern = _like(search)
    if pattern:
        where.append("(v.cve LIKE ? ESCAPE '\\' OR v.vuln_id LIKE ? ESCAPE '\\' OR v.pkg_name LIKE ? ESCAPE '\\')")
        params += [pattern, pattern, pattern]
    order = "v.kev DESC, v.sev_rank DESC, v.risk DESC, v.pkg_name, v.cve, v.pkg_version, v.path"
    columns = ", ".join("v." + c for c in _CVE_COLUMNS)
    try:
        with _reader() as conn:
            if conn is None:
                return _empty_page(page, page_size)
            # The server id binds to the subquery's placeholder, which comes first.
            return _page_of(conn, " AND ".join(where), [int(server_id)] + params, order, columns, page,
                            page_size, _CVE_SOURCE, _cve_row)
    except sqlite3.OperationalError as exc:
        _busy(exc, "the CVE list")
        return _empty_page(page, page_size, unavailable=not _missing_table(exc))
