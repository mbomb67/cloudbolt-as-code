"""
Checks for the storage and run-marker side of Install updates on all servers (vuln_store.py and
vuln_fleet.py) without CloudBolt: CloudBolt's modules are stubs.

    python -B -m unittest discover -s extensions/XUI-wqrj0e3p/tests -p "test_vuln_patch_resource_store.py"

The stubs are in place only while this file imports the extension's modules, so another test file's
stubs are not disturbed.
"""
import contextlib
import importlib
import os
import sys
import tempfile
import types
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
EXT = os.path.dirname(HERE)
PACKAGE = "xui.io_cloudbolt_vulnerabilities"


class IntegrityError(Exception):
    pass


class Http404(Exception):
    pass


class FakeLogger:
    def __init__(self, *a):
        pass

    def __getattr__(self, name):
        return lambda *a, **k: None


class FakeQ:
    """Records the keyword of each Q and how they are joined."""

    def __init__(self, **kw):
        self.keys = sorted(kw)

    def __or__(self, other):
        joined = FakeQ()
        joined.keys = self.keys + other.keys
        return joined


def _module(name, **attrs):
    module = types.ModuleType(name)
    module.__dict__.update(attrs)
    return module


TMP = tempfile.mkdtemp(prefix="vuln-patch-store-test-")
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
_stub("django.db.models", Q=FakeQ)
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
store, tools, hooks, jobs, fleet = (MODS[n] for n in ("vuln_store", "vuln_tools", "vuln_hooks", "vuln_jobs",
                                                      "vuln_fleet"))


class FakeQuery:
    def __init__(self, rows):
        self.rows = list(rows)

    def first(self):
        return self.rows[0] if self.rows else None

    def __iter__(self):
        return iter(self.rows)


class FakeJobTable:
    """vuln_jobs.Job: the jobs the markers can name. A job that is not in the table is gone."""

    def __init__(self):
        self.live = {}

    def add(self, job_id, active=True, done=None, total=None, message=None):
        progress = types.SimpleNamespace(message=message)
        self.live[job_id] = types.SimpleNamespace(
            id=job_id, is_active=lambda: active, tasks_done=done, total_tasks=total,
            get_progress=lambda: (0, 0, progress), get_absolute_url=lambda: "/jobs/{}/".format(job_id))

    def model(self):
        table = self

        class Manager:
            def filter(self, **kw):
                found = table.live.get(kw.get("id"))
                return FakeQuery([found] if found else [])

        return types.SimpleNamespace(objects=Manager())


class Base(unittest.TestCase):
    RESOURCE_ID = 5

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="vuln-patch-resource-")
        self.table = FakeJobTable()
        self.queued = []
        self.owner = types.SimpleNamespace(id=77, user=types.SimpleNamespace(is_active=True, username="alice"))
        for patcher in (mock.patch.object(store, "RESOURCES_DIR", os.path.join(self.root, "resources")),
                        mock.patch.object(store, "own", lambda *a, **k: None),
                        mock.patch.object(jobs, "Job", self.table.model()),
                        mock.patch.object(tools, "status", return_value={"ready": True, "db": {"built": "b"}})):
            patcher.start()
            self.addCleanup(patcher.stop)

    def resource(self):
        return types.SimpleNamespace(id=self.RESOURCE_ID, name="Web tier",
                                     jobs=types.SimpleNamespace(add=lambda job: None))

    def start_scan(self):
        created = types.SimpleNamespace(id=90, label="", save=lambda **k: None)
        hook = types.SimpleNamespace(run_as_job=lambda **kw: self.queued.append(kw) or [created])
        with mock.patch.object(hooks, "resource_scan_hook", lambda: hook):
            return fleet.start_resource_scan(self.owner, self.resource(), [1, 2], "https://cb.example")


class MarkerKindTests(Base):
    def test_a_patch_marker_round_trips_with_its_kind(self):
        marker = {"job_id": 40, "started": "t", "requested_by": "alice", "server_ids": [1, 2], "kind": "patch"}
        store.set_resource_run(5, marker)
        self.assertEqual(store.read_resource_run(5), marker)
        self.assertTrue(store.clear_resource_run(5, job_id=40))
        self.assertIsNone(store.read_resource_run(5))

    def test_the_run_reports_kind_and_message(self):
        store.set_resource_run(5, {"job_id": 40, "started": "t", "server_ids": [1, 2, 3], "kind": "patch"})
        self.table.add(40, done=1, total=3, message="Server 2 of 3: web-02: installing")
        self.assertEqual(fleet.resource_run(5), {"job_id": 40, "job_url": "/jobs/40/", "started": "t", "done": 1,
                                                 "total": 3, "kind": "patch",
                                                 "message": "Server 2 of 3: web-02: installing"})

    def test_a_marker_without_a_kind_is_a_scan(self):
        store.set_resource_run(5, {"job_id": 40, "started": "t", "server_ids": [1]})
        self.table.add(40)
        run = fleet.resource_run(5)
        self.assertEqual((run["kind"], run["message"]), ("scan", ""))

    def test_a_stale_patch_marker_is_no_run(self):
        store.set_resource_run(5, {"job_id": 40, "kind": "patch"})
        self.assertIsNone(fleet.resource_run(5))          # its job is gone
        self.table.add(40, active=False)
        self.assertIsNone(fleet.resource_run(5))          # its job has finished

    def test_another_resource_is_not_blocked(self):
        store.set_resource_run(6, {"job_id": 40, "kind": "patch"})
        self.table.add(40)
        self.assertIsNone(fleet.resource_run(5))
        self.assertIsNotNone(fleet.resource_run(6))


class RefusalWordingTests(Base):
    def test_a_scan_is_refused_while_a_scan_runs(self):
        store.set_resource_run(5, {"job_id": 40})
        self.table.add(40)
        refusal = self.start_scan()
        self.assertEqual((refusal.status, refusal.message), (
            409, "Job 40 is already scanning the servers of this resource."))
        self.assertEqual(self.queued, [])

    def test_a_scan_is_refused_while_updates_install(self):
        store.set_resource_run(5, {"job_id": 40, "kind": "patch"})
        self.table.add(40)
        refusal = self.start_scan()
        self.assertEqual((refusal.status, refusal.message), (
            409, "Job 40 is already installing updates on the servers of this resource."))
        self.assertEqual(self.queued, [])

    def test_a_scan_starts_when_the_patch_marker_is_stale(self):
        store.set_resource_run(5, {"job_id": 40, "kind": "patch"})
        self.table.add(40, active=False)
        self.assertEqual(self.start_scan().id, 90)
        self.assertEqual(len(self.queued), 1)


class PatchRecordTests(Base):
    RECORD = {"at": "t", "by": "alice", "job_id": 40, "status": "warning",
              "counts": {"updated": 1, "restart_pending": 0, "failed": 1, "skipped": 2},
              "restart_if_needed": True, "message": "x"}

    def test_round_trip_and_replace(self):
        self.assertIsNone(store.read_resource_patch(5))
        store.set_resource_patch("5", self.RECORD)
        self.assertEqual(store.read_resource_patch(5), self.RECORD)
        store.set_resource_patch(5, dict(self.RECORD, job_id=41))
        self.assertEqual(store.read_resource_patch(5)["job_id"], 41)

    def test_clear(self):
        store.set_resource_patch(5, self.RECORD)
        self.assertTrue(store.clear_resource_patch(5))
        self.assertIsNone(store.read_resource_patch(5))
        self.assertFalse(store.clear_resource_patch(5))

    def test_the_record_and_the_marker_are_independent_files(self):
        store.set_resource_patch(5, self.RECORD)
        self.assertIsNone(store.read_resource_run(5))
        store.set_resource_run(5, {"job_id": 40, "kind": "patch"})
        store.clear_resource_run(5)
        self.assertEqual(store.read_resource_patch(5), self.RECORD)
        store.set_resource_run(5, {"job_id": 41})
        store.clear_resource_patch(5)
        self.assertEqual(store.read_resource_run(5), {"job_id": 41})

    def test_resources_are_independent(self):
        store.set_resource_patch(5, self.RECORD)
        self.assertIsNone(store.read_resource_patch(6))
        store.set_resource_patch(6, dict(self.RECORD, job_id=2))
        store.clear_resource_patch(5)
        self.assertEqual(store.read_resource_patch(6)["job_id"], 2)

    def test_damaged_file_reads_as_none(self):
        store.set_resource_patch(5, self.RECORD)
        with open(store._resource_patch_path(5), "w") as handle:
            handle.write("[1, 2")
        self.assertIsNone(store.read_resource_patch(5))
        with open(store._resource_patch_path(5), "w") as handle:
            handle.write("[1, 2]")
        self.assertIsNone(store.read_resource_patch(5))

    def test_ids_cannot_leave_the_resources_folder(self):
        for call in (lambda: store.read_resource_patch("../5"), lambda: store.set_resource_patch("5/../../x", {}),
                     lambda: store.clear_resource_patch("x")):
            with self.assertRaises(ValueError):
                call()
        path = store._resource_patch_path(5)
        self.assertEqual(os.path.dirname(path), store.RESOURCES_DIR)
        self.assertTrue(path.endswith("5.patch.json"))


class SupportedQTests(unittest.TestCase):
    def test_linux_and_windows_at_three_levels(self):
        with mock.patch.dict(sys.modules, STUBS):
            q = fleet.supported_q()
        self.assertEqual(sorted(q.keys), sorted([
            "os_family__name", "os_family__parent__name", "os_family__parent__parent__name"] * 2))


if __name__ == "__main__":
    unittest.main()
