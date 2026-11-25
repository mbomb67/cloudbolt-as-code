"""
Checks for the findings store of the Vulnerabilities XUI (vuln_db.py), without
CloudBolt: vuln_store is a stub, the database is a SQLite file in a temp folder.

    python -B -m unittest discover -s extensions/XUI-wqrj0e3p/tests -p "test_vuln_db.py"

The package is loaded under a private name so that another test file's stub of
the same extension cannot get in the way.
"""
import contextlib
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
    def test_new_file_is_version_4(self):
        save(1, sample_matches()[1])
        conn = self.raw()
        self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 4)
        names = {row[0] for row in conn.execute("SELECT name FROM sqlite_master")}
        for name in ("servers", "matches", "match_cves", "server_matches", "server_packages", "server_cves",
                     "fleet_cves", "server_cves_cve", "fleet_cves_order", "matches_vuln", "servers_fleet",
                     "match_cves_cve", "server_matches_match"):
            self.assertIn(name, names)
        columns = {row[1] for row in conn.execute("PRAGMA table_info(servers)")}
        self.assertTrue({"match_hash", "vulns", "vuln_unknown", "vuln_fixable", "owner", "os_build"} <= columns)

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


class V1Builder:
    """Makes a version-1 store (the first release's tables and rows)."""

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


class UpgradeTests(V1Builder, StoreTestCase):
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
                # An old file has no owner or os_build column: an empty page the caller retries after the upgrade.
                self.assertTrue(page["unavailable"])
                self.assertEqual((page["total"], page["rows"]), (0, []))
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
                self.assertEqual(vuln_db.upgrade(), 4)
                conn = self.raw()
                self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 4)
                columns = [row[1] for row in conn.execute("PRAGMA table_info(servers)")]
                self.assertEqual(columns.count("match_hash"), 1)
                self.assertEqual(len(columns), 28)
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
                self.assertEqual(vuln_db.upgrade(), 4)
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
        self.assertEqual(real(loser), 4)

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
        self.assertEqual(vuln_db.schema_version(), 4)
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
        self.assertEqual(vuln_db.upgrade(), 4)  # and the next try succeeds


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
class FleetStore(StoreTestCase):
    """Sixty servers with overlapping findings, and the matches each was saved with in self.model."""

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
                 environment="Env %s" % ("A" if server_id % 2 else "B"), matched_at="2026-11-%02dT00:00:00Z" % (server_id % 28 + 1),
                 inventory_at="2026-10-%02dT12:00:00Z" % (server_id % 20 + 1))
            self.model[server_id] = matches


class ReadTests(FleetStore):
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
                                           "oldest_matched", "servers", "servers_exploited", "vuln_fixable",
                                           "vulns"])
        self.assertEqual(summary["vuln_fixable"], sum(1 for f in self.fleet().values() if f[3] > 0))

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

    def test_fleet_cves_fix_filter(self):
        save(100, [mk("nofix", "1", "ALAS-900", "High", [("CVE-2024-9000", False)], False, "")])  # no fix anywhere
        every = vuln_db.fleet_cves(page_size=100)["rows"]
        fixable = vuln_db.fleet_cves(fix_only=True, page_size=100)
        self.assertTrue(0 < fixable["total"] < len(every))
        self.assertEqual(fixable["rows"], [r for r in every if r["fixable"] > 0])
        self.assertNotIn("CVE-2024-9000", [r["cve"] for r in fixable["rows"]])
        self.assertEqual(vuln_db.fleet_cves(fix_only=False)["total"], len(every))
        both = vuln_db.fleet_cves(severity="critical", kev_only=True, fix_only=True, page_size=100)
        self.assertEqual(both["total"], sum(1 for r in every if r["fixable"] and r["kev"] and r["sev_rank"] == 4))
        searched = vuln_db.fleet_cves(search="CVE-2024-00", fix_only=True, page_size=100)
        self.assertEqual({r["cve"] for r in searched["rows"]},
                         {r["cve"] for r in every if r["fixable"] and "CVE-2024-00" in r["cve"]})

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
            "vuln_high", "kev", "inventory_at", "matched_at", "db_built", "vuln_fixable", "owner", "os_build"]))
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


# ---------------------------------------------------------------- set reads
def page_dicts_agree(test, page, total, rows, page_size):
    test.assertEqual(sorted(page), ["page", "page_size", "pages", "rows", "total", "truncated", "unavailable"])
    test.assertEqual((page["total"], page["rows"], page["page_size"]), (total, rows, page_size))


class SetReadTests(FleetStore):
    """set_servers, set_summary, set_cves and set_cve_servers against a brute-force
    computation from the matches each server was saved with."""

    def expected_identities(self, ids):
        """cve -> (worst rank, exploited, servers in the set, servers with a fix), from the model alone."""
        found = {}
        for server_id in set(ids):
            for row in vuln_match.vuln_rows(self.model.get(server_id, [])):
                found.setdefault(row["cve"], []).append((row["sev_rank"], int(bool(row["kev"])), row["fix_version"]))
        return {cve: (max(r for r, _k, _f in rows), max(k for _r, k, _f in rows), len(rows),
                      sum(1 for _r, _k, f in rows if f)) for cve, rows in found.items()}

    def expected_rows(self, ids, keep=lambda cve, value: True):
        rows = [{"cve": cve, "severity": vuln_match.SEVERITIES[4 - value[0]], "sev_rank": value[0],
                 "kev": bool(value[1]), "servers": value[2], "fixable": value[3]}
                for cve, value in self.expected_identities(ids).items() if keep(cve, value)]
        return sorted(rows, key=lambda r: (not r["kev"], -r["sev_rank"], -r["servers"], r["cve"]))

    def everything(self, function, ids, *args, **kwargs):
        """Every row of a paged read, through its pages."""
        rows, number = [], 1
        while True:
            page = function(ids, *args, page=number, page_size=100, **kwargs)
            rows += page["rows"]
            if number >= page["pages"]:
                return page, rows
            number += 1

    def sets(self):
        rng = random.Random(5)
        picks = [[1], [2, 3, 4], rng.sample(range(1, 61), 10), rng.sample(range(1, 61), 25), list(range(1, 61)),
                 [58, 59, 60, 61, 500], [7, 7, 7]]
        return picks + [rng.sample(range(1, 61), rng.randint(1, 60)) for _ in range(6)]

    # ---- ids
    def test_ids_are_cleaned(self):
        clean = vuln_db._clean_ids
        self.assertEqual(clean([3, 1, 3, 2]), ([1, 2, 3], False))
        self.assertEqual(clean([True, False, 1, 2.0, "3", None, 4, b"5", 2 ** 70, -2 ** 70, 2 ** 63 - 1]),
                         ([1, 4, 2 ** 63 - 1], False))
        self.assertEqual(clean(i for i in (9, 8, 9)), ([8, 9], False))
        self.assertEqual(clean(set([5, 1])), ([1, 5], False))
        for bad in (None, 7, 3.5, object()):
            self.assertEqual(clean(bad), ([], False))
        self.assertEqual(clean("123"), ([], False))
        self.assertEqual(clean(range(vuln_db.MAX_SET + 3)), (list(range(vuln_db.MAX_SET)), True))
        self.assertEqual(clean(range(vuln_db.MAX_SET)), (list(range(vuln_db.MAX_SET)), False))
        with mock.patch.object(vuln_db, "MAX_SET", 4):
            self.assertEqual(clean([9, 3, 7, 1, 5, 3]), ([1, 3, 5, 7], True))

    def test_junk_ids_read_as_nothing_and_never_raise(self):
        for junk in (None, 5, [True, None, "1", 1.5, 2 ** 80], "12", [], (), {}):
            with self.subTest(junk=junk):
                self.assertEqual(vuln_db.set_servers(junk), {})
                self.assertEqual(vuln_db.set_summary(junk)["scanned"], 0)
                for page in (vuln_db.set_cves(junk), vuln_db.set_cve_servers(junk, "CVE-2024-0001")):
                    page_dicts_agree(self, page, 0, [], 25)
                    self.assertEqual((page["pages"], page["unavailable"], page["truncated"]), (1, False, False))
        self.assertEqual(vuln_db.set_servers({3: "x", 4: "y"}), vuln_db.set_servers([3, 4]))  # a mapping is its keys
        self.assertEqual(set(vuln_db.set_servers([True, 3, "4", 4.0])), {3})

    def test_empty_and_unknown_ids(self):
        summary = vuln_db.set_summary([])
        self.assertEqual(summary, {"scanned": 0, "vulns": 0, "critical": 0, "high": 0, "exploited": 0,
                                   "vuln_fixable": 0, "servers_exploited": 0, "oldest_inventory": None, "newest_matched": None,
                                   "db_builts": [], "truncated": False})
        self.assertEqual(vuln_db.set_summary([9001, 9002]), summary)  # no row: scanned 0, not None
        self.assertEqual(vuln_db.set_servers([9001]), {})
        page_dicts_agree(self, vuln_db.set_cves([9001]), 0, [], 25)
        page_dicts_agree(self, vuln_db.set_cve_servers([9001], "CVE-2024-0001"), 0, [], 25)

    # ---- set_servers
    def test_set_servers(self):
        rows = vuln_db.set_servers([5, 6, 999, 5])
        self.assertEqual(sorted(rows), [5, 6])
        self.assertEqual(sorted(rows[5]), sorted(["server_id", "hostname", "os_name", "packages", "vulns",
                                                  "vuln_critical", "vuln_high", "kev", "inventory_at", "matched_at",
                                                  "db_built"]))
        for server_id, row in rows.items():
            whole = vuln_db.server_summary(server_id)
            self.assertEqual(row, {key: whole[key] for key in row})
            self.assertEqual(row["vulns"], len(vuln_match.vuln_rows(self.model[server_id])))
        self.assertEqual(rows[5]["inventory_at"], "2026-10-06T12:00:00Z")
        self.assertEqual(sorted(vuln_db.set_servers(range(1, 61))), list(range(1, 61)))

    # ---- set_summary
    def test_set_summary_equals_a_brute_force_computation(self):
        conn = self.raw()
        for ids in self.sets():
            with self.subTest(ids=ids):
                known = [i for i in set(ids) if i in self.model]
                idents = self.expected_identities(ids)
                rows = [conn.execute("SELECT kev, inventory_at, matched_at, db_built FROM servers WHERE server_id = ?",
                                     (i,)).fetchone() for i in known]
                summary = vuln_db.set_summary(ids)
                self.assertEqual(sorted(summary), ["critical", "db_builts", "exploited", "high", "newest_matched",
                                                   "oldest_inventory", "scanned", "servers_exploited", "truncated",
                                                   "vuln_fixable", "vulns"])
                self.assertEqual(summary["vuln_fixable"], sum(1 for v in idents.values() if v[3] > 0))
                self.assertEqual(summary["scanned"], len(known))
                self.assertEqual(summary["vulns"], len(idents))
                self.assertEqual(summary["critical"], sum(1 for v in idents.values() if v[0] == 4))
                self.assertEqual(summary["high"], sum(1 for v in idents.values() if v[0] == 3))
                self.assertEqual(summary["exploited"], sum(1 for v in idents.values() if v[1]))
                self.assertEqual(summary["servers_exploited"], sum(1 for r in rows if r[0] > 0))
                self.assertEqual(summary["oldest_inventory"], min(r[1] for r in rows) if rows else None)
                self.assertEqual(summary["newest_matched"], max(r[2] for r in rows) if rows else None)
                self.assertEqual(summary["db_builts"], sorted({r[3] for r in rows}, reverse=True)[:5])
                self.assertIs(summary["truncated"], False)

    def test_set_summary_counts_a_shared_identity_once_at_its_worst_severity(self):
        self.reset()
        save(1, [mk("a", "1", "A1", "Low", [("CVE-2024-0900", False)], False, "")], inventory_at="2026-11-05T00:00:00Z")
        save(2, [mk("b", "1", "A2", "Critical", [("CVE-2024-0900", True)], True, "2")], inventory_at="2026-11-02T00:00:00Z")
        save(3, [mk("c", "1", "A3", "High", [("CVE-2024-0901", False)], False, "")])
        both = vuln_db.set_summary([1, 2])
        self.assertEqual((both["scanned"], both["vulns"], both["critical"], both["high"], both["exploited"]),
                         (2, 1, 1, 0, 1))
        self.assertEqual((both["servers_exploited"], both["oldest_inventory"]), (1, "2026-11-02T00:00:00Z"))
        alone = vuln_db.set_summary([1])
        self.assertEqual((alone["vulns"], alone["critical"], alone["exploited"], alone["servers_exploited"]), (1, 0, 0, 0))
        self.assertEqual(vuln_db.set_summary([1, 2, 3])["vulns"], 2)

    def test_set_summary_db_builts_are_distinct_newest_first_and_at_most_five(self):
        ids = list(range(1, 61))
        builts = vuln_db.set_summary(ids)["db_builts"]
        self.assertEqual(builts, ["2026-10-0%dT00:00:00Z" % n for n in (7, 6, 5, 4, 3)])
        small = vuln_db.set_summary([1, 8, 15])  # all the same build (id % 7 == 1)
        self.assertEqual(small["db_builts"], ["2026-10-02T00:00:00Z"])

    # ---- set_cves
    def test_set_cves_equal_a_brute_force_computation(self):
        for ids in self.sets():
            with self.subTest(ids=ids):
                page, rows = self.everything(vuln_db.set_cves, ids)
                self.assertEqual(rows, self.expected_rows(ids))
                self.assertEqual(page["total"], len(rows))
                self.assertEqual(sorted(page["rows"][0]) if rows else None,
                                 ["cve", "fixable", "kev", "servers", "sev_rank", "severity"] if rows else None)
                self.assertIs(page["truncated"], False)
                self.assertIs(page["unavailable"], False)

    def test_servers_and_fixable_count_the_set_not_the_fleet(self):
        fleet = {r["cve"]: r for r in vuln_db.fleet_cves(page_size=100)["rows"]}
        ids = [1, 2, 3]
        rows = {r["cve"]: r for r in self.everything(vuln_db.set_cves, ids)[1]}
        self.assertTrue(any(rows[c]["servers"] < fleet[c]["servers"] for c in rows))
        for cve, row in rows.items():
            self.assertLessEqual(row["servers"], 3)
            self.assertLessEqual(row["fixable"], row["servers"])
        # The whole fleet as the set gives the fleet's own roll-up.
        whole = {r["cve"]: r for r in self.everything(vuln_db.set_cves, range(1, 61))[1]}
        self.assertEqual(whole, fleet)

    def test_set_cves_filters(self):
        ids = list(range(1, 41))
        every = self.expected_rows(ids)
        for severity, rank in (("critical", 4), ("HIGH", 3), ("low", 1), ("Unknown", 0)):
            _page, rows = self.everything(vuln_db.set_cves, ids, severity=severity)
            self.assertEqual(rows, [r for r in every if r["sev_rank"] == rank], severity)
        self.assertEqual(self.everything(vuln_db.set_cves, ids, severity="nonsense")[1], every)
        _page, rows = self.everything(vuln_db.set_cves, ids, kev_only=True)
        self.assertEqual(rows, [r for r in every if r["kev"]])
        self.assertTrue(rows)
        _page, rows = self.everything(vuln_db.set_cves, ids, severity="medium", kev_only=True)
        self.assertEqual(rows, [r for r in every if r["kev"] and r["sev_rank"] == 2])
        # The filters see the set's worst severity and exploited flag, not the fleet's.
        self.reset()
        save(1, [mk("a", "1", "A1", "Low", [("CVE-2024-0900", False)])])
        save(2, [mk("b", "1", "A2", "Critical", [("CVE-2024-0900", True)], True)])
        self.assertEqual(vuln_db.set_cves([1], severity="low")["total"], 1)
        self.assertEqual(vuln_db.set_cves([1], severity="critical")["total"], 0)
        self.assertEqual(vuln_db.set_cves([1], kev_only=True)["total"], 0)
        self.assertEqual(vuln_db.set_cves([1, 2], kev_only=True)["total"], 1)

    def test_set_cves_fix_filter(self):
        self.model[100] = [mk("nofix", "1", "ALAS-900", "High", [("CVE-2024-9000", False)], False, "")]
        save(100, self.model[100])
        ids = list(range(1, 41)) + [100]
        every = self.expected_rows(ids)
        page, rows = self.everything(vuln_db.set_cves, ids, fix_only=True)
        self.assertEqual(rows, [r for r in every if r["fixable"] > 0])
        self.assertTrue(0 < len(rows) < len(every))
        self.assertEqual(page["total"], len(rows))
        _page, rows = self.everything(vuln_db.set_cves, ids, severity="high", kev_only=True, fix_only=True)
        self.assertEqual(rows, [r for r in every if r["fixable"] and r["kev"] and r["sev_rank"] == 3])
        _page, rows = self.everything(vuln_db.set_cves, ids, search="CVE-2024-00", fix_only=True)
        self.assertEqual(rows, [r for r in every if r["fixable"] and "CVE-2024-00" in r["cve"]])
        # The filter sees the set's servers, not the fleet's: only server 2 has a fix for this one.
        self.reset()
        save(1, [mk("a", "1", "A1", "Low", [("CVE-2024-0900", False)], False, "")])
        save(2, [mk("b", "1", "A2", "Low", [("CVE-2024-0900", False)], False, "2")])
        self.assertEqual(vuln_db.set_cves([1], fix_only=True)["total"], 0)
        self.assertEqual(vuln_db.set_cves([1, 2], fix_only=True)["total"], 1)
        self.assertEqual(vuln_db.set_summary([1])["vuln_fixable"], 0)
        self.assertEqual(vuln_db.set_summary([1, 2])["vuln_fixable"], 1)

    def test_set_cves_search_by_cve(self):
        ids = list(range(1, 31))
        for term in ("CVE-2024-001", "cve-2024-001", " CVE-2024-001 ", "2024-001", "0042"):
            _page, rows = self.everything(vuln_db.set_cves, ids, search=term)
            needle = term.strip().lower()
            self.assertEqual(rows, self.expected_rows(ids, lambda cve, _v: needle in cve.lower()), term)
        self.assertEqual(vuln_db.set_cves(ids, search="CVE-2099")["total"], 0)
        self.assertEqual(vuln_db.set_cves(ids, search="%")["total"], 0)  # a wildcard is a literal
        self.assertEqual(vuln_db.set_cves(ids, search="_")["total"], 0)
        _page, rows = self.everything(vuln_db.set_cves, ids, search="CVE-2024-00", severity="critical")
        self.assertEqual(rows, [r for r in self.expected_rows(ids, lambda c, _v: "cve-2024-00" in c.lower())
                                if r["sev_rank"] == 4])

    def advisory_cves(self, advisory):
        found = set()
        for matches in self.model.values():
            for m in matches:
                if m["vuln_id"].lower() == advisory.lower():
                    found.update(c["cve"] for c in m["cves"] or [{"cve": m["vuln_id"]}])
        return found

    def test_set_cves_search_by_advisory_is_limited_to_the_set(self):
        for term in ("ALAS-7", "alas-7", "Alas-7"):
            for ids in ([1, 2, 3, 4], list(range(1, 61)), [9]):
                wanted = self.advisory_cves(term)
                self.assertTrue(wanted)
                _page, rows = self.everything(vuln_db.set_cves, ids, search=term)
                self.assertEqual(rows, self.expected_rows(ids, lambda cve, _v: cve in wanted), (term, ids))
        # The advisory's CVEs the set does not have are not in the answer, however many servers outside have them.
        outside = [s for s in range(1, 61) if not any(m["vuln_id"] == "ALAS-7" for m in self.model[s])]
        self.assertTrue(outside)
        self.assertEqual(vuln_db.set_cves(outside[:3], search="ALAS-7")["total"], 0)
        # A pure advisory is its own identity; a partial advisory id is a plain substring.
        _page, rows = self.everything(vuln_db.set_cves, range(1, 61), search="ghsa-xxxx-yyyy-0001")
        self.assertEqual([r["cve"] for r in rows], ["GHSA-xxxx-yyyy-0001"])
        servers = [s for s in range(1, 61) if s % 3 == 1]
        self.assertEqual(rows[0]["servers"], len(servers))
        self.assertEqual(vuln_db.set_cves([1, 2, 3], search="ghsa-xxxx-yyyy-0001")["rows"][0]["servers"], 1)
        self.assertEqual({r["cve"] for r in vuln_db.set_cves(range(1, 61), search="GHSA-xxxx")["rows"]},
                         {"GHSA-xxxx-yyyy-000%d" % n for n in range(3)})

    def test_set_cves_paging(self):
        ids = list(range(1, 61))
        every = self.expected_rows(ids)
        total = len(every)
        first = vuln_db.set_cves(ids, page=1, page_size=10)
        self.assertEqual((first["total"], first["page"], first["page_size"], first["pages"]),
                         (total, 1, 10, -(-total // 10)))
        self.assertEqual(first["rows"], every[:10])
        mid = vuln_db.set_cves(ids, page=3, page_size=10)
        self.assertEqual(mid["rows"], every[20:30])
        last = vuln_db.set_cves(ids, page=999, page_size=10)
        self.assertEqual(last["page"], first["pages"])
        self.assertEqual(last["rows"], every[10 * (first["pages"] - 1):])
        self.assertEqual(vuln_db.set_cves(ids, page_size=1)["page_size"], 10)  # clamped
        self.assertEqual(vuln_db.set_cves(ids, page_size=5000)["page_size"], 100)
        self.assertEqual(vuln_db.set_cves(ids, page="x", page_size="y")["page"], 1)
        self.assertEqual(vuln_db.set_cves(ids, page=0)["page"], 1)
        # A search that matches nothing is one empty page of the set, not "unavailable".
        none = vuln_db.set_cves(ids, search="CVE-2099")
        self.assertEqual((none["total"], none["pages"], none["page"], none["unavailable"]), (0, 1, 1, False))

    # ---- set_cve_servers
    def test_set_cve_servers_equal_the_fleet_rows_of_the_set(self):
        for cve in ("CVE-2024-0001", "CVE-2024-0002", "GHSA-xxxx-yyyy-0000"):
            fleet = vuln_db.cve_servers(cve, page_size=100)["rows"]
            for ids in ([1, 2, 3, 4, 5, 6], [3], list(range(1, 61)), [58, 59, 60, 61]):
                with self.subTest(cve=cve, ids=ids):
                    page = vuln_db.set_cve_servers(ids, cve, page_size=100)
                    expected = [r for r in fleet if r["server_id"] in set(ids)]
                    self.assertEqual(page["rows"], expected)
                    self.assertEqual((page["total"], page["unavailable"], page["truncated"]), (len(expected), False, False))
        page = vuln_db.set_cve_servers([1, 2, 3], "CVE-2024-0001")
        self.assertEqual(sorted(page["rows"][0]), sorted([
            "server_id", "hostname", "group_name", "environment", "os_name", "pkg_name", "pkg_version",
            "fix_version", "pkg_count", "severity", "sev_rank", "kev", "matched_at", "inventory_at"]))
        self.assertEqual(vuln_db.set_cve_servers([1, 2, 3], "cve-2024-0001")["rows"], page["rows"])
        self.assertEqual(vuln_db.set_cve_servers(range(1, 61), "CVE-1999-0000")["total"], 0)

    def test_set_cve_servers_search_and_paging(self):
        ids = list(range(1, 61))
        cve = "CVE-2024-0002"
        everyone = vuln_db.set_cve_servers(ids, cve, page_size=100)
        self.assertGreater(everyone["total"], 10)
        db_hosts = vuln_db.set_cve_servers(ids, cve, search="db_", page_size=100)  # the underscore is a literal
        self.assertEqual(db_hosts["rows"], [r for r in everyone["rows"] if r["hostname"].startswith("db_")])
        self.assertEqual(db_hosts["total"], len(db_hosts["rows"]))
        self.assertEqual(vuln_db.set_cve_servers(ids, cve, search="WEB-0")["total"],
                         sum(1 for r in everyone["rows"] if "web-0" in r["hostname"]))
        self.assertEqual(vuln_db.set_cve_servers(ids, cve, search="%")["total"], 0)
        small = vuln_db.set_cve_servers(ids, cve, page=2, page_size=10)
        self.assertEqual((small["page"], small["pages"]), (2, -(-everyone["total"] // 10)))
        self.assertEqual(small["rows"], everyone["rows"][10:20])
        self.assertEqual(vuln_db.set_cve_servers(ids, cve, page=99, page_size=10)["page"], small["pages"])
        keys = [(r["hostname"], r["server_id"]) for r in everyone["rows"]]
        self.assertEqual(keys, sorted(keys))
        # A server of the set without a servers row is not listed, and not counted either.
        self.assertEqual(vuln_db.set_cve_servers([1000, 1001], cve)["total"], 0)

    # ---- set_cve_info
    def test_set_cve_info_is_the_set_cves_row_of_that_identity(self):
        for ids in ([1, 2, 3, 4, 5, 6], [3], list(range(1, 61)), [58, 59, 60, 61]):
            rows = {r["cve"]: r for r in self.everything(vuln_db.set_cves, ids)[1]}
            for cve in ("CVE-2024-0001", "CVE-2024-0002", "CVE-2024-0003", "GHSA-xxxx-yyyy-0000", "CVE-2024-0042"):
                with self.subTest(cve=cve, ids=ids):
                    self.assertEqual(vuln_db.set_cve_info(ids, cve), rows.get(cve))
        info = vuln_db.set_cve_info(range(1, 61), "cve-2024-0001")      # upper-cased, as set_cve_servers does
        self.assertEqual(sorted(info), ["cve", "fixable", "kev", "servers", "sev_rank", "severity"])
        self.assertEqual(info["cve"], "CVE-2024-0001")
        self.assertIs(type(info["kev"]), bool)

    def test_set_cve_info_is_none_when_absent_or_only_present_outside_the_set(self):
        present = [i for i in range(1, 61) if vuln_db.set_cve_servers([i], "CVE-2024-0001")["total"]]
        self.assertTrue(present and len(present) < 60)
        outside = [i for i in range(1, 61) if i not in present]
        self.assertIsNone(vuln_db.set_cve_info(outside, "CVE-2024-0001"))
        self.assertIsNone(vuln_db.set_cve_info(range(1, 61), "CVE-1999-0000"))
        self.assertIsNone(vuln_db.set_cve_info(present, ""))
        self.assertIsNone(vuln_db.set_cve_info(present, None))
        self.assertEqual(vuln_db.set_cve_info(present, "CVE-2024-0001")["servers"], len(present))

    def test_set_cve_info_junk_ids_and_large_sets(self):
        for junk in (None, 5, [True, None, "1", 1.5, 2 ** 80], "12", [], (), {}):
            self.assertIsNone(vuln_db.set_cve_info(junk, "CVE-2024-0001"))
        ids = list(range(1, 61))
        before = vuln_db.set_cve_info(ids, "CVE-2024-0001")
        with mock.patch.object(vuln_db, "CHUNK", 7):                    # the temp-table branch
            self.assertEqual(vuln_db.set_cve_info(ids, "CVE-2024-0001"), before)
        with mock.patch.object(vuln_db, "MAX_SET", 10):                 # the lowest ids only
            self.assertEqual(vuln_db.set_cve_info(ids, "CVE-2024-0001"),
                             vuln_db.set_cve_info(range(1, 11), "CVE-2024-0001"))

    def test_set_cve_info_uses_the_cve_index_and_no_group_by(self):
        seen = self.statements(lambda: vuln_db.set_cve_info(range(1, 41), "CVE-2024-0001"))
        sql = [s for s in seen if "FROM server_cves" in s]
        self.assertEqual(len(sql), 1)
        self.assertNotIn("GROUP BY", sql[0])
        self.assertNotIn("fleet_cves", sql[0])
        # The trace callback gives the statement with its parameters filled in.
        text = " | ".join(row[3] for row in self.raw().execute("EXPLAIN QUERY PLAN " + sql[0]))
        self.assertIn("server_cves_cve", text)
        self.assertNotIn("SCAN", text)

    def test_set_cve_info_on_a_store_that_is_not_ready(self):
        self.reset()
        self.assertIsNone(vuln_db.set_cve_info([1], "CVE-2024-0001"))
        self.assertFalse(os.path.exists(store.FINDINGS_DB))
        with open(store.FINDINGS_DB, "wb") as handle:
            handle.write(b"this is not a sqlite file" * 100)
        self.assertIsNone(vuln_db.set_cve_info([1], "CVE-2024-0001"))
        with mock.patch.object(vuln_db, "_reader", side_effect=sqlite3.OperationalError("database is locked")):
            self.assertIsNone(vuln_db.set_cve_info([1], "CVE-2024-0001"))

    # ---- large and truncated sets
    def test_a_set_larger_than_one_chunk_goes_through_a_temp_table_with_the_same_answers(self):
        ids = self.sets()[4] + [999, 1000]
        before = (vuln_db.set_summary(ids), vuln_db.set_cves(ids, page_size=100), vuln_db.set_servers(ids),
                  vuln_db.set_cve_servers(ids, "CVE-2024-0001", page_size=100),
                  vuln_db.set_cves(ids, search="ALAS-7"), vuln_db.set_cves(ids, search="CVE-2024-00", kev_only=True))
        for chunk in (1, 7, 59, 60):
            with self.subTest(chunk=chunk), mock.patch.object(vuln_db, "CHUNK", chunk):
                seen = []
                real = vuln_db._id_set

                def spy(conn, items):
                    seen.append(len(items))
                    return real(conn, items)

                with mock.patch.object(vuln_db, "_id_set", spy):
                    after = (vuln_db.set_summary(ids), vuln_db.set_cves(ids, page_size=100), vuln_db.set_servers(ids),
                             vuln_db.set_cve_servers(ids, "CVE-2024-0001", page_size=100),
                             vuln_db.set_cves(ids, search="ALAS-7"),
                             vuln_db.set_cves(ids, search="CVE-2024-00", kev_only=True))
                self.assertEqual(after, before)
                self.assertTrue(all(n > chunk for n in seen), seen)

    def test_the_temp_table_is_dropped_and_the_file_untouched(self):
        ids = list(range(1, 31))
        size, stamp = os.path.getsize(store.FINDINGS_DB), os.stat(store.FINDINGS_DB).st_mtime_ns
        with mock.patch.object(vuln_db, "CHUNK", 5), vuln_db._reader() as conn:
            with vuln_db._id_set(conn, ids) as (member, params):
                self.assertEqual((member, params), ("IN (SELECT id FROM temp.vset_ids)", []))
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM temp.vset_ids").fetchone()[0], 30)
                self.assertEqual(conn.execute(
                    "SELECT COUNT(*) FROM servers WHERE server_id " + member).fetchone()[0], 30)
                with self.assertRaises(sqlite3.OperationalError):
                    conn.execute("INSERT INTO servers(server_id) VALUES (99999)")  # the file itself is read-only
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM sqlite_temp_master").fetchone()[0], 0)
            # The same when the body raises.
            with self.assertRaises(RuntimeError):
                with vuln_db._id_set(conn, ids):
                    raise RuntimeError("boom")
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM sqlite_temp_master").fetchone()[0], 0)
        self.assertEqual((os.path.getsize(store.FINDINGS_DB), os.stat(store.FINDINGS_DB).st_mtime_ns), (size, stamp))
        self.assertEqual(vuln_db.schema_version(), 4)

    def test_set_cves_leaves_no_temp_table_behind(self):
        names = []
        real = vuln_db._reader

        @contextlib.contextmanager
        def watching():
            with real() as conn:
                try:
                    yield conn
                finally:
                    if conn is not None:
                        names.append(conn.execute("SELECT COUNT(*) FROM sqlite_temp_master").fetchone()[0])

        with mock.patch.object(vuln_db, "_reader", watching), mock.patch.object(vuln_db, "CHUNK", 5):
            vuln_db.set_cves(range(1, 40))
            vuln_db.set_summary(range(1, 40))
            vuln_db.set_cve_servers(range(1, 40), "CVE-2024-0001")
            vuln_db.set_servers(range(1, 40))
            self.assertEqual(vuln_db.set_cves(range(1, 40), search="[")["total"], 0)
        self.assertEqual(names, [0] * 5)

    def test_more_than_max_set_uses_the_lowest_ids_and_says_so(self):
        # 500 ids below every real one: the real servers fall outside the first MAX_SET.
        low = list(range(-vuln_db.MAX_SET, 0))
        ids = low + list(range(1, 61))
        summary = vuln_db.set_summary(ids)
        self.assertEqual((summary["truncated"], summary["scanned"]), (True, 0))
        page = vuln_db.set_cves(ids)
        self.assertEqual((page["truncated"], page["total"], page["unavailable"]), (True, 0, False))
        page = vuln_db.set_cve_servers(ids, "CVE-2024-0001")
        self.assertEqual((page["truncated"], page["total"]), (True, 0))
        self.assertEqual(vuln_db.set_servers(ids), {})
        # Exactly MAX_SET ids is not truncated.
        self.assertIs(vuln_db.set_summary(low)["truncated"], False)
        # With a smaller limit the first ones (sorted) are the ones used.
        with mock.patch.object(vuln_db, "MAX_SET", 10):
            ids = list(range(60, 0, -1))
            self.assertEqual(sorted(vuln_db.set_servers(ids)), list(range(1, 11)))
            summary = vuln_db.set_summary(ids)
            self.assertEqual((summary["truncated"], summary["scanned"]), (True, 10))
            self.assertEqual(summary["vulns"], len(self.expected_identities(range(1, 11))))
            page, rows = self.everything(vuln_db.set_cves, ids)
            self.assertTrue(page["truncated"])
            self.assertEqual(rows, self.expected_rows(range(1, 11)))
            page = vuln_db.set_cve_servers(ids, "CVE-2024-0001", page_size=100)
            self.assertTrue(page["truncated"])
            self.assertTrue(all(r["server_id"] <= 10 for r in page["rows"]))
            self.assertFalse(vuln_db.set_cves(range(1, 11))["truncated"])
        full = vuln_db.set_servers(range(1, 601))  # the real limit, 600 ids of which 60 exist
        self.assertEqual(len(full), 60)

    def test_the_real_limit_runs_a_full_set_in_one_chunk(self):
        ids = list(range(1, vuln_db.MAX_SET + 1))
        self.assertLessEqual(len(ids), vuln_db.CHUNK)
        self.assertEqual(vuln_db.set_summary(ids)["scanned"], 60)
        self.assertEqual(vuln_db.set_cves(ids)["total"], len(self.expected_identities(ids)))

    # ---- one aggregation, and the plans
    def statements(self, call):
        seen = []
        real = vuln_db._reader

        @contextlib.contextmanager
        def tracing():
            with real() as conn:
                if conn is not None:
                    conn.set_trace_callback(seen.append)
                yield conn

        with mock.patch.object(vuln_db, "_reader", tracing):
            call()
        return seen

    def test_one_aggregation_per_call(self):
        def groups(call):
            return sum(1 for s in self.statements(call) if "GROUP BY cve" in s)

        ids = list(range(1, 41))
        for chunk in (500, 5):
            with mock.patch.object(vuln_db, "CHUNK", chunk):
                self.assertEqual(groups(lambda: vuln_db.set_summary(ids)), 1)
                self.assertEqual(groups(lambda: vuln_db.set_cves(ids)), 1)
                self.assertEqual(groups(lambda: vuln_db.set_cves(ids, page=3, page_size=10)), 1)
                self.assertEqual(groups(lambda: vuln_db.set_cves(ids, page=99, severity="high", kev_only=True)), 1)
                self.assertEqual(groups(lambda: vuln_db.set_cves(ids, search="ALAS-7")), 1)
                self.assertEqual(groups(lambda: vuln_db.set_cve_servers(ids, "CVE-2024-0001")), 0)
        # Nothing here reads the fleet table or counts the shared tables.
        for call in (lambda: vuln_db.set_summary(ids), lambda: vuln_db.set_cves(ids),
                     lambda: vuln_db.set_cve_servers(ids, "CVE-2024-0001"), lambda: vuln_db.set_servers(ids)):
            for statement in self.statements(call):
                self.assertNotIn("fleet_cves", statement)

    def test_the_set_statements_use_the_indexes_not_scans(self):
        ids = list(range(1, 11))
        plan = lambda sql, params: " | ".join(
            row[3] for row in self.raw().execute("EXPLAIN QUERY PLAN " + sql, params))
        marks = ", ".join("?" for _ in ids)
        agg = ("SELECT MAX(sev_rank), MAX(kev), COUNT(*) FROM server_cves WHERE server_id IN ({}) "
               "AND cve LIKE ? ESCAPE '\\' GROUP BY cve").format(marks)
        text = plan(agg, ids + ["%x%"])
        self.assertIn("SEARCH server_cves USING PRIMARY KEY (server_id=?)", text)
        self.assertNotIn("SCAN server_cves", text)
        join = ("SELECT 1 FROM server_cves sc JOIN servers s ON s.server_id = sc.server_id WHERE sc.cve = ? "
                "AND sc.server_id IN ({})").format(marks)
        text = plan(join, ["CVE-2024-0001"] + ids)
        # Which way round the planner joins is its choice; either is bounded by the set and by one identity.
        self.assertTrue("server_cves_cve (cve=? AND server_id=?)" in text
                        or "SEARCH sc USING PRIMARY KEY (server_id=? AND cve=?)" in text, text)
        self.assertNotIn("SCAN", text)
        # The statements the functions really run are the ones above.
        sql = [s for s in self.statements(lambda: vuln_db.set_cve_servers(ids, "CVE-2024-0001"))
               if s.startswith("SELECT s.server_id")]
        self.assertTrue(sql)
        self.assertNotIn("SCAN", plan(sql[0].split(" ORDER BY")[0], []))

    # ---- stores that are not ready
    def test_a_missing_store_reads_empty(self):
        self.reset()
        self.assertEqual(vuln_db.set_servers([1, 2]), {})
        self.assertIsNone(vuln_db.set_summary([1, 2]))
        for page in (vuln_db.set_cves([1, 2]), vuln_db.set_cve_servers([1, 2], "CVE-2024-0001")):
            page_dicts_agree(self, page, 0, [], 25)
            self.assertEqual((page["pages"], page["unavailable"]), (1, False))
        self.assertFalse(os.path.exists(store.FINDINGS_DB))

    def test_a_store_that_is_not_a_database_reads_empty_and_flagged_unavailable(self):
        self.reset()
        with open(store.FINDINGS_DB, "wb") as handle:
            handle.write(b"this is not a sqlite file" * 100)
        self.assertEqual(vuln_db.set_servers([1]), {})
        self.assertIsNone(vuln_db.set_summary([1]))
        self.assertTrue(vuln_db.set_cves([1])["unavailable"])
        self.assertTrue(vuln_db.set_cve_servers([1], "CVE-2024-0001")["unavailable"])

    def test_a_busy_store_reads_empty_and_flagged_unavailable(self):
        error = sqlite3.OperationalError("database is locked")
        with mock.patch.object(vuln_db, "_reader", side_effect=error):
            self.assertEqual(vuln_db.set_servers([1]), {})
            self.assertIsNone(vuln_db.set_summary([1]))
            self.assertTrue(vuln_db.set_cves([1])["unavailable"])
            self.assertTrue(vuln_db.set_cve_servers([1], "CVE-2024-0001")["unavailable"])


class ReadableTests(V1Builder, StoreTestCase):
    def test_no_file_is_none_and_nothing_is_created(self):
        self.assertIsNone(vuln_db.readable())
        self.assertFalse(os.path.exists(store.FINDINGS_DB))

    def test_a_current_store_is_readable_even_when_empty(self):
        vuln_db.upgrade()
        self.assertIs(vuln_db.readable(), True)
        save(1, sample_matches()[1])
        self.assertIs(vuln_db.readable(), True)

    def test_a_version_1_store_is_not_readable_and_is_not_upgraded(self):
        for with_added in (True, False):
            with self.subTest(with_added=with_added):
                self.reset()
                self.populated_v1(with_added)
                self.assertIs(vuln_db.readable(), False)
                self.assertEqual(vuln_db.schema_version(), 1)

    def test_a_file_that_is_not_a_database_is_not_readable(self):
        with open(store.FINDINGS_DB, "wb") as handle:
            handle.write(b"this is not a sqlite file" * 100)
        self.assertIs(vuln_db.readable(), False)

    def test_a_busy_store_is_not_readable_and_nothing_raises(self):
        vuln_db.upgrade()
        with mock.patch.object(vuln_db, "_reader", side_effect=sqlite3.OperationalError("database is locked")):
            self.assertIs(vuln_db.readable(), False)
        with mock.patch.object(vuln_db, "_reader", side_effect=RuntimeError("boom")):
            self.assertIs(vuln_db.readable(), False)


class SetReadsOnAVersionOneStoreTests(V1Builder, StoreTestCase):
    def test_a_version_1_store_is_empty_and_is_not_upgraded(self):
        for with_added in (True, False):
            with self.subTest(with_added=with_added):
                self.reset()
                self.populated_v1(with_added)
                self.assertIsNone(vuln_db.set_summary([1, 2, 3]))
                for page in (vuln_db.set_cves([1, 2, 3]), vuln_db.set_cves([1, 2, 3], search="ALAS-1"),
                             vuln_db.set_cves(range(1, 40), search="CVE-2024"),
                             vuln_db.set_cve_servers([1, 2, 3], "CVE-2024-0001")):
                    page_dicts_agree(self, page, 0, [], 25)
                    self.assertEqual((page["pages"], page["unavailable"]), (1, False))
                servers = vuln_db.set_servers([1, 2, 3])
                self.assertEqual(sorted(servers) if with_added else servers, [1, 2, 3] if with_added else {})
                self.assertEqual(vuln_db.schema_version(), 1)  # a reader never upgrades
                self.assertNotIn("server_cves", {r[0] for r in self.raw().execute("SELECT name FROM sqlite_master")})

    def test_once_upgraded_the_same_calls_answer(self):
        data = self.populated_v1(True)
        vuln_db.upgrade()
        summary = vuln_db.set_summary([1, 2, 3])
        self.assertEqual(summary["scanned"], 3)
        expected = {r["cve"] for m in data.values() for r in vuln_match.vuln_rows(m)}
        self.assertEqual(summary["vulns"], len(expected))
        self.assertEqual(vuln_db.set_cves([1, 2, 3], page_size=100)["total"], len(expected))
        self.assertEqual(vuln_db.set_cve_servers([1, 2, 3], "CVE-2024-0001")["total"], 3)


class FixableTests(V1Builder, StoreTestCase):
    """vuln_fixable: the vulnerabilities of a server that have a fix on at least one match, and the filter."""

    def mixed(self):
        return [
            # One vulnerability on two packages, only the later-sorting one with a fix: counts once.
            mk("aaa", "1", "ALAS-1", "High", [("CVE-2024-0100", False)], False, ""),
            mk("zzz", "1", "ALAS-1", "High", [("CVE-2024-0100", False)], False, "2.0"),
            # A vulnerability that no package has a fix for: does not count.
            mk("bbb", "1", "ALAS-2", "Critical", [("CVE-2024-0101", True)], True, ""),
            # An advisory with no CVE, with a fix; and a plain fixed one.
            mk("ccc", "1", "GHSA-aaaa-bbbb", "Medium", [], False, "1.1", "", "deb"),
            mk("ddd", "1", "ALAS-4", "Low", [("CVE-2024-0102", False)], False, "9"),
        ]

    def test_computed_on_save_in_the_unit_of_vulns(self):
        row = save(1, self.mixed())
        self.assertEqual((row["vulns"], row["vuln_fixable"]), (4, 3))
        self.assertEqual(vuln_db.server_summary(1)["vuln_fixable"], 3)
        stored = self.raw().execute("SELECT vulns, vuln_fixable FROM servers WHERE server_id = 1").fetchone()
        self.assertEqual(stored, (4, 3))
        nofix = save(2, [mk("a", "1", "A1", "High", [("CVE-2024-0200", False)]),
                         mk("b", "1", "A2", "Low", [("CVE-2024-0201", False)], False, "")])
        self.assertEqual((nofix["vulns"], nofix["vuln_fixable"]), (2, 0))
        self.assertEqual(save(3, [])["vuln_fixable"], 0)

    def test_follows_a_resave_and_the_fast_path(self):
        save(1, self.mixed())
        self.assertEqual(save(1, self.mixed()[2:3])["vuln_fixable"], 0)  # the fixed ones are gone
        self.assertEqual(save(1, self.mixed())["vuln_fixable"], 3)
        again = save(1, self.mixed(), packages=999)
        self.assertTrue(again["unchanged"])
        self.assertEqual((again["vuln_fixable"], again["packages"]), (3, 999))
        # A fix that arrives for the vulnerability that had none changes the match hash and the count.
        fixed = self.mixed()
        fixed[2] = mk("bbb", "1", "ALAS-2", "Critical", [("CVE-2024-0101", True)], True, "3")
        self.assertEqual(save(1, fixed)["vuln_fixable"], 4)

    def test_windows_rows_all_have_a_fix(self):
        matches = [mk("KB1", "1", "CVE-2024-0300", "High", [("CVE-2024-0300", False)], False, "10.0.1", "", "windows"),
                   mk("KB2", "1", "CVE-2024-0301", "Low", [("CVE-2024-0301", False)], False, "10.0.2", "", "windows")]
        row = save(1, matches, os_name="Windows Server 2022")
        self.assertEqual((row["vulns"], row["vuln_fixable"]), (2, 2))
        self.assertEqual(vuln_db.server_cves(1, fix_only=True)["total"], 2)

    def test_upgrade_from_version_2_backfills_from_the_stored_findings(self):
        if sqlite3.sqlite_version_info < (3, 35):
            self.skipTest("ALTER TABLE .. DROP COLUMN needs SQLite 3.35")
        for server_id, matches in sample_matches().items():
            save(server_id, matches)
        save(4, self.mixed())
        expected = {i: vuln_db.server_summary(i)["vuln_fixable"] for i in (1, 2, 3, 4)}
        self.assertEqual(expected[4], 3)
        # Make the file what version 2 left: no column, the representative's fix only, a stale fleet.
        conn = self.raw()
        conn.isolation_level = None
        conn.execute("ALTER TABLE servers DROP COLUMN vuln_fixable")
        conn.execute("UPDATE server_cves SET fix_version = '' WHERE server_id = 4 AND cve = 'CVE-2024-0100'")
        conn.execute("UPDATE fleet_cves SET fixable = 0")
        conn.execute("PRAGMA user_version = 2")
        self.assertEqual(vuln_db.schema_version(), 2)
        self.assertIs(vuln_db.readable(), False)  # a reader waits for the writer's upgrade
        self.assertIsNone(vuln_db.server_summary(1))  # why the server panel route upgrades before it reads
        self.assertEqual(vuln_db.upgrade(), 4)
        self.assertEqual({i: vuln_db.server_summary(i)["vuln_fixable"] for i in (1, 2, 3, 4)}, expected)
        self.assertEqual(self.fleet(), self.fleet_by_hand())
        self.assertEqual(self.fleet()["CVE-2024-0100"][-1], 1)
        self.assertEqual(vuln_db.upgrade(), 4)  # idempotent
        self.assertEqual({i: vuln_db.server_summary(i)["vuln_fixable"] for i in (1, 2, 3, 4)}, expected)
        self.assertIs(vuln_db.readable(), True)

    def test_a_version_1_store_gets_the_count_too(self):
        for with_added in (True, False):
            with self.subTest(with_added=with_added):
                self.reset()
                data = self.populated_v1(with_added)
                vuln_db.upgrade()
                for server_id, matches in data.items():
                    want = sum(1 for r in vuln_match.vuln_rows(matches) if r["fix_version"])
                    self.assertEqual(vuln_db.server_summary(server_id)["vuln_fixable"], want, server_id)

    def test_the_findings_filter(self):
        save(1, self.mixed())
        every = vuln_db.server_cves(1, page_size=100)
        self.assertEqual(every["total"], 5)  # a row per CVE and package
        fixed = vuln_db.server_cves(1, fix_only=True, page_size=100)
        self.assertEqual(fixed["total"], 3)
        self.assertEqual(sorted(r["pkg_name"] for r in fixed["rows"]), ["ccc", "ddd", "zzz"])
        self.assertTrue(all(r["fix_version"] for r in fixed["rows"]))
        self.assertEqual(vuln_db.server_cves(1, fix_only=False)["total"], 5)
        # With the other filters, and the total is the filtered one.
        self.assertEqual(vuln_db.server_cves(1, severity="high", fix_only=True)["total"], 1)
        self.assertEqual(vuln_db.server_cves(1, severity="critical", fix_only=True)["total"], 0)
        self.assertEqual(vuln_db.server_cves(1, kev_only=True, fix_only=True)["total"], 0)
        self.assertEqual(vuln_db.server_cves(1, kev_only=True)["total"], 1)
        self.assertEqual(vuln_db.server_cves(1, search="CVE-2024-010", fix_only=True)["total"], 2)
        self.assertEqual(vuln_db.server_cves(1, search="ALAS-2", fix_only=True)["total"], 0)
        self.assertEqual(vuln_db.server_cves(1, search="ALAS-2")["total"], 1)
        # The package list takes the same flag.
        packages = vuln_db.server_packages(1, fix_only=True, page_size=100)
        self.assertEqual(sorted(r["pkg_name"] for r in packages["rows"]), ["ccc", "ddd", "zzz"])
        self.assertEqual(vuln_db.server_packages(1, severity="low", fix_only=True)["total"], 1)
        self.assertEqual(vuln_db.server_packages(1, fix_only=True, search="ALAS-2")["total"], 0)

    def test_a_fix_on_a_lower_advisory_of_the_same_package_still_counts_and_lists(self):
        # Package p: advisory A is Critical with no fix, advisory B is Low with a fix, for one CVE.
        both = [mk("p", "1", "ALAS-A", "Critical", [("CVE-2024-0500", True)], True, ""),
                mk("p", "1", "ALAS-B", "Low", [("CVE-2024-0500", False)], False, "2"),
                mk("q", "1", "ALAS-C", "High", [("CVE-2024-0501", False)], False, "")]
        row = save(1, both)
        self.assertEqual((row["vulns"], row["vuln_fixable"]), (2, 1))
        listed = vuln_db.server_cves(1, fix_only=True, page_size=100)
        self.assertEqual([(r["cve"], r["pkg_name"], r["severity"], r["fix_version"]) for r in listed["rows"]],
                         [("CVE-2024-0500", "p", "Critical", "2")])  # the worst row, with the package's fix
        self.assertEqual(listed["total"], 1)
        self.assertEqual(vuln_db.server_cves(1, page_size=100)["total"], 2)
        for kwargs, total in (({"kev_only": True}, 1), ({"severity": "critical"}, 1), ({"severity": "low"}, 0),
                              ({"search": "0500"}, 1), ({"search": "ALAS-A"}, 1), ({"search": "0501"}, 0),
                              ({"search": "q"}, 0), ({"kev_only": True, "severity": "critical", "search": "p"}, 1)):
            self.assertEqual(vuln_db.server_cves(1, fix_only=True, **kwargs)["total"], total, kwargs)

    def test_the_backfill_and_a_save_pick_the_same_representative(self):
        for with_added in (True,):
            self.reset()
            conn = self.make_v1(with_added)
            data = {1: [mk("bbb", "1", "ALAS-1", "High", [("CVE-2024-0600", False)], False, "10"),
                        mk("aaa", "1", "ALAS-1", "High", [("CVE-2024-0600", False)], False, "9"),
                        mk("ccc", "1", "ALAS-2", "Critical", [("CVE-2024-0601", True)], True, ""),
                        mk("ddd", "1", "ALAS-3", "Low", [("CVE-2024-0601", False)], False, "4")]}
            self.v1_save(conn, 1, data[1], with_added)
            conn.close()
            vuln_db.upgrade()
            self.assertEqual(self.server_cve_rows(1), self.expected_server_cves(data[1]))
            self.assertEqual(vuln_db.server_summary(1)["vuln_fixable"], 2)

    def test_the_findings_filter_pages_in_the_database(self):
        matches = [mk("pkg%02d" % n, "1", "ALAS-%d" % n, "High", [("CVE-2024-%04d" % n, False)], False,
                      "2" if n % 2 else "") for n in range(40)]
        save(1, matches)
        page = vuln_db.server_cves(1, fix_only=True, page=2, page_size=10)
        self.assertEqual((page["total"], page["pages"], page["page"], len(page["rows"])), (20, 2, 2, 10))
        self.assertTrue(all(r["fix_version"] for r in page["rows"]))


if __name__ == "__main__":
    unittest.main()
