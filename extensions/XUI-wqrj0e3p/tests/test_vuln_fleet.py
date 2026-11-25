"""
Checks for the jobs of the Vulnerabilities XUI (vuln_fleet.py, vuln_hooks.py and the edits to
vuln_jobs.py, vuln_store.py and vuln_repo.py) without CloudBolt: CloudBolt's modules are stubs and the
findings store's functions are replaced where a test needs them.

    python -B -m unittest discover -s extensions/XUI-wqrj0e3p/tests -p "test_*.py"

The stubs are in place only while this file imports the extension's modules, so another test file's
stubs (test_vuln_db.py has its own) are not disturbed; a test that reaches a lazy import patches
sys.modules for its own run.
"""
import contextlib
import importlib
import json
import os
import sys
import tempfile
import threading
import types
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
EXT = os.path.dirname(HERE)
PACKAGE = "xui.io_cloudbolt_vulnerabilities"


class IntegrityError(Exception):
    pass


class CancelJob(BaseException):
    """Stands in for CloudBolt's CancelJobException, which is a BaseException."""


class Http404(Exception):
    pass


class FakeLogger:
    def __init__(self, *a):
        pass

    def __getattr__(self, name):
        return lambda *a, **k: None


def _module(name, **attrs):
    module = types.ModuleType(name)
    module.__dict__.update(attrs)
    return module


TMP = tempfile.mkdtemp(prefix="vuln-fleet-test-")
STUBS = {}


def _stub(name, **attrs):
    STUBS[name] = _module(name, **attrs)
    return STUBS[name]


_stub("django")
_stub("django.conf", settings=types.SimpleNamespace(PROSERV_DIR=TMP, CLOUDBOLT_OS_USER="u", CLOUDBOLT_OS_GROUP="g"))
_stub("django.http", FileResponse=object, Http404=Http404, HttpResponse=object)
_stub("django.urls", reverse=lambda *a, **k: "/x/")
_stub("django.db", IntegrityError=IntegrityError,
      transaction=types.SimpleNamespace(atomic=lambda: contextlib.nullcontext()))
_stub("cbhooks")
_stub("cbhooks.models", CloudBoltHook=object)
_stub("accounts")
_stub("accounts.models", Group=object, UserProfile=object)
_stub("infrastructure")
_stub("infrastructure.models", Server=object)
_stub("jobs")
_stub("jobs.models", Job=object)
_stub("portals")
_stub("portals.models")
_stub("utilities")
_stub("utilities.events", add_server_event=lambda *a, **k: None)
_stub("utilities.logger", ThreadLogger=FakeLogger)
_xui = _stub("xui")
_xui.__path__ = [EXT]


def _import_extension():
    """Import the extension's modules against the stubs, leaving sys.modules as it was found."""
    touched = [k for k in sys.modules if k == "xui" or k.startswith(PACKAGE) or k in STUBS]
    saved = {k: sys.modules[k] for k in touched}
    for key in touched:
        del sys.modules[key]
    sys.modules.update(STUBS)
    try:
        names = ("vuln_store", "vuln_match", "vuln_db", "vuln_tools", "vuln_repo", "vuln_hooks", "vuln_jobs",
                 "vuln_fleet")
        return {name: importlib.import_module(PACKAGE + "." + name) for name in names}
    finally:
        for key in [k for k in sys.modules if k == "xui" or k.startswith(PACKAGE) or k in STUBS]:
            del sys.modules[key]
        sys.modules.update(saved)


MODS = _import_extension()
store, match, db, tools, repo, hooks, jobs, fleet = (MODS[n] for n in (
    "vuln_store", "vuln_match", "vuln_db", "vuln_tools", "vuln_repo", "vuln_hooks", "vuln_jobs", "vuln_fleet"))


# ---------------------------------------------------------------- fakes
def make_server(**over):
    data = dict(
        id=7, hostname="web-01", status="ACTIVE", power_status="POWERON",
        group=types.SimpleNamespace(is_unassigned=False, name="Dev"),
        environment=types.SimpleNamespace(name="Env"),
        os_family=types.SimpleNamespace(get_base_name=lambda: "Linux", name="Linux"),
        tags=types.SimpleNamespace(all=lambda: []),
        resource_handler=types.SimpleNamespace(cast=lambda: types.SimpleNamespace(can_run_scripts_on_servers=True)),
        use_tech_specific_script_execution=lambda: False,
        get_preferred_ip=lambda: "10.0.0.5",
        get_credentials=lambda **kw: {"username": "root", "password": "secret"},
    )
    data.update(over)
    return types.SimpleNamespace(**data)


class FakeQuery:
    def __init__(self, rows):
        self.rows = list(rows)

    def select_related(self, *a):
        return self

    def prefetch_related(self, *a):
        return self

    def exclude(self, **kw):
        return self

    def order_by(self, *a):
        return self

    def distinct(self):
        return self

    def first(self):
        return self.rows[0] if self.rows else None

    def values_list(self, *fields, **kw):
        if len(fields) == 1 and kw.get("flat"):
            return FakeQuery([getattr(r, fields[0], None) for r in self.rows])
        return [tuple(getattr(r, f, None) for f in fields) for r in self.rows]

    def __iter__(self):
        return iter(self.rows)


def main_thread_only(what):
    if threading.current_thread() is not threading.main_thread():
        VIOLATIONS.append(what)


VIOLATIONS = []


def fake_server_model(servers, fail=False):
    """A stand-in for Server whose objects.filter(id__in=...) reads a list; it records any use from a thread."""
    class Manager:
        def filter(self, **kw):
            main_thread_only("Server.objects")
            if fail:
                raise RuntimeError("database is gone")
            rows = servers
            if "id" in kw:
                rows = [s for s in rows if s.id == kw["id"]]
            if "id__in" in kw:
                rows = [s for s in rows if s.id in set(kw["id__in"])]
            for key, value in kw.items():
                if key in ("status", "power_status"):
                    rows = [s for s in rows if getattr(s, key) == value]
            return FakeQuery([types.SimpleNamespace(**dict({"group_id": None}, **vars(s))) for s in rows])

    return types.SimpleNamespace(objects=Manager())


def fake_group_model(unassigned_ids):
    return types.SimpleNamespace(unassigned=types.SimpleNamespace(ids=lambda: list(unassigned_ids)))


def fake_job_model(status="RUNNING", statuses=None):
    """A stand-in for Job: objects.filter(id=...) answers the cancel check, filter(id__in=...) the polls."""
    calls = []

    class Manager:
        def filter(self, **kw):
            main_thread_only("Job.objects")
            calls.append(kw)
            if "id__in" in kw:
                table = statuses() if callable(statuses) else (statuses or {})
                return FakeQuery([types.SimpleNamespace(id=i, status=table.get(i)) for i in kw["id__in"]
                                  if i in table])
            return FakeQuery([types.SimpleNamespace(status=status() if callable(status) else status)])

    return types.SimpleNamespace(objects=Manager(), COMPLETED_STATUSES=["WARNING", "SUCCESS", "FAILURE", "CANCELED"],
                                 calls=calls)


class Base(unittest.TestCase):
    def setUp(self):
        VIOLATIONS.clear()
        patcher = mock.patch.dict(sys.modules, STUBS)
        patcher.start()
        self.addCleanup(patcher.stop)
        # vuln_db.upgrade() returns the schema version; the jobs call it first.
        patcher = mock.patch.object(db, "upgrade", return_value=2)
        self.upgrade = patcher.start()
        self.addCleanup(patcher.stop)


class OldRefusal:
    """A Refusal made by an older class object, as after a module reload: not an instance of jobs.Refusal."""

    def __init__(self, message, status=409):
        self.message, self.status = message, status


def fake_clock(*early):
    """A clock that answers the given values, then a time past the job budget."""
    values = list(early)

    def clock():
        return values.pop(0) if values else fleet.JOB_BUDGET_S + 1

    return clock


# ---------------------------------------------------------------- eligibility
class EligibilityTests(Base):
    def test_ok(self):
        self.assertEqual(fleet.eligibility(make_server(), "linux"), "")

    def test_historical_and_unassigned(self):
        self.assertEqual(fleet.eligibility(make_server(status="HISTORICAL"), "linux"), "historical")
        server = make_server(group=types.SimpleNamespace(is_unassigned=True, name="Unassigned"))
        self.assertEqual(fleet.eligibility(server, "linux"), "unassigned")

    def test_a_server_that_is_not_active_has_its_own_reason(self):
        for status in ("PROVFAILED", "DECOMMISSIONING", "PROVISIONING"):
            self.assertEqual(fleet.eligibility(make_server(status=status), "linux"), "not_active", status)
        self.assertEqual(fleet.eligibility(make_server(status="HISTORICAL"), "linux"), "historical")
        self.assertEqual(fleet.REASONS["not_active"], "not active")

    def test_not_linux_or_windows_and_hidden(self):
        windows = make_server(os_family=types.SimpleNamespace(get_base_name=lambda: "Windows", name="Windows"))
        self.assertEqual(fleet.eligibility(windows, "linux"), "unsupported_os")
        self.assertEqual(fleet.eligibility(make_server(), "label"), "hidden")
        tagged = make_server(tags=types.SimpleNamespace(all=lambda: [types.SimpleNamespace(name="Vulnerabilities")]))
        self.assertEqual(fleet.eligibility(tagged, "label"), "")

    def test_powered_off(self):
        self.assertEqual(fleet.eligibility(make_server(power_status="POWEROFF"), "linux"), "powered_off")

    def test_no_script_channel(self):
        self.assertEqual(fleet.eligibility(make_server(resource_handler=None), "linux"), "no_script_channel")
        handler = types.SimpleNamespace(cast=lambda: types.SimpleNamespace(can_run_scripts_on_servers=False))
        self.assertEqual(fleet.eligibility(make_server(resource_handler=handler), "linux"), "no_script_channel")

    def test_no_address(self):
        self.assertEqual(fleet.eligibility(make_server(get_preferred_ip=lambda: ""), "linux"), "no_address")

    def test_no_credentials(self):
        def raises(**kw):
            raise RuntimeError("key file is not on CloudBolt")

        self.assertEqual(fleet.eligibility(make_server(get_credentials=raises), "linux"), "no_credentials")
        for creds in ({}, {"username": "root"}, {"username": "root", "password": ""}, None):
            self.assertEqual(fleet.eligibility(make_server(get_credentials=lambda **kw: creds), "linux"),
                             "no_credentials", creds)
        keyed = make_server(get_credentials=lambda **kw: {"username": "root", "keyfile": "k.pem"})
        self.assertEqual(fleet.eligibility(keyed, "linux"), "")

    def test_credentials_are_not_armed(self):
        seen = {}

        def credentials(**kw):
            seen.update(kw)
            return {"password": "x"}

        fleet.eligibility(make_server(get_credentials=credentials), "linux")
        self.assertIs(seen.get("arm_for_use"), False)

    def test_tech_specific_needs_no_address_or_credentials(self):
        def raises(**kw):
            raise RuntimeError("no")

        server = make_server(use_tech_specific_script_execution=lambda: True, get_preferred_ip=lambda: "",
                             get_credentials=raises)
        self.assertEqual(fleet.eligibility(server, "linux"), "")

    def test_openshift_vm_needs_no_handler_address_or_credentials(self):
        def raises(**kw):
            raise RuntimeError("no")

        server = make_server(ovirtserverinfo=object(), resource_handler=None, get_preferred_ip=lambda: "",
                             get_credentials=raises)
        self.assertEqual(fleet.eligibility(server, "linux"), "")

    def test_a_failed_check_is_unknown_never_an_exception(self):
        def raises():
            raise RuntimeError("no")

        self.assertEqual(fleet.eligibility(make_server(get_preferred_ip=raises), "linux"), "unknown")


# ---------------------------------------------------------------- forget_stale
class ForgetStaleTests(Base):
    def run_forget(self, known, servers, unassigned=(), active=(), state_ids=(), repo_ids=(), fail=False):
        forgot = {"db": [], "repo": [], "state": []}

        def active_job(server_id, state=None):
            if server_id in active:
                return ({"job_id": 1}, object())
            return None

        patches = [
            mock.patch.object(fleet, "Server", fake_server_model(servers, fail=fail)),
            mock.patch.object(fleet, "Group", fake_group_model(unassigned)),
            mock.patch.object(db, "known_servers", create=True,
                              return_value=[{"server_id": i} for i in known]),
            mock.patch.object(db, "forget_server", create=True, side_effect=lambda i: forgot["db"].append(i)),
            mock.patch.object(store, "known_state_ids", return_value=list(state_ids)),
            mock.patch.object(store, "forget_state", side_effect=lambda i: forgot["state"].append(i)),
            mock.patch.object(repo, "known_ids", return_value=list(repo_ids)),
            mock.patch.object(repo, "forget", side_effect=lambda i: forgot["repo"].append(i)),
            mock.patch.object(jobs, "active_job", side_effect=active_job),
        ]
        with contextlib.ExitStack() as stack:
            for patcher in patches:
                stack.enter_context(patcher)
            result = fleet.forget_stale()
        return result, forgot

    def test_missing_historical_and_unassigned_are_forgotten_a_live_one_is_kept(self):
        servers = [
            make_server(id=1),                                       # live, normal group: kept
            make_server(id=2, status="HISTORICAL"),                  # historical: forgotten
            make_server(id=3, group_id=99),                          # in the Unassigned group: forgotten
            make_server(id=5, group_id=11),                          # live, a normal group: kept
        ]
        result, forgot = self.run_forget(known=[1, 2, 3, 4, 5], servers=servers, unassigned=[99],
                                         state_ids=[4, 6], repo_ids=[6])
        # 4 and 6 have no Server row at all.
        self.assertEqual(sorted(forgot["db"]), [2, 3, 4, 6])
        self.assertEqual(sorted(forgot["repo"]), [2, 3, 4, 6])
        self.assertEqual(sorted(forgot["state"]), [2, 3, 4, 6])
        self.assertEqual(result, {"forgotten": 4, "kept": 2})
        self.assertEqual(VIOLATIONS, [])

    def test_a_server_with_an_active_scan_is_never_touched(self):
        servers = [make_server(id=2, status="HISTORICAL")]
        result, forgot = self.run_forget(known=[2, 3], servers=servers, active=[2, 3])
        self.assertEqual(forgot, {"db": [], "repo": [], "state": []})
        self.assertEqual(result, {"forgotten": 0, "kept": 2})

    def test_when_the_server_query_fails_nothing_is_forgotten(self):
        result, forgot = self.run_forget(known=[1, 2], servers=[], fail=True, state_ids=[3])
        self.assertEqual(forgot, {"db": [], "repo": [], "state": []})
        self.assertEqual(result, {"forgotten": 0, "kept": 3})

    def test_when_the_group_query_fails_nothing_is_forgotten(self):
        class Broken:
            @staticmethod
            def ids():
                raise RuntimeError("no")

        forgot = {"db": []}
        with mock.patch.object(fleet, "Server", fake_server_model([])), \
                mock.patch.object(fleet, "Group", types.SimpleNamespace(unassigned=Broken)), \
                mock.patch.object(db, "known_servers", create=True, return_value=[{"server_id": 1}]), \
                mock.patch.object(db, "forget_server", create=True, side_effect=forgot["db"].append), \
                mock.patch.object(store, "known_state_ids", return_value=[]), \
                mock.patch.object(repo, "known_ids", return_value=[]):
            result = fleet.forget_stale()
        self.assertEqual(forgot["db"], [])
        self.assertEqual(result["forgotten"], 0)

    def test_nothing_stored_means_no_query(self):
        result, forgot = self.run_forget(known=[], servers=[], fail=True)
        self.assertEqual(result, {"forgotten": 0, "kept": 0})

    def test_one_failing_forget_does_not_stop_the_rest(self):
        servers = [make_server(id=1, status="HISTORICAL"), make_server(id=2, status="HISTORICAL")]
        done = []

        def forget(server_id):
            if server_id == 1:
                raise RuntimeError("locked")
            done.append(server_id)

        with mock.patch.object(fleet, "Server", fake_server_model(servers)), \
                mock.patch.object(fleet, "Group", fake_group_model([])), \
                mock.patch.object(db, "known_servers", create=True, return_value=[{"server_id": 1}, {"server_id": 2}]), \
                mock.patch.object(db, "forget_server", create=True, side_effect=forget), \
                mock.patch.object(store, "known_state_ids", return_value=[]), \
                mock.patch.object(store, "forget_state"), mock.patch.object(repo, "known_ids", return_value=[]), \
                mock.patch.object(repo, "forget"), \
                mock.patch.object(jobs, "active_job", return_value=None):
            result = fleet.forget_stale()
        self.assertEqual(done, [2])
        self.assertEqual(result, {"forgotten": 1, "kept": 1})


class StateFilesTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="vuln-state-")
        self.patches = [
            mock.patch.object(store, "SERVERS_DIR", os.path.join(self.root, "servers")),
            mock.patch.object(store, "INVENTORY_DIR", os.path.join(self.root, "inventories")),
            mock.patch.object(store, "ROOT", self.root),
            mock.patch.object(store, "own", lambda *a, **k: None),
        ]
        for patcher in self.patches:
            patcher.start()
            self.addCleanup(patcher.stop)
        os.makedirs(store.SERVERS_DIR)
        os.makedirs(os.path.join(store.INVENTORY_DIR, "12"))
        os.makedirs(os.path.join(store.INVENTORY_DIR, "13"))
        for name in ("12.json", "12.lock", "12.start", "13.json", "x.json", "notes.txt", ".tmp-abc"):
            open(os.path.join(store.SERVERS_DIR, name), "w").close()
        open(os.path.join(store.INVENTORY_DIR, "12", "s.sbom.json.gz"), "w").close()
        open(os.path.join(store.INVENTORY_DIR, "stray.txt"), "w").close()

    def test_known_ids(self):
        self.assertEqual(store.known_state_ids(), [12, 13])
        self.assertEqual(repo.known_ids(), [12, 13])

    def test_forget_removes_the_three_files_and_the_folder_of_that_server_only(self):
        self.assertTrue(store.forget_state(12))
        self.assertTrue(repo.forget(12))
        self.assertEqual(sorted(os.listdir(store.SERVERS_DIR)), [".tmp-abc", "13.json", "notes.txt", "x.json"])
        self.assertEqual(sorted(os.listdir(store.INVENTORY_DIR)), ["13", "stray.txt"])
        self.assertFalse(store.forget_state(12))
        self.assertFalse(repo.forget(12))

    def test_forget_stays_inside_the_folders(self):
        # The id is turned into an int, so no path can come out of it.
        with self.assertRaises(ValueError):
            store.forget_state("../13")
        self.assertFalse(repo.forget(99))
        self.assertTrue(os.path.isdir(os.path.join(store.INVENTORY_DIR, "13")))


# ---------------------------------------------------------------- settings
class SettingsTests(unittest.TestCase):
    def test_defaults(self):
        self.assertIs(store.DEFAULT_SETTINGS["scan_on_provision"], False)
        self.assertEqual(store.DEFAULT_SETTINGS["rematch_workers"], 1)
        self.assertEqual(store.DEFAULT_SETTINGS["inventory_parallel"], 4)
        clean, errors = store.validate_settings({})
        self.assertEqual(errors, {})
        self.assertIs(clean["scan_on_provision"], False)
        self.assertEqual((clean["rematch_workers"], clean["inventory_parallel"]), (1, 4))

    def test_valid_values(self):
        clean, errors = store.validate_settings({"scan_on_provision": "on", "rematch_workers": "4",
                                                 "inventory_parallel": "10"})
        self.assertEqual(errors, {})
        self.assertIs(clean["scan_on_provision"], True)
        self.assertEqual((clean["rematch_workers"], clean["inventory_parallel"]), (4, 10))

    def test_invalid_values_are_errors_and_clamped(self):
        for key, bad, expect in (("rematch_workers", "0", 1), ("rematch_workers", "5", 4),
                                 ("rematch_workers", "two", 1), ("inventory_parallel", "11", 10),
                                 ("inventory_parallel", "-1", 1), ("inventory_parallel", "", 4)):
            clean, errors = store.validate_settings({key: bad})
            self.assertIn(key, errors, (key, bad))
            self.assertEqual(clean[key], expect, (key, bad))

    def test_non_ascii_digits_are_a_field_error_not_a_crash(self):
        # str.isdigit() is True for these, int() refuses the first two, and the Arabic-Indic digit is not ASCII.
        for key in ("scan_timeout_min", "rematch_workers", "inventory_parallel"):
            for bad in ("\u00b2", "\u00b9\u00b2", "\u0663", "1\u00b2"):
                clean, errors = store.validate_settings({key: bad})
                self.assertIn(key, errors, (key, bad))
                self.assertIsInstance(clean[key], int)

    def test_get_settings_fills_and_clamps(self):
        with mock.patch.object(store, "_read_json", return_value={"rematch_workers": 99, "inventory_parallel": "x",
                                                                 "scan_on_provision": 1}):
            result = store.get_settings()
        self.assertEqual(result["rematch_workers"], 4)
        self.assertEqual(result["inventory_parallel"], 4)
        self.assertIs(result["scan_on_provision"], True)
        with mock.patch.object(store, "_read_json", return_value={}):
            result = store.get_settings()
        self.assertEqual((result["rematch_workers"], result["inventory_parallel"], result["scan_on_provision"]),
                         (1, 4, False))

    def test_a_saved_match_limit_is_kept_and_the_message_names_the_new_label(self):
        with mock.patch.object(store, "_read_json", return_value={"rematch_workers": 3}):
            self.assertEqual(store.get_settings()["rematch_workers"], 3)
        clean, errors = store.validate_settings({"rematch_workers": "9"})
        self.assertEqual(errors["rematch_workers"], "Vulnerability matches at once must be a whole number from 1 to 4.")


# ---------------------------------------------------------------- the nightly re-match
class RematchTests(Base):
    BUILT = "2026-11-23T05:00:00Z"

    def setUp(self):
        super().setUp()
        self.replaced = []
        self.matched = []
        self.scan_state = {}      # server id -> the scan id server_summary reports now
        self.running = set()      # ids with a scan in progress
        self.listed = set()       # ids that have a stored software list
        self.pruned = []
        self.settings = {"rematch_workers": 1, "inventory_parallel": 2, "scan_on_provision": False,
                         "visibility": "linux"}
        self.job_status = "RUNNING"
        self.job = types.SimpleNamespace(id=500, set_progress=lambda *a, **k: self.progress.append(a))
        self.progress = []
        self.match_error = None

    def known(self):
        return [
            {"server_id": 1, "scan_id": "s1", "matched_at": "2026-11-23T01:00:00Z", "db_built": self.BUILT},   # current
            {"server_id": 2, "scan_id": "s2", "matched_at": "2026-11-20T01:00:00Z", "db_built": "old"},
            {"server_id": 3, "scan_id": "s3", "matched_at": "2026-11-19T01:00:00Z", "db_built": "old"},       # scanning
            {"server_id": 4, "scan_id": "s4", "matched_at": "2026-11-18T01:00:00Z", "db_built": "old"},       # no list
            {"server_id": 5, "scan_id": "s5", "matched_at": "2026-11-17T01:00:00Z", "db_built": "old"},       # gone
            {"server_id": 6, "scan_id": "s6", "matched_at": "2026-11-16T01:00:00Z", "db_built": "old"},       # unassigned
            {"server_id": 7, "scan_id": "s7", "matched_at": "2026-11-15T01:00:00Z", "db_built": None},        # changes
            {"server_id": 8, "scan_id": "s8", "matched_at": "2026-11-14T01:00:00Z", "db_built": "old"},       # historical
        ]

    def servers(self):
        return [
            make_server(id=i, hostname="h{}".format(i)) for i in (1, 2, 3, 4, 7)
        ] + [
            make_server(id=6, group=types.SimpleNamespace(is_unassigned=True, name="Unassigned")),
            make_server(id=8, status="HISTORICAL"),
        ]

    def fake_match(self, path, out):
        main_thread_only_ok = threading.current_thread() is not threading.main_thread()
        self.matched.append((os.path.basename(path), main_thread_only_ok))
        if self.match_error:
            raise self.match_error
        with open(out, "w", encoding="utf-8") as handle:
            json.dump({"matches": []}, handle)

    def summary(self, server_id):
        main_thread_only("server_summary")
        scan_id = self.scan_state.get(server_id, "s{}".format(server_id))
        return {"server_id": server_id, "scan_id": scan_id, "hostname": "old-name", "group_name": "g",
                "environment": "e", "packages": 10, "inventory_at": "2026-11-01T00:00:00Z",
                "matched_at": "2026-11-20T01:00:00Z", "db_built": "old", "os_name": "Amazon Linux"}

    def replace(self, row, parsed):
        main_thread_only("replace_server")
        self.replaced.append((dict(row), dict(parsed)))
        # What the real replace_server returns: the stored row plus "unchanged" (no match_hash column).
        return dict(row, unchanged=row["server_id"] == 2)

    def run_it(self, update_error=None):
        def inventory_path(server_id, scan_id):
            return "/lists/{}-{}.gz".format(server_id, scan_id) if server_id in self.listed else None

        def active_job(server_id, state=None):
            main_thread_only("active_job")
            return ({"job_id": 1}, object()) if server_id in self.running else None

        patches = [
            mock.patch.object(fleet, "Server", fake_server_model(self.servers())),
            mock.patch.object(fleet, "Job", fake_job_model(status=lambda: self.job_status)),
            mock.patch.object(fleet, "forget_stale", return_value={"forgotten": 0, "kept": 0}),
            mock.patch.object(tools, "status", return_value={"ready": True, "db": {"built": self.BUILT}}),
            mock.patch.object(tools, "update_database", side_effect=update_error, return_value={"updated": True, "built": self.BUILT}),
            # The nightly job also updates the Microsoft data; never let a test reach the network.
            mock.patch.object(tools, "update_microsoft", return_value={"updated": False, "built": ""}),
            mock.patch.object(tools, "match", side_effect=self.fake_match),
            mock.patch.object(store, "get_settings", return_value=self.settings),
            mock.patch.object(store, "start_lock", lambda server_id: contextlib.nullcontext()),
            mock.patch.object(store, "WORK_DIR", os.path.join(TMP, "work")),
            mock.patch.object(store, "ensure_dir", lambda path: os.makedirs(path, exist_ok=True)),
            mock.patch.object(repo, "inventory_path", side_effect=inventory_path),
            mock.patch.object(jobs, "active_job", side_effect=active_job),
            mock.patch.object(db, "known_servers", create=True, side_effect=self.known),
            mock.patch.object(db, "server_summary", create=True, side_effect=self.summary),
            mock.patch.object(db, "replace_server", create=True, side_effect=self.replace),
            mock.patch.object(db, "prune_orphans", create=True, side_effect=lambda: self.pruned.append("prune")),
            mock.patch.object(db, "rebuild_fleet", create=True, side_effect=lambda: self.pruned.append("rebuild")),
        ]
        with contextlib.ExitStack() as stack:
            for patcher in patches:
                stack.enter_context(patcher)
            return fleet.run_rematch(self.job)

    def test_selection_and_skips(self):
        self.listed = {2, 3, 6, 7, 8}
        self.running = {3}
        self.scan_state = {7: "s7-newer"}   # a scan finished between the selection and the save
        status, output, errors, outputs = self.run_it()
        self.assertEqual((status, errors), ("SUCCESS", ""))
        # Only server 2 was saved: 1 is current, 3 has a scan, 4 has no list, 5 is gone, 6 is unassigned,
        # 7 was scanned again meanwhile, 8 is historical.
        self.assertEqual([row["server_id"] for row, _ in self.replaced], [2])
        self.assertEqual(outputs["rematched"], 1)
        self.assertEqual(outputs["unchanged"], 1)
        self.assertEqual(outputs["failed"], 0)
        self.assertEqual(outputs["skipped"], {"changed": 1, "gone": 1, "historical": 1, "no_list": 1,
                                              "scanning": 1, "unassigned": 1})
        self.assertIn("Re-matched 1 server", output)
        self.assertIn(self.BUILT, output)
        self.assertEqual(self.pruned, ["prune", "rebuild"])
        self.assertEqual(VIOLATIONS, [])

    def test_the_saved_row_keeps_its_snapshot_and_takes_the_new_build_and_time(self):
        self.listed = {2}
        self.run_it()
        row, parsed = self.replaced[0]
        self.assertEqual(row["db_built"], self.BUILT)
        self.assertEqual(parsed["db_built"], self.BUILT)
        self.assertNotEqual(row["matched_at"], "2026-11-20T01:00:00Z")
        self.assertEqual(row["packages"], 10)
        self.assertEqual(row["inventory_at"], "2026-11-01T00:00:00Z")
        self.assertEqual(row["scan_id"], "s2")
        self.assertEqual(row["hostname"], "h2")   # the name the Server has now

    def test_the_scanner_runs_in_a_worker_thread_and_only_there(self):
        self.listed = {2, 7}
        self.run_it()
        self.assertTrue(self.matched)
        self.assertTrue(all(in_thread for _, in_thread in self.matched))
        self.assertEqual(VIOLATIONS, [])

    def test_oldest_match_first(self):
        self.listed = {2, 7, 4}
        self.run_it()
        self.assertEqual([name for name, _ in self.matched], ["7-s7.gz", "4-s4.gz", "2-s2.gz"])

    def test_a_failing_server_does_not_fail_the_job_but_all_failing_does(self):
        self.listed = {2, 7}
        self.match_error = tools.ToolError("The vulnerability match failed.")
        status, output, errors, outputs = self.run_it()
        self.assertEqual(status, "FAILURE")
        self.assertEqual(outputs["failed"], 2)
        self.assertEqual(self.replaced, [])

    def test_one_failure_among_others_is_a_success(self):
        self.listed = {2, 7}
        real = self.fake_match

        def flaky(path, out):
            if "7-" in path:
                raise tools.ToolError("The vulnerability match failed.")
            return real(path, out)

        self.fake_match = flaky
        status, output, errors, outputs = self.run_it()
        self.assertEqual(status, "SUCCESS")
        self.assertEqual((outputs["rematched"], outputs["failed"]), (1, 1))

    def test_cancel_stops_cleanly(self):
        self.listed = {2, 7}
        self.job_status = "TO_CANCEL"
        status, output, errors, outputs = self.run_it()
        self.assertEqual(status, "WARNING")
        self.assertIn("canceled", output)
        self.assertEqual(self.replaced, [])
        self.assertEqual(self.pruned, [])

    def test_unchanged_is_counted_from_what_replace_server_returns(self):
        self.listed = {2, 7}
        status, output, errors, outputs = self.run_it()
        self.assertEqual((outputs["rematched"], outputs["unchanged"]), (2, 1))
        self.assertIn("2 servers", output)
        self.assertIn("1 with no change in findings", output)

    def test_a_server_that_stops_qualifying_while_the_job_runs_is_dropped_before_it_starts(self):
        self.listed = {2, 7}
        servers = self.servers()
        by_id = {s.id: s for s in servers}
        real = self.fake_match

        def match(path, out):
            by_id[2].status = "HISTORICAL"      # while server 7 (the oldest) is being matched
            return real(path, out)

        with mock.patch.object(self, "servers", return_value=servers), mock.patch.object(self, "fake_match", match):
            status, output, errors, outputs = self.run_it()
        self.assertEqual([row["server_id"] for row, _ in self.replaced], [7])
        self.assertEqual(outputs["skipped"]["historical"], 2)      # server 8 from the start, server 2 later
        self.assertEqual(len([name for name, _ in self.matched]), 1)

    def test_a_server_that_stops_qualifying_during_its_match_is_not_saved(self):
        self.listed = {2}
        servers = self.servers()
        by_id = {s.id: s for s in servers}
        real = self.fake_match

        def match(path, out):
            by_id[2].group = types.SimpleNamespace(is_unassigned=True, name="Unassigned")
            return real(path, out)

        with mock.patch.object(self, "servers", return_value=servers), mock.patch.object(self, "fake_match", match):
            status, output, errors, outputs = self.run_it()
        self.assertEqual(self.replaced, [])
        self.assertEqual(outputs["rematched"], 0)
        self.assertEqual(outputs["skipped"]["unassigned"], 2)      # server 6 from the start, server 2 at the save

    def test_the_saved_names_come_from_the_fresh_row(self):
        self.listed = {2}
        servers = self.servers()
        by_id = {s.id: s for s in servers}
        real = self.fake_match

        def match(path, out):
            by_id[2].hostname = "renamed-meanwhile"
            return real(path, out)

        with mock.patch.object(self, "servers", return_value=servers), mock.patch.object(self, "fake_match", match):
            self.run_it()
        self.assertEqual(self.replaced[0][0]["hostname"], "renamed-meanwhile")

    def test_the_time_budget_stops_new_servers_but_finishes_the_cleanup(self):
        self.listed = {2, 7, 4}
        with mock.patch.object(fleet, "_clock", fake_clock(0, 0)):
            status, output, errors, outputs = self.run_it()
        # One server was matched (the oldest), then the budget ran out with two left.
        self.assertEqual([row["server_id"] for row, _ in self.replaced], [7])
        self.assertEqual(outputs["left"], 2)
        self.assertEqual(status, "WARNING")
        self.assertIn("2 servers left for the next run", output)
        self.assertEqual(self.pruned, ["prune", "rebuild"])

    def test_a_match_that_fails_because_a_scan_replaced_the_list_is_skipped_not_failed(self):
        self.listed = {2}
        self.scan_state = {}
        self.match_error = tools.ToolError("The stored software list could not be read.")
        real_summary = self.summary

        def summary(server_id):
            row = real_summary(server_id)
            if self.matched:                       # asked after the match ran: a scan finished meanwhile
                row["scan_id"] = "s2-newer"
            return row

        with mock.patch.object(self, "summary", summary):
            status, output, errors, outputs = self.run_it()
        self.assertEqual(outputs["failed"], 0)
        self.assertEqual(outputs["skipped"]["changed"], 1)
        self.assertEqual(status, "SUCCESS")

    def test_a_match_that_fails_while_a_scan_is_running_is_skipped_not_failed(self):
        self.listed = {2}
        self.match_error = tools.ToolError("The stored software list could not be read.")
        real = self.fake_match

        def match(path, out):
            self.running.add(2)                    # a scan started while the match ran
            return real(path, out)

        with mock.patch.object(self, "fake_match", match):
            status, output, errors, outputs = self.run_it()
        self.assertEqual((outputs["failed"], outputs["skipped"]["changed"]), (0, 1))

    def test_a_match_that_fails_for_another_reason_is_a_failure(self):
        self.listed = {2}
        self.match_error = tools.ToolError("The scanner produced no report.")
        status, output, errors, outputs = self.run_it()
        self.assertEqual(outputs["failed"], 1)

    def test_an_unreadable_store_ends_the_job_before_anything_else(self):
        self.listed = {2}
        self.upgrade.side_effect = OSError("disk is read-only")
        status, output, errors, outputs = self.run_it()
        self.assertEqual((status, output, outputs), ("FAILURE", "", {}))
        self.assertIn("findings store could not be opened", errors)
        self.assertEqual((self.replaced, self.matched, self.pruned, self.progress), ([], [], [], []))

    def test_not_set_up_is_refused(self):
        with mock.patch.object(tools, "status", return_value={"ready": False, "db": {"built": ""}}):
            status, output, errors, outputs = fleet.run_rematch(self.job)
        self.assertEqual(status, "FAILURE")
        self.assertIn("not set up", errors)

    def test_a_failed_database_update_is_reported_and_the_job_continues(self):
        self.listed = {2}
        status, output, errors, outputs = self.run_it(update_error=tools.ToolError("No route to the database."))
        self.assertEqual(status, "SUCCESS")
        self.assertIn("could not be updated", output)
        self.assertEqual(outputs["rematched"], 1)

    def test_an_unreadable_scan_state_counts_as_busy(self):
        self.listed = {2}

        def broken(server_id, state=None):
            raise store.StoreError("cannot read")

        with mock.patch.object(jobs, "active_job", side_effect=broken):
            self.assertTrue(fleet._scan_running(2))


# ---------------------------------------------------------------- the weekly inventory
class InventoryTests(Base):
    def setUp(self):
        super().setUp()
        self.job = types.SimpleNamespace(id=500, set_progress=lambda *a, **k: None)
        self.started = []
        self.in_flight = set()
        self.peak = 0
        self.status_of = {}
        self.settings = {"inventory_parallel": 2, "visibility": "linux", "rematch_workers": 1,
                         "scan_on_provision": False}
        self.refuse = set()
        self.old_refuse = set()
        self.explode = set()
        self.known = []
        self.servers = []
        self.after_start = None

    def fake_start(self, profile, server, base_url, parent_job=None):
        self.started.append((profile, server.id, base_url, parent_job))
        if self.after_start:
            self.after_start(server)
        if server.id in self.explode:
            raise RuntimeError("no route to build the upload links")
        if server.id in self.old_refuse:
            return OldRefusal("Job 1 is still running on this server; wait for it to finish.")
        if server.id in self.refuse:
            return jobs.Refusal("Job 1 is still running on this server; wait for it to finish.", 409)
        job_id = 1000 + server.id
        self.in_flight.add(job_id)
        self.peak = max(self.peak, len(self.in_flight))
        self.status_of[job_id] = "RUNNING"
        return types.SimpleNamespace(id=job_id)

    def fake_sleep(self, seconds):
        # Every scan in flight ends: the odd ones fail.
        for job_id in list(self.status_of):
            if self.status_of[job_id] == "RUNNING":
                self.status_of[job_id] = "FAILURE" if job_id % 2 else "SUCCESS"
                self.in_flight.discard(job_id)

    def run_it(self, servers, base_url="https://cb.example.com"):
        self.servers = servers
        with contextlib.ExitStack() as stack:
            for patcher in (
                mock.patch.object(fleet, "candidates", return_value=servers),
                mock.patch.object(fleet, "Server", fake_server_model(servers)),
                mock.patch.object(fleet, "Job", fake_job_model(statuses=lambda: dict(self.status_of))),
                mock.patch.object(fleet, "forget_stale", return_value={"forgotten": 2, "kept": 0}),
                mock.patch.object(fleet, "default_base_url", return_value=base_url),
                mock.patch.object(fleet.time, "sleep", side_effect=self.fake_sleep),
                mock.patch.object(tools, "status", return_value={"ready": True, "db": {"built": "b"}}),
                mock.patch.object(store, "get_settings", return_value=self.settings),
                mock.patch.object(jobs, "start", side_effect=self.fake_start),
                mock.patch.object(db, "known_servers", create=True, side_effect=lambda: self.known),
            ):
                stack.enter_context(patcher)
            return fleet.run_inventory(self.job)

    def test_scans_eligible_servers_in_parallel_up_to_the_setting_and_counts_the_skips(self):
        servers = [make_server(id=i, hostname="h{}".format(i)) for i in (1, 2, 3, 4, 5)]
        servers.append(make_server(id=6, get_preferred_ip=lambda: ""))                     # no address
        servers.append(make_server(id=7, resource_handler=None))                            # no script channel
        servers.append(make_server(id=8))                                                   # refused at start
        self.refuse = {8}
        self.known = [{"server_id": 5, "inventory_at": jobs.store.stamp()}]                 # a list from just now
        status, output, errors, outputs = self.run_it(servers)
        started_ids = [entry[1] for entry in self.started]
        self.assertEqual(started_ids, [1, 2, 3, 4, 8])
        self.assertEqual(self.peak, 2)
        # Every start is by the schedule (no profile), a child of the inventory job.
        self.assertTrue(all(entry[0] is None and entry[3] is self.job for entry in self.started))
        self.assertEqual(outputs["scanned"], 2)
        self.assertEqual(outputs["failed"], 2)
        self.assertEqual(outputs["skipped"], {"no_address": 1, "no_script_channel": 1, "recent": 1, "refused": 1})
        self.assertEqual(outputs["forgotten"], 2)
        self.assertEqual(status, "WARNING")      # two scanned, two failed
        self.assertIn("Scanned 2 servers", output)
        self.assertEqual(VIOLATIONS, [])

    def test_an_old_list_does_not_skip(self):
        self.known = [{"server_id": 1, "inventory_at": "2020-01-01T00:00:00Z"}]
        status, output, errors, outputs = self.run_it([make_server(id=2)])
        self.assertEqual([entry[1] for entry in self.started], [2])

    def test_the_oldest_software_list_goes_first_and_servers_with_none_before_that(self):
        self.settings["inventory_parallel"] = 10
        self.known = [{"server_id": 1, "inventory_at": "2020-03-01T00:00:00Z"},
                      {"server_id": 3, "inventory_at": "2020-01-01T00:00:00Z"},
                      {"server_id": 4, "inventory_at": None}]
        servers = [make_server(id=i) for i in (1, 2, 3, 4)]       # 2 has no stored row at all
        self.run_it(servers)
        self.assertEqual([entry[1] for entry in self.started], [2, 4, 3, 1])

    def test_a_server_that_stops_qualifying_before_its_turn_is_skipped_not_started(self):
        servers = [make_server(id=i) for i in (1, 2, 3)]
        self.settings["inventory_parallel"] = 1
        by_id = {s.id: s for s in servers}

        def after_start(server):
            if server.id == 1:
                by_id[2].power_status = "POWEROFF"      # hours later, while 1 was scanning
                by_id[3].status = "HISTORICAL"

        self.after_start = after_start
        status, output, errors, outputs = self.run_it(servers)
        self.assertEqual([entry[1] for entry in self.started], [1])
        self.assertEqual(outputs["skipped"], {"powered_off": 1, "historical": 1})

    def test_a_server_deleted_before_its_turn_is_skipped(self):
        servers = [make_server(id=i) for i in (1, 2)]
        self.settings["inventory_parallel"] = 1
        self.after_start = lambda server: servers.remove(servers[1]) if server.id == 1 else None
        status, output, errors, outputs = self.run_it(servers)
        self.assertEqual([entry[1] for entry in self.started], [1])
        self.assertEqual(outputs["skipped"], {"gone": 1})

    def test_a_refusal_from_an_older_class_is_still_a_refusal(self):
        self.old_refuse = {2}
        status, output, errors, outputs = self.run_it([make_server(id=1), make_server(id=2)])
        self.assertEqual(outputs["skipped"], {"refused": 1})
        self.assertEqual(outputs["scanned"] + outputs["failed"], 1)

    def test_one_failed_start_does_not_end_the_run(self):
        self.explode = {1}
        status, output, errors, outputs = self.run_it([make_server(id=i) for i in (1, 2, 3)])
        self.assertEqual([entry[1] for entry in self.started], [1, 2, 3])
        self.assertEqual(outputs["failed"], 2)        # server 1 (could not start) and server 3 (odd id: fails)
        self.assertEqual(outputs["scanned"], 1)
        self.assertEqual(status, "WARNING")

    def test_success_only_when_no_server_failed(self):
        status, output, errors, outputs = self.run_it([make_server(id=2), make_server(id=4)])    # even ids succeed
        self.assertEqual((status, outputs["failed"]), ("SUCCESS", 0))
        status, output, errors, outputs = self.run_it([make_server(id=2), make_server(id=3)])    # one fails
        self.assertEqual((status, outputs["scanned"], outputs["failed"]), ("WARNING", 1, 1))
        status, output, errors, outputs = self.run_it([make_server(id=3)])
        self.assertEqual(status, "FAILURE")

    def test_a_server_skipped_during_the_run_counts_as_done(self):
        progress = []
        self.job = types.SimpleNamespace(id=500, set_progress=lambda *a, **k: progress.append((a, k)))
        self.settings["inventory_parallel"] = 1
        servers = [make_server(id=i) for i in (2, 4, 6)]
        by_id = {s.id: s for s in servers}
        self.after_start = lambda server: setattr(by_id[4], "power_status", "POWEROFF") if server.id == 2 else None
        status, output, errors, outputs = self.run_it(servers)
        self.assertEqual(outputs["skipped"], {"powered_off": 1})
        self.assertEqual((outputs["scanned"], outputs["failed"]), (2, 0))
        counted = [(a[0], k["tasks_done"], k["total_tasks"]) for a, k in progress if "tasks_done" in k]
        self.assertEqual(counted[-1], ("3 of 3 servers done", 3, 3))
        self.assertEqual([c[1] for c in counted], sorted(set(c[1] for c in counted)))   # only when it changes
        self.assertEqual(status, "SUCCESS")

    def test_the_time_budget_stops_new_scans_and_waits_for_the_running_ones(self):
        servers = [make_server(id=i) for i in range(1, 7)]
        with mock.patch.object(fleet, "_clock", fake_clock(0, 0)):
            status, output, errors, outputs = self.run_it(servers)
        self.assertEqual([entry[1] for entry in self.started], [1, 2])
        self.assertEqual(outputs["left"], 4)
        self.assertEqual(outputs["scanned"] + outputs["failed"], 2)     # both were waited for
        self.assertEqual(status, "WARNING")
        self.assertIn("4 servers left for the next run", output)

    def test_an_unreadable_store_ends_the_job_before_anything_else(self):
        self.upgrade.side_effect = RuntimeError("database disk image is malformed")
        status, output, errors, outputs = self.run_it([make_server()])
        self.assertEqual((status, output, outputs), ("FAILURE", "", {}))
        self.assertIn("findings store could not be opened", errors)
        self.assertEqual(self.started, [])

    def test_refuses_without_a_site_url_or_scanner(self):
        status, output, errors, outputs = self.run_it([make_server()], base_url="")
        self.assertEqual(status, "FAILURE")
        self.assertEqual(self.started, [])
        with mock.patch.object(tools, "status", return_value={"ready": False, "db": {"built": ""}}):
            status, output, errors, outputs = fleet.run_inventory(self.job)
        self.assertEqual(status, "FAILURE")

    def test_cancel_stops_starting_new_scans(self):
        servers = [make_server(id=i) for i in (1, 2, 3, 4, 5, 6)]
        calls = {"n": 0}

        def status():
            calls["n"] += 1
            return "TO_CANCEL" if calls["n"] > 1 else "RUNNING"

        with contextlib.ExitStack() as stack:
            for patcher in (
                mock.patch.object(fleet, "candidates", return_value=servers),
                mock.patch.object(fleet, "Server", fake_server_model(servers)),
                mock.patch.object(fleet, "Job", fake_job_model(status=status, statuses=lambda: dict(self.status_of))),
                mock.patch.object(fleet, "forget_stale", return_value={"forgotten": 0, "kept": 0}),
                mock.patch.object(fleet, "default_base_url", return_value="https://cb"),
                mock.patch.object(fleet.time, "sleep", side_effect=self.fake_sleep),
                mock.patch.object(tools, "status", return_value={"ready": True, "db": {"built": "b"}}),
                mock.patch.object(store, "get_settings", return_value=self.settings),
                mock.patch.object(jobs, "start", side_effect=self.fake_start),
                mock.patch.object(db, "known_servers", create=True, return_value=[]),
            ):
                stack.enter_context(patcher)
            status_text, output, errors, outputs = fleet.run_inventory(self.job)
        self.assertEqual(status_text, "WARNING")
        self.assertEqual(len(self.started), 2)     # the first fill only; the next pass saw the cancel


# ---------------------------------------------------------------- scan after provisioning
class ProvisionTests(Base):
    def setUp(self):
        super().setUp()
        self.started = []
        self.settings = {"scan_on_provision": True, "visibility": "linux", "rematch_workers": 2,
                         "inventory_parallel": 4}
        self.owner = object()

    def job(self, status="SUCCESS", servers=()):
        return types.SimpleNamespace(
            id=9, status=status, owner=self.owner,
            server_set=types.SimpleNamespace(all=lambda: list(servers)))

    def run_it(self, job, ready=True, base_url="https://cb.example.com", **kwargs):
        def start(profile, server, url, parent_job=None):
            self.started.append((profile, server.id, url, parent_job))
            if server.id == 3:
                return jobs.Refusal("busy", 409)
            return types.SimpleNamespace(id=700 + server.id)

        with contextlib.ExitStack() as stack:
            for patcher in (
                mock.patch.object(store, "get_settings", return_value=self.settings),
                mock.patch.object(tools, "status", return_value={"ready": ready, "db": {"built": "b"}}),
                mock.patch.object(fleet, "default_base_url", return_value=base_url),
                mock.patch.object(jobs, "start", side_effect=start),
            ):
                stack.enter_context(patcher)
            return fleet.run_provision(job, **kwargs)

    def test_starts_an_independent_scan_of_each_eligible_server(self):
        servers = [make_server(id=1, hostname="a"), make_server(id=2, power_status="POWEROFF"),
                   make_server(id=3, hostname="c"), make_server(id=4, status="PROVFAILED")]
        status, output, errors, outputs = self.run_it(self.job(servers=servers))
        self.assertEqual((status, errors), ("SUCCESS", ""))
        # parent_job is None on purpose; the owner is the build's owner.
        self.assertEqual(self.started, [(self.owner, 1, "https://cb.example.com", None),
                                        (self.owner, 3, "https://cb.example.com", None)])
        self.assertIn("a (job 701)", output)
        self.assertIn("1 powered off", output)
        self.assertIn("1 not active", output)
        self.assertIn("1 refused to start", output)

    def test_a_refusal_from_an_older_class_is_still_a_refusal(self):
        def start(profile, server, url, parent_job=None):
            return OldRefusal("busy")

        with contextlib.ExitStack() as stack:
            for patcher in (
                mock.patch.object(store, "get_settings", return_value=self.settings),
                mock.patch.object(tools, "status", return_value={"ready": True, "db": {"built": "b"}}),
                mock.patch.object(fleet, "default_base_url", return_value="https://cb"),
                mock.patch.object(jobs, "start", side_effect=start),
            ):
                stack.enter_context(patcher)
            status, output, errors, outputs = fleet.run_provision(self.job(servers=[make_server(id=1)]))
        self.assertIn("1 refused to start", output)

    def test_servers_in_kwargs_win(self):
        status, output, errors, outputs = self.run_it(self.job(servers=[make_server(id=1)]),
                                                      servers=[make_server(id=5)])
        self.assertEqual([entry[1] for entry in self.started], [5])

    def test_nothing_starts_after_a_failed_or_cancelled_build_when_off_or_not_set_up(self):
        for status_text in ("FAILURE", "CANCELED", "TO_CANCEL"):
            self.run_it(self.job(status=status_text, servers=[make_server(id=1)]))
        self.settings["scan_on_provision"] = False
        self.run_it(self.job(servers=[make_server(id=1)]))
        self.settings["scan_on_provision"] = True
        self.run_it(self.job(servers=[make_server(id=1)]), ready=False)
        self.run_it(self.job(servers=[make_server(id=1)]), base_url="")
        self.assertEqual(self.started, [])

    def test_always_success_and_never_raises(self):
        with mock.patch.object(store, "get_settings", side_effect=RuntimeError("disk gone")):
            status, output, errors, outputs = fleet.run_provision(self.job(servers=[make_server()]))
        self.assertEqual((status, errors, outputs), ("SUCCESS", "", {}))
        status, output, errors, outputs = fleet.run_provision(None)
        self.assertEqual(status, "SUCCESS")

    def test_a_cancel_still_propagates(self):
        with mock.patch.object(store, "get_settings", side_effect=CancelJob()):
            with self.assertRaises(CancelJob):
                fleet.run_provision(self.job(servers=[make_server()]))

    def test_a_failing_start_does_not_fail_the_hook(self):
        with contextlib.ExitStack() as stack:
            for patcher in (
                mock.patch.object(store, "get_settings", return_value=self.settings),
                mock.patch.object(tools, "status", return_value={"ready": True, "db": {"built": "b"}}),
                mock.patch.object(fleet, "default_base_url", return_value="https://cb"),
                mock.patch.object(jobs, "start", side_effect=RuntimeError("queue is down")),
            ):
                stack.enter_context(patcher)
            status, output, errors, outputs = fleet.run_provision(self.job(servers=[make_server()]))
        self.assertEqual((status, errors), ("SUCCESS", ""))


# ---------------------------------------------------------------- vuln_jobs.start with no profile
class StartWithoutProfileTests(Base):
    def test_requested_by_text(self):
        self.assertEqual(jobs._requested_by(None), "schedule")
        profile = types.SimpleNamespace(user=types.SimpleNamespace(username="alice"))
        self.assertEqual(jobs._requested_by(profile), "alice")

    def test_start_and_queue_work_for_a_schedule(self):
        queued = {}

        class Hook:
            def run_as_job(self, **kw):
                queued.update(kw)
                return [types.SimpleNamespace(id=42, save=lambda **k: None)]

        marker = {}
        server = make_server(jobs=types.SimpleNamespace(add=lambda j: None), save=None)
        with contextlib.ExitStack() as stack:
            for patcher in (
                mock.patch.object(tools, "status", return_value={"ready": True, "db": {"built": "b"}}),
                mock.patch.object(store, "job_settings", return_value={"timeout_min": 20}),
                mock.patch.object(store, "start_lock", lambda server_id: contextlib.nullcontext()),
                mock.patch.object(store, "read_state", return_value={"active": None, "last": None, "history": []}),
                mock.patch.object(store, "set_active", side_effect=lambda server_id, m: marker.update(m)),
                mock.patch.object(repo, "targets", return_value={"upload_url": "u", "tools": {}, "token": "t"}),
                mock.patch.object(repo, "prepare"),
                mock.patch.object(hooks, "scan_hook", return_value=Hook()),
            ):
                stack.enter_context(patcher)
            created = jobs.start(None, server, "https://cb.example.com", parent_job="parent")
        self.assertEqual(created.id, 42)
        self.assertIsNone(queued["owner"])
        self.assertEqual(queued["parent_job"], "parent")
        self.assertEqual(queued["context"]["vuln_requested_by"], "schedule")
        self.assertEqual(marker["requested_by"], "schedule")

    def test_start_refuses_a_server_that_is_not_active(self):
        for status in ("PROVFAILED", "DECOMMISSIONING"):
            refused = jobs.start(None, make_server(status=status), "https://cb.example.com")
            self.assertIsInstance(refused, jobs.Refusal, status)
            self.assertEqual((refused.message, refused.status), (jobs.NOT_ACTIVE, 409))
        self.assertEqual(jobs.start(None, make_server(status="HISTORICAL"), "https://cb").message,
                         "This server is historical.")

    def test_a_scan_no_longer_prunes_the_findings_store(self):
        with open(os.path.join(EXT, "io_cloudbolt_vulnerabilities", "vuln_jobs.py"), encoding="utf-8") as handle:
            text = handle.read()
        self.assertNotIn("prune_orphans()", text)


# ---------------------------------------------------------------- recurring jobs and the hook point action
class FakeHook:
    rows = []
    next_id = 100

    class objects:
        @staticmethod
        def filter(name):
            found = [r for r in FakeHook.rows if r.name == name]
            return FakeQuery(found)

    type_slug = "plugin"

    def __init__(self, name="", description="", shared=False):
        self.name, self.description, self.shared, self.module_file = name, description, shared, ""
        self.pk = None

    def save(self):
        if self not in FakeHook.rows:
            FakeHook.next_id += 1
            self.pk = FakeHook.next_id
            FakeHook.rows.append(self)


class FakeRecurring:
    rows = []
    created = []
    race_with = None   # a row another request makes between this one's read and its insert

    class objects:
        @staticmethod
        def filter(name):
            return FakeQuery([r for r in FakeRecurring.rows if r.name == name])

        @staticmethod
        def create(**kw):
            FakeRecurring.created.append(dict(kw))
            if FakeRecurring.race_with is not None:
                FakeRecurring.rows.append(FakeRecurring.race_with)
                FakeRecurring.race_with = None
                raise IntegrityError("duplicate name")
            row = FakeRecurring(**kw)
            FakeRecurring.rows.append(row)
            return row

    def __init__(self, hook=None, name="", type="", owner=None, enabled=False, schedule="", description=""):
        self.hook, self.hook_id = hook, hook.pk
        self.name, self.type, self.owner, self.enabled = name, type, owner, enabled
        self.schedule, self.description = schedule, description
        self.last_run = None
        self.id = len(FakeRecurring.rows) + 1
        self.saved = []

    def save(self, update_fields=None):
        self.saved.append(update_fields)
        if "hook" in (update_fields or []):
            self.hook_id = self.hook.pk

    def get_absolute_url(self):
        return "/jobs/recurring/{}/".format(self.id)


class FakeHookPoint:
    rows = [types.SimpleNamespace(name="post_provision")]

    class objects:
        @staticmethod
        def filter(name):
            return FakeQuery([r for r in FakeHookPoint.rows if r.name == name])


class FakeAction:
    rows = []

    class objects:
        @staticmethod
        def filter(hook=None, hook_point__name=None):
            return FakeQuery([r for r in FakeAction.rows if r.hook is hook and r.hook_point.name == hook_point__name])

        @staticmethod
        def create(**kw):
            row = FakeAction(**kw)
            FakeAction.rows.append(row)
            return row

    def __init__(self, **kw):
        self.__dict__.update(kw)
        self.saves = 0

    def save(self):
        self.saves += 1


class ScheduleTests(Base):
    def setUp(self):
        super().setUp()
        FakeHook.rows, FakeRecurring.rows, FakeRecurring.created, FakeRecurring.race_with = [], [], [], None
        FakeAction.rows = []
        models = STUBS["cbhooks.models"]
        for name, value in (("RecurringActionJob", FakeRecurring), ("HookPoint", FakeHookPoint),
                            ("HookPointAction", FakeAction)):
            patcher = mock.patch.object(models, name, value, create=True)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = mock.patch.object(hooks, "CloudBoltHook", FakeHook)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_creates_two_disabled_rows_once(self):
        found = hooks.ensure_schedules()
        self.assertEqual([f["key"] for f in found], ["rematch", "inventory"])
        self.assertEqual([f["name"] for f in found], ["Vulnerability Nightly Re-match", "Vulnerability Weekly Inventory"])
        self.assertEqual([f["schedule"] for f in found], ["0 3 * * *", "0 1 * * 0"])
        self.assertEqual([f["enabled"] for f in found], [False, False])
        self.assertTrue(all(f["url"].startswith("/jobs/recurring/") and f["last_run"] is None for f in found))
        self.assertEqual(len(FakeRecurring.created), 2)
        for kwargs in FakeRecurring.created:
            self.assertIs(kwargs["enabled"], False)       # passed explicitly, not left to a default
            self.assertIsNone(kwargs["owner"])
            self.assertEqual(kwargs["type"], "plugin")
            self.assertTrue(kwargs["description"].endswith("."))
        again = hooks.ensure_schedules()
        self.assertEqual(len(FakeRecurring.created), 2)
        self.assertEqual(again, found)
        self.assertEqual(sorted(h.name for h in FakeHook.rows),
                         ["Vulnerability Nightly Re-match", "Vulnerability Weekly Inventory"])

    def test_an_existing_row_keeps_its_schedule_and_enabled_flag(self):
        hooks.ensure_schedules()
        rematch, inventory = FakeRecurring.rows
        rematch.schedule, rematch.enabled = "30 4 * * 1", True
        inventory.enabled = True
        found = hooks.ensure_schedules()
        self.assertEqual((rematch.schedule, rematch.enabled), ("30 4 * * 1", True))
        self.assertEqual((inventory.schedule, inventory.enabled), ("0 1 * * 0", True))
        self.assertEqual((found[0]["schedule"], found[0]["enabled"]), ("30 4 * * 1", True))
        self.assertEqual(rematch.saved, [])       # nothing was written to a row that already pointed at its plugin

    def test_a_row_is_repointed_at_its_plugin_and_nothing_else_changes(self):
        hooks.ensure_schedules()
        rematch = FakeRecurring.rows[0]
        rematch.schedule, rematch.enabled = "1 1 * * *", True
        rematch.hook_id = 999
        hooks.ensure_schedules()
        self.assertEqual(rematch.saved, [["hook"]])
        self.assertEqual(rematch.hook_id, rematch.hook.pk)
        self.assertEqual((rematch.schedule, rematch.enabled), ("1 1 * * *", True))

    def test_losing_the_creation_race_reads_the_winner(self):
        winner_hook = hooks.rematch_hook()
        FakeRecurring.race_with = FakeRecurring(hook=winner_hook, name="Vulnerability Nightly Re-match", enabled=True,
                                                schedule="9 9 * * *")
        FakeRecurring.race_with.id = 77
        found = hooks.ensure_schedules()
        self.assertEqual((found[0]["schedule"], found[0]["enabled"], found[0]["url"]),
                         ("9 9 * * *", True, "/jobs/recurring/77/"))

    def test_it_never_raises(self):
        with mock.patch.object(FakeRecurring.objects, "filter", side_effect=RuntimeError("database is gone")):
            found = hooks.ensure_schedules()
        self.assertEqual(len(found), 2)
        self.assertTrue(all(f["error"] and f["url"] == "" and f["enabled"] is False for f in found))

    def test_apply_provision_setting_off_makes_nothing(self):
        hooks.apply_provision_setting(False)
        self.assertEqual((FakeHook.rows, FakeAction.rows), ([], []))
        self.assertEqual(hooks.provision_action_state(), {"exists": False, "enabled": False})
        # Even with the plugin present, off never makes an action.
        hooks.provision_hook()
        hooks.apply_provision_setting(False)
        self.assertEqual(FakeAction.rows, [])

    def test_apply_provision_setting_on_makes_one_action_then_follows_the_setting(self):
        hooks.apply_provision_setting(True)
        self.assertEqual(len(FakeAction.rows), 1)
        action = FakeAction.rows[0]
        self.assertEqual((action.enabled, action.continue_on_failure, action.run_on_statuses), (True, True, ""))
        self.assertEqual(action.hook_point.name, "post_provision")
        self.assertTrue(action.name and action.description)
        self.assertEqual(hooks.provision_action_state(), {"exists": True, "enabled": True})
        hooks.apply_provision_setting(True)
        self.assertEqual(len(FakeAction.rows), 1)
        hooks.apply_provision_setting(False)
        self.assertEqual(len(FakeAction.rows), 1)
        self.assertFalse(action.enabled)
        self.assertEqual(hooks.provision_action_state(), {"exists": True, "enabled": False})
        hooks.apply_provision_setting(True)
        self.assertEqual(len(FakeAction.rows), 1)
        self.assertTrue(action.enabled)

    def test_provision_action_state_reads_without_making_anything(self):
        hooks.provision_action_state()
        self.assertEqual((FakeHook.rows, FakeAction.rows, FakeRecurring.rows), ([], [], []))
        with mock.patch.object(FakeHook.objects, "filter", side_effect=RuntimeError("no")):
            self.assertEqual(hooks.provision_action_state(), {"exists": False, "enabled": False})

    def test_a_missing_hook_point_is_an_error_not_a_made_up_one(self):
        with mock.patch.object(FakeHookPoint, "rows", []):
            with self.assertRaises(RuntimeError):
                hooks.apply_provision_setting(True)
        self.assertEqual(FakeAction.rows, [])


class DefaultBaseUrlTests(Base):
    def test_url_without_a_trailing_slash_or_empty(self):
        models = STUBS["portals.models"]
        portal = types.SimpleNamespace(site_url="https://cb.example.com/")
        seen = []
        models.PortalConfig = types.SimpleNamespace(get_current_portal=lambda request=None: seen.append(request) or portal)
        self.assertEqual(fleet.default_base_url(), "https://cb.example.com")
        self.assertEqual(seen, [None])
        portal.site_url = None
        self.assertEqual(fleet.default_base_url(), "")
        models.PortalConfig = types.SimpleNamespace(get_current_portal=lambda request=None: None)
        self.assertEqual(fleet.default_base_url(), "")


# ---------------------------------------------------------------- the per-resource scan marker
class ResourceMarkerFileTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="vuln-resource-")
        for patcher in (mock.patch.object(store, "RESOURCES_DIR", os.path.join(self.root, "resources")),
                        mock.patch.object(store, "own", lambda *a, **k: None)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_round_trip_and_ids_are_forced_to_int(self):
        self.assertIsNone(store.read_resource_run(5))
        marker = {"job_id": 9, "started": "2026-11-23T10:00:00Z", "requested_by": "alice", "server_ids": [1, 2]}
        store.set_resource_run(5, marker)
        self.assertEqual(store.read_resource_run("5"), marker)
        self.assertIn("5.json", os.listdir(store.RESOURCES_DIR))
        with self.assertRaises(ValueError):
            store.read_resource_run("../5")
        with self.assertRaises(ValueError):
            store.set_resource_run("5/../../x", marker)
        with self.assertRaises(ValueError):
            store.clear_resource_run("x")
        with self.assertRaises(ValueError):
            store.resource_lock("../1")

    def test_clear_with_a_job_id_clears_only_that_jobs_marker(self):
        store.set_resource_run(5, {"job_id": 9})
        self.assertFalse(store.clear_resource_run(5, job_id=10))
        self.assertEqual(store.read_resource_run(5), {"job_id": 9})
        self.assertTrue(store.clear_resource_run(5, job_id=9))
        self.assertIsNone(store.read_resource_run(5))
        self.assertFalse(store.clear_resource_run(5, job_id=9))
        self.assertFalse(store.clear_resource_run(5))

    def test_clear_without_a_job_id_clears_any_marker(self):
        store.set_resource_run(5, {"job_id": 9})
        self.assertTrue(store.clear_resource_run(5))
        self.assertIsNone(store.read_resource_run(5))

    def test_a_damaged_or_non_dict_marker_reads_as_none(self):
        os.makedirs(store.RESOURCES_DIR)
        with open(os.path.join(store.RESOURCES_DIR, "5.json"), "w") as handle:
            handle.write("{not json")
        self.assertIsNone(store.read_resource_run(5))
        with open(os.path.join(store.RESOURCES_DIR, "6.json"), "w") as handle:
            handle.write("[1, 2]")
        self.assertIsNone(store.read_resource_run(6))

    def test_resources_are_independent(self):
        store.set_resource_run(5, {"job_id": 1})
        store.set_resource_run(6, {"job_id": 2})
        store.clear_resource_run(5)
        self.assertEqual(store.read_resource_run(6), {"job_id": 2})


class ResourceHookTests(Base):
    def test_the_plugin_points_at_the_resource_job_file(self):
        FakeHook.rows = []
        with mock.patch.object(hooks, "CloudBoltHook", FakeHook):
            hook = hooks.resource_scan_hook()
            again = hooks.resource_scan_hook()
        self.assertIs(hook, again)
        self.assertEqual(hook.name, "Vulnerability Resource Scan")
        self.assertEqual(os.path.basename(hook.module_file), "vuln_resource_job.py")
        self.assertTrue(os.path.isabs(hook.module_file))
        self.assertEqual(len(FakeHook.rows), 1)
        FakeHook.rows = []


# ---------------------------------------------------------------- scan all servers of a resource
class FakeServerSet:
    """resource.server_set: exclude(status=...) filters, order_by sorts, the loading hints are ignored."""

    def __init__(self, rows):
        self.rows = list(rows)

    def exclude(self, **kw):
        return FakeServerSet([r for r in self.rows if getattr(r, "status", None) != kw.get("status")])

    def select_related(self, *a):
        return self

    def prefetch_related(self, *a):
        return self

    def order_by(self, *fields):
        return FakeServerSet(sorted(self.rows, key=lambda r: tuple(getattr(r, f) for f in fields)))

    def __iter__(self):
        return iter(self.rows)


class FakeJobTable:
    """vuln_jobs.Job: the jobs the markers can name. A job that is not in the table is gone."""

    def __init__(self):
        self.live = {}

    def add(self, job_id, active=True, done=None, total=None):
        self.live[job_id] = types.SimpleNamespace(
            id=job_id, is_active=lambda: active, tasks_done=done, total_tasks=total,
            get_absolute_url=lambda: "/jobs/{}/".format(job_id))

    def model(self):
        table = self

        class Manager:
            def filter(self, **kw):
                found = table.live.get(kw.get("id"))
                return FakeQuery([found] if found else [])

        return types.SimpleNamespace(objects=Manager())


class ResourceBase(Base):
    RESOURCE_ID = 5

    def setUp(self):
        super().setUp()
        self.root = tempfile.mkdtemp(prefix="vuln-resource-")
        self.table = FakeJobTable()
        self.denied_scan = set()
        self.hidden = set()
        self.owner = types.SimpleNamespace(
            id=77, user=types.SimpleNamespace(is_active=True, username="alice"), is_cbadmin=False,
            has_permission=lambda perm, server: server.id not in self.denied_scan)
        for patcher in (mock.patch.object(store, "RESOURCES_DIR", os.path.join(self.root, "resources")),
                        mock.patch.object(store, "own", lambda *a, **k: None),
                        mock.patch.object(jobs, "Job", self.table.model()),
                        mock.patch.object(jobs, "can_view", lambda profile, server: server.id not in self.hidden),
                        mock.patch.object(tools, "status", return_value={"ready": True, "db": {"built": "b"}})):
            patcher.start()
            self.addCleanup(patcher.stop)

    def resource(self, servers, jobs_added=None):
        added = [] if jobs_added is None else jobs_added
        return types.SimpleNamespace(id=self.RESOURCE_ID, name="Web tier", server_set=FakeServerSet(servers),
                                     jobs=types.SimpleNamespace(add=added.append))


class ScannableIdsTests(ResourceBase):
    def run_it(self, servers, visibility="linux"):
        viewable = [s for s in servers if s.id not in self.hidden]
        queried = []

        class Query:
            def filter(self_inner, **kw):
                queried.append(kw)
                return FakeQuery(viewable)

        model = types.SimpleNamespace(objects_for_profile=lambda profile: queried.append(profile) or Query())
        with mock.patch.object(fleet, "Server", model):
            found = fleet.scannable_ids(self.owner, self.resource(servers), visibility)
        return found, queried

    def test_only_servers_the_profile_may_view_and_scan_that_pass_eligibility(self):
        servers = [
            make_server(id=1, hostname="a-fine"),
            make_server(id=2, hostname="b-hidden"),
            make_server(id=3, hostname="c-no-scan"),
            make_server(id=4, hostname="d-unassigned", group=types.SimpleNamespace(is_unassigned=True, name="Unassigned")),
            make_server(id=5, hostname="e-off", power_status="POWEROFF"),
            make_server(id=6, hostname="f-fine"),
            make_server(id=7, hostname="g-historical", status="HISTORICAL"),
        ]
        self.hidden = {2}
        self.denied_scan = {3}
        found, queried = self.run_it(servers)
        self.assertEqual(found, [1, 6])
        # One objects_for_profile query for the whole resource, whatever the number of servers.
        self.assertEqual(len([q for q in queried if q is self.owner]), 1)
        self.assertEqual(len(queried), 2)       # the profile, then the one filter(resource=...)

    def test_order_is_hostname_then_id(self):
        servers = [make_server(id=3, hostname="b"), make_server(id=2, hostname="b"), make_server(id=1, hostname="c"),
                   make_server(id=9, hostname="a")]
        found, _ = self.run_it(servers)
        self.assertEqual(found, [9, 2, 3, 1])

    def test_a_cb_admin_may_scan_everything_they_can_view(self):
        self.owner.is_cbadmin = True
        self.denied_scan = {1}
        found, _ = self.run_it([make_server(id=1)])
        self.assertEqual(found, [1])

    def test_an_unassigned_server_is_never_scannable_even_for_an_admin(self):
        self.owner.is_cbadmin = True
        server = make_server(id=1, group=types.SimpleNamespace(is_unassigned=True, name="Unassigned"))
        self.assertEqual(self.run_it([server])[0], [])

    def test_label_visibility_hides_unlabelled_servers(self):
        self.assertEqual(self.run_it([make_server(id=1)], visibility="label")[0], [])

    def test_empty_resource(self):
        self.assertEqual(self.run_it([])[0], [])


class AnyScannableTests(ResourceBase):
    def run_it(self, servers, visibility="linux", fail=False):
        viewable = [s for s in servers if s.id not in self.hidden]
        queried = []

        class Query:
            def filter(self_inner, **kw):
                if fail:
                    raise RuntimeError("database is gone")
                queried.append(kw)
                return FakeQuery(viewable)

        model = types.SimpleNamespace(objects_for_profile=lambda profile: queried.append(profile) or Query())
        with mock.patch.object(fleet, "Server", model):
            found = fleet.any_scannable(self.owner, self.resource(servers), visibility)
        return found, queried

    def test_true_when_one_server_is_viewable_scannable_and_eligible(self):
        servers = [make_server(id=1, hostname="a-hidden"), make_server(id=2, hostname="b-no-scan"),
                   make_server(id=3, hostname="c-off", power_status="POWEROFF"), make_server(id=4, hostname="d-fine")]
        self.hidden, self.denied_scan = {1}, {2}
        found, queried = self.run_it(servers)
        self.assertIs(found, True)
        self.assertEqual(len(queried), 2)       # the same one objects_for_profile query as scannable_ids

    def test_false_when_none_qualifies(self):
        self.hidden = {1}
        self.assertIs(self.run_it([make_server(id=1)])[0], False)
        self.assertIs(self.run_it([])[0], False)
        self.assertIs(self.run_it([make_server(id=2, status="HISTORICAL")])[0], False)

    def test_stops_at_the_first_hit(self):
        asked = []
        servers = [make_server(id=i, hostname="h{}".format(i)) for i in (1, 2, 3)]
        with mock.patch.object(jobs, "can_scan", lambda profile, server: asked.append(server.id) or True):
            self.assertIs(self.run_it(servers)[0], True)
        self.assertEqual(asked, [1])

    def test_agrees_with_scannable_ids_and_never_raises(self):
        self.denied_scan = {1}
        servers = [make_server(id=1)]
        self.assertIs(self.run_it(servers)[0], bool(ScannableIdsTests.run_it(self, servers)[0]))
        self.assertIs(self.run_it(servers, fail=True)[0], False)


class StartResourceScanTests(ResourceBase):
    def setUp(self):
        super().setUp()
        self.queued = []
        self.added = []
        self.saved = []
        self.hook_result = "job"
        patcher = mock.patch.object(hooks, "resource_scan_hook", return_value=self)
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_as_job(self, **kw):
        self.queued.append(kw)
        if self.hook_result is None:
            return []
        job = types.SimpleNamespace(id=77, save=lambda **k: self.saved.append(k))
        return [job]

    def start(self, ids=(1, 2), base_url="https://cb.example.com", resource=None):
        return fleet.start_resource_scan(self.owner, resource or self.resource([], self.added), list(ids), base_url)

    def test_a_started_scan_is_one_parent_job_with_a_marker(self):
        job = self.start([2, 1, 2, "3"])
        self.assertEqual(job.id, 77)
        self.assertEqual(len(self.queued), 1)
        queued = self.queued[0]
        self.assertIs(queued["owner"], self.owner)
        self.assertIsNone(queued["parent_job"])
        self.assertEqual(queued["context"]["vuln_resource_id"], 5)
        self.assertEqual(queued["context"]["vuln_server_ids"], [2, 1, 3])
        self.assertEqual(queued["context"]["vuln_requested_by"], jobs._requested_by(self.owner))
        self.assertEqual(job.label, "Vulnerability scan: Web tier")
        self.assertEqual(self.saved, [{"update_fields": ["label"]}])
        self.assertEqual(self.added, [job])
        marker = store.read_resource_run(5)
        self.assertEqual((marker["job_id"], marker["server_ids"]), (77, [2, 1, 3]))
        self.assertTrue(marker["started"] and marker["requested_by"])

    def test_nothing_to_scan_is_a_400_and_starts_nothing(self):
        for ids in ([], [0, -1, "x", None, True]):
            refusal = self.start(ids)
            self.assertEqual(refusal.status, 400)
        self.assertEqual(self.queued, [])

    def test_a_run_in_progress_is_a_409(self):
        store.set_resource_run(5, {"job_id": 40})
        self.table.add(40, active=True)
        refusal = self.start()
        self.assertEqual(refusal.status, 409)
        self.assertIn("40", refusal.message)
        self.assertEqual(self.queued, [])

    def test_a_stale_marker_does_not_block(self):
        store.set_resource_run(5, {"job_id": 40})          # its job is gone
        self.assertEqual(self.start().id, 77)
        store.set_resource_run(5, {"job_id": 41})
        self.table.add(41, active=False)                   # its job finished
        self.assertEqual(self.start().id, 77)
        self.assertEqual(store.read_resource_run(5)["job_id"], 77)

    def test_scanner_not_set_up_and_no_site_url_are_409(self):
        with mock.patch.object(tools, "status", return_value={"ready": False, "db": {"built": ""}}):
            self.assertEqual(self.start().status, 409)
        refusal = self.start(base_url="")
        self.assertEqual(refusal.status, 409)
        self.assertEqual(refusal.message, jobs.NO_SITE_URL)
        self.assertEqual(self.queued, [])
        self.assertIsNone(store.read_resource_run(5))

    def test_the_hook_returning_no_job_is_a_500(self):
        self.hook_result = None
        self.assertEqual(self.start().status, 500)
        self.assertIsNone(store.read_resource_run(5))

    def test_a_failed_link_or_marker_write_never_loses_the_job(self):
        def boom(job):
            raise RuntimeError("m2m is gone")

        resource = self.resource([])
        resource.jobs = types.SimpleNamespace(add=boom)
        with mock.patch.object(store, "set_resource_run", side_effect=OSError("disk is read-only")):
            job = self.start(resource=resource)
        self.assertEqual(job.id, 77)

    def test_an_unreadable_marker_is_a_500_not_an_exception(self):
        with mock.patch.object(store, "read_resource_run", side_effect=store.StoreError("cannot read")):
            refusal = self.start()
        self.assertEqual(refusal.status, 500)
        self.assertEqual(self.queued, [])

    def test_the_start_runs_under_the_resource_lock(self):
        events = []

        @contextlib.contextmanager
        def lock(resource_id):
            events.append(("lock", resource_id))
            yield
            events.append(("unlock", resource_id))

        original = self.run_as_job

        def run_as_job(**kw):
            events.append("queued")
            return original(**kw)

        self.run_as_job = run_as_job
        with mock.patch.object(store, "resource_lock", lock):
            self.start()
        self.assertEqual(events, [("lock", 5), "queued", ("unlock", 5)])


class ResourceRunTests(ResourceBase):
    def test_no_marker_is_no_run(self):
        self.assertIsNone(fleet.resource_run(5))

    def test_a_live_run_reports_the_jobs_progress(self):
        store.set_resource_run(5, {"job_id": 40, "started": "2026-11-23T10:00:00Z", "server_ids": [1, 2, 3]})
        self.table.add(40, done=2, total=3)
        self.assertEqual(fleet.resource_run(5), {"job_id": 40, "job_url": "/jobs/40/",
                                                 "started": "2026-11-23T10:00:00Z", "done": 2, "total": 3})

    def test_before_the_job_reports_anything_the_total_comes_from_the_marker(self):
        store.set_resource_run(5, {"job_id": 40, "started": "t", "server_ids": [1, 2, 3]})
        self.table.add(40)
        run = fleet.resource_run(5)
        self.assertEqual((run["done"], run["total"]), (0, 3))

    def test_a_marker_whose_job_is_gone_or_finished_is_ignored(self):
        store.set_resource_run(5, {"job_id": 40})
        self.assertIsNone(fleet.resource_run(5))
        self.table.add(40, active=False)
        self.assertIsNone(fleet.resource_run(5))

    def test_a_marker_without_a_job_id_or_not_a_dict_is_ignored(self):
        store.set_resource_run(5, {"started": "t"})
        self.assertIsNone(fleet.resource_run(5))

    def test_it_never_raises(self):
        with mock.patch.object(store, "read_resource_run", side_effect=store.StoreError("cannot read")):
            self.assertIsNone(fleet.resource_run(5))
        store.set_resource_run(5, {"job_id": 40})
        self.table.add(40)
        self.table.live[40].get_absolute_url = lambda: 1 / 0
        self.assertIsNone(fleet.resource_run(5))


class RunResourceScanTests(ResourceBase):
    def setUp(self):
        super().setUp()
        self.job = types.SimpleNamespace(id=500, owner=self.owner, set_progress=lambda *a, **k: self.progress.append((a, k)))
        self.progress = []
        self.started = []
        self.markers_seen = []
        self.in_flight = set()
        self.peak = 0
        self.status_of = {}
        self.settings = {"inventory_parallel": 2, "visibility": "linux", "rematch_workers": 1,
                         "scan_on_provision": False}
        self.refuse = set()
        self.old_refuse = set()
        self.explode = set()
        self.cancel_on = set()
        self.after_start = None
        self.profile_queries = []
        self.fresh = {77: self.owner}   # what the database holds per profile id; a missing key is a deleted profile
        self.table.add(500)            # the job itself is active while it runs
        self.job_status = "RUNNING"

    def servers(self, ids=(1, 2, 3, 4, 5, 6)):
        return [make_server(id=i, hostname="h{}".format(i), resource_id=5) for i in ids]

    def fake_start(self, profile, server, base_url, parent_job=None):
        self.started.append((profile, server.id, base_url, parent_job))
        self.markers_seen.append(store.read_resource_run(5))
        if self.after_start:
            self.after_start(server)
        if server.id in self.cancel_on:
            raise CancelJob()
        if server.id in self.explode:
            raise RuntimeError("no route to build the upload links")
        if server.id in self.old_refuse:
            return OldRefusal("Job 1 is still running on this server; wait for it to finish.")
        if server.id in self.refuse:
            return jobs.Refusal("This server's software list was collected less than a minute ago.", 429)
        job_id = 1000 + server.id
        self.in_flight.add(job_id)
        self.peak = max(self.peak, len(self.in_flight))
        self.status_of[job_id] = "RUNNING"
        return types.SimpleNamespace(id=job_id)

    def fake_sleep(self, seconds):
        for job_id in list(self.status_of):
            if self.status_of[job_id] == "RUNNING":
                self.status_of[job_id] = "FAILURE" if job_id % 2 else "SUCCESS"
                self.in_flight.discard(job_id)

    def fake_profile_model(self):
        test = self

        class Manager:
            def select_related(self_inner, *names):
                test.profile_queries.append(names)
                return self_inner

            def filter(self_inner, **kw):
                main_thread_only("UserProfile.objects")
                row = test.fresh.get(kw["id"])
                return FakeQuery([row] if row is not None else [])

        return types.SimpleNamespace(objects=Manager())

    def run_it(self, servers, ids=None, base_url="https://cb.example.com", **kwargs):
        ids = [s.id for s in servers] if ids is None else ids
        with contextlib.ExitStack() as stack:
            for patcher in (
                mock.patch.object(fleet, "Server", fake_server_model(servers)),
                mock.patch.object(fleet, "UserProfile", self.fake_profile_model()),
                mock.patch.object(fleet, "Job", fake_job_model(
                    status=lambda: self.job_status() if callable(self.job_status) else self.job_status,
                    statuses=lambda: dict(self.status_of))),
                mock.patch.object(fleet, "default_base_url", return_value=base_url),
                mock.patch.object(fleet.time, "sleep", side_effect=self.fake_sleep),
                mock.patch.object(store, "get_settings", return_value=self.settings),
                mock.patch.object(jobs, "start", side_effect=self.fake_start),
            ):
                stack.enter_context(patcher)
            return fleet.run_resource_scan(self.job, vuln_resource_id=5, vuln_server_ids=ids,
                                           vuln_requested_by="alice", **kwargs)

    def test_scans_in_order_as_the_owner_as_children_with_bounded_parallelism(self):
        status, output, errors, outputs = self.run_it(self.servers())
        self.assertEqual([entry[1] for entry in self.started], [1, 2, 3, 4, 5, 6])
        self.assertTrue(all(entry[0] is self.owner and entry[3] is self.job and entry[2] == "https://cb.example.com"
                            for entry in self.started))
        self.assertEqual(self.peak, 2)
        self.assertEqual((outputs["scanned"], outputs["failed"]), (3, 3))
        self.assertEqual(status, "WARNING")
        self.assertIn("Scanned 3 servers; 3 failed", output)
        self.assertEqual(VIOLATIONS, [])

    def test_progress_is_a_count_of_finished_scans_and_only_written_when_it_changes(self):
        self.run_it(self.servers((1, 2)))
        self.assertEqual(self.progress[0], (("Scanning 2 servers",), {"tasks_done": 0, "total_tasks": 2}))
        counts = [k["tasks_done"] for a, k in self.progress[1:]]
        self.assertEqual(counts, sorted(set(counts)))       # strictly increasing: written only when it changes
        self.assertEqual(self.progress[-1][1], {"tasks_done": 2, "total_tasks": 2})

    def test_the_marker_is_there_while_it_runs_and_gone_afterwards(self):
        self.run_it(self.servers((1, 2)))
        self.assertTrue(all(m and m["job_id"] == 500 and m["server_ids"] == [1, 2] and m["requested_by"] == "alice"
                            for m in self.markers_seen))
        self.assertIsNone(store.read_resource_run(5))

    def test_a_marker_the_web_request_wrote_is_replaced_by_the_same_job_and_cleared(self):
        store.set_resource_run(5, {"job_id": 500, "started": "t", "server_ids": [1]})
        self.run_it(self.servers((1,)))
        self.assertIsNone(store.read_resource_run(5))

    def test_a_newer_runs_marker_is_neither_overwritten_nor_cleared(self):
        store.set_resource_run(5, {"job_id": 600, "started": "t", "server_ids": [9]})
        self.table.add(600)
        self.run_it(self.servers((1,)))
        self.assertEqual(store.read_resource_run(5), {"job_id": 600, "started": "t", "server_ids": [9]})

    def test_a_stale_marker_of_another_job_is_replaced(self):
        store.set_resource_run(5, {"job_id": 600})          # its job is gone
        self.run_it(self.servers((1,)))
        self.assertEqual(self.markers_seen[0]["job_id"], 500)

    def test_each_server_is_checked_again_just_before_its_start(self):
        servers = self.servers((1, 2, 3, 4, 5, 6, 7))
        by_id = {s.id: s for s in servers}
        self.settings["inventory_parallel"] = 1

        def after_start(server):
            if server.id == 1:       # hours later, while server 1 was scanning
                by_id[2].resource_id = 9                                              # moved to another resource
                by_id[3].status = "HISTORICAL"                                        # made historical
                by_id[4].group = types.SimpleNamespace(is_unassigned=True, name="Unassigned")   # to Unassigned
                self.hidden.add(5)                                                    # the owner can no longer view
                self.denied_scan.add(6)                                               # ... or scan

        self.after_start = after_start
        status, output, errors, outputs = self.run_it(servers)
        self.assertEqual([entry[1] for entry in self.started], [1, 7])
        self.assertEqual(outputs["skipped"], {"left_resource": 1, "historical": 1, "unassigned": 1,
                                              "no_permission": 2})
        self.assertIn("1 no longer in the resource", output)
        self.assertIn("2 without permission to scan", output)
        self.assertEqual(VIOLATIONS, [])

    def test_the_owner_is_read_again_before_every_permission_check_with_one_query(self):
        self.settings["inventory_parallel"] = 1
        self.run_it(self.servers((1, 2, 3)))
        self.assertEqual(self.profile_queries, [("user",)] * 3)
        self.assertEqual(VIOLATIONS, [])

    def test_the_fresh_profile_is_the_one_checked_and_the_one_that_owns_the_scan(self):
        fresh = types.SimpleNamespace(id=77, user=types.SimpleNamespace(is_active=True), is_cbadmin=False,
                                      has_permission=lambda perm, server: True)
        self.fresh[77] = fresh
        asked = []
        with mock.patch.object(jobs, "can_view", lambda profile, server: asked.append(profile) or True):
            self.run_it(self.servers((1,)))
        self.assertTrue(asked and all(p is fresh for p in asked))
        self.assertTrue(self.started and all(entry[0] is fresh for entry in self.started))

    def test_a_role_removed_after_the_click_is_seen_on_the_next_server(self):
        self.settings["inventory_parallel"] = 1
        self.fresh[77] = types.SimpleNamespace(id=77, user=types.SimpleNamespace(is_active=True), is_cbadmin=True)

        def after_start(server):
            if server.id == 1:     # the role is removed while server 1 is scanning
                self.fresh[77] = types.SimpleNamespace(id=77, user=types.SimpleNamespace(is_active=True),
                                                       is_cbadmin=False)

        self.after_start = after_start
        with mock.patch.object(jobs, "can_scan", lambda profile, server: profile.is_cbadmin):
            status, output, errors, outputs = self.run_it(self.servers((1, 2, 3)))
        self.assertEqual([entry[1] for entry in self.started], [1])
        self.assertEqual(outputs["skipped"], {"no_permission": 2})

    def test_a_deactivated_owner_or_a_deleted_profile_ends_every_remaining_start(self):
        self.settings["inventory_parallel"] = 1
        for what in ("inactive", "deleted"):
            with self.subTest(what=what):
                self.started.clear()
                self.profile_queries.clear()
                self.owner.user.is_active = True
                self.fresh[77] = self.owner

                def after_start(server, what=what):
                    if server.id == 1:
                        if what == "inactive":
                            self.owner.user.is_active = False
                        else:
                            del self.fresh[77]

                self.after_start = after_start
                status, output, errors, outputs = self.run_it(self.servers((1, 2, 3, 4)))
                self.assertEqual([entry[1] for entry in self.started], [1])
                self.assertEqual(outputs["skipped"], {"no_permission": 3})
                self.assertEqual(len(self.profile_queries), 2)    # no more reads once the owner is out
        self.owner.user.is_active = True

    def test_an_owner_read_that_raises_fails_closed_as_unknown(self):
        def boom(*names):
            raise RuntimeError("db down")

        self.fake_profile_model = lambda: types.SimpleNamespace(objects=types.SimpleNamespace(select_related=boom))
        status, output, errors, outputs = self.run_it(self.servers((1,)))
        self.assertEqual(self.started, [])
        self.assertEqual(outputs["skipped"], {"unknown": 1})

    def test_an_unassigned_server_is_never_started_even_for_an_admin_owner(self):
        self.owner.is_cbadmin = True
        servers = [make_server(id=1, resource_id=5, group=types.SimpleNamespace(is_unassigned=True, name="Unassigned"))]
        status, output, errors, outputs = self.run_it(servers)
        self.assertEqual(self.started, [])
        self.assertEqual(outputs["skipped"], {"unassigned": 1})
        self.assertEqual(status, "FAILURE")

    def test_a_server_that_became_ineligible_or_was_deleted_is_a_skip(self):
        servers = self.servers((1, 2, 3))
        servers[1].power_status = "POWEROFF"
        status, output, errors, outputs = self.run_it(servers, ids=[1, 2, 3, 99])
        self.assertEqual([entry[1] for entry in self.started], [1, 3])
        self.assertEqual(outputs["skipped"], {"gone": 1, "powered_off": 1})

    def test_a_server_not_in_the_list_the_request_made_is_never_scanned(self):
        # The job scans the ids it was given, never every server the resource has now.
        self.run_it(self.servers((1, 2, 3)), ids=[2])
        self.assertEqual([entry[1] for entry in self.started], [2])

    def test_a_job_without_an_owner_scans_nothing_and_ends_cleanly(self):
        self.job.owner = None
        status, output, errors, outputs = self.run_it(self.servers())
        self.assertEqual((status, outputs), ("FAILURE", {}))
        self.assertIn("no owner", output)
        self.assertEqual(self.started, [])
        self.assertIsNone(store.read_resource_run(5))

    def test_a_check_that_raises_fails_closed_as_unknown(self):
        def boom(profile, server):
            raise RuntimeError("permission tables are down")

        with mock.patch.object(jobs, "can_view", boom):
            status, output, errors, outputs = self.run_it(self.servers((1,)))
        self.assertEqual(self.started, [])
        self.assertEqual(outputs["skipped"], {"unknown": 1})

    def test_a_refusal_is_a_counted_skip_including_one_from_an_older_class(self):
        self.refuse = {1}
        self.old_refuse = {2}
        status, output, errors, outputs = self.run_it(self.servers((1, 2, 3)))
        self.assertEqual(outputs["skipped"], {"refused": 2})
        self.assertEqual(outputs["scanned"] + outputs["failed"], 1)

    def test_one_failed_start_does_not_end_the_run(self):
        self.explode = {1}
        status, output, errors, outputs = self.run_it(self.servers((1, 2, 3)))
        self.assertEqual([entry[1] for entry in self.started], [1, 2, 3])
        self.assertEqual((outputs["scanned"], outputs["failed"]), (1, 2))
        self.assertEqual(status, "WARNING")

    def test_failure_only_when_nothing_could_run(self):
        status, output, errors, outputs = self.run_it(self.servers((1,)))     # server 1's scan fails (odd id)
        self.assertEqual((status, errors), ("FAILURE", "No server could be scanned."))
        self.refuse = {2}
        status, output, errors, outputs = self.run_it(self.servers((2,)))
        self.assertEqual(status, "FAILURE")
        status, output, errors, outputs = self.run_it(self.servers((2, 4)))   # one refused, one scanned
        self.assertEqual(status, "SUCCESS")
        status, output, errors, outputs = self.run_it(self.servers((4, 3)))   # one scanned, one failed
        self.assertEqual((status, outputs["scanned"], outputs["failed"]), ("WARNING", 1, 1))

    def test_nothing_given_to_scan_is_a_failure_without_a_start(self):
        status, output, errors, outputs = self.run_it(self.servers(), ids=[])
        self.assertEqual(status, "FAILURE")
        self.assertEqual(self.started, [])

    def test_not_set_up_or_no_site_url_scans_nothing_and_clears_the_marker(self):
        status, output, errors, outputs = self.run_it(self.servers(), base_url="")
        self.assertEqual((status, errors), ("FAILURE", jobs.NO_SITE_URL))
        with mock.patch.object(tools, "status", return_value={"ready": False, "db": {"built": ""}}):
            status, output, errors, outputs = fleet.run_resource_scan(self.job, vuln_resource_id=5, vuln_server_ids=[1])
        self.assertEqual(status, "FAILURE")
        self.assertEqual(self.started, [])
        self.assertIsNone(store.read_resource_run(5))

    def test_a_job_without_a_resource_id_does_nothing(self):
        for value in (None, "x"):
            status, output, errors, outputs = fleet.run_resource_scan(self.job, vuln_resource_id=value)
            self.assertEqual(status, "FAILURE")

    def test_an_exception_ends_in_a_failure_sentence_and_the_marker_is_cleared(self):
        with mock.patch.object(store, "get_settings", side_effect=RuntimeError("settings are gone")),                 mock.patch.object(fleet, "default_base_url", return_value="https://cb"):
            status, output, errors, outputs = fleet.run_resource_scan(self.job, vuln_resource_id=5, vuln_server_ids=[1])
        self.assertEqual(status, "FAILURE")
        self.assertIn("settings are gone", errors)
        self.assertIsNone(store.read_resource_run(5))

    def test_a_cancel_raised_into_the_job_propagates_and_the_marker_is_still_cleared(self):
        self.cancel_on = {2}
        with self.assertRaises(CancelJob):
            self.run_it(self.servers((1, 2, 3)))
        self.assertIsNone(store.read_resource_run(5))

    def test_a_cancel_raised_while_the_marker_is_written_still_clears_it(self):
        real = store.set_resource_run

        def cancelling(resource_id, marker):
            real(resource_id, marker)
            raise CancelJob()

        with mock.patch.object(store, "set_resource_run", cancelling):
            with self.assertRaises(CancelJob):
                self.run_it(self.servers((1,)))
        self.assertIsNone(store.read_resource_run(5))

    def test_a_cancel_request_stops_new_starts_and_ends_with_a_warning(self):
        calls = {"n": 0}

        def status():
            calls["n"] += 1
            return "TO_CANCEL" if calls["n"] > 1 else "RUNNING"

        self.job_status = status
        status_text, output, errors, outputs = self.run_it(self.servers((1, 2, 3, 4, 5, 6)))
        self.assertEqual(status_text, "WARNING")
        self.assertEqual(len(self.started), 2)
        self.assertIn("canceled", output)
        self.assertIsNone(store.read_resource_run(5))

    def test_the_time_budget_stops_new_starts_and_waits_for_the_running_ones(self):
        with mock.patch.object(fleet, "_clock", fake_clock(0, 0)):
            status, output, errors, outputs = self.run_it(self.servers())
        self.assertEqual([entry[1] for entry in self.started], [1, 2])
        self.assertEqual(outputs["left"], 4)
        self.assertEqual(status, "WARNING")
        self.assertIn("4 servers not started", output)
        self.assertIsNone(store.read_resource_run(5))

    def test_the_marker_is_cleared_for_its_own_job_only_when_another_run_replaced_it_meanwhile(self):
        def after_start(server):
            store.set_resource_run(5, {"job_id": 600})

        self.after_start = after_start
        self.run_it(self.servers((2,)))
        self.assertEqual(store.read_resource_run(5), {"job_id": 600})


# ---------------------------------------------------------------- the job files
# ---------------------------------------------------------------- the match slots and the shared match helper
class FakeFcntl:
    """flock over files that are really opened, for a platform without fcntl: a lock belongs to the open
    file descriptor and goes when that descriptor is closed, so two descriptors of one file conflict, as
    with the real flock."""
    LOCK_EX = 2
    LOCK_SH = 1
    LOCK_NB = 4

    def __init__(self):
        self.held = {}
        self.mutex = threading.Lock()

    @staticmethod
    def _key(fd):
        info = os.fstat(fd)
        return info.st_dev, info.st_ino

    def flock(self, fd, flags):
        with self.mutex:
            key = self._key(fd)
            holder = self.held.get(key)
            if holder is not None and holder != fd:
                try:
                    alive = self._key(holder) == key
                except OSError:
                    alive = False
                if alive:
                    raise BlockingIOError("held")
            self.held[key] = fd


class MatchSlotTests(Base):
    def setUp(self):
        super().setUp()
        self.fcntl = FakeFcntl()
        for patcher in (mock.patch.object(store, "fcntl", self.fcntl),
                        mock.patch.object(store, "MATCH_SLOT_POLL_S", 0.01),
                        mock.patch.object(store, "own", lambda *a, **k: None)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_at_most_slots_run_at_once_across_threads(self):
        state = {"now": 0, "peak": 0}
        guard = threading.Lock()

        def work():
            with store.match_slot(2):
                with guard:
                    state["now"] += 1
                    state["peak"] = max(state["peak"], state["now"])
                threading.Event().wait(0.05)
                with guard:
                    state["now"] -= 1

        threads = [threading.Thread(target=work) for _ in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(state["peak"], 2)
        self.assertTrue(os.path.isfile(os.path.join(store.ROOT, "match-slot-0.lock")))
        self.assertTrue(os.path.isfile(os.path.join(store.ROOT, "match-slot-1.lock")))
        self.assertFalse(os.path.exists(os.path.join(store.ROOT, "match-slot-2.lock")))

    def test_a_pool_larger_than_the_limit_waits_and_finishes(self):
        from concurrent.futures import ThreadPoolExecutor

        def work(number):
            with store.match_slot(1):
                return number

        with ThreadPoolExecutor(max_workers=4) as pool:
            self.assertEqual(sorted(pool.map(work, range(8), timeout=30)), list(range(8)))

    def test_a_slot_is_free_again_after_use_and_after_an_error(self):
        with self.assertRaises(RuntimeError):
            with store.match_slot(1):
                raise RuntimeError("the match failed")
        with store.match_slot(1, wait_s=0):
            pass

    def test_no_free_slot_in_time_is_the_busy_error_and_on_wait_is_called_once(self):
        calls = []
        errors = []
        with store.match_slot(1):

            def other():
                try:
                    with store.match_slot(1, wait_s=0.1, on_wait=lambda: calls.append("wait")):
                        pass
                except store.StoreError as exc:
                    errors.append(str(exc))

            thread = threading.Thread(target=other)
            thread.start()
            thread.join(30)
        self.assertEqual(errors, ["The scanner is busy. Try again later."])
        self.assertEqual(calls, ["wait"])

    def test_a_nested_slot_in_one_thread_is_refused_not_a_deadlock(self):
        with store.match_slot(4):
            with self.assertRaises(store.StoreError):
                with store.match_slot(4, wait_s=0):
                    pass
        with store.match_slot(4, wait_s=0):      # the refusal did not leak the flag
            pass

    def test_without_fcntl_it_does_nothing(self):
        with mock.patch.object(store, "fcntl", None):
            with store.match_slot(1):
                with store.match_slot(1):
                    pass

    def test_the_slot_files_are_not_the_scanner_lock(self):
        self.assertNotEqual(store.MATCH_SLOT_FILE.format(0), store.SCANNER_LOCK_FILE)


class MatchAndParseTests(Base):
    def setUp(self):
        super().setUp()
        self.events = []
        self.messages = []
        self.out = os.path.join(TMP, "work", "helper.grype.json")
        os.makedirs(os.path.dirname(self.out), exist_ok=True)
        self.limit = 1
        self.wait = False

    @contextlib.contextmanager
    def fake_slot(self, slots, wait_s=None, on_wait=None):
        self.events.append(("slot", slots))
        if self.wait:
            on_wait()
        try:
            yield
        finally:
            self.events.append(("slot released",))

    def fake_match(self, path, out):
        self.events.append(("match",))
        with open(out, "w", encoding="utf-8") as handle:
            json.dump({"matches": []}, handle)

    def parse(self, document):
        self.events.append(("parse",))
        return {"matches": [], "db_built": "b"}

    def run_it(self, **kwargs):
        with contextlib.ExitStack() as stack:
            for patcher in (
                mock.patch.object(store, "match_slot", self.fake_slot),
                mock.patch.object(store, "get_settings", lambda: {"rematch_workers": self.limit}),
                mock.patch.object(tools, "match", side_effect=self.fake_match),
                mock.patch.object(jobs.vuln_match, "parse_grype", side_effect=self.parse),
            ):
                stack.enter_context(patcher)
            return jobs.match_and_parse("/lists/x.gz", self.out, **kwargs)

    def test_the_slot_covers_the_match_the_load_and_the_parse_and_the_files_are_gone(self):
        self.limit = 3
        result = self.run_it()
        self.assertEqual(result, {"matches": [], "db_built": "b"})
        self.assertEqual(self.events, [("slot", 3), ("match",), ("parse",), ("slot released",)])
        self.assertFalse(os.path.exists(self.out))

    def test_the_slot_is_released_and_the_file_deleted_when_the_match_fails(self):
        def fails(path, out):
            with open(out, "w") as handle:
                handle.write("{}")
            raise tools.ToolError("The vulnerability match failed.")

        with mock.patch.object(self, "fake_match", fails):
            with self.assertRaises(tools.ToolError):
                self.run_it()
        self.assertEqual(self.events[-1], ("slot released",))
        self.assertFalse(os.path.exists(self.out))

    def test_a_report_over_the_limit_fails_the_scan(self):
        with mock.patch.object(jobs, "MAX_REPORT_BYTES", 1):
            with self.assertRaises(jobs.ScanFailed):
                self.run_it()
        self.assertNotIn(("parse",), self.events)
        self.assertEqual(self.events[-1], ("slot released",))

    def test_a_scan_that_has_to_wait_says_so_and_then_says_it_is_matching(self):
        self.wait = True
        self.run_it(progress=self.messages.append)
        self.assertEqual(self.messages, [jobs.WAITING_FOR_SLOT, jobs.MATCHING])
        self.assertIn("Waiting for the vulnerability database to be free", self.messages[0])

    def test_no_wait_no_message(self):
        self.run_it(progress=self.messages.append)
        self.assertEqual(self.messages, [])

    def test_the_default_limit_is_used_when_the_setting_cannot_be_read(self):
        def broken():
            raise OSError("no")

        with contextlib.ExitStack() as stack:
            for patcher in (
                mock.patch.object(store, "get_settings", broken),
                mock.patch.object(store, "match_slot", self.fake_slot),
                mock.patch.object(tools, "match", side_effect=self.fake_match),
                mock.patch.object(jobs.vuln_match, "parse_grype", side_effect=self.parse),
            ):
                stack.enter_context(patcher)
            jobs.match_and_parse("/lists/x.gz", self.out)
        self.assertEqual(self.events[0], ("slot", store.DEFAULT_SETTINGS["rematch_workers"]))

    def test_the_rematch_worker_goes_through_the_helper(self):
        seen = []
        with mock.patch.object(jobs, "match_and_parse", side_effect=lambda *a, **k: seen.append(a) or {"x": 1}):
            with mock.patch.object(store, "WORK_DIR", os.path.join(TMP, "work")):
                result = fleet._match_worker({"server_id": 4, "scan_id": "s4", "path": "/lists/4.gz"})
        self.assertEqual(result, {"x": 1})
        self.assertEqual(seen[0][0], "/lists/4.gz")
        self.assertTrue(seen[0][1].endswith("rematch-4-s4.grype.json"))


class SignalMessageTests(Base):
    def run_with(self, code, what=None):
        done = types.SimpleNamespace(returncode=code, stderr=b"", stdout=b"")
        with mock.patch.object(tools.subprocess, "run", return_value=done):
            with self.assertRaises(tools.ToolError) as caught:
                tools._run(["grype"], {}, 60, what or tools.MATCH_WHAT)
        return caught.exception.public

    def test_signal_9_during_the_match_is_the_out_of_memory_message(self):
        self.assertEqual(
            self.run_with(-9),
            "The appliance ran out of memory while matching the software list. Lower 'Vulnerability matches at "
            "once' in the Vulnerability Settings, or give the appliance more memory.")

    def test_another_signal_is_named(self):
        self.assertEqual(self.run_with(-11), "The scanner was stopped by signal 11.")

    def test_a_signal_outside_the_match_does_not_blame_the_memory(self):
        self.assertEqual(self.run_with(-9, "The database update"), "The database update was stopped by signal 9.")

    def test_a_plain_exit_code_keeps_its_message(self):
        self.assertEqual(self.run_with(2), "The vulnerability match failed (exit code 2).")


class JobFileTests(unittest.TestCase):
    FILES = ("vuln_job.py", "vuln_setup_job.py", "vuln_rematch_job.py", "vuln_inventory_job.py", "vuln_provision_job.py",
             "vuln_resource_job.py")
    ORDER = ("vuln_store", "vuln_match", "vuln_db", "vuln_msrc", "vuln_tools", "vuln_repo", "vuln_hooks", "vuln_jobs", "vuln_fleet")

    def modules_of(self, filename):
        ns = {}
        with open(os.path.join(EXT, "io_cloudbolt_vulnerabilities", filename), encoding="utf-8") as handle:
            source = handle.read()
        exec(compile(source, filename, "exec"), ns)
        return tuple(name.rsplit(".", 1)[1] for name in ns["MODULES"]), ns

    def test_every_job_file_reloads_every_module_in_dependency_order(self):
        for filename in self.FILES:
            order, _ = self.modules_of(filename)
            self.assertEqual(order, self.ORDER, filename)

    def test_every_module_a_job_imports_is_in_the_list(self):
        package = os.path.join(EXT, "io_cloudbolt_vulnerabilities")
        for filename in ("vuln_fleet.py", "vuln_jobs.py", "vuln_hooks.py"):
            with open(os.path.join(package, filename), encoding="utf-8") as handle:
                text = handle.read()
            for line in text.splitlines():
                if line.startswith("from . import "):
                    names = [n.split(" as ")[0].strip() for n in line[len("from . import "):].split(",")]
                    for name in names:
                        self.assertIn(name, self.ORDER, (filename, name))

    def test_no_double_braces_in_any_python_file(self):
        package = os.path.join(EXT, "io_cloudbolt_vulnerabilities")
        for filename in os.listdir(package):
            if filename.endswith(".py"):
                with open(os.path.join(package, filename), encoding="utf-8") as handle:
                    text = handle.read()
                self.assertNotIn("{" + "{", text, filename)
                self.assertNotIn("}" + "}", text, filename)

    def test_the_provision_file_never_raises_but_lets_a_cancel_through(self):
        _, ns = self.modules_of("vuln_provision_job.py")
        broken = types.ModuleType("broken")

        def boom(name):
            raise RuntimeError("module broken")

        with mock.patch.object(ns["importlib"], "import_module", side_effect=boom):
            result = ns["run"](object())
        self.assertEqual(result[0], "SUCCESS")

        def cancel(name):
            raise CancelJob()

        with mock.patch.object(ns["importlib"], "import_module", side_effect=cancel):
            with self.assertRaises(CancelJob):
                ns["run"](object())
        del broken


if __name__ == "__main__":
    unittest.main()
