"""
Vulnerabilities XUI: the findings store, a SQLite file.

Jobs write it (one short transaction per server); the web process only reads
it, through a read-only connection. Every public function opens its own
connection and closes it, so no connection is shared between a web worker and
a job thread. The journal is SQLite's default rollback journal, not WAL: the
web process and a possibly-root job engine share this file, and WAL's side
files make ownership fragile.

A reader treats a missing file, a missing table or a missing column as an empty
store, and never upgrades the file; only a writer does. All SQL is
parameterized; the only text put into a statement is fixed fragments chosen
here (ORDER BY, WHERE clauses), never user input. A search term only ever
travels as a parameter, with LIKE wildcards escaped.

Schema version 2 adds the fleet tables, so the pages that list every server's
vulnerabilities never read the per-server match tables:

    server_cves    one row per (server, vulnerability identity): the worst
                   severity, the exploited flag, and the package that carries it
    fleet_cves     one row per identity across all servers: how many servers
                   have it and how many have a fix; kept current by every
                   save (replace_server, forget_server) and repaired by
                   rebuild_fleet

https://www.sqlite.org/lang_upsert.html
https://www.sqlite.org/lang_transaction.html
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

SCHEMA_VERSION = 3
BUSY_MS = 30000
MAX_SEARCH = 100
PAGE_MIN, PAGE_MAX = 10, 100
CHUNK = 500  # values per IN (...) list, well under every SQLite variable limit

_SERVER_BASE_COLUMNS = (
    "server_id", "hostname", "os_name", "group_name", "environment", "scan_id", "inventory_at", "packages",
    "matched_at", "db_built", "findings", "critical", "high", "medium", "low", "unknown", "kev", "cves",
)
# Added after the first release; a file made by that release gets them by ALTER in the upgrade.
_SERVER_ADDED_COLUMNS = ("vulns", "vuln_critical", "vuln_high", "vuln_medium", "vuln_low", "vuln_unknown")
# Added by version 3: the vulnerabilities of the server that have a fix. NOT NULL DEFAULT 0, set by the upgrade.
_SERVER_FIXABLE_COLUMN = "vuln_fixable"
_SERVER_COLUMNS = _SERVER_BASE_COLUMNS + _SERVER_ADDED_COLUMNS + (_SERVER_FIXABLE_COLUMN,)
# What the server's snapshot supplies; every other column comes from the totals.
_SNAPSHOT_COLUMNS = ("hostname", "os_name", "group_name", "environment", "scan_id", "inventory_at", "packages",
                     "matched_at")

_TABLES = (
    """CREATE TABLE IF NOT EXISTS servers(
        server_id INTEGER PRIMARY KEY, hostname TEXT, os_name TEXT, group_name TEXT, environment TEXT,
        scan_id TEXT, inventory_at TEXT, packages INTEGER, matched_at TEXT, db_built TEXT,
        findings INTEGER, critical INTEGER, high INTEGER, medium INTEGER, low INTEGER, unknown INTEGER,
        kev INTEGER, cves INTEGER, vulns INTEGER, vuln_critical INTEGER, vuln_high INTEGER,
        vuln_medium INTEGER, vuln_low INTEGER, vuln_unknown INTEGER, vuln_fixable INTEGER NOT NULL DEFAULT 0,
        match_hash TEXT)""",
    """CREATE TABLE IF NOT EXISTS matches(
        id INTEGER PRIMARY KEY, pkg_name TEXT, pkg_version TEXT, pkg_type TEXT, vuln_id TEXT,
        severity TEXT, sev_rank INTEGER, fix_state TEXT, fix_version TEXT, kev INTEGER, epss REAL,
        risk REAL, data_source TEXT, UNIQUE(pkg_name, pkg_version, pkg_type, vuln_id))""",
    """CREATE TABLE IF NOT EXISTS match_cves(
        match_id INTEGER, cve TEXT, kev INTEGER, PRIMARY KEY(match_id, cve)) WITHOUT ROWID""",
    """CREATE TABLE IF NOT EXISTS server_matches(
        server_id INTEGER, match_id INTEGER, path TEXT, PRIMARY KEY(server_id, match_id, path)) WITHOUT ROWID""",
    """CREATE TABLE IF NOT EXISTS server_packages(
        server_id INTEGER, pkg_name TEXT, pkg_version TEXT, pkg_type TEXT, path TEXT,
        severity TEXT, sev_rank INTEGER, cve_count INTEGER, kev INTEGER, fix_version TEXT, risk REAL,
        advisories TEXT, advisory_count INTEGER, cves TEXT,
        PRIMARY KEY(server_id, pkg_name, pkg_version, pkg_type, path)) WITHOUT ROWID""",
    """CREATE TABLE IF NOT EXISTS server_cves(
        server_id INTEGER, cve TEXT, sev_rank INTEGER, kev INTEGER,
        pkg_name TEXT, pkg_version TEXT, fix_version TEXT, pkg_count INTEGER,
        PRIMARY KEY(server_id, cve)) WITHOUT ROWID""",
    """CREATE TABLE IF NOT EXISTS fleet_cves(
        cve TEXT PRIMARY KEY, sev_rank INTEGER, kev INTEGER, servers INTEGER, fixable INTEGER) WITHOUT ROWID""",
)
# Created after the ALTERs, because servers_fleet needs the added columns.
_INDEXES = (
    "CREATE INDEX IF NOT EXISTS match_cves_cve ON match_cves(cve)",
    "CREATE INDEX IF NOT EXISTS server_matches_match ON server_matches(match_id)",
    "CREATE INDEX IF NOT EXISTS server_cves_cve ON server_cves(cve, server_id)",
    "CREATE INDEX IF NOT EXISTS fleet_cves_order ON fleet_cves(kev DESC, sev_rank DESC, servers DESC, cve)",
    # NOCASE: a search for an advisory id matches however the person typed it, and still uses the index.
    "CREATE INDEX IF NOT EXISTS matches_vuln ON matches(vuln_id COLLATE NOCASE)",
    # The order of the fleet's server list; not in the contract's DDL, needed to page it without a sort.
    "CREATE INDEX IF NOT EXISTS servers_fleet ON servers(kev DESC, vuln_critical DESC, vuln_high DESC, hostname)",
)
_PACKAGE_COLUMNS = (
    "pkg_name", "pkg_version", "pkg_type", "path", "severity", "sev_rank", "cve_count", "kev", "fix_version",
    "risk", "advisories", "advisory_count", "cves",
)
_SEVERITY_NAMES = {vuln_match.sev_rank(name): name for name in vuln_match.SEVERITIES}

# One fleet row per identity, computed from server_cves; a fix counts per server.
_FLEET_FROM_SERVERS = (
    "SELECT cve, MAX(sev_rank), MAX(kev), COUNT(*), SUM(CASE WHEN fix_version <> '' THEN 1 ELSE 0 END) "
    "FROM server_cves"
)

# The same identity rule as vuln_match.vuln_rows, in SQL, for the rows a version-1 file already holds:
# one row per (server, CVE or advisory id), the worst severity, exploited if any entry is, and the
# representative package (one with a fix, then worst severity, then exploited, then package name), as vuln_rows
# picks it, so a row has a fix exactly when any of its entries has.
_BACKFILL_SERVER_CVES = """
WITH e AS (
    SELECT COALESCE(c.cve, m.vuln_id) AS cve, m.sev_rank AS sev_rank, COALESCE(c.kev, m.kev, 0) AS kev,
           m.pkg_name AS pkg_name, m.pkg_version AS pkg_version, m.pkg_type AS pkg_type, sm.path AS path,
           COALESCE(m.fix_version, '') AS fix_version
    FROM server_matches sm
    JOIN matches m ON m.id = sm.match_id
    LEFT JOIN match_cves c ON c.match_id = m.id
    WHERE sm.server_id = ?),
n AS (
    SELECT cve, COUNT(*) AS pkg_count
    FROM (SELECT DISTINCT cve, pkg_name, pkg_version, pkg_type, path FROM e) GROUP BY cve),
r AS (
    SELECT cve, pkg_name, pkg_version, fix_version,
           ROW_NUMBER() OVER (PARTITION BY cve
               ORDER BY (fix_version <> '') DESC, sev_rank DESC, kev DESC, pkg_name, pkg_version, pkg_type, path) AS rn,
           MAX(sev_rank) OVER (PARTITION BY cve) AS top_rank, MAX(kev) OVER (PARTITION BY cve) AS top_kev
    FROM e)
INSERT INTO server_cves(server_id, cve, sev_rank, kev, pkg_name, pkg_version, fix_version, pkg_count)
SELECT ?, r.cve, r.top_rank, r.top_kev, r.pkg_name, r.pkg_version, r.fix_version, n.pkg_count
FROM r JOIN n ON n.cve = r.cve WHERE r.rn = 1"""


# ---------------------------------------------------------------- connections
def _version(conn):
    return conn.execute("PRAGMA user_version").fetchone()[0]


def _upgrade(conn, before):
    """Bring the file to SCHEMA_VERSION; runs inside the caller's transaction."""
    for statement in _TABLES:
        conn.execute(statement)
    have = {row[1] for row in conn.execute("PRAGMA table_info(servers)")}
    for column in _SERVER_ADDED_COLUMNS:
        if column not in have:
            conn.execute("ALTER TABLE servers ADD COLUMN {} INTEGER".format(column))
    if "match_hash" not in have:
        conn.execute("ALTER TABLE servers ADD COLUMN match_hash TEXT")  # NULL: the first save is a full one
    if _SERVER_FIXABLE_COLUMN not in have:
        conn.execute("ALTER TABLE servers ADD COLUMN {} INTEGER NOT NULL DEFAULT 0".format(_SERVER_FIXABLE_COLUMN))
    for statement in _INDEXES:
        conn.execute(statement)
    if before < 3:
        # Version 2 rows took the representative match's fix; version 3 counts a fix on any match, so the rows
        # are rebuilt from the stored matches (a no-op delete for a version-1 file, which has none yet).
        # One server at a time keeps the window function's working set small.
        conn.execute("DELETE FROM server_cves")
        for (server_id,) in conn.execute("SELECT server_id FROM servers").fetchall():
            conn.execute(_BACKFILL_SERVER_CVES, (server_id, server_id))
            conn.execute(
                "UPDATE servers SET vuln_fixable = (SELECT COUNT(*) FROM server_cves WHERE server_id = ? "
                "AND fix_version <> '') WHERE server_id = ?", (server_id, server_id))
        conn.execute("DELETE FROM fleet_cves")
        conn.execute("INSERT INTO fleet_cves(cve, sev_rank, kev, servers, fixable) {} GROUP BY cve".format(
            _FLEET_FROM_SERVERS))
    conn.execute("PRAGMA user_version = {}".format(SCHEMA_VERSION))


def ensure_schema(conn):
    """Create or upgrade the tables; a no-op (one PRAGMA read) when the file is
    at SCHEMA_VERSION already. The whole upgrade is one BEGIN IMMEDIATE
    transaction (DDL and user_version are transactional in SQLite), so a crash
    leaves the old version intact. Two writers that race both see the old
    version, but the second waits for the lock and then finds the new version
    when it looks again inside the transaction, so it never tries to add a
    column twice. The connection must be in manual-transaction mode."""
    if _version(conn) >= SCHEMA_VERSION:
        return
    with _transaction(conn):
        before = _version(conn)
        if before < SCHEMA_VERSION:
            _upgrade(conn, before)


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
    """A read-write connection in manual-transaction mode; closed on exit.
    Upgrades the file first when it is older than SCHEMA_VERSION."""
    vuln_store.ensure_dir(vuln_store.ROOT)
    conn = sqlite3.connect(vuln_store.FINDINGS_DB, timeout=BUSY_MS / 1000.0, isolation_level=None)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = {}".format(BUSY_MS))
        conn.execute("PRAGMA temp_store = MEMORY")  # the staging tables of one save
        conn.execute("PRAGMA cache_size = -65536")  # 64 MB for one big transaction
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
    """A store the writer has not upgraded yet: no table, or no column, yet."""
    text = str(exc)
    return "no such table" in text or "no such column" in text


def _empty_page(page, page_size, unavailable=False):
    return {"total": 0, "page": page, "page_size": page_size, "pages": 1, "rows": [], "unavailable": unavailable}


def upgrade():
    """Create the store, or bring an older one to the current schema, now.
    Returns the schema version. Saves, prune_orphans, rebuild_fleet and
    forget_server do this themselves; a reader never does."""
    with _writer() as conn:
        return _version(conn)


def readable():
    """None when there is no findings store file; True when a read-only connection can read the current
    version's tables (servers with the added columns, and server_cves); False when the file exists but is busy,
    unreadable, damaged or not yet upgraded. One statement, which only has to compile and read at most a row.
    Never raises."""
    try:
        with _reader() as conn:
            if conn is None:
                return None if not os.path.exists(vuln_store.FINDINGS_DB) else False
            conn.execute("SELECT {} FROM servers s LEFT JOIN server_cves c ON 0 LIMIT 1".format(
                ", ".join(["s." + name for name in _SERVER_ADDED_COLUMNS + (_SERVER_FIXABLE_COLUMN,)] + ["c.cve"]))
            ).fetchone()
            return True
    except Exception as exc:
        if not isinstance(exc, sqlite3.Error) or not _missing_table(exc):
            logger.warning("Vulnerabilities: findings store not readable: %s", exc)
        return False


def schema_version():
    """The file's schema version, or None when there is no readable store."""
    try:
        with _reader() as conn:
            return None if conn is None else _version(conn)
    except sqlite3.OperationalError:
        return None


# ---------------------------------------------------------------- writes
_SERVER_SELECT = "SELECT {} FROM servers".format(", ".join(_SERVER_COLUMNS))


def _server_row(conn, server_id):
    row = conn.execute(_SERVER_SELECT + " WHERE server_id = ?", (server_id,)).fetchone()
    return dict(row) if row else None


def _chunks(items):
    items = list(items)
    for start in range(0, len(items), CHUNK):
        yield items[start:start + CHUNK]


def _refit_fleet(conn, cves):
    """Recompute the fleet rows of these identities from server_cves (through
    server_cves_cve); an identity no server has any more loses its row."""
    for chunk in _chunks(cves):
        marks = ", ".join("?" for _ in chunk)
        found = conn.execute(
            "{} WHERE cve IN ({}) GROUP BY cve".format(_FLEET_FROM_SERVERS, marks), chunk
        ).fetchall()
        conn.executemany(
            "INSERT OR REPLACE INTO fleet_cves(cve, sev_rank, kev, servers, fixable) VALUES (?, ?, ?, ?, ?)",
            [tuple(row) for row in found],
        )
        present = {row[0] for row in found}
        gone = [cve for cve in chunk if cve not in present]
        if gone:
            conn.execute("DELETE FROM fleet_cves WHERE cve IN ({})".format(", ".join("?" for _ in gone)), gone)


def _shrink_fleet(conn, removed):
    """Take servers' deleted server_cves rows out of the fleet rows. removed maps
    an identity to (sev_rank, kev, fixable) of the deleted row. A fleet row only
    needs a full recompute when the deleted row may have been the only one that
    set its maximum severity or its exploited flag, which two LIMIT 1 probes
    through server_cves_cve tell; otherwise the counts just go down by one."""
    refit = []
    for cve, (rank, kev, fixable) in removed.items():
        row = conn.execute("SELECT sev_rank, kev, servers FROM fleet_cves WHERE cve = ?", (cve,)).fetchone()
        if row is None or row[2] <= 1:
            refit.append(cve)  # the last holder (or a row that was not there): the recompute deletes it
        elif rank >= row[0] and not conn.execute(
                "SELECT 1 FROM server_cves WHERE cve = ? AND sev_rank >= ? LIMIT 1", (cve, row[0])).fetchone():
            refit.append(cve)
        elif kev and row[1] and not conn.execute(
                "SELECT 1 FROM server_cves WHERE cve = ? AND kev = 1 LIMIT 1", (cve,)).fetchone():
            refit.append(cve)
        else:
            conn.execute("UPDATE fleet_cves SET servers = servers - 1, fixable = fixable - ? WHERE cve = ?",
                         (fixable, cve))
    _refit_fleet(conn, refit)


def _prepare(server_id, matches, rows):
    """Everything a changed save writes, computed outside the transaction."""
    shared, seqs, raw = {}, {}, []
    for match in matches:
        key = (match["pkg_name"], match["pkg_version"], match["pkg_type"], match["vuln_id"])
        item = shared.get(key)
        if item is None:
            shared[key] = dict(match, cves=list(match["cves"]))
            seqs[key] = len(seqs)
        else:
            # The same package, version and advisory found at two paths is one matches row.
            vuln_match.merge_matches(item, dict(match, cves=list(match["cves"])))
        raw.append((seqs[key], match["path"]))
    staged, links = [], []
    for key, m in shared.items():
        seq = seqs[key]
        staged.append((
            seq, key[0], key[1], key[2], key[3], m["severity"], m["sev_rank"], m["fix_state"], m["fix_version"],
            int(bool(m["kev"])), m["epss"], m["risk"], m["data_source"],
        ))
        links.extend((seq, c["cve"], int(bool(c["kev"]))) for c in m["cves"])
    packages = [
        (
            server_id, p["pkg_name"], p["pkg_version"], p["pkg_type"], p["path"], p["severity"], p["sev_rank"],
            p["cve_count"], int(bool(p["kev"])), p["fix_version"], p["risk"], json.dumps(p["advisories"]),
            p["advisory_count"], json.dumps(p["cves"]),
        )
        for p in vuln_match.package_rows(matches)
    ]
    identities = {
        row["cve"]: (row["sev_rank"], int(bool(row["kev"])), row["pkg_name"], row["pkg_version"],
                     row["fix_version"], row["pkg_count"])
        for row in rows
    }
    return {"staged": staged, "links": links, "raw": raw, "packages": packages, "identities": identities}


_STAGE_TABLES = (
    """CREATE TEMP TABLE vstage(
        seq INTEGER PRIMARY KEY, pkg_name TEXT, pkg_version TEXT, pkg_type TEXT, vuln_id TEXT, severity TEXT,
        sev_rank INTEGER, fix_state TEXT, fix_version TEXT, kev INTEGER, epss REAL, risk REAL, data_source TEXT,
        mid INTEGER)""",
    "CREATE TEMP TABLE vstage_raw(seq INTEGER, path TEXT, PRIMARY KEY(seq, path)) WITHOUT ROWID",
    "CREATE TEMP TABLE vstage_cve(seq INTEGER, cve TEXT, kev INTEGER, PRIMARY KEY(seq, cve)) WITHOUT ROWID",
)


def _store_matches(conn, server_id, prep):
    """The set-based half of a changed save: stage this server's rows in temp
    tables, then change only what differs in the shared tables."""
    for statement in _STAGE_TABLES:
        conn.execute(statement)
    conn.executemany(
        "INSERT INTO vstage(seq, pkg_name, pkg_version, pkg_type, vuln_id, severity, sev_rank, fix_state, "
        "fix_version, kev, epss, risk, data_source) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        prep["staged"],
    )
    conn.executemany("INSERT OR IGNORE INTO vstage_raw(seq, path) VALUES (?, ?)", prep["raw"])
    conn.executemany("INSERT OR REPLACE INTO vstage_cve(seq, cve, kev) VALUES (?, ?, ?)", prep["links"])
    # Shared rows: written only when a value differs, so a re-save of unchanged software touches nothing.
    conn.execute(
        """INSERT INTO matches(pkg_name, pkg_version, pkg_type, vuln_id, severity, sev_rank, fix_state,
               fix_version, kev, epss, risk, data_source)
           SELECT pkg_name, pkg_version, pkg_type, vuln_id, severity, sev_rank, fix_state, fix_version, kev,
                  epss, risk, data_source FROM vstage WHERE true
           ON CONFLICT(pkg_name, pkg_version, pkg_type, vuln_id) DO UPDATE SET
               severity = excluded.severity, sev_rank = excluded.sev_rank, fix_state = excluded.fix_state,
               fix_version = excluded.fix_version, kev = excluded.kev, epss = excluded.epss,
               risk = excluded.risk, data_source = excluded.data_source
           WHERE matches.severity IS NOT excluded.severity OR matches.sev_rank IS NOT excluded.sev_rank
              OR matches.fix_state IS NOT excluded.fix_state OR matches.fix_version IS NOT excluded.fix_version
              OR matches.kev IS NOT excluded.kev OR matches.epss IS NOT excluded.epss
              OR matches.risk IS NOT excluded.risk OR matches.data_source IS NOT excluded.data_source"""
    )
    conn.execute(
        """UPDATE vstage SET mid = (SELECT m.id FROM matches m WHERE m.pkg_name = vstage.pkg_name
               AND m.pkg_version = vstage.pkg_version AND m.pkg_type = vstage.pkg_type
               AND m.vuln_id = vstage.vuln_id)"""
    )
    conn.execute("CREATE INDEX temp.vstage_mid ON vstage(mid)")
    conn.execute(
        """INSERT INTO match_cves(match_id, cve, kev)
           SELECT v.mid, c.cve, c.kev FROM vstage_cve c JOIN vstage v ON v.seq = c.seq WHERE true
           ON CONFLICT(match_id, cve) DO UPDATE SET kev = excluded.kev WHERE match_cves.kev IS NOT excluded.kev"""
    )
    conn.execute(
        """DELETE FROM match_cves WHERE match_id IN (SELECT mid FROM vstage) AND NOT EXISTS (
               SELECT 1 FROM vstage v JOIN vstage_cve c ON c.seq = v.seq
               WHERE v.mid = match_cves.match_id AND c.cve = match_cves.cve)"""
    )
    # This server's own rows: delete what it no longer has, add what it gained.
    conn.execute(
        """DELETE FROM server_matches WHERE server_id = ? AND NOT EXISTS (
               SELECT 1 FROM vstage v JOIN vstage_raw r ON r.seq = v.seq
               WHERE v.mid = server_matches.match_id AND r.path = server_matches.path)""",
        (server_id,),
    )
    conn.execute(
        """INSERT OR IGNORE INTO server_matches(server_id, match_id, path)
           SELECT ?, v.mid, r.path FROM vstage_raw r JOIN vstage v ON v.seq = r.seq""",
        (server_id,),
    )
    conn.execute("DELETE FROM server_packages WHERE server_id = ?", (server_id,))
    conn.executemany(
        """INSERT OR REPLACE INTO server_packages(server_id, pkg_name, pkg_version, pkg_type, path,
               severity, sev_rank, cve_count, kev, fix_version, risk, advisories, advisory_count, cves)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        prep["packages"],
    )
    for table in ("vstage_cve", "vstage_raw", "vstage"):
        conn.execute("DROP TABLE temp.{}".format(table))


def _store_identities(conn, server_id, identities):
    """Make this server's server_cves rows equal to identities, and carry the
    difference into fleet_cves: an identity the server gained is added to its
    fleet row (exact, because a maximum only grows when a row is added); one it
    lost, or whose severity, exploited flag or fix changed, is recomputed."""
    old = {
        row[0]: tuple(row[1:])
        for row in conn.execute(
            "SELECT cve, sev_rank, kev, pkg_name, pkg_version, fix_version, pkg_count FROM server_cves "
            "WHERE server_id = ?", (server_id,))
    }
    gained = [cve for cve in identities if cve not in old]
    lost = [cve for cve in old if cve not in identities]
    rewrite = [cve for cve in identities if cve in old and identities[cve] != old[cve]]
    refit = [cve for cve in rewrite if _fleet_part(identities[cve]) != _fleet_part(old[cve])]
    if lost:
        conn.executemany("DELETE FROM server_cves WHERE server_id = ? AND cve = ?", [(server_id, c) for c in lost])
    conn.executemany(
        """INSERT OR REPLACE INTO server_cves(server_id, cve, sev_rank, kev, pkg_name, pkg_version, fix_version,
               pkg_count) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        [(server_id, cve) + identities[cve] for cve in gained + rewrite],
    )
    conn.executemany(
        """INSERT INTO fleet_cves(cve, sev_rank, kev, servers, fixable) VALUES (?, ?, ?, 1, ?)
           ON CONFLICT(cve) DO UPDATE SET servers = servers + 1, fixable = fixable + excluded.fixable,
               sev_rank = MAX(sev_rank, excluded.sev_rank), kev = MAX(kev, excluded.kev)""",
        [(cve, identities[cve][0], identities[cve][1], 1 if identities[cve][4] else 0) for cve in gained],
    )
    _shrink_fleet(conn, {cve: _fleet_part(old[cve]) for cve in lost})
    _refit_fleet(conn, refit)


def _fleet_part(identity):
    """The parts of a server_cves row that feed its fleet row."""
    return identity[0], identity[1], 1 if identity[4] else 0


def replace_server(server, parsed):
    """Store one server's matches, replacing what it had: one transaction.

    The servers row always takes every column from server, the totals and the
    database build. When the matches themselves are unchanged (equal
    match_hash) nothing else is touched; otherwise the shared match tables,
    the server's own rows and the fleet rows move by exactly the difference.

    Returns the servers row as stored, plus "unchanged": True when the save took
    the fast path because the stored match_hash equalled the new one, else False
    (a first save and a save of different matches). "unchanged" is not a stored
    column and no read function returns it."""
    server_id = int(server["server_id"])
    matches = parsed.get("matches") or []
    rows = vuln_match.vuln_rows(matches)
    totals = vuln_match.summarize(matches, rows)
    digest = vuln_match.match_hash(matches)
    values = {"server_id": server_id, "db_built": parsed.get("db_built") or ""}
    values.update((column, server.get(column)) for column in _SNAPSHOT_COLUMNS)
    values.update((column, totals[column]) for column in _SERVER_COLUMNS if column not in values)
    others = [c for c in _SERVER_COLUMNS if c != "server_id"]

    prepared = []

    def prep():  # computed once, and outside the write lock when the pre-check says the matches changed
        if not prepared:
            prepared.append(_prepare(server_id, matches, rows))
        return prepared[0]

    unchanged = False
    with _writer() as conn:
        before = conn.execute("SELECT match_hash FROM servers WHERE server_id = ?", (server_id,)).fetchone()
        if before is None or before[0] != digest:
            prep()
        with _transaction(conn):
            stored = conn.execute("SELECT match_hash FROM servers WHERE server_id = ?", (server_id,)).fetchone()
            if stored is not None and stored[0] == digest:
                unchanged = True
                conn.execute(
                    "UPDATE servers SET {} WHERE server_id = ?".format(", ".join(c + " = ?" for c in others)),
                    [values[c] for c in others] + [server_id],
                )
            else:
                prep_data = prep()
                _store_matches(conn, server_id, prep_data)
                _store_identities(conn, server_id, prep_data["identities"])
                conn.execute(
                    "INSERT INTO servers({cols}, match_hash) VALUES ({marks}, ?) ON CONFLICT(server_id) DO UPDATE SET "
                    "{sets}, match_hash = excluded.match_hash".format(
                        cols=", ".join(_SERVER_COLUMNS),
                        marks=", ".join("?" for _ in _SERVER_COLUMNS),
                        sets=", ".join("{0} = excluded.{0}".format(c) for c in others)),
                    [values[c] for c in _SERVER_COLUMNS] + [digest],
                )
        saved = _server_row(conn, server_id)
    if saved is not None:
        saved["unchanged"] = unchanged
    return saved


def forget_server(server_id):
    """Drop a server's rows and its share of the fleet rows, in one transaction.
    Shared matches stay until prune_orphans. True when the server had a row."""
    server_id = int(server_id)
    if not os.path.exists(vuln_store.FINDINGS_DB):
        return False
    with _writer() as conn:
        with _transaction(conn):
            removed = {row[0]: (row[1], row[2], 1 if row[3] else 0) for row in conn.execute(
                "SELECT cve, sev_rank, kev, fix_version FROM server_cves WHERE server_id = ?", (server_id,))}
            conn.execute("DELETE FROM server_cves WHERE server_id = ?", (server_id,))
            _shrink_fleet(conn, removed)
            for table in ("server_packages", "server_matches"):
                conn.execute("DELETE FROM {} WHERE server_id = ?".format(table), (server_id,))
            return conn.execute("DELETE FROM servers WHERE server_id = ?", (server_id,)).rowcount > 0


def prune_orphans():
    """Delete matches (and their CVE links) no server references; returns how many
    matches. The orphan set is computed once. Meant for the nightly job."""
    if not os.path.exists(vuln_store.FINDINGS_DB):
        return 0
    with _writer() as conn:
        with _transaction(conn):
            conn.execute(
                """CREATE TEMP TABLE vorphans AS SELECT id FROM matches m
                   WHERE NOT EXISTS (SELECT 1 FROM server_matches s WHERE s.match_id = m.id)"""
            )
            conn.execute("DELETE FROM match_cves WHERE match_id IN (SELECT id FROM vorphans)")
            count = conn.execute("DELETE FROM matches WHERE id IN (SELECT id FROM vorphans)").rowcount
            conn.execute("DROP TABLE temp.vorphans")
            return count


def rebuild_fleet():
    """Rebuild fleet_cves from server_cves in one transaction: the authoritative
    repair. Returns the number of rows."""
    if not os.path.exists(vuln_store.FINDINGS_DB):
        return 0
    with _writer() as conn:
        with _transaction(conn):
            conn.execute("DELETE FROM fleet_cves")
            conn.execute("INSERT INTO fleet_cves(cve, sev_rank, kev, servers, fixable) {} GROUP BY cve".format(
                _FLEET_FROM_SERVERS))
            return conn.execute("SELECT COUNT(*) FROM fleet_cves").fetchone()[0]


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


def _page_of(conn, where, params, order, columns, page, page_size, source, convert, count_source=None,
             count_where=None, count_params=None):
    """Count, clamp the page, fetch it. where/order/source are fixed text built
    in this module; every value travels in params. The count may use a lighter
    source and filter than the page when the page needs a join the count does not."""
    total = conn.execute(
        "SELECT COUNT(*) FROM {} WHERE {}".format(count_source or source, count_where or where),
        params if count_params is None else count_params,
    ).fetchone()[0]
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


def server_packages(server_id, search="", severity="", kev_only=False, page=1, page_size=25, fix_only=False):
    page, page_size = _paging(page, page_size)
    where, params = ["sp.server_id = ?"], [int(server_id)]
    rank = _rank_filter(severity)
    if rank is not None:
        where.append("sp.sev_rank = ?")
        params.append(rank)
    if kev_only:
        where.append("sp.kev = 1")
    if fix_only:
        where.append("COALESCE(sp.fix_version, '') <> ''")
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
# worst of them. A match with no CVE shows under its advisory id. The fix shown is the picked match's own,
# or, when it has none, the highest string among the collapsed matches (the same package), so the with-a-fix
# filter keeps every row of a vulnerability that has a fix on that package.
_CVE_SOURCE = """(
    SELECT cve, vuln_id, severity, sev_rank, kev, epss, risk, pkg_name, pkg_version, pkg_type, path,
           CASE WHEN COALESCE(own_fix, '') <> '' THEN own_fix ELSE any_fix END AS fix_version,
           fix_state, data_source FROM (
        SELECT COALESCE(c.cve, m.vuln_id) AS cve, m.vuln_id AS vuln_id, m.severity AS severity,
               m.sev_rank AS sev_rank, COALESCE(c.kev, m.kev) AS kev, m.epss AS epss, m.risk AS risk,
               m.pkg_name AS pkg_name, m.pkg_version AS pkg_version, m.pkg_type AS pkg_type, sm.path AS path,
               m.fix_version AS own_fix, m.fix_state AS fix_state, m.data_source AS data_source,
               MAX(COALESCE(m.fix_version, '')) OVER (
                   PARTITION BY COALESCE(c.cve, m.vuln_id), m.pkg_name, m.pkg_version, m.pkg_type, sm.path) AS any_fix,
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


def server_cves(server_id, search="", severity="", kev_only=False, page=1, page_size=25, fix_only=False):
    page, page_size = _paging(page, page_size)
    where, params = ["1 = 1"], []
    rank = _rank_filter(severity)
    if rank is not None:
        where.append("v.sev_rank = ?")
        params.append(rank)
    if kev_only:
        where.append("v.kev = 1")
    if fix_only:
        where.append("COALESCE(v.fix_version, '') <> ''")
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


# ---------------------------------------------------------------- fleet reads
def known_servers():
    """Every row of servers, small: server_id, hostname, scan_id, matched_at,
    db_built and inventory_at. Not paged; [] when the store is missing."""
    try:
        with _reader() as conn:
            if conn is None:
                return []
            return [
                dict(row) for row in conn.execute(
                    "SELECT server_id, hostname, scan_id, matched_at, db_built, inventory_at FROM servers "
                    "ORDER BY server_id")
            ]
    except sqlite3.OperationalError as exc:
        _busy(exc, "the server list")
        return []


def fleet_summary():
    """Totals across every server with stored findings, or None when the store
    is missing, not upgraded yet, or not readable."""
    try:
        with _reader() as conn:
            if conn is None:
                return None
            servers = conn.execute(
                "SELECT COUNT(*), COALESCE(SUM(kev > 0), 0), MIN(matched_at), MAX(matched_at) FROM servers"
            ).fetchone()
            fleet = conn.execute(
                "SELECT COUNT(*), COALESCE(SUM(kev = 1), 0), COALESCE(SUM(sev_rank = 4), 0), "
                "COALESCE(SUM(sev_rank = 3), 0), COALESCE(SUM(fixable > 0), 0) FROM fleet_cves"
            ).fetchone()
            builts = [row[0] for row in conn.execute(
                "SELECT db_built FROM servers WHERE db_built <> '' GROUP BY db_built ORDER BY db_built DESC LIMIT 5")]
    except sqlite3.OperationalError as exc:
        _busy(exc, "the fleet summary")
        return None
    return {
        "servers": servers[0], "servers_exploited": servers[1], "vulns": fleet[0], "exploited": fleet[1],
        "critical": fleet[2], "high": fleet[3], "vuln_fixable": fleet[4], "oldest_matched": servers[2], "newest_matched": servers[3],
        "db_builts": builts,
    }


def _fleet_row(row):
    return {"cve": row["cve"], "severity": _SEVERITY_NAMES.get(row["sev_rank"], "Unknown"),
            "sev_rank": row["sev_rank"], "kev": bool(row["kev"]), "servers": row["servers"],
            "fixable": row["fixable"]}


def fleet_cves(search="", severity="", kev_only=False, page=1, page_size=25, fix_only=False):
    """The vulnerabilities across the fleet, worst first: exploited, severity,
    most servers, id. fixable is how many of those servers have a fix; fix_only
    keeps the vulnerabilities with fixable > 0.

    A search that contains CVE- (any case) is a substring match on the id. Any
    other term that equals an advisory id (exact, any case) selects that
    advisory's CVEs; any other term is a substring match on the id."""
    page, page_size = _paging(page, page_size)
    where, params = ["1 = 1"], []
    rank = _rank_filter(severity)
    if rank is not None:
        where.append("f.sev_rank = ?")
        params.append(rank)
    if kev_only:
        where.append("f.kev = 1")
    if fix_only:
        where.append("f.fixable > 0")
    term = str(search or "").strip()[:MAX_SEARCH]
    order = "f.kev DESC, f.sev_rank DESC, f.servers DESC, f.cve"
    columns = "f.cve, f.sev_rank, f.kev, f.servers, f.fixable"
    try:
        with _reader() as conn:
            if conn is None:
                return _empty_page(page, page_size)
            if not conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'fleet_cves'").fetchone():
                return _empty_page(page, page_size)  # a version-1 file no writer has upgraded yet
            if term:
                advisory = "cve-" not in term.lower() and conn.execute(
                    "SELECT 1 FROM matches WHERE vuln_id = ? COLLATE NOCASE LIMIT 1", (term,)).fetchone()
                if advisory:
                    where.append(
                        "f.cve IN (SELECT COALESCE(c.cve, m.vuln_id) FROM matches m "
                        "LEFT JOIN match_cves c ON c.match_id = m.id WHERE m.vuln_id = ? COLLATE NOCASE)")
                    params.append(term)
                else:
                    where.append("f.cve LIKE ? ESCAPE '\\'")
                    params.append(_like(term))
            return _page_of(conn, " AND ".join(where), params, order, columns, page, page_size,
                            "fleet_cves f", _fleet_row)
    except sqlite3.OperationalError as exc:
        _busy(exc, "the fleet vulnerabilities")
        return _empty_page(page, page_size, unavailable=not _missing_table(exc))


def _cve_server_row(row):
    return {
        "server_id": row["server_id"], "hostname": row["hostname"], "group_name": row["group_name"],
        "environment": row["environment"], "os_name": row["os_name"], "pkg_name": row["pkg_name"],
        "pkg_version": row["pkg_version"], "fix_version": row["fix_version"], "pkg_count": row["pkg_count"],
        "severity": _SEVERITY_NAMES.get(row["sev_rank"], "Unknown"), "sev_rank": row["sev_rank"],
        "kev": bool(row["kev"]), "matched_at": row["matched_at"], "inventory_at": row["inventory_at"],
    }


def cve_servers(cve, search="", page=1, page_size=25):
    """The servers that have one vulnerability, by hostname; search is a
    substring of the hostname. A CVE id is looked up in upper case."""
    page, page_size = _paging(page, page_size)
    ident = str(cve or "").strip()[:MAX_SEARCH]
    if ident.lower().startswith("cve-"):
        ident = ident.upper()
    where, params = ["sc.cve = ?"], [ident]
    count_where, count_params = None, None
    pattern = _like(search)
    if pattern:
        where.append("s.hostname LIKE ? ESCAPE '\\'")
        params.append(pattern)
    else:
        count_where, count_params = "sc.cve = ?", [ident]  # no join needed to count
    columns = ("s.server_id, s.hostname, s.group_name, s.environment, s.os_name, sc.pkg_name, sc.pkg_version, "
               "sc.fix_version, sc.pkg_count, sc.sev_rank, sc.kev, s.matched_at, s.inventory_at")
    try:
        with _reader() as conn:
            if conn is None:
                return _empty_page(page, page_size)
            return _page_of(
                conn, " AND ".join(where), params, "s.hostname, s.server_id", columns, page, page_size,
                "server_cves sc JOIN servers s ON s.server_id = sc.server_id", _cve_server_row,
                count_source="server_cves sc" if count_where else None, count_where=count_where,
                count_params=count_params)
    except sqlite3.OperationalError as exc:
        _busy(exc, "the servers of a vulnerability")
        return _empty_page(page, page_size, unavailable=not _missing_table(exc))


_FLEET_SERVER_COLUMNS = (
    "server_id", "hostname", "group_name", "environment", "os_name", "packages", "vulns", "vuln_critical",
    "vuln_high", "kev", "inventory_at", "matched_at", "db_built",
)


def fleet_servers(search="", kev_only=False, page=1, page_size=25):
    """One row per server with stored findings, the most exposed first; search
    is a substring of the hostname, the group or the environment."""
    page, page_size = _paging(page, page_size)
    where, params = ["1 = 1"], []
    if kev_only:
        where.append("s.kev > 0")
    pattern = _like(search)
    if pattern:
        where.append("(s.hostname LIKE ? ESCAPE '\\' OR s.group_name LIKE ? ESCAPE '\\' "
                     "OR s.environment LIKE ? ESCAPE '\\')")
        params += [pattern, pattern, pattern]
    order = "s.kev DESC, s.vuln_critical DESC, s.vuln_high DESC, s.hostname, s.server_id"
    columns = ", ".join("s." + c for c in _FLEET_SERVER_COLUMNS)
    try:
        with _reader() as conn:
            if conn is None:
                return _empty_page(page, page_size)
            return _page_of(conn, " AND ".join(where), params, order, columns, page, page_size, "servers s",
                            lambda row: {key: row[key] for key in _FLEET_SERVER_COLUMNS})
    except sqlite3.OperationalError as exc:
        _busy(exc, "the fleet servers")
        return _empty_page(page, page_size, unavailable=not _missing_table(exc))


# ---------------------------------------------------------------- set reads
# The roll-up of a given set of servers (a resource's servers). The cost of each
# call is the sum of the set's own server_cves rows, reached through the primary
# key prefix server_id; nothing here reads fleet_cves or any fleet-sized table.
MAX_SET = 500  # more ids than this: the first MAX_SET (sorted) are used and "truncated" says so
_INT64 = 2 ** 63


def _clean_ids(ids):
    """(sorted unique real ints, truncated). Anything that is not an int (a bool
    included), or does not fit SQLite's 64-bit integer, is dropped; a value that
    is not iterable gives an empty set."""
    try:
        wanted = {i for i in ids if isinstance(i, int) and not isinstance(i, bool) and -_INT64 <= i < _INT64}
    except TypeError:
        wanted = set()
    wanted = sorted(wanted)
    return wanted[:MAX_SET], len(wanted) > MAX_SET


@contextlib.contextmanager
def _id_set(conn, ids):
    """Yield (fragment, params): the text that follows a column name, like
    IN (?, ?) with the ids bound, or, for more than CHUNK ids, IN (SELECT id
    FROM temp.vset_ids) with the ids loaded into a temp table of this
    connection (a read-only connection may create temp tables; they live in
    the temp database, never in the file). The table is dropped on the way out,
    and goes with the connection anyway."""
    if len(ids) <= CHUNK:  # always, while MAX_SET == CHUNK; the temp table below only applies if MAX_SET grows
        yield "IN ({})".format(", ".join("?" for _ in ids)), list(ids)
        return
    conn.execute("PRAGMA temp_store = MEMORY")
    conn.execute("DROP TABLE IF EXISTS temp.vset_ids")
    conn.execute("CREATE TEMP TABLE vset_ids(id INTEGER PRIMARY KEY)")
    try:
        conn.executemany("INSERT INTO vset_ids(id) VALUES (?)", [(i,) for i in ids])
        yield "IN (SELECT id FROM temp.vset_ids)", []
    finally:
        try:
            conn.execute("DROP TABLE IF EXISTS temp.vset_ids")
        except sqlite3.Error:
            pass  # the connection closes next, and the table goes with it


def _set_empty_page(page, page_size, truncated=False, unavailable=False):
    result = _empty_page(page, page_size, unavailable)
    result["truncated"] = truncated
    return result


_SET_SERVER_COLUMNS = (
    "server_id", "hostname", "os_name", "packages", "vulns", "vuln_critical", "vuln_high", "kev", "inventory_at",
    "matched_at", "db_built",
)


def set_servers(ids):
    """{server_id: row} for the given ids that have a servers row (at most
    MAX_SET of them, the lowest ids); {} when the store is missing or unreadable."""
    ids, _truncated = _clean_ids(ids)
    if not ids:
        return {}
    try:
        with _reader() as conn:
            if conn is None:
                return {}
            with _id_set(conn, ids) as (member, params):
                cursor = conn.execute(
                    "SELECT {} FROM servers WHERE server_id {}".format(", ".join(_SET_SERVER_COLUMNS), member),
                    params)
                return {row["server_id"]: {key: row[key] for key in _SET_SERVER_COLUMNS} for row in cursor}
    except sqlite3.Error as exc:
        _busy(exc, "the servers of a set")
        return {}


def set_summary(ids):
    """Totals across the given servers: how many have stored findings and how
    many distinct vulnerabilities they have between them, each counted once at
    its worst severity and as exploited when any of them has it exploited.
    vuln_fixable is how many of those vulnerabilities have a fix on at least one server of the set.

    None when the store is missing, not upgraded yet, or not readable. An empty
    set is a summary of nothing (scanned 0) and does not look at the store."""
    ids, truncated = _clean_ids(ids)
    result = {"scanned": 0, "vulns": 0, "critical": 0, "high": 0, "exploited": 0, "vuln_fixable": 0,
              "servers_exploited": 0, "oldest_inventory": None, "newest_matched": None, "db_builts": [], "truncated": truncated}
    if not ids:
        return result
    try:
        with _reader() as conn:
            if conn is None:
                return None
            with _id_set(conn, ids) as (member, params):
                # The one aggregation: the set's rows grouped by identity.
                vulns = conn.execute(
                    "SELECT COUNT(*), COALESCE(SUM(worst = 4), 0), COALESCE(SUM(worst = 3), 0), "
                    "COALESCE(SUM(hot = 1), 0), COALESCE(SUM(fixed > 0), 0) FROM (SELECT MAX(sev_rank) AS worst, "
                    "MAX(kev) AS hot, SUM(CASE WHEN fix_version <> '' THEN 1 ELSE 0 END) AS fixed "
                    "FROM server_cves WHERE server_id {} GROUP BY cve)".format(member), params).fetchone()
                servers = conn.execute(
                    "SELECT kev, inventory_at, matched_at, db_built FROM servers WHERE server_id {}".format(member),
                    params).fetchall()
    except sqlite3.Error as exc:
        _busy(exc, "the summary of a set")
        return None
    inventories = [row["inventory_at"] for row in servers if row["inventory_at"]]
    matched = [row["matched_at"] for row in servers if row["matched_at"]]
    builts = sorted({row["db_built"] for row in servers if row["db_built"]}, reverse=True)[:5]
    result.update({
        "scanned": len(servers), "vulns": vulns[0], "critical": vulns[1], "high": vulns[2], "exploited": vulns[3],
        "vuln_fixable": vulns[4],
        "servers_exploited": sum(1 for row in servers if row["kev"] and row["kev"] > 0),
        "oldest_inventory": min(inventories) if inventories else None,
        "newest_matched": max(matched) if matched else None, "db_builts": builts,
    })
    return result


def set_cves(ids, search="", severity="", kev_only=False, page=1, page_size=25, fix_only=False):
    """The vulnerabilities of the given servers, worst first: exploited,
    severity, most servers, id. servers and fixable count servers in the set;
    fix_only keeps the identities with fixable > 0.

    Search works as in fleet_cves: a term containing CVE- is a substring match
    on the id; any other term that equals an advisory id (exact, any case)
    selects that advisory's identities; any other term is a substring match.
    Only identities some server of the set has can appear either way. The set's
    rows are aggregated once into a temp table, then counted and paged from it."""
    page, page_size = _paging(page, page_size)
    ids, truncated = _clean_ids(ids)
    if not ids:
        return _set_empty_page(page, page_size, truncated)
    where, params = ["1 = 1"], []
    rank = _rank_filter(severity)
    if rank is not None:
        where.append("a.sev_rank = ?")
        params.append(rank)
    if kev_only:
        where.append("a.kev = 1")
    if fix_only:
        where.append("a.fixable > 0")
    term = str(search or "").strip()[:MAX_SEARCH]
    order = "a.kev DESC, a.sev_rank DESC, a.servers DESC, a.cve"
    columns = "a.cve, a.sev_rank, a.kev, a.servers, a.fixable"
    try:
        with _reader() as conn:
            if conn is None:
                return _set_empty_page(page, page_size, truncated)
            with _id_set(conn, ids) as (member, member_params):
                inner, inner_params = ["server_id " + member], list(member_params)
                if term:
                    advisory = "cve-" not in term.lower() and conn.execute(
                        "SELECT 1 FROM matches WHERE vuln_id = ? COLLATE NOCASE LIMIT 1", (term,)).fetchone()
                    if advisory:
                        inner.append(
                            "cve IN (SELECT COALESCE(c.cve, m.vuln_id) FROM matches m "
                            "LEFT JOIN match_cves c ON c.match_id = m.id WHERE m.vuln_id = ? COLLATE NOCASE)")
                        inner_params.append(term)
                    else:
                        inner.append("cve LIKE ? ESCAPE '\\'")
                        inner_params.append(_like(term))
                conn.execute("PRAGMA temp_store = MEMORY")
                conn.execute("DROP TABLE IF EXISTS temp.vset_agg")
                conn.execute(
                    "CREATE TEMP TABLE vset_agg AS SELECT cve, MAX(sev_rank) AS sev_rank, MAX(kev) AS kev, "
                    "COUNT(*) AS servers, SUM(CASE WHEN fix_version <> '' THEN 1 ELSE 0 END) AS fixable "
                    "FROM server_cves WHERE {} GROUP BY cve".format(" AND ".join(inner)), inner_params)
                try:
                    result = _page_of(conn, " AND ".join(where), params, order, columns, page, page_size,
                                      "temp.vset_agg a", _fleet_row)
                finally:
                    try:
                        conn.execute("DROP TABLE IF EXISTS temp.vset_agg")
                    except sqlite3.Error:
                        pass
            result["truncated"] = truncated
            return result
    except sqlite3.Error as exc:
        _busy(exc, "the vulnerabilities of a set")
        return _set_empty_page(page, page_size, truncated, unavailable=not _missing_table(exc))


def set_cve_servers(ids, cve, search="", page=1, page_size=25):
    """The servers of the given set that have one vulnerability, by hostname;
    search is a substring of the hostname. A CVE id is looked up in upper case.
    The rows are those of cve_servers."""
    page, page_size = _paging(page, page_size)
    ids, truncated = _clean_ids(ids)
    if not ids:
        return _set_empty_page(page, page_size, truncated)
    ident = str(cve or "").strip()[:MAX_SEARCH]
    if ident.lower().startswith("cve-"):
        ident = ident.upper()
    where, params = ["sc.cve = ?"], [ident]
    pattern = _like(search)
    if pattern:
        where.append("s.hostname LIKE ? ESCAPE '\\'")
        params.append(pattern)
    columns = ("s.server_id, s.hostname, s.group_name, s.environment, s.os_name, sc.pkg_name, sc.pkg_version, "
               "sc.fix_version, sc.pkg_count, sc.sev_rank, sc.kev, s.matched_at, s.inventory_at")
    try:
        with _reader() as conn:
            if conn is None:
                return _set_empty_page(page, page_size, truncated)
            with _id_set(conn, ids) as (member, member_params):
                where.append("sc.server_id " + member)
                result = _page_of(
                    conn, " AND ".join(where), params + member_params, "s.hostname, s.server_id", columns, page,
                    page_size, "server_cves sc JOIN servers s ON s.server_id = sc.server_id", _cve_server_row)
            result["truncated"] = truncated
            return result
    except sqlite3.Error as exc:
        _busy(exc, "the servers of a vulnerability in a set")
        return _set_empty_page(page, page_size, truncated, unavailable=not _missing_table(exc))


def set_cve_info(ids, cve):
    """The row set_cves gives for one identity among the given servers: {"cve", "severity", "sev_rank",
    "kev", "servers", "fixable"}, or None when no server of the set has it or the store is missing,
    unreadable or not upgraded. A CVE id is looked up in upper case, as set_cve_servers does. One indexed
    lookup through server_cves_cve (cve, server_id); no GROUP BY over the set."""
    ids, _truncated = _clean_ids(ids)
    ident = str(cve or "").strip()[:MAX_SEARCH]
    if not ids or not ident:
        return None
    if ident.lower().startswith("cve-"):
        ident = ident.upper()
    try:
        with _reader() as conn:
            if conn is None:
                return None
            with _id_set(conn, ids) as (member, member_params):
                # INDEXED BY: the primary key (server_id, cve) would also serve, and walks every id of the set.
                row = conn.execute(
                    "SELECT COUNT(*) AS servers, MAX(sev_rank) AS sev_rank, MAX(kev) AS kev, "
                    "COALESCE(SUM(CASE WHEN fix_version <> '' THEN 1 ELSE 0 END), 0) AS fixable "
                    "FROM server_cves INDEXED BY server_cves_cve WHERE cve = ? AND server_id {}".format(member),
                    [ident] + member_params).fetchone()
    except sqlite3.Error as exc:
        _busy(exc, "one vulnerability of a set")
        return None
    if not row or not row["servers"]:
        return None
    return _fleet_row({"cve": ident, "sev_rank": row["sev_rank"], "kev": row["kev"], "servers": row["servers"],
                       "fixable": row["fixable"]})
