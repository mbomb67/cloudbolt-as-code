"""
Checks for the findings store of the Vulnerabilities XUI (vuln_db.py), without
CloudBolt: vuln_store is a stub, the database is a SQLite file in a temp folder.

    python -B -m unittest discover -s extensions/XUI-wqrj0e3p/tests -p "test_vuln_db.py"

The package is loaded under a private name so that another test file's stub of
the same extension cannot get in the way.
"""
import importlib
import os
import random
import shutil
import sqlite3
import sys
import tempfile
import threading
import types
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
PKG_DIR = os.path.join(os.path.dirname(HERE), "io_cloudbolt_vulnerabilities")
PKG = "vuln_s_db_pkg"

_pkg = types.ModuleType(PKG)
_pkg.__path__ = [PKG_DIR]
sys.modules[PKG] = _pkg
store = types.ModuleType(PKG + ".vuln_store")
store.ROOT = ""
store.FINDINGS_DB = ""
store.ensure_dir = lambda path: os.makedirs(path, exist_ok=True)
store.own = lambda path, directory=False: None
sys.modules[PKG + ".vuln_store"] = store
_pkg.vuln_store = store
vuln_match = importlib.import_module(PKG + ".vuln_match")
vuln_db = importlib.import_module(PKG + ".vuln_db")

# The version-1 tables, as the first release created them
# (git show vulnerabilities-xui:extensions/XUI-wqrj0e3p/io_cloudbolt_vulnerabilities/vuln_db.py).
V1_SERVERS_OLD = """CREATE TABLE servers(
    server_id INTEGER PRIMARY KEY, hostname TEXT, os_name TEXT, group_name TEXT, environment TEXT,
    scan_id TEXT, inventory_at TEXT, packages INTEGER, matched_at TEXT, db_built TEXT,
    findings INTEGER, critical INTEGER, high INTEGER, medium INTEGER, low INTEGER, unknown INTEGER,
    kev INTEGER, cves INTEGER)"""
V1_SERVERS_ADDED = """CREATE TABLE servers(
    server_id INTEGER PRIMARY KEY, hostname TEXT, os_name TEXT, group_name TEXT, environment TEXT,
    scan_id TEXT, inventory_at TEXT, packages INTEGER, matched_at TEXT, db_built TEXT,
    findings INTEGER, critical INTEGER, high INTEGER, medium INTEGER, low INTEGER, unknown INTEGER,
    kev INTEGER, cves INTEGER, vulns INTEGER, vuln_critical INTEGER, vuln_high INTEGER,
    vuln_medium INTEGER, vuln_low INTEGER, vuln_unknown INTEGER)"""
V1_OTHERS = (
    """CREATE TABLE matches(
        id INTEGER PRIMARY KEY, pkg_name TEXT, pkg_version TEXT, pkg_type TEXT, vuln_id TEXT,
        severity TEXT, sev_rank INTEGER, fix_state TEXT, fix_version TEXT, kev INTEGER, epss REAL,
        risk REAL, data_source TEXT, UNIQUE(pkg_name, pkg_version, pkg_type, vuln_id))""",
    """CREATE TABLE match_cves(
        match_id INTEGER, cve TEXT, kev INTEGER, PRIMARY KEY(match_id, cve)) WITHOUT ROWID""",
    "CREATE INDEX match_cves_cve ON match_cves(cve)",
    """CREATE TABLE server_matches(
        server_id INTEGER, match_id INTEGER, path TEXT, PRIMARY KEY(server_id, match_id, path)) WITHOUT ROWID""",
    "CREATE INDEX server_matches_match ON server_matches(match_id)",
    """CREATE TABLE server_packages(
        server_id INTEGER, pkg_name TEXT, pkg_version TEXT, pkg_type TEXT, path TEXT,
        severity TEXT, sev_rank INTEGER, cve_count INTEGER, kev INTEGER, fix_version TEXT, risk REAL,
        advisories TEXT, advisory_count INTEGER, cves TEXT,
        PRIMARY KEY(server_id, pkg_name, pkg_version, pkg_type, path)) WITHOUT ROWID""",
)
DB_OLD = "2026-10-01T00:00:00Z"
DB_NEW = "2026-11-01T00:00:00Z"


def mk(name, version, vuln_id, severity="High", cves=(), kev=False, fix="", path="", pkg_type="rpm"):
    return {
        "pkg_name": name, "pkg_version": version, "pkg_type": pkg_type, "path": path, "vuln_id": vuln_id,
        "severity": severity, "sev_rank": vuln_match.sev_rank(severity), "fix_state": "fixed" if fix else "unknown",
        "fix_version": fix, "kev": kev, "epss": None, "risk": None, "data_source": "",
        "cves": [{"cve": cve, "kev": bool(flag)} for cve, flag in cves],
    }


def snap(server_id, **extra):
    data = {
        "server_id": server_id, "hostname": "host-%d" % server_id, "os_name": "Amazon Linux 2023",
        "group_name": "Group A", "environment": "Env 1", "scan_id": "20261101T000000Z-%06d" % server_id,
        "inventory_at": "2026-11-01T00:00:00Z", "packages": 100, "matched_at": "2026-11-01T00:05:00Z",
    }
    data.update(extra)
    return data


def save(server_id, matches, db_built=DB_NEW, **extra):
    return vuln_db.replace_server(snap(server_id, **extra), {"matches": matches, "db_built": db_built})


TABLES = ("matches", "match_cves", "server_matches", "server_packages", "server_cves", "fleet_cves", "servers")


class StoreTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="vuln-db-test-")
        store.ROOT = self.tmp
        store.FINDINGS_DB = os.path.join(self.tmp, "findings.db")
        self.conns = []
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.addCleanup(self.reset)

    def raw(self):
        conn = sqlite3.connect(store.FINDINGS_DB)
        self.conns.append(conn)
        return conn

    def reset(self):
        """Delete the store; a Windows file stays locked while a connection of the test is open."""
        for conn in self.conns:
            conn.close()
        self.conns = []
        if os.path.exists(store.FINDINGS_DB):
            os.remove(store.FINDINGS_DB)

    def dump(self, *tables):
        conn = self.raw()
        return {t: sorted(conn.execute("SELECT * FROM {}".format(t)).fetchall(), key=repr)
                for t in (tables or TABLES)}

    def fleet(self):
        return {row[0]: tuple(row[1:]) for row in self.raw().execute("SELECT * FROM fleet_cves")}

    def fleet_by_hand(self):
        """The fleet rows worked out from server_cves in Python, independent of rebuild_fleet."""
        groups = {}
        for cve, rank, kev, _sid, fix in self.raw().execute(
                "SELECT cve, sev_rank, kev, server_id, fix_version FROM server_cves"):
            groups.setdefault(cve, []).append((rank, kev, fix))
        return {cve: (max(r for r, _k, _f in rows), max(k for _r, k, _f in rows), len(rows),
                      sum(1 for _r, _k, f in rows if f)) for cve, rows in groups.items()}

    def server_cve_rows(self, server_id):
        return sorted(self.raw().execute(
            "SELECT cve, sev_rank, kev, pkg_name, pkg_version, fix_version, pkg_count FROM server_cves "
            "WHERE server_id = ?", (server_id,)).fetchall())

    @staticmethod
    def expected_server_cves(matches):
        return sorted(
            (r["cve"], r["sev_rank"], int(r["kev"]), r["pkg_name"], r["pkg_version"], r["fix_version"], r["pkg_count"])
            for r in vuln_match.vuln_rows(matches))


# A small store used by several tests: three servers, overlapping on purpose.
def sample_matches():
    shared = [
        mk("bash", "5.1", "ALAS-1", "High", [("CVE-2024-0001", True), ("CVE-2024-0002", False)], True, "5.2"),
        mk("openssl", "3.0", "ALAS-2", "Critical", [("CVE-2024-0003", False)], False, "3.1"),
        mk("zlib", "1.2", "ALAS-3", "Low", [("CVE-2024-0004", False)], False, ""),
        mk("libx", "1.0", "GHSA-aaaa-bbbb", "Medium", [], False, "1.1", "", "deb"),
    ]
    only_b = [mk("curl", "8.0", "ALAS-4", "Medium", [("CVE-2024-0005", False)], False, "8.1")]
    only_c = [mk("curl", "8.0", "ALAS-4", "Medium", [("CVE-2024-0005", False)], False, "8.1"),
              mk("sqlite", "3.4", "ALAS-5", "Critical", [("CVE-2024-0006", True)], True, "3.5")]
    return {1: shared, 2: shared + only_b, 3: shared[:2] + only_c}


# ---------------------------------------------------------------- schema
class FreshSchemaTests(StoreTestCase):
    def test_new_file_is_version_2(self):
        save(1, sample_matches()[1])
        conn = self.raw()
        self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 2)
        names = {row[0] for row in conn.execute("SELECT name FROM sqlite_master")}
        for name in ("servers", "matches", "match_cves", "server_matches", "server_packages", "server_cves",
                     "fleet_cves", "server_cves_cve", "fleet_cves_order", "matches_vuln", "servers_fleet",
                     "match_cves_cve", "server_matches_match"):
            self.assertIn(name, names)
        columns = {row[1] for row in conn.execute("PRAGMA table_info(servers)")}
        self.assertTrue({"match_hash", "vulns", "vuln_unknown"} <= columns)

    def test_returned_row_has_the_server_columns_not_the_hash(self):
        row = save(1, sample_matches()[1])
        self.assertEqual(sorted(row), sorted(list(vuln_db._SERVER_COLUMNS) + ["unchanged"]))
        self.assertEqual(row["hostname"], "host-1")
        self.assertEqual(row["findings"], 4)
        self.assertEqual(row["vulns"], 5)
        self.assertEqual(row["db_built"], DB_NEW)
        self.assertEqual({k: v for k, v in row.items() if k != "unchanged"}, vuln_db.server_summary(1))

    def test_a_missing_store_reads_empty(self):
        self.assertEqual(vuln_db.known_servers(), [])
        self.assertIsNone(vuln_db.fleet_summary())
        for page in (vuln_db.fleet_cves(), vuln_db.cve_servers("CVE-1"), vuln_db.fleet_servers()):
            self.assertEqual((page["total"], page["rows"], page["unavailable"], page["pages"]), (0, [], False, 1))
        self.assertFalse(vuln_db.forget_server(1))
        self.assertEqual((vuln_db.prune_orphans(), vuln_db.rebuild_fleet()), (0, 0))
        self.assertIsNone(vuln_db.schema_version())
        self.assertFalse(os.path.exists(store.FINDINGS_DB))  # readers and no-op writers never create it


class UpgradeTests(StoreTestCase):
    def make_v1(self, with_added, user_version=1):
        conn = sqlite3.connect(store.FINDINGS_DB, isolation_level=None)
        conn.execute(V1_SERVERS_ADDED if with_added else V1_SERVERS_OLD)
        for statement in V1_OTHERS:
            conn.execute(statement)
        conn.execute("PRAGMA user_version = {}".format(user_version))
        return conn

    def v1_save(self, conn, server_id, matches, with_added):
        """What the first release's replace_server wrote, in the simplest way that gives the same rows."""
        totals = vuln_match.summarize(matches)
        for m in matches:
            conn.execute(
                "INSERT OR IGNORE INTO matches(pkg_name, pkg_version, pkg_type, vuln_id, severity, sev_rank, "
                "fix_state, fix_version, kev, epss, risk, data_source) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (m["pkg_name"], m["pkg_version"], m["pkg_type"], m["vuln_id"], m["severity"], m["sev_rank"],
                 m["fix_state"], m["fix_version"], int(m["kev"]), m["epss"], m["risk"], m["data_source"]))
            mid = conn.execute(
                "SELECT id FROM matches WHERE pkg_name = ? AND pkg_version = ? AND pkg_type = ? AND vuln_id = ?",
                (m["pkg_name"], m["pkg_version"], m["pkg_type"], m["vuln_id"])).fetchone()[0]
            for c in m["cves"]:
                conn.execute("INSERT OR REPLACE INTO match_cves(match_id, cve, kev) VALUES (?, ?, ?)",
                             (mid, c["cve"], int(c["kev"])))
            conn.execute("INSERT OR IGNORE INTO server_matches(server_id, match_id, path) VALUES (?, ?, ?)",
                         (server_id, mid, m["path"]))
        for p in vuln_match.package_rows(matches):
            conn.execute(
                "INSERT OR REPLACE INTO server_packages VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (server_id, p["pkg_name"], p["pkg_version"], p["pkg_type"], p["path"], p["severity"], p["sev_rank"],
                 p["cve_count"], int(p["kev"]), p["fix_version"], p["risk"], "[]", p["advisory_count"], "[]"))
        data = snap(server_id)
        columns = list(vuln_db._SERVER_BASE_COLUMNS) + (list(vuln_db._SERVER_ADDED_COLUMNS) if with_added else [])
        values = []
        for column in columns:
            values.append(data[column] if column in data else (DB_OLD if column == "db_built" else totals.get(column)))
        values[columns.index("server_id")] = server_id
        conn.execute("INSERT INTO servers({}) VALUES ({})".format(", ".join(columns), ", ".join("?" for _ in columns)),
                     values)

    def populated_v1(self, with_added=True, user_version=1):
        conn = self.make_v1(with_added, user_version)
        data = sample_matches()
        # Same package at two paths, an advisory reached by two CVEs, a pure advisory, and a tie on severity.
        data[1] = data[1] + [
            mk("libfoo", "1.0", "CVE-2024-0007", "High", [("CVE-2024-0007", False)], False, "1.1", "/opt/a", "python"),
            mk("libfoo", "1.0", "CVE-2024-0007", "High", [("CVE-2024-0007", False)], False, "1.1", "/opt/b", "python"),
            mk("aaa", "1", "ALAS-9", "High", [("CVE-2024-0002", False)], False, "9"),
        ]
        for server_id, matches in data.items():
            self.v1_save(conn, server_id, matches, with_added)
        conn.close()
        return data

    def test_reader_never_upgrades_and_never_raises(self):
        for with_added in (True, False):
            with self.subTest(with_added=with_added):
                self.reset()
                self.populated_v1(with_added)
                self.assertIsNone(vuln_db.fleet_summary())
                for page in (vuln_db.fleet_cves(), vuln_db.fleet_cves(search="CVE-2024"),
                             vuln_db.cve_servers("CVE-2024-0001")):
                    self.assertEqual((page["total"], page["rows"], page["unavailable"]), (0, [], False))
                page = vuln_db.fleet_servers()
                self.assertFalse(page["unavailable"])
                self.assertEqual(page["total"], len(page["rows"]))  # the added columns may be absent: empty page
                self.assertEqual(len(vuln_db.known_servers()), 3)
                self.assertEqual(vuln_db.schema_version(), 1)
                self.assertEqual(sum(1 for row in self.raw().execute("PRAGMA table_info(servers)")),
                                 24 if with_added else 18)
                # The version-1 reads keep working.
                self.assertTrue(vuln_db.server_cves(1)["total"] > 0)

    def test_upgrade_backfills_and_matches_a_fresh_save(self):
        for with_added, user_version in ((True, 1), (False, 1), (True, 0), (False, 0)):
            with self.subTest(with_added=with_added, user_version=user_version):
                self.reset()
                data = self.populated_v1(with_added, user_version)
                self.assertEqual(vuln_db.upgrade(), 2)
                conn = self.raw()
                self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 2)
                columns = [row[1] for row in conn.execute("PRAGMA table_info(servers)")]
                self.assertEqual(columns.count("match_hash"), 1)
                self.assertEqual(len(columns), 25)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM servers WHERE match_hash IS NOT NULL").fetchone()[0], 0)
                for server_id, matches in data.items():
                    self.assertEqual(self.server_cve_rows(server_id), self.expected_server_cves(matches), server_id)
                self.assertEqual(self.fleet(), self.fleet_by_hand())
                self.assertEqual(vuln_db.rebuild_fleet(), len(self.fleet()))
                self.assertEqual(self.fleet(), self.fleet_by_hand())
                # The upgraded file answers the fleet reads.
                self.assertEqual(vuln_db.fleet_summary()["servers"], 3)
                self.assertEqual(vuln_db.fleet_cves()["total"], len(self.fleet()))
                self.assertEqual(vuln_db.cve_servers("CVE-2024-0001")["total"], 3)
                self.assertTrue(vuln_db.server_cves(1)["total"] > 0)
                # Idempotent, and the first save of each server takes the full path, the second the fast one.
                self.assertEqual(vuln_db.upgrade(), 2)
                before = self.dump("server_cves", "fleet_cves")
                save(1, data[1])
                self.assertIsNotNone(self.raw().execute("SELECT match_hash FROM servers WHERE server_id = 1").fetchone()[0])
                self.assertEqual(self.dump("server_cves", "fleet_cves"), before)
                with mock.patch.object(vuln_db, "_store_matches", side_effect=AssertionError("full path")):
                    save(1, data[1])

    def test_a_writer_that_loses_the_race_finds_the_new_version(self):
        self.populated_v1(True)
        winner = sqlite3.connect(store.FINDINGS_DB, isolation_level=None)
        loser = sqlite3.connect(store.FINDINGS_DB, isolation_level=None)
        self.addCleanup(winner.close)
        self.addCleanup(loser.close)
        real = vuln_db._version
        calls = []

        def version(conn):
            if conn is loser and not calls:
                calls.append(1)
                return 1  # what the loser saw before the winner committed
            return real(conn)

        vuln_db.ensure_schema(winner)
        with mock.patch.object(vuln_db, "_version", version), \
                mock.patch.object(vuln_db, "_upgrade", side_effect=AssertionError("upgraded twice")):
            vuln_db.ensure_schema(loser)  # would raise "duplicate column name" if it tried again
        self.assertEqual(real(loser), 2)

    def test_two_threads_upgrading_at_once(self):
        self.populated_v1(False)
        errors, gate = [], threading.Barrier(4)

        def work():
            try:
                gate.wait(10)
                vuln_db.upgrade()
            except Exception as exc:  # the test reports it below
                errors.append(repr(exc))

        threads = [threading.Thread(target=work) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(60)
        self.assertEqual(errors, [])
        columns = [row[1] for row in self.raw().execute("PRAGMA table_info(servers)")]
        self.assertEqual(len(columns), len(set(columns)))
        self.assertEqual(vuln_db.schema_version(), 2)
        self.assertEqual(self.fleet(), self.fleet_by_hand())

    def test_a_failed_upgrade_leaves_version_1_intact(self):
        self.populated_v1(False)
        with mock.patch.object(vuln_db, "_INDEXES", vuln_db._INDEXES + ("CREATE INDEX broken ON nowhere(x)",)):
            with self.assertRaises(sqlite3.OperationalError):
                vuln_db.upgrade()
        conn = self.raw()
        self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 1)
        columns = [row[1] for row in conn.execute("PRAGMA table_info(servers)")]
        self.assertNotIn("match_hash", columns)
        self.assertEqual(len(columns), 18)
        names = {row[0] for row in conn.execute("SELECT name FROM sqlite_master")}
        self.assertNotIn("server_cves", names)
        self.assertEqual(vuln_db.upgrade(), 2)  # and the next try succeeds


# ---------------------------------------------------------------- writes
class ReplaceTests(StoreTestCase):
    def test_rows_land_in_every_table(self):
        matches = sample_matches()[1]
        save(1, matches)
        conn = self.raw()
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM matches").fetchone()[0], 4)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM server_matches").fetchone()[0], 4)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM match_cves").fetchone()[0], 4)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM server_packages").fetchone()[0], 4)
        self.assertEqual(self.server_cve_rows(1), self.expected_server_cves(matches))
        self.assertEqual(self.fleet(), self.fleet_by_hand())
        self.assertEqual(self.fleet()["CVE-2024-0001"], (3, 1, 1, 1))

    def test_shared_rows_are_not_duplicated(self):
        for server_id, matches in sample_matches().items():
            save(server_id, matches)
        self.assertEqual(self.raw().execute("SELECT COUNT(*) FROM matches").fetchone()[0], 6)
        self.assertEqual(self.raw().execute("SELECT COUNT(*) FROM server_matches").fetchone()[0], 4 + 5 + 4)

    def test_same_package_at_two_paths_is_one_match_row_and_two_server_rows(self):
        matches = [mk("libfoo", "1", "CVE-2024-0100", "High", [("CVE-2024-0100", False)], False, "2", "/a", "python"),
                   mk("libfoo", "1", "CVE-2024-0100", "High", [("CVE-2024-0100", False)], False, "2", "/b", "python")]
        save(1, matches)
        self.assertEqual(self.raw().execute("SELECT COUNT(*) FROM matches").fetchone()[0], 1)
        self.assertEqual(self.raw().execute("SELECT COUNT(*) FROM server_matches").fetchone()[0], 2)
        self.assertEqual(self.server_cve_rows(1)[0][-1], 2)

    def test_unchanged_save_takes_the_fast_path_and_refreshes_the_server_row(self):
        matches = sample_matches()[2]
        save(2, matches)
        before = self.dump("matches", "match_cves", "server_matches", "server_packages", "server_cves", "fleet_cves")
        with mock.patch.object(vuln_db, "_store_matches", side_effect=AssertionError("full path")), \
                mock.patch.object(vuln_db, "_store_identities", side_effect=AssertionError("full path")):
            row = vuln_db.replace_server(
                {"server_id": 2, "hostname": "renamed", "scan_id": "20261102T000000Z-000002", "packages": 777,
                 "matched_at": "2026-11-02T00:05:00Z", "inventory_at": "2026-11-02T00:00:00Z"},
                {"matches": list(reversed(matches)), "db_built": DB_OLD})
        self.assertEqual(self.dump("matches", "match_cves", "server_matches", "server_packages", "server_cves",
                                   "fleet_cves"), before)
        self.assertEqual(row["hostname"], "renamed")
        self.assertEqual(row["packages"], 777)
        self.assertEqual(row["db_built"], DB_OLD)
        self.assertEqual(row["matched_at"], "2026-11-02T00:05:00Z")
        # A key the caller leaves out becomes NULL, as it always did.
        for column in ("os_name", "group_name", "environment"):
            self.assertIsNone(row[column])
        totals = vuln_match.summarize(matches)
        for key, value in totals.items():
            self.assertEqual(row[key], value, key)

    def test_unchanged_key_says_whether_the_fast_path_was_taken(self):
        matches = sample_matches()[1]
        self.assertIs(save(1, matches)["unchanged"], False)                       # first save
        self.assertIs(save(1, list(reversed(matches)), matched_at="2026-11-03T00:00:00Z")["unchanged"], True)
        changed = [dict(matches[0], severity="Critical", sev_rank=4)] + matches[1:]
        self.assertIs(save(1, changed)["unchanged"], False)                       # different matches
        self.assertIs(save(1, changed)["unchanged"], True)
        # It is a return value only: neither a stored column nor part of what the read functions return.
        self.assertNotIn("unchanged", vuln_db.server_summary(1))
        self.assertNotIn("unchanged", [c[1] for c in self.raw().execute("PRAGMA table_info(servers)")])

    def test_fast_path_after_a_changed_save_and_back_again(self):
        original = sample_matches()[1]
        save(1, original)
        changed = [dict(original[0], severity="Critical", sev_rank=4)] + original[1:]
        save(1, changed)
        self.assertEqual(self.server_cve_rows(1), self.expected_server_cves(changed))
        save(1, original)  # not the stored hash: full path again
        self.assertEqual(self.server_cve_rows(1), self.expected_server_cves(original))
        self.assertEqual(self.fleet(), self.fleet_by_hand())

    def test_changed_save_moves_exactly_the_difference(self):
        a, b = sample_matches()[1], sample_matches()[2]
        save(1, a)
        save(2, b)
        self.assertEqual(self.fleet()["CVE-2024-0001"][2], 2)
        # Server 2 loses a CVE it shared, gains another, and one fix and one severity change.
        changed = [m for m in b if m["vuln_id"] != "ALAS-1"]
        changed[0] = dict(changed[0], severity="High", sev_rank=3, fix_version="")  # was Critical with a fix
        changed.append(mk("newpkg", "1", "ALAS-8", "High", [("CVE-2024-0008", True)], True, "2"))
        save(2, changed)
        self.assertEqual(self.server_cve_rows(2), self.expected_server_cves(changed))
        self.assertEqual(self.fleet(), self.fleet_by_hand())
        self.assertEqual(self.fleet()["CVE-2024-0001"][2], 1)  # only server 1 now
        self.assertEqual(self.fleet()["CVE-2024-0003"][2:], (2, 1))  # server 2 lost its fix; server 1 still has one
        self.assertEqual(self.fleet()["CVE-2024-0003"][0], 4)  # the maximum stays: server 1 is still Critical
        self.assertEqual(self.fleet()["CVE-2024-0008"], (3, 1, 1, 1))
        # The shared and per-server tables follow, and an orphaned match goes at the next prune.
        paths = self.raw().execute(
            "SELECT m.vuln_id FROM server_matches s JOIN matches m ON m.id = s.match_id WHERE s.server_id = 2"
        ).fetchall()
        self.assertEqual(sorted(p[0] for p in paths), sorted(m["vuln_id"] for m in changed))
        self.assertEqual(self.raw().execute("SELECT COUNT(*) FROM server_packages WHERE server_id = 2").fetchone()[0],
                         len(changed))

    def test_match_cve_links_follow_a_changed_match(self):
        save(1, [mk("p", "1", "A1", "High", [("CVE-2024-0001", False), ("CVE-2024-0002", False)])])
        save(1, [mk("p", "1", "A1", "High", [("CVE-2024-0002", True), ("CVE-2024-0009", False)])])
        links = sorted(self.raw().execute("SELECT cve, kev FROM match_cves").fetchall())
        self.assertEqual(links, [("CVE-2024-0002", 1), ("CVE-2024-0009", 0)])
        self.assertEqual(self.raw().execute("SELECT COUNT(*) FROM matches").fetchone()[0], 1)

    def test_empty_matches_clear_the_server_and_its_fleet_share(self):
        save(1, sample_matches()[1])
        save(2, sample_matches()[1])
        row = save(1, [])
        self.assertEqual((row["findings"], row["vulns"]), (0, 0))
        self.assertEqual(self.server_cve_rows(1), [])
        self.assertEqual(self.fleet(), self.fleet_by_hand())
        self.assertEqual(self.fleet()["CVE-2024-0001"][2], 1)
        self.assertEqual(self.raw().execute("SELECT COUNT(*) FROM server_matches WHERE server_id = 1").fetchone()[0], 0)

    def test_a_failure_inside_the_transaction_changes_nothing(self):
        save(1, sample_matches()[1])
        before = self.dump()
        with mock.patch.object(vuln_db, "_store_identities", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                save(1, sample_matches()[3])
        self.assertEqual(self.dump(), before)


class ForgetAndPruneTests(StoreTestCase):
    def test_forget_fixes_the_fleet_rows(self):
        data = sample_matches()
        for server_id, matches in data.items():
            save(server_id, matches)
        self.assertEqual(self.fleet()["CVE-2024-0001"], (3, 1, 3, 3))
        self.assertTrue(vuln_db.forget_server(3))
        self.assertEqual(self.fleet(), self.fleet_by_hand())
        self.assertEqual(self.fleet()["CVE-2024-0001"][2], 2)
        self.assertNotIn("CVE-2024-0006", self.fleet())  # only server 3 had it
        self.assertEqual(self.fleet()["CVE-2024-0005"][2], 1)
        for table in ("servers", "server_matches", "server_packages", "server_cves"):
            self.assertEqual(self.raw().execute("SELECT COUNT(*) FROM {} WHERE server_id = 3".format(table)).fetchone()[0], 0)
        self.assertFalse(vuln_db.forget_server(3))
        self.assertFalse(vuln_db.forget_server(99))
        self.assertEqual(self.fleet(), self.fleet_by_hand())
        self.assertTrue(vuln_db.forget_server(1))
        self.assertTrue(vuln_db.forget_server(2))
        self.assertEqual(self.fleet(), {})

    def test_forget_recomputes_the_maximum_and_the_fix_count(self):
        save(1, [mk("a", "1", "A1", "Critical", [("CVE-2024-0500", True)], True, "2")])
        save(2, [mk("b", "1", "A2", "Low", [("CVE-2024-0500", False)], False, "")])
        self.assertEqual(self.fleet()["CVE-2024-0500"], (4, 1, 2, 1))
        vuln_db.forget_server(1)
        self.assertEqual(self.fleet()["CVE-2024-0500"], (1, 0, 1, 0))

    def test_prune_orphans_deletes_only_unreferenced_matches(self):
        for server_id, matches in sample_matches().items():
            save(server_id, matches)
        vuln_db.forget_server(3)
        self.assertEqual(vuln_db.prune_orphans(), 1)  # sqlite / CVE-2024-0006
        names = {row[0] for row in self.raw().execute("SELECT pkg_name FROM matches")}
        self.assertNotIn("sqlite", names)
        self.assertIn("curl", names)  # server 2 still has it
        self.assertEqual(self.raw().execute(
            "SELECT COUNT(*) FROM match_cves WHERE cve = 'CVE-2024-0006'").fetchone()[0], 0)
        self.assertEqual(vuln_db.prune_orphans(), 0)
        self.assertEqual(self.fleet(), self.fleet_by_hand())


class RandomisedFleetTests(StoreTestCase):
    POOL = [("pkg%d" % i, "1.%d" % (i % 3), "ADV-%d" % (i // 2), ["CVE-2024-%04d" % (i // 2),
            "CVE-2024-%04d" % (100 + i % 5)] if i % 4 else []) for i in range(30)]

    def draw(self, rng):
        matches = []
        for name, version, vuln_id, cves in rng.sample(self.POOL, rng.randint(0, 14)):
            matches.append(mk(name, version, vuln_id, rng.choice(["Critical", "High", "Medium", "Low", "Unknown"]),
                              [(c, rng.random() < 0.25) for c in cves], rng.random() < 0.2,
                              rng.choice(["", "", "2.0", "3.1"]), rng.choice(["", "/a"]), "python"))
        return matches

    def test_fleet_equals_a_rebuild_after_any_sequence(self):
        for seed in (11, 12, 13):
            self.reset()
            rng = random.Random(seed)
            model = {}
            for step in range(45):
                server_id = rng.randint(1, 6)
                action = rng.choice(["save", "save", "save", "again", "forget"])
                if action == "forget":
                    self.assertEqual(vuln_db.forget_server(server_id), server_id in model)
                    model.pop(server_id, None)
                else:
                    matches = model[server_id] if action == "again" and server_id in model else self.draw(rng)
                    save(server_id, matches, matched_at="step-%d" % step)
                    model[server_id] = matches
                if self.raw().execute("SELECT COUNT(*) FROM sqlite_master WHERE name = 'fleet_cves'").fetchone()[0]:
                    self.assertEqual(self.fleet(), self.fleet_by_hand(), (seed, step, action))
            for server_id, matches in model.items():
                self.assertEqual(self.server_cve_rows(server_id), self.expected_server_cves(matches), (seed, server_id))
            incremental = self.fleet()
            self.assertEqual(vuln_db.rebuild_fleet(), len(incremental))
            self.assertEqual(self.fleet(), incremental, seed)
            self.assertEqual(self.raw().execute("SELECT COUNT(*) FROM servers").fetchone()[0], len(model))

    def test_rebuild_repairs_a_damaged_fleet(self):
        for server_id, matches in sample_matches().items():
            save(server_id, matches)
        good = self.fleet()
        conn = sqlite3.connect(store.FINDINGS_DB)
        conn.execute("DELETE FROM fleet_cves WHERE cve = 'CVE-2024-0001'")
        conn.execute("UPDATE fleet_cves SET servers = 99")
        conn.execute("INSERT INTO fleet_cves VALUES ('CVE-9999-0000', 4, 1, 5, 5)")
        conn.commit()
        conn.close()
        self.assertEqual(vuln_db.rebuild_fleet(), len(good))
        self.assertEqual(self.fleet(), good)


# ---------------------------------------------------------------- reads
class ReadTests(StoreTestCase):
    def setUp(self):
        super().setUp()
        rng = random.Random(3)
        builds = ["2026-10-0%dT00:00:00Z" % n for n in range(1, 8)]
        self.model = {}
        for server_id in range(1, 61):
            matches = []
            for _n in range(rng.randint(3, 25)):
                # Everything about a match except which servers have it is fixed by its key, as in real data
                # (the shared tables keep one row per package, version and advisory).
                n, advisory = rng.randint(0, 5), rng.randint(0, 39)
                ids = [2 * advisory + 1, 2 * advisory + 2]
                matches.append(mk("pkg%d" % n, str(n % 2), "ALAS-%d" % advisory,
                                  ["Critical", "High", "Medium", "Low", "Unknown"][(advisory + n) % 5],
                                  [("CVE-2024-%04d" % i, i % 7 == 0) for i in ids], advisory % 9 == 0,
                                  "1.5" if (advisory + n) % 3 else "", "", "rpm"))
            matches.append(mk("onlyadv", "1", "GHSA-xxxx-yyyy-%04d" % (server_id % 3), "Medium", [], False, "2", "", "deb"))
            host = "web-%02d" % server_id if server_id % 4 else "db_%02d" % server_id
            save(server_id, matches, db_built=builds[server_id % 7], hostname=host, group_name="Team %d" % (server_id % 3),
                 environment="Env %s" % ("A" if server_id % 2 else "B"), matched_at="2026-11-%02dT00:00:00Z" % (server_id % 28 + 1))
            self.model[server_id] = matches

    def test_known_servers(self):
        rows = vuln_db.known_servers()
        self.assertEqual([r["server_id"] for r in rows], list(range(1, 61)))
        self.assertEqual(sorted(rows[0]), ["db_built", "hostname", "inventory_at", "matched_at", "scan_id", "server_id"])

    def test_fleet_summary(self):
        summary = vuln_db.fleet_summary()
        rows = self.raw().execute("SELECT sev_rank, kev FROM fleet_cves").fetchall()
        self.assertEqual(summary["servers"], 60)
        self.assertEqual(summary["servers_exploited"], self.raw().execute("SELECT COUNT(*) FROM servers WHERE kev > 0").fetchone()[0])
        self.assertEqual(summary["vulns"], len(rows))
        self.assertEqual(summary["exploited"], sum(1 for _r, k in rows if k))
        self.assertEqual(summary["critical"], sum(1 for r, _k in rows if r == 4))
        self.assertEqual(summary["high"], sum(1 for r, _k in rows if r == 3))
        self.assertEqual(summary["oldest_matched"], "2026-11-01T00:00:00Z")
        self.assertEqual(summary["newest_matched"], "2026-11-28T00:00:00Z")
        self.assertEqual(summary["db_builts"], ["2026-10-0%dT00:00:00Z" % n for n in (7, 6, 5, 4, 3)])
        self.assertEqual(sorted(summary), ["critical", "db_builts", "exploited", "high", "newest_matched",
                                           "oldest_matched", "servers", "servers_exploited", "vulns"])

    def test_fleet_summary_of_an_empty_store(self):
        for server_id in range(1, 61):
            vuln_db.forget_server(server_id)
        summary = vuln_db.fleet_summary()
        self.assertEqual((summary["servers"], summary["vulns"], summary["db_builts"], summary["oldest_matched"]),
                         (0, 0, [], None))

    def test_fleet_cves_order_and_shape(self):
        page = vuln_db.fleet_cves(page_size=100)
        self.assertEqual(page["total"], len(self.fleet()))
        self.assertEqual(sorted(page["rows"][0]), ["cve", "fixable", "kev", "servers", "sev_rank", "severity"])
        keys = [(not r["kev"], -r["sev_rank"], -r["servers"], r["cve"]) for r in page["rows"]]
        self.assertEqual(keys, sorted(keys))
        row = page["rows"][0]
        self.assertEqual(row["severity"], vuln_match.SEVERITIES[4 - row["sev_rank"]])
        self.assertIsInstance(row["kev"], bool)
        # The order is the whole table's, not the page's.
        everything = vuln_db.fleet_cves(page_size=100)["rows"]
        self.assertEqual(everything[:10], vuln_db.fleet_cves(page_size=10)["rows"])

    def test_fleet_cves_paging(self):
        total = len(self.fleet())
        first = vuln_db.fleet_cves(page=1, page_size=10)
        self.assertEqual((first["total"], first["page"], first["page_size"], first["pages"]), (total, 1, 10, -(-total // 10)))
        self.assertEqual(len(first["rows"]), 10)
        last = vuln_db.fleet_cves(page=999, page_size=10)
        self.assertEqual(last["page"], first["pages"])
        self.assertEqual(len(last["rows"]), total - 10 * (first["pages"] - 1))
        self.assertEqual(vuln_db.fleet_cves(page_size=1)["page_size"], 10)  # clamped
        self.assertEqual(vuln_db.fleet_cves(page_size=5000)["page_size"], 100)
        self.assertEqual(vuln_db.fleet_cves(page="x", page_size="y")["page"], 1)
        seen = []
        for number in range(1, first["pages"] + 1):
            seen += [r["cve"] for r in vuln_db.fleet_cves(page=number, page_size=10)["rows"]]
        self.assertEqual(len(seen), len(set(seen)))
        self.assertEqual(len(seen), total)

    def test_fleet_cves_filters(self):
        every = vuln_db.fleet_cves(page_size=100)["rows"]
        crit = vuln_db.fleet_cves(severity="critical", page_size=100)
        self.assertEqual(crit["total"], sum(1 for r in every if r["sev_rank"] == 4))
        self.assertTrue(all(r["severity"] == "Critical" for r in crit["rows"]))
        self.assertEqual(vuln_db.fleet_cves(severity="HIGH")["total"], sum(1 for r in every if r["sev_rank"] == 3))
        self.assertEqual(vuln_db.fleet_cves(severity="nonsense")["total"], len(every))
        exploited = vuln_db.fleet_cves(kev_only=True, page_size=100)
        self.assertEqual(exploited["total"], sum(1 for r in every if r["kev"]))
        self.assertTrue(all(r["kev"] for r in exploited["rows"]))
        both = vuln_db.fleet_cves(severity="low", kev_only=True, page_size=100)
        self.assertEqual(both["total"], sum(1 for r in every if r["kev"] and r["sev_rank"] == 1))

    def test_fleet_cves_search_by_cve(self):
        every = vuln_db.fleet_cves(page_size=100)["rows"]
        for term in ("CVE-2024-001", "cve-2024-001", " CVE-2024-001 "):
            page = vuln_db.fleet_cves(search=term, page_size=100)
            self.assertEqual({r["cve"] for r in page["rows"]}, {r["cve"] for r in every if "CVE-2024-001" in r["cve"]}, term)
        self.assertEqual({r["cve"] for r in vuln_db.fleet_cves(search="2024-0042")["rows"]}, {"CVE-2024-0042"})
        self.assertEqual(vuln_db.fleet_cves(search="CVE-2099")["total"], 0)
        self.assertEqual(vuln_db.fleet_cves(search="%")["total"], 0)  # a wildcard is a literal
        self.assertEqual(vuln_db.fleet_cves(search="_")["total"], 0)
        with_filter = vuln_db.fleet_cves(search="CVE-2024-00", severity="critical", page_size=100)
        self.assertTrue(all(r["severity"] == "Critical" and "CVE-2024-00" in r["cve"] for r in with_filter["rows"]))

    def test_fleet_cves_search_by_advisory(self):
        conn = self.raw()
        expected = {row[0] for row in conn.execute(
            "SELECT COALESCE(c.cve, m.vuln_id) FROM matches m LEFT JOIN match_cves c ON c.match_id = m.id "
            "WHERE m.vuln_id = 'ALAS-7'")}
        self.assertTrue(expected)
        for term in ("ALAS-7", "alas-7", "Alas-7"):
            page = vuln_db.fleet_cves(search=term, page_size=100)
            self.assertEqual({r["cve"] for r in page["rows"]}, expected, term)
        # An advisory with no CVE is its own identity.
        page = vuln_db.fleet_cves(search="ghsa-xxxx-yyyy-0001")
        self.assertEqual([r["cve"] for r in page["rows"]], ["GHSA-xxxx-yyyy-0001"])
        # A partial advisory id is a plain substring search on the identity.
        self.assertEqual({r["cve"] for r in vuln_db.fleet_cves(search="GHSA-xxxx")["rows"]},
                         {"GHSA-xxxx-yyyy-000%d" % n for n in range(3)})

    def test_cve_servers(self):
        cve = "CVE-2024-0001"
        holders = sorted((s, 1) for s, ms in self.model.items() if any(c["cve"] == cve for x in ms for c in x["cves"]))
        page = vuln_db.cve_servers(cve, page_size=100)
        self.assertEqual(page["total"], len(holders))
        self.assertEqual(sorted(page["rows"][0]), sorted([
            "server_id", "hostname", "group_name", "environment", "os_name", "pkg_name", "pkg_version",
            "fix_version", "pkg_count", "severity", "sev_rank", "kev", "matched_at", "inventory_at"]))
        keys = [(r["hostname"], r["server_id"]) for r in page["rows"]]
        self.assertEqual(keys, sorted(keys))
        self.assertEqual({r["server_id"] for r in page["rows"]}, {s for s, _m in holders})
        row = page["rows"][0]
        self.assertEqual(row["severity"], vuln_match.SEVERITIES[4 - row["sev_rank"]])
        self.assertEqual(vuln_db.cve_servers(cve.lower(), page_size=100)["total"], len(holders))
        small = vuln_db.cve_servers(cve, page=2, page_size=10)
        self.assertEqual((small["page"], small["page_size"], small["pages"]), (2, 10, -(-len(holders) // 10)))
        self.assertEqual([r["server_id"] for r in small["rows"]], [r["server_id"] for r in page["rows"][10:20]])
        self.assertEqual(vuln_db.cve_servers(cve, page=99, page_size=10)["page"], small["pages"])
        self.assertEqual(vuln_db.cve_servers("CVE-1999-0000")["total"], 0)

    def test_cve_servers_search_and_advisory_identity(self):
        cve = "CVE-2024-0002"
        everyone = vuln_db.cve_servers(cve, page_size=100)
        db_hosts = vuln_db.cve_servers(cve, search="db_", page_size=100)  # the underscore is a literal
        self.assertEqual({r["hostname"] for r in db_hosts["rows"]}, {r["hostname"] for r in everyone["rows"] if r["hostname"].startswith("db_")})
        self.assertEqual(db_hosts["total"], len(db_hosts["rows"]))
        self.assertEqual(vuln_db.cve_servers(cve, search="WEB-0")["total"], sum(1 for r in everyone["rows"] if "web-0" in r["hostname"]))
        advisory = vuln_db.cve_servers("GHSA-xxxx-yyyy-0000", page_size=100)
        self.assertEqual(advisory["total"], sum(1 for s in self.model if s % 3 == 0))
        self.assertEqual(advisory["rows"][0]["pkg_name"], "onlyadv")

    def test_fleet_servers(self):
        page = vuln_db.fleet_servers(page_size=100)
        self.assertEqual(page["total"], 60)
        self.assertEqual(sorted(page["rows"][0]), sorted([
            "server_id", "hostname", "group_name", "environment", "os_name", "packages", "vulns", "vuln_critical",
            "vuln_high", "kev", "inventory_at", "matched_at", "db_built"]))
        keys = [(-r["kev"], -r["vuln_critical"], -r["vuln_high"], r["hostname"], r["server_id"]) for r in page["rows"]]
        self.assertEqual(keys, sorted(keys))
        self.assertEqual(vuln_db.fleet_servers(page_size=10)["rows"], page["rows"][:10])
        self.assertEqual(vuln_db.fleet_servers(page=99, page_size=25)["page"], 3)
        exploited = vuln_db.fleet_servers(kev_only=True, page_size=100)
        self.assertEqual(exploited["total"], sum(1 for r in page["rows"] if r["kev"] > 0))
        self.assertEqual({r["server_id"] for r in vuln_db.fleet_servers(search="team 1", page_size=100)["rows"]},
                         {s for s in range(1, 61) if s % 3 == 1})
        self.assertEqual({r["server_id"] for r in vuln_db.fleet_servers(search="env b", page_size=100)["rows"]},
                         {s for s in range(1, 61) if s % 2 == 0})
        self.assertEqual({r["hostname"][:3] for r in vuln_db.fleet_servers(search="db_", page_size=100)["rows"]}, {"db_"})
        self.assertEqual(vuln_db.fleet_servers(search="nothing like this")["total"], 0)
        self.assertEqual(vuln_db.fleet_servers(search="%")["total"], 0)

    def test_the_existing_reads_are_unchanged(self):
        page = vuln_db.server_cves(5, page_size=100)
        self.assertTrue(page["total"] > 0)
        self.assertEqual({r["cve"] for r in page["rows"]}, {r["cve"] for r in vuln_match.vuln_rows(self.model[5])})
        self.assertTrue(vuln_db.server_packages(5)["total"] > 0)
        stats = vuln_db.stats()
        self.assertEqual(stats["servers"], 60)


if __name__ == "__main__":
    unittest.main()
