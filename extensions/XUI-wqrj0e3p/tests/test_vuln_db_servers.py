"""
Checks for the Servers list of the findings store (vuln_db.py), without CloudBolt: filters, sorting, hidden ids,
the filter choices, the version-4 columns and the upgrade of a version-3 file. vuln_store is a stub, the database
is a SQLite file in a temp folder.

    python -B -m unittest discover -s extensions/XUI-wqrj0e3p/tests -p "test_vuln_db_servers.py"
"""
import importlib
import os
import shutil
import sqlite3
import sys
import tempfile
import types
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
PKG_DIR = os.path.join(os.path.dirname(HERE), "io_cloudbolt_vulnerabilities")
PKG = "vuln_s_dbsrv_pkg"

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

NONE = "__none__"

# id, hostname, group, environment, os_name, os_build, owner, critical, high, fixable, kev, inventory_at
SERVERS = [
    (1, "Web-01", "Alpha", "Prod", "Amazon Linux 2023", "AL2023", "Ann Lee", 2, 1, 1, 0, "2026-10-05T00:00:00Z"),
    (2, "web-02", "Alpha", "Dev", "Windows Server 2022 build 20348", "Windows Server 2022", "", 0, 3, 0, 1,
     "2026-10-01T00:00:00Z"),
    (3, "db-01", "Beta", "Prod", "Red Hat Enterprise Linux 9.4", "RHEL 9", "bob", 2, 1, 2, 0, "2026-10-09T00:00:00Z"),
    (4, "db-02", "Beta", "Prod", "Red Hat Enterprise Linux 9.4", "RHEL 9", "Ann Lee", 5, 0, 0, 1,
     "2026-10-02T00:00:00Z"),
    (5, "app_01", "Gamma", "Dev", "Ubuntu 22.04", "", "Carl Voss", 0, 0, 0, 0, "2026-10-07T00:00:00Z"),
    (6, "app-02", "Gamma", "Dev", "Ubuntu 22.04", "", "", 0, 0, 4, 0, "2026-10-03T00:00:00Z"),
    (7, "Cache-01", "Alpha", "Prod", "Amazon Linux 2023", "AL2023", "bob", 2, 1, 1, 0, "2026-10-06T00:00:00Z"),
]
DEFAULT_ORDER = [4, 2, 7, 1, 3, 6, 5]  # exploited, critical, high, then hostname (binary: C, W, d) and id


def default_order(rows):
    return sorted(rows, key=lambda r: (-r["kev"], -r["vuln_critical"], -r["vuln_high"], r["hostname"], r["server_id"]))


class ServersStore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="vuln-db-servers-test-")
        store.ROOT = self.tmp
        store.FINDINGS_DB = os.path.join(self.tmp, "findings.db")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def raw(self):
        conn = sqlite3.connect(store.FINDINGS_DB)
        self.addCleanup(conn.close)
        return conn

    def seed(self, servers=SERVERS):
        vuln_db.upgrade()
        conn = self.raw()
        for (sid, host, group, env, os_name, build, owner, crit, high, fix, kev, inv) in servers:
            conn.execute(
                "INSERT INTO servers(server_id, hostname, group_name, environment, os_name, os_build, owner, "
                "packages, vulns, vuln_critical, vuln_high, vuln_fixable, kev, inventory_at, matched_at, db_built) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 10, ?, ?, ?, ?, ?, ?, '2026-11-01T00:00:00Z', 'b')",
                (sid, host, group, env, os_name, build, owner, crit + high, crit, high, fix, kev, inv))
        conn.commit()

    def ids(self, **kwargs):
        kwargs.setdefault("page_size", 100)
        return [r["server_id"] for r in vuln_db.fleet_servers(**kwargs)["rows"]]


class FilterTests(ServersStore):
    def setUp(self):
        super().setUp()
        self.seed()

    def test_default_order_is_unchanged(self):
        page = vuln_db.fleet_servers(page_size=100)
        self.assertEqual([r["server_id"] for r in page["rows"]], DEFAULT_ORDER)
        self.assertEqual([r["server_id"] for r in page["rows"]], [r["server_id"] for r in default_order(page["rows"])])
        self.assertEqual(sorted(page["rows"][0]), sorted([
            "server_id", "hostname", "group_name", "environment", "os_name", "packages", "vulns", "vuln_critical",
            "vuln_high", "kev", "inventory_at", "matched_at", "db_built", "vuln_fixable", "owner", "os_build"]))
        self.assertEqual((page["total"], page["unavailable"]), (7, False))

    def test_each_filter_alone(self):
        self.assertEqual(sorted(self.ids(group="Alpha")), [1, 2, 7])
        self.assertEqual(sorted(self.ids(environment="Dev")), [2, 5, 6])
        self.assertEqual(sorted(self.ids(os_build="RHEL 9")), [3, 4])
        self.assertEqual(sorted(self.ids(owner="bob")), [3, 7])
        self.assertEqual(sorted(self.ids(kev_only=True)), [2, 4])
        self.assertEqual(sorted(self.ids(fix_only=True)), [1, 3, 6, 7])

    def test_filters_are_exact_and_case_sensitive(self):
        self.assertEqual(self.ids(group="Alph"), [])
        self.assertEqual(self.ids(group="alpha"), [])
        self.assertEqual(self.ids(owner="Ann"), [])
        self.assertEqual(sorted(self.ids(owner="Ann Lee")), [1, 4])

    def test_none_value_selects_the_servers_without_one(self):
        self.assertEqual(sorted(self.ids(owner=NONE)), [2, 6])
        self.assertEqual(sorted(self.ids(os_build=NONE)), [5, 6])
        self.assertEqual(vuln_db.NONE_VALUE, NONE)
        # For a group or an environment the text is just a value that matches nothing.
        self.assertEqual(self.ids(group=NONE), [])
        self.assertEqual(self.ids(environment=NONE), [])

    def test_filters_combine_with_and_and_with_search_kev_and_fix(self):
        self.assertEqual(sorted(self.ids(group="Alpha", environment="Prod")), [1, 7])
        self.assertEqual(sorted(self.ids(group="Alpha", environment="Prod", owner="bob")), [7])
        self.assertEqual(sorted(self.ids(group="Alpha", os_build="AL2023", owner="Ann Lee")), [1])
        self.assertEqual(self.ids(group="Alpha", environment="Dev", owner=NONE), [2])
        self.assertEqual(self.ids(group="Alpha", environment="Dev", os_build="AL2023"), [])
        self.assertEqual(self.ids(group="Alpha", kev_only=True), [2])
        self.assertEqual(sorted(self.ids(group="Alpha", fix_only=True)), [1, 7])
        self.assertEqual(self.ids(group="Beta", kev_only=True, fix_only=True), [])
        self.assertEqual(sorted(self.ids(group="Beta", fix_only=True)), [3])
        self.assertEqual(self.ids(group="Alpha", search="cache"), [7])
        self.assertEqual(self.ids(owner="bob", search="db"), [3])
        self.assertEqual(self.ids(group="Beta", search="web"), [])

    def test_search_matches_hostname_group_environment_os_os_build_and_owner(self):
        self.assertEqual(sorted(self.ids(search="web")), [1, 2])
        self.assertEqual(sorted(self.ids(search="gamma")), [5, 6])
        self.assertEqual(sorted(self.ids(search="PROD")), [1, 3, 4, 7])
        self.assertEqual(sorted(self.ids(search="ubuntu")), [5, 6])            # os_name
        self.assertEqual(sorted(self.ids(search="build 20348")), [2])          # os_name
        self.assertEqual(sorted(self.ids(search="rhel 9")), [3, 4])            # os_build only
        self.assertEqual(sorted(self.ids(search="al2023")), [1, 7])            # os_build only
        self.assertEqual(sorted(self.ids(search="carl")), [5])                 # owner
        self.assertEqual(sorted(self.ids(search="ann lee")), [1, 4])

    def test_search_wildcards_are_literal(self):
        self.assertEqual(self.ids(search="app_01"), [5])
        self.assertEqual(self.ids(search="app_0"), [5])          # the underscore matches itself only: app-02 is out
        self.assertEqual(self.ids(search="%"), [])
        self.assertEqual(self.ids(search="_"), [5])

    def test_a_filter_value_with_quotes_or_wildcards_matches_literally_and_is_harmless(self):
        for value in ("Alpha' OR '1'='1", "%", "_", "Alph%", "Alpha; DROP TABLE servers", "\\"):
            for name in ("group", "environment", "os_build", "owner"):
                self.assertEqual(self.ids(**{name: value}), [], (name, value))
        self.assertEqual(vuln_db.fleet_servers()["total"], 7)  # the table survived
        conn = self.raw()
        conn.execute("UPDATE servers SET group_name = ? WHERE server_id = 5", ("O'Brien 100%",))
        conn.commit()
        self.assertEqual(self.ids(group="O'Brien 100%"), [5])
        self.assertEqual(self.ids(group="O'Brien 100"), [])

    def test_a_long_filter_value_is_cut_not_an_error(self):
        self.assertEqual(self.ids(group="x" * 5000), [])

    def test_paging_clamps_a_page_past_the_end(self):
        page = vuln_db.fleet_servers(page=99, page_size=10)
        self.assertEqual((page["page"], page["pages"], page["total"], len(page["rows"])), (1, 1, 7, 7))
        self.seed([(100 + n, "bulk-%03d" % n, "G", "E", "", "", "", 0, 0, 0, 0, "") for n in range(40)])
        page = vuln_db.fleet_servers(page=99, page_size=10)
        self.assertEqual((page["page"], page["pages"], page["total"]), (5, 5, 47))
        self.assertEqual(len(page["rows"]), 7)
        self.assertEqual(vuln_db.fleet_servers(page=2, page_size=10)["rows"][0]["server_id"],
                         [r["server_id"] for r in vuln_db.fleet_servers(page_size=100)["rows"]][10])


class SortTests(ServersStore):
    KEYS = {
        "hostname": lambda r: r["hostname"].lower(),
        "group": lambda r: r["group_name"].lower(),
        "environment": lambda r: r["environment"].lower(),
        "os": lambda r: r["os_name"].lower(),
        "owner": lambda r: r["owner"].lower(),
        "critical": lambda r: r["vuln_critical"],
        "high": lambda r: r["vuln_high"],
        "fixable": lambda r: r["vuln_fixable"],
        "exploited": lambda r: r["kev"],
        "inventory": lambda r: r["inventory_at"],
    }

    def setUp(self):
        super().setUp()
        self.seed()
        self.rows = {r["server_id"]: r for r in vuln_db.fleet_servers(page_size=100)["rows"]}

    def expected(self, key, desc):
        """Equal values keep hostname (case-insensitive) then id order, in both directions."""
        rows = sorted(self.rows.values(), key=lambda r: (r["hostname"].lower(), r["server_id"]))
        rows.sort(key=self.KEYS[key], reverse=desc)
        return [r["server_id"] for r in rows]

    def test_every_sort_key_both_directions(self):
        self.assertEqual(sorted(vuln_db.FLEET_SERVER_SORTS), sorted(self.KEYS))
        for key in self.KEYS:
            for direction, desc in (("asc", False), ("desc", True)):
                with self.subTest(key=key, direction=direction):
                    self.assertEqual(self.ids(sort=key, direction=direction), self.expected(key, desc))

    def test_ties_are_ordered_by_hostname_then_id(self):
        # Critical 2: Cache-01 (7), db-01 (3), Web-01 (1) in hostname order, either direction; critical 0:
        # app-02 (6), app_01 (5), web-02 (2), since "-" sorts before "_".
        self.assertEqual(self.ids(sort="critical", direction="desc"), [4, 7, 3, 1, 6, 5, 2])
        self.assertEqual(self.ids(sort="critical", direction="asc"), [6, 5, 2, 7, 3, 1, 4])
        self.seed([(50, "dup", "G", "E", "", "", "", 0, 0, 0, 0, ""), (49, "dup", "G", "E", "", "", "", 0, 0, 0, 0, "")])
        order = self.ids(sort="hostname", direction="asc")
        self.assertEqual(order.index(49) + 1, order.index(50))

    def test_direction_other_than_desc_is_ascending(self):
        asc = self.ids(sort="critical", direction="asc")
        for junk in ("", "ASC", "up", "desc; DROP TABLE servers", None, 5):
            self.assertEqual(self.ids(sort="critical", direction=junk), asc, junk)
        self.assertEqual(self.ids(sort="critical", direction="DESC"), self.ids(sort="critical", direction="desc"))

    def test_an_unknown_sort_falls_back_to_the_default_order_and_never_reaches_sql(self):
        for hostile in ("s.kev; DROP TABLE servers", "hostname; DROP TABLE servers", "bogus", "", None, "HOSTNAME",
                        "s.hostname", "1", ["hostname"], 7, "hostname COLLATE NOCASE"):
            for direction in ("asc", "desc"):
                with self.subTest(sort=hostile, direction=direction):
                    self.assertEqual(self.ids(sort=hostile, direction=direction), DEFAULT_ORDER)
        self.assertEqual(vuln_db.fleet_servers()["total"], 7)  # the table survived
        text = vuln_db._fleet_server_order("s.kev; DROP TABLE servers", "desc")
        self.assertEqual(text, vuln_db._FLEET_SERVER_DEFAULT_ORDER)
        self.assertNotIn("DROP", text)

    def test_only_whitelisted_expressions_are_in_the_order_by(self):
        allowed = set(vuln_db.FLEET_SERVER_SORTS.values())
        for key, expr in vuln_db.FLEET_SERVER_SORTS.items():
            for direction in ("asc", "desc", "x"):
                way = "DESC" if direction == "desc" else "ASC"
                self.assertEqual(vuln_db._fleet_server_order(key, direction),
                                 expr + " " + way + ", s.hostname COLLATE NOCASE ASC, s.server_id ASC")
        self.assertEqual(len(allowed), len(vuln_db.FLEET_SERVER_SORTS))

    def test_sort_honors_filters_and_paging(self):
        self.assertEqual(self.ids(group="Alpha", sort="hostname", direction="desc"), [2, 1, 7])
        page = vuln_db.fleet_servers(sort="hostname", page=2, page_size=10)
        self.assertEqual(page["page"], 1)  # 7 servers: page 2 clamps to 1
        self.assertEqual([r["server_id"] for r in page["rows"]], self.ids(sort="hostname"))


class HideTests(ServersStore):
    def test_hide_ids_hides_exactly_those(self):
        self.seed()
        self.assertEqual(self.ids(hide_ids=[4, 2]), [7, 1, 3, 6, 5])
        page = vuln_db.fleet_servers(hide_ids={4, 2}, page_size=100)
        self.assertEqual(page["total"], 5)
        self.assertEqual(self.ids(hide_ids=[999, 4]), [2, 7, 1, 3, 6, 5])
        self.assertEqual(self.ids(hide_ids=[]), DEFAULT_ORDER)
        self.assertEqual(self.ids(hide_ids=(True, "x", None, 4.5, 2 ** 70, 4)), [2, 7, 1, 3, 6, 5])
        self.assertEqual(self.ids(hide_ids=5), DEFAULT_ORDER)  # not iterable: nothing hidden, no error

    def test_hide_ids_combine_with_filters(self):
        self.seed()
        self.assertEqual(sorted(self.ids(group="Alpha", hide_ids=[1])), [2, 7])
        self.assertEqual(self.ids(owner=NONE, hide_ids=[2]), [6])
        self.assertEqual(vuln_db.fleet_server_ids(group="Alpha", hide_ids=[1])["total"], 2)

    def test_more_than_chunk_ids_are_hidden_exactly(self):
        count = vuln_db.CHUNK * 2 + 300
        self.seed([(n, "h%05d" % n, "G", "E", "", "", "", 0, 0, 0, 0, "") for n in range(1, count + 1)])
        hidden = [n for n in range(1, count + 1) if n % 5 != 0]            # 1,040 of the 1,300
        self.assertGreater(len(hidden), vuln_db.CHUNK)
        page = vuln_db.fleet_servers(hide_ids=hidden, page_size=100)
        self.assertEqual(page["total"], count - len(hidden))
        self.assertEqual(sorted(r["server_id"] for r in page["rows"]), [n for n in range(1, count + 1) if n % 5 == 0][:100])
        ids = vuln_db.fleet_server_ids(hide_ids=hidden, limit=10000)
        self.assertEqual(sorted(ids["ids"]), [n for n in range(1, count + 1) if n % 5 == 0])
        options = vuln_db.fleet_server_options(hide_ids=hidden)
        self.assertEqual(options["group"], ["G"])
        self.assertEqual(self.raw().execute("SELECT COUNT(*) FROM sqlite_temp_master").fetchone()[0], 0)


class IdsAndOptionsTests(ServersStore):
    def setUp(self):
        super().setUp()
        self.seed()

    def test_fleet_server_ids_follow_the_visible_order_limit_and_total(self):
        every = vuln_db.fleet_server_ids()
        self.assertEqual(every, {"total": 7, "ids": DEFAULT_ORDER, "unavailable": False})
        first = vuln_db.fleet_server_ids(limit=3)
        self.assertEqual((first["total"], first["ids"]), (7, DEFAULT_ORDER[:3]))
        self.assertEqual(vuln_db.fleet_server_ids(limit=0)["ids"], [])
        self.assertEqual(vuln_db.fleet_server_ids(limit="junk")["ids"], DEFAULT_ORDER)
        by_host = vuln_db.fleet_server_ids(sort="hostname", direction="desc", limit=2)
        self.assertEqual((by_host["total"], by_host["ids"]), (7, self.ids(sort="hostname", direction="desc")[:2]))
        match = vuln_db.fleet_server_ids(group="Alpha", fix_only=True, kev_only=False, search="e", owner="",
                                         sort="critical", direction="asc")
        self.assertEqual(match["ids"], self.ids(group="Alpha", fix_only=True, search="e", sort="critical"))
        self.assertEqual(vuln_db.fleet_server_ids(owner=NONE)["total"], 2)
        hostile = vuln_db.fleet_server_ids(sort="s.kev; DROP TABLE servers")
        self.assertEqual(hostile["ids"], DEFAULT_ORDER)
        self.assertEqual(vuln_db.fleet_server_ids(group="nope"), {"total": 0, "ids": [], "unavailable": False})

    def test_options_are_distinct_non_empty_and_case_insensitively_ordered(self):
        conn = self.raw()
        conn.execute("UPDATE servers SET group_name = 'alpha2' WHERE server_id = 5")
        conn.commit()
        options = vuln_db.fleet_server_options()
        self.assertEqual(options["group"], ["Alpha", "alpha2", "Beta", "Gamma"])
        self.assertEqual(options["environment"], ["Dev", "Prod"])
        self.assertEqual(options["os_build"], ["AL2023", "RHEL 9", "Windows Server 2022"])
        self.assertEqual(options["owner"], ["Ann Lee", "bob", "Carl Voss"])
        self.assertEqual((options["has_no_owner"], options["has_no_os_build"]), (True, True))
        self.assertEqual((options["truncated"], options["unavailable"]), (False, False))
        self.assertEqual(sorted(options), sorted(["group", "environment", "os_build", "owner", "has_no_owner",
                                                  "has_no_os_build", "truncated", "unavailable"]))

    def test_options_flags_and_hidden_servers(self):
        options = vuln_db.fleet_server_options(hide_ids=[2, 6])  # the two servers with no owner
        self.assertFalse(options["has_no_owner"])
        self.assertTrue(options["has_no_os_build"])                # server 5 has none either
        options = vuln_db.fleet_server_options(hide_ids=[2, 6, 5])
        self.assertFalse(options["has_no_os_build"])
        self.assertNotIn("Gamma", options["group"])
        self.assertNotIn("Carl Voss", options["owner"])

    def test_options_truncate_at_the_limit(self):
        options = vuln_db.fleet_server_options(limit=2)
        self.assertEqual(options["group"], ["Alpha", "Beta"])
        self.assertEqual(options["owner"], ["Ann Lee", "bob"])
        self.assertEqual(options["environment"], ["Dev", "Prod"])
        self.assertTrue(options["truncated"])
        self.assertFalse(vuln_db.fleet_server_options(limit=3)["truncated"])

    def test_options_of_an_empty_store(self):
        vuln_db.forget_servers(range(1, 8))
        options = vuln_db.fleet_server_options()
        self.assertEqual((options["group"], options["owner"], options["has_no_owner"], options["has_no_os_build"]),
                         ([], [], False, False))


class UnavailableTests(ServersStore):
    def test_a_missing_store_reads_empty_and_is_not_unavailable(self):
        page = vuln_db.fleet_servers(group="x", sort="hostname")
        self.assertEqual((page["total"], page["rows"], page["unavailable"]), (0, [], False))
        self.assertEqual(vuln_db.fleet_server_ids(), {"total": 0, "ids": [], "unavailable": False})
        options = vuln_db.fleet_server_options()
        self.assertEqual((options["group"], options["unavailable"]), ([], False))
        self.assertEqual(vuln_db.server_attributes(), {})
        self.assertFalse(os.path.exists(store.FINDINGS_DB))

    def test_a_damaged_file_gives_empty_unavailable_answers_and_never_raises(self):
        with open(store.FINDINGS_DB, "wb") as handle:
            handle.write(b"this is not a database" * 100)
        page = vuln_db.fleet_servers()
        self.assertEqual((page["total"], page["rows"], page["unavailable"]), (0, [], True))
        self.assertEqual(vuln_db.fleet_server_ids(), {"total": 0, "ids": [], "unavailable": True})
        self.assertTrue(vuln_db.fleet_server_options()["unavailable"])
        self.assertEqual(vuln_db.server_attributes(), {})

    def test_a_store_with_no_servers_table_is_just_empty(self):
        sqlite3.connect(store.FINDINGS_DB).close()
        self.assertFalse(vuln_db.fleet_servers()["unavailable"])
        self.assertFalse(vuln_db.fleet_server_ids()["unavailable"])
        self.assertFalse(vuln_db.fleet_server_options()["unavailable"])


class VersionThreeTests(ServersStore):
    def make_v3(self):
        """A store as the previous release left it: current tables without owner and os_build, version 3."""
        self.seed()
        conn = self.raw()
        conn.execute("ALTER TABLE servers DROP COLUMN owner")
        conn.execute("ALTER TABLE servers DROP COLUMN os_build")
        conn.execute("PRAGMA user_version = 3")
        conn.commit()

    def test_a_version_3_file_is_unavailable_until_upgraded_and_a_reader_never_upgrades(self):
        self.make_v3()
        self.assertEqual(vuln_db.schema_version(), 3)
        page = vuln_db.fleet_servers()
        self.assertEqual((page["total"], page["rows"], page["unavailable"]), (0, [], True))
        self.assertTrue(vuln_db.fleet_server_options()["unavailable"])
        for ids in (vuln_db.fleet_server_ids(owner="x"), vuln_db.fleet_server_ids(sort="owner"),
                    vuln_db.fleet_server_ids(search="a")):  # whatever touches a missing column
            self.assertEqual((ids["total"], ids["ids"], ids["unavailable"]), (0, [], True))
        self.assertEqual(vuln_db.server_attributes(), {})
        self.assertEqual(vuln_db.schema_version(), 3)
        self.assertEqual(vuln_db.known_servers()[0]["server_id"], 1)  # the small reads still work

    def test_upgrade_adds_the_columns_in_place_with_empty_values(self):
        self.make_v3()
        before = self.raw().execute("SELECT server_id, hostname, vuln_critical FROM servers ORDER BY 1").fetchall()
        self.assertEqual(vuln_db.upgrade(), 4)
        self.assertEqual(vuln_db.schema_version(), 4)
        conn = self.raw()
        self.assertEqual(conn.execute("SELECT server_id, hostname, vuln_critical FROM servers ORDER BY 1").fetchall(),
                         before)  # rows are kept: no rebuild
        self.assertEqual(conn.execute("SELECT DISTINCT owner, os_build FROM servers").fetchall(), [("", "")])
        page = vuln_db.fleet_servers(page_size=100)
        self.assertEqual((page["total"], page["unavailable"]), (7, False))
        self.assertTrue(all(r["owner"] == "" and r["os_build"] == "" for r in page["rows"]))
        self.assertEqual(sorted(self.ids(owner=NONE)), [1, 2, 3, 4, 5, 6, 7])
        self.assertEqual(vuln_db.upgrade(), 4)  # idempotent

    def test_a_save_into_a_version_3_file_upgrades_it(self):
        self.make_v3()
        vuln_db.replace_server({"server_id": 1, "hostname": "h", "owner": "Ann Lee", "os_build": "RHEL 9"},
                               {"matches": [], "db_built": "b"})
        self.assertEqual(vuln_db.schema_version(), 4)
        self.assertEqual(vuln_db.server_summary(1)["owner"], "Ann Lee")


class WriteTests(ServersStore):
    def test_replace_server_stores_owner_and_os_build_and_empty_when_absent(self):
        row = vuln_db.replace_server({"server_id": 1, "hostname": "h1", "owner": "Ann Lee", "os_build": "RHEL 9"},
                                     {"matches": [], "db_built": "b"})
        self.assertEqual((row["owner"], row["os_build"]), ("Ann Lee", "RHEL 9"))
        row = vuln_db.replace_server({"server_id": 2, "hostname": "h2"}, {"matches": [], "db_built": "b"})
        self.assertEqual((row["owner"], row["os_build"]), ("", ""))
        row = vuln_db.replace_server({"server_id": 3, "hostname": "h3", "owner": None, "os_build": None},
                                     {"matches": [], "db_built": "b"})
        self.assertEqual((row["owner"], row["os_build"]), ("", ""))
        # The unchanged-matches fast path writes them too.
        row = vuln_db.replace_server({"server_id": 1, "hostname": "h1", "owner": "Bob", "os_build": ""},
                                     {"matches": [], "db_built": "b"})
        self.assertTrue(row["unchanged"])
        self.assertEqual((row["owner"], row["os_build"]), ("Bob", ""))

    def test_update_server_attributes_changes_only_given_rows_and_counts_real_changes(self):
        self.seed()
        rows = [
            {"server_id": 1, "hostname": "Web-01", "group_name": "Alpha", "environment": "Prod", "owner": "Ann Lee",
             "os_build": "AL2023"},                                     # identical: not a change
            {"server_id": 2, "hostname": "web-02", "group_name": "Omega", "environment": "Dev", "owner": "Zed",
             "os_build": "Windows Server 2022"},                         # group and owner change
            {"server_id": 5, "hostname": "renamed", "group_name": "Gamma", "environment": "Dev", "owner": None,
             "os_build": None},                                          # hostname changes; None means none
            {"server_id": 999, "hostname": "x", "group_name": "x", "environment": "x", "owner": "x", "os_build": "x"},
            {"server_id": 3, "hostname": "db-01"},                       # a missing key: skipped, not blanked
            {"hostname": "no id"},
        ]
        self.assertEqual(vuln_db.update_server_attributes(rows), 2)
        now = {r["server_id"]: r for r in vuln_db.fleet_servers(page_size=100)["rows"]}
        self.assertEqual((now[2]["group_name"], now[2]["owner"]), ("Omega", "Zed"))
        self.assertEqual((now[5]["hostname"], now[5]["owner"], now[5]["os_build"]), ("renamed", "", ""))
        self.assertEqual((now[3]["group_name"], now[3]["owner"], now[3]["os_build"]), ("Beta", "bob", "RHEL 9"))
        self.assertEqual(now[1]["owner"], "Ann Lee")
        self.assertNotIn(999, now)
        for sid in (1, 3, 4, 6, 7):  # untouched rows are as seeded
            self.assertEqual(now[sid]["hostname"], {1: "Web-01", 3: "db-01", 4: "db-02", 6: "app-02", 7: "Cache-01"}[sid])
        self.assertEqual(vuln_db.update_server_attributes(rows[:3]), 0)  # nothing left to change
        self.assertEqual(vuln_db.update_server_attributes([]), 0)
        self.assertEqual(vuln_db.update_server_attributes(None), 0)

    def test_update_server_attributes_does_nothing_without_a_store(self):
        self.assertEqual(vuln_db.update_server_attributes([{"server_id": 1, "hostname": "h", "group_name": "g",
                                                            "environment": "e", "owner": "o", "os_build": "b"}]), 0)
        self.assertFalse(os.path.exists(store.FINDINGS_DB))

    def test_server_attributes_reads_what_is_stored(self):
        self.seed()
        found = vuln_db.server_attributes()
        self.assertEqual(sorted(found), [1, 2, 3, 4, 5, 6, 7])
        self.assertEqual(found[1], {"hostname": "Web-01", "group_name": "Alpha", "environment": "Prod",
                                    "owner": "Ann Lee", "os_build": "AL2023"})


def match(name, vuln_id, severity, cves, fix=""):
    return {
        "pkg_name": name, "pkg_version": "1", "pkg_type": "rpm", "path": "", "vuln_id": vuln_id, "severity": severity,
        "sev_rank": vuln_match.sev_rank(severity), "fix_state": "fixed" if fix else "unknown", "fix_version": fix,
        "kev": False, "epss": None, "risk": None, "data_source": "",
        "cves": [{"cve": cve, "kev": False} for cve in cves],
    }


class ForgetServersTests(ServersStore):
    def save(self, server_id, matches):
        return vuln_db.replace_server(
            {"server_id": server_id, "hostname": "h%d" % server_id, "os_name": "", "group_name": "g",
             "environment": "e", "scan_id": "s", "inventory_at": "i", "packages": 1, "matched_at": "m"},
            {"matches": matches, "db_built": "b"})

    def fleet(self):
        return {row[0]: tuple(row[1:]) for row in self.raw().execute("SELECT * FROM fleet_cves")}

    def by_hand(self):
        groups = {}
        for cve, rank, kev, _sid, fix in self.raw().execute(
                "SELECT cve, sev_rank, kev, server_id, fix_version FROM server_cves"):
            groups.setdefault(cve, []).append((rank, kev, fix))
        return {cve: (max(r for r, _k, _f in rows), max(k for _r, k, _f in rows), len(rows),
                      sum(1 for _r, _k, f in rows if f)) for cve, rows in groups.items()}

    def test_forget_servers_removes_each_and_keeps_the_fleet_counts_right(self):
        common = match("curl", "ALAS-1", "High", ["CVE-2024-0001"], "2")
        self.save(1, [common, match("a", "ALAS-2", "Critical", ["CVE-2024-0002"])])
        self.save(2, [common, match("b", "ALAS-3", "Low", ["CVE-2024-0003"], "1")])
        self.save(3, [common])
        self.save(4, [match("c", "ALAS-4", "Medium", ["CVE-2024-0004"])])
        self.assertEqual(self.fleet(), self.by_hand())
        self.assertEqual(vuln_db.forget_servers([1, 2, 999]), 2)         # 999 had no row: not counted
        self.assertEqual(self.fleet(), self.by_hand())
        self.assertEqual([r[0] for r in self.raw().execute("SELECT server_id FROM servers ORDER BY 1")], [3, 4])
        self.assertEqual(self.fleet()["CVE-2024-0001"][2], 1)
        self.assertNotIn("CVE-2024-0002", self.fleet())
        for table in ("server_cves", "server_packages", "server_matches"):
            self.assertEqual(self.raw().execute("SELECT COUNT(*) FROM {} WHERE server_id IN (1, 2)".format(table))
                             .fetchone()[0], 0)
        self.assertEqual(vuln_db.forget_servers([]), 0)
        self.assertEqual(vuln_db.forget_servers(iter([3, 4])), 2)
        self.assertEqual(self.fleet(), {})

    def test_forget_servers_without_a_store_is_zero(self):
        self.assertEqual(vuln_db.forget_servers([1, 2]), 0)
        self.assertFalse(os.path.exists(store.FINDINGS_DB))


if __name__ == "__main__":
    unittest.main()
