"""
Checks for installing updates on all servers of a resource (vuln_patch_resource.py, vuln_resource_patch_job.py and
vuln_resource_patch_condition.py) without CloudBolt: CloudBolt's modules are stubs, the resource marker and the last-run
record are in memory, the per-server engine (vuln_patch._patch_one) is a scripted fake, and the permission and
eligibility checks are fakes the tests turn on and off.

    python -B -m unittest discover -s extensions/XUI-wqrj0e3p/tests -p "test_vuln_patch_resource.py"

The stubs are in place only while this file imports the extension's modules (the same swap test_vuln_patch.py makes),
so another test file's stubs are not disturbed.
"""
import contextlib
import importlib
import json
import os
import re
import sys
import tempfile
import types
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
EXT = os.path.dirname(HERE)
PACKAGE = "xui.io_cloudbolt_vulnerabilities"
PKG_DIR = os.path.join(EXT, "io_cloudbolt_vulnerabilities")


class IntegrityError(Exception):
    pass


class CancelJob(BaseException):
    """Stands in for CloudBolt's CancelJobException, which is a BaseException."""


class Http404(Exception):
    pass


class TimeoutException(Exception):
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


TMP = tempfile.mkdtemp(prefix="vuln-patch-resource-test-")
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
_stub("utilities.exceptions", TimeoutException=TimeoutException)
_stub("utilities.logger", ThreadLogger=FakeLogger)
_stub("utilities.permissions", server_action_permitted=lambda *a, **k: False)
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
                 "vuln_fleet", "vuln_patch", "vuln_patch_resource")
        return {name: importlib.import_module(PACKAGE + "." + name) for name in names}
    finally:
        for key in [k for k in sys.modules if k == "xui" or k.startswith(PACKAGE) or k in STUBS]:
            del sys.modules[key]
        sys.modules.update(saved)


MODS = _import_extension()
store, hooks, jobs, fleet, patch, rsrc = (MODS[n] for n in (
    "vuln_store", "vuln_hooks", "vuln_jobs", "vuln_fleet", "vuln_patch", "vuln_patch_resource"))

RESOURCE_ID = 50
OWNER_ID = 3
# The template engine's markers, built here so this file has none of its own.
OPEN, CLOSE = "{" * 2, "}" * 2


# ---------------------------------------------------------------- fakes
def make_server(server_id, hostname=None, **over):
    data = dict(id=server_id, hostname=hostname or "web-{:02d}".format(server_id), status="ACTIVE",
                resource_id=RESOURCE_ID, group=types.SimpleNamespace(is_unassigned=False, name="Dev"))
    data.update(over)
    return types.SimpleNamespace(**data)


def record(status="updated", **over):
    """What vuln_patch._patch_one returns."""
    data = {"family": "linux", "status": status, "updated": 4, "failed": 0, "restarted": False,
            "restart_needed": status == "restart_pending", "kernels_removed": 0, "before": None, "after": None,
            "message": "", "warning": status in ("failed", "restart_pending"), "hostname": "x"}
    data.update(over)
    return data


def make_job(job_id=11, owner="default", username="alice"):
    progress = []
    if owner == "default":
        owner = types.SimpleNamespace(id=OWNER_ID, user=types.SimpleNamespace(username=username, is_active=True))
    return types.SimpleNamespace(id=job_id, owner=owner, progress=progress,
                                 set_progress=lambda message, **k: progress.append((message, k)))


class FakeQuerySet:
    def __init__(self, items=None):
        self.items = items or []
        self.calls = []

    def exclude(self, *a, **k):
        self.calls.append(("exclude", a, k))
        return self

    def filter(self, *a, **k):
        self.calls.append(("filter", a, k))
        return self

    def exists(self):
        return bool(self.items)


class Base(unittest.TestCase):
    """In-memory resource marker and record, a fake engine, fake checks, a clock."""

    def setUp(self):
        self.servers = [make_server(1), make_server(2), make_server(3)]
        self.settings = {"visibility": "linux", "patch_timeout_min": 90, "restart_wait_min": 30, "patching": True}
        self.marker = None
        self.marker_writes = []
        self.last = None
        self.record_writes = []
        self.clock_now = 0.0
        self.cancelled = False
        self.live_jobs = {}
        self.engine = {}
        self.engine_calls = []
        self.codes = {}
        self.no_view = set()
        self.no_patch = set()
        self.active_user = True
        self.profile_gone = False
        self.progress_calls = []
        self.permission_profiles = []
        self.action_state = {"exists": True, "enabled": True, "action_id": 9}
        self.patches = [
            mock.patch.dict(sys.modules, STUBS),
            mock.patch.object(store, "get_settings", side_effect=lambda: dict(self.settings)),
            mock.patch.object(store, "resource_lock", lambda resource_id: contextlib.nullcontext()),
            mock.patch.object(store, "read_resource_run", side_effect=lambda rid: self.marker),
            mock.patch.object(store, "set_resource_run", side_effect=self._set_marker),
            mock.patch.object(store, "clear_resource_run", side_effect=self._clear_marker),
            mock.patch.object(store, "set_resource_patch", side_effect=self._set_record),
            mock.patch.object(fleet, "default_base_url", return_value="https://cb.example.com"),
            mock.patch.object(fleet, "prerequisites", return_value={}),
            mock.patch.object(fleet, "resource_servers", side_effect=lambda resource: list(self.servers)),
            mock.patch.object(fleet, "_fetch_for_scan", side_effect=self._fetch),
            mock.patch.object(fleet, "_cancelled", side_effect=lambda job: self.cancelled),
            mock.patch.object(fleet, "_progress", side_effect=self._progress),
            mock.patch.object(jobs, "Job", self._job_model()),
            mock.patch.object(jobs, "can_view", side_effect=self._can_view),
            mock.patch.object(hooks, "server_patch_permitted", side_effect=self._permitted),
            mock.patch.object(patch, "eligibility",
                              side_effect=lambda server, problems=None: self.codes.get(server.id, "")),
            mock.patch.object(patch, "_clock", side_effect=lambda: self.clock_now),
            mock.patch.object(patch, "_patch_one", side_effect=self._patch_one),
            mock.patch.object(rsrc, "UserProfile", self._profile_model()),
            mock.patch.object(rsrc, "Server", self._server_model()),
            mock.patch.object(hooks, "resource_patch_action_state", side_effect=lambda: dict(self.action_state)),
        ]
        for patcher in self.patches:
            patcher.start()
            self.addCleanup(patcher.stop)

    # -- the marker and the record
    def _set_marker(self, resource_id, marker):
        self.marker = dict(marker)
        self.marker_writes.append(dict(marker))

    def _clear_marker(self, resource_id, job_id=None):
        if job_id is not None and (not isinstance(self.marker, dict) or self.marker.get("job_id") != job_id):
            return False
        self.marker = None
        return True

    def _set_record(self, resource_id, rec):
        self.last = rec
        self.record_writes.append(rec)

    # -- the world
    def _job_model(self):
        outer = self

        class Manager:
            def filter(self, **kw):
                job = outer.live_jobs.get(kw.get("id"))
                return types.SimpleNamespace(first=lambda: job)

        return types.SimpleNamespace(objects=Manager())

    def _profile_model(self):
        outer = self

        class Manager:
            def select_related(self, *a):
                return self

            def filter(self, **kw):
                fresh = None if outer.profile_gone else types.SimpleNamespace(
                    id=kw.get("id"), user=types.SimpleNamespace(is_active=outer.active_user, username="alice"))
                return types.SimpleNamespace(first=lambda: fresh)

        return types.SimpleNamespace(objects=Manager())

    def _server_model(self):
        outer = self

        class Server:
            @staticmethod
            def objects_for_profile(profile):
                return types.SimpleNamespace(filter=lambda **kw: types.SimpleNamespace(
                    values_list=lambda *a, **k: [s.id for s in outer.servers if s.id not in outer.no_view]))

        return Server

    def _fetch(self, server_id):
        for server in self.servers:
            if server.id == server_id:
                return server
        return None

    def _can_view(self, profile, server):
        return server.id not in self.no_view

    def _permitted(self, profile, server):
        self.permission_profiles.append(profile)
        return server.id not in self.no_patch

    def _progress(self, job, message, done=None, total=None):
        self.progress_calls.append((message, done, total))

    def _patch_one(self, job, server, restart, cfg):
        self.engine_calls.append({"id": server.id, "restart": restart, "marker": dict(self.marker or {}),
                                  "clock": self.clock_now})
        item = self.engine.get(server.id, record())
        if callable(item):
            item = item(server)
        if isinstance(item, BaseException):
            raise item
        return dict(item, hostname=server.hostname)

    def live(self, job_id, kind="patch"):
        """A marker of another job that is still active."""
        self.marker = {"job_id": job_id, "started": "2026-11-24T10:00:00Z", "requested_by": "bob", "server_ids": [1],
                       "kind": kind}
        self.live_jobs[job_id] = types.SimpleNamespace(id=job_id, is_active=lambda: True)

    def go(self, job=None, restart=True, resource=None, **kw):
        job = job or make_job()
        resource = resource or types.SimpleNamespace(id=RESOURCE_ID, name="Web tier")
        return job, rsrc.run_resource_patch(job, resource=resource, restart_if_needed=restart, **kw)

    def ids_by_status(self, outputs, status):
        return [int(key) for key, value in outputs["servers"].items() if value["status"] == status]


# ---------------------------------------------------------------- the run
class FlowTests(Base):
    def test_servers_go_one_at_a_time_in_the_resource_order(self):
        job, (status, output, errors, outputs) = self.go()
        self.assertEqual((status, errors), ("SUCCESS", ""))
        self.assertEqual([call["id"] for call in self.engine_calls], [1, 2, 3])
        self.assertEqual(outputs["order"], [1, 2, 3])
        self.assertEqual(outputs["counts"], {"updated": 3, "no_updates": 0, "restart_pending": 0, "failed": 0, "skipped": 0})
        self.assertEqual(outputs["resource_id"], RESOURCE_ID)
        self.assertIsNone(outputs["stopped_at"])
        self.assertEqual(list(outputs["servers"]), ["1", "2", "3"])
        self.assertEqual(outputs["servers"]["1"]["hostname"], "web-01")
        self.assertIn("web-02: 4 packages updated.", output)

    def test_progress_names_the_server_before_it_starts(self):
        self.go()
        self.assertEqual(self.progress_calls, [("Server 1 of 3: web-01", 0, 3), ("Server 2 of 3: web-02", 1, 3),
                                               ("Server 3 of 3: web-03", 2, 3)])

    def test_the_restart_flag_reaches_every_server(self):
        self.go(restart="false")
        self.assertEqual([call["restart"] for call in self.engine_calls], [False, False, False])
        self.assertIs(self.last["restart_if_needed"], False)
        self.engine_calls[:] = []
        self.go()
        self.assertEqual([call["restart"] for call in self.engine_calls], [True, True, True])

    def test_the_marker_is_set_while_it_runs_and_cleared_after(self):
        self.go()
        self.assertEqual(len(self.marker_writes), 1)
        first = self.marker_writes[0]
        self.assertEqual((first["job_id"], first["kind"], first["server_ids"], first["requested_by"]),
                         (11, "patch", [1, 2, 3], "alice"))
        self.assertTrue(first["started"])
        self.assertEqual(self.engine_calls[1]["marker"]["job_id"], 11)
        self.assertIsNone(self.marker)

    def test_a_no_updates_server_is_not_a_failure(self):
        self.engine[2] = record("no_updates", warning=False, message="")
        _, (status, output, errors, outputs) = self.go()
        self.assertEqual(status, "SUCCESS")
        self.assertEqual(outputs["servers"]["2"]["status"], "no_updates")
        self.assertIn("web-02: no updates were available.", output)
        self.assertEqual(outputs["counts"]["updated"], 2)
        self.assertEqual(outputs["counts"]["no_updates"], 1)
        self.assertEqual(self.last["counts"]["no_updates"], 1)

    def test_a_restart_pending_server_does_not_stop_the_run(self):
        self.engine[1] = record("restart_pending")
        _, (status, output, errors, outputs) = self.go()
        self.assertEqual(status, "WARNING")
        self.assertEqual([call["id"] for call in self.engine_calls], [1, 2, 3])
        self.assertEqual(outputs["counts"], {"updated": 2, "no_updates": 0, "restart_pending": 1, "failed": 0, "skipped": 0})
        self.assertEqual(self.last["status"], "warning")

    def test_a_warning_makes_the_run_a_warning(self):
        self.engine[3] = record("updated", warning=True, message="The rescan failed.")
        _, (status, output, errors, outputs) = self.go()
        self.assertEqual(status, "WARNING")
        self.assertEqual(outputs["counts"]["updated"], 3)

    def test_the_run_stops_at_the_first_failure_and_reports_the_rest_as_skipped(self):
        self.engine[2] = record("failed", message="The update stopped before it finished.")
        _, (status, output, errors, outputs) = self.go()
        self.assertEqual([call["id"] for call in self.engine_calls], [1, 2])
        self.assertEqual(outputs["stopped_at"], 2)
        self.assertEqual(outputs["counts"], {"updated": 1, "no_updates": 0, "restart_pending": 0, "failed": 1, "skipped": 1})
        self.assertEqual(outputs["servers"]["3"]["status"], "skipped")
        self.assertIn("not attempted, web-02 failed", outputs["servers"]["3"]["message"])
        self.assertIn("web-02 failed", errors)
        self.assertEqual(status, "FAILURE")

    def test_a_first_server_failure_with_nothing_updated_is_a_failure(self):
        self.engine[1] = record("failed", message="Could not be reached.")
        _, (status, output, errors, outputs) = self.go()
        self.assertEqual(status, "FAILURE")
        self.assertEqual(len(self.engine_calls), 1)
        self.assertEqual(outputs["counts"], {"updated": 0, "no_updates": 0, "restart_pending": 0, "failed": 1, "skipped": 2})
        self.assertEqual(self.last["status"], "failure")

    def test_an_unexpected_error_in_the_engine_is_a_failure_of_that_server(self):
        self.engine[1] = RuntimeError("boom")
        _, (status, output, errors, outputs) = self.go()
        self.assertEqual(outputs["servers"]["1"]["status"], "failed")
        self.assertEqual(len(self.engine_calls), 1)
        self.assertEqual(status, "FAILURE")

    def test_a_server_the_engine_did_not_touch_is_skipped_and_the_run_goes_on(self):
        self.engine[1] = patch.PatchFailed("Job 5 is still running on this server; wait for it to finish.")
        _, (status, output, errors, outputs) = self.go()
        self.assertEqual(outputs["servers"]["1"]["status"], "skipped")
        self.assertIn("still running", outputs["servers"]["1"]["message"])
        self.assertEqual([call["id"] for call in self.engine_calls], [1, 2, 3])
        self.assertEqual(outputs["counts"], {"updated": 2, "no_updates": 0, "restart_pending": 0, "failed": 0, "skipped": 1})
        self.assertEqual(status, "WARNING")
        self.assertIsNone(outputs["stopped_at"])

    def test_an_ineligible_server_is_skipped_with_its_reason(self):
        self.codes[2] = "powered_off"
        _, (status, output, errors, outputs) = self.go()
        self.assertEqual([call["id"] for call in self.engine_calls], [1, 3])
        self.assertEqual(outputs["servers"]["2"], {"hostname": "web-02", "status": "skipped",
                                                  "message": "Skipped: powered off."})
        self.assertEqual(status, "WARNING")

    def test_nothing_eligible_is_a_failure(self):
        for server in self.servers:
            self.codes[server.id] = "powered_off"
        _, (status, output, errors, outputs) = self.go()
        self.assertEqual(status, "FAILURE")
        self.assertEqual(errors, rsrc.NOTHING_UPDATED)
        self.assertEqual(self.engine_calls, [])
        self.assertEqual(outputs["counts"]["skipped"], 3)
        self.assertEqual(self.last["status"], "failure")

    def test_a_resource_with_no_servers_is_a_failure(self):
        self.servers = []
        _, (status, output, errors, outputs) = self.go()
        self.assertEqual(status, "FAILURE")
        self.assertEqual(outputs["order"], [])


# ---------------------------------------------------------------- permissions
class PermissionTests(Base):
    def test_a_server_the_owner_may_not_patch_is_skipped_and_not_named(self):
        self.no_patch.add(2)
        _, (status, output, errors, outputs) = self.go()
        self.assertEqual([call["id"] for call in self.engine_calls], [1, 3])
        item = outputs["servers"]["2"]
        self.assertEqual(item["status"], "skipped")
        self.assertEqual(item["hostname"], "web-02")
        self.assertIn("may not install updates", item["message"])
        self.assertEqual(status, "WARNING")

    def test_a_server_the_owner_may_not_view_is_skipped(self):
        self.no_view.add(1)
        _, (status, output, errors, outputs) = self.go()
        self.assertEqual(self.ids_by_status(outputs, "skipped"), [1])
        self.assertEqual(outputs["servers"]["1"]["hostname"], "Server 1")
        self.assertNotIn("web-01", json.dumps(outputs) + output + json.dumps(self.last))

    def test_the_permission_is_checked_as_the_owner_read_fresh(self):
        self.go()
        self.assertEqual(len(self.permission_profiles), 3)
        self.assertTrue(all(profile.id == OWNER_ID and profile.user.is_active for profile in self.permission_profiles))

    def test_an_owner_who_is_deactivated_mid_run_gets_no_more_servers(self):
        def deactivate(server):
            self.active_user = False
            return record()

        self.engine[1] = deactivate
        _, (status, output, errors, outputs) = self.go()
        self.assertEqual([call["id"] for call in self.engine_calls], [1])
        self.assertEqual(self.ids_by_status(outputs, "skipped"), [2, 3])
        self.assertEqual(outputs["servers"]["2"]["hostname"], "web-02")
        self.assertEqual(status, "WARNING")

    def test_an_owner_whose_profile_is_gone_updates_nothing(self):
        self.profile_gone = True
        _, (status, output, errors, outputs) = self.go()
        self.assertEqual(self.engine_calls, [])
        self.assertEqual(status, "FAILURE")

    def test_a_server_in_an_unassigned_group_is_never_touched(self):
        self.servers[1].group = types.SimpleNamespace(is_unassigned=True, name="Unassigned")
        _, (status, output, errors, outputs) = self.go()
        self.assertEqual([call["id"] for call in self.engine_calls], [1, 3])
        self.assertEqual(outputs["servers"]["2"]["message"], "Skipped: in an Unassigned group.")
        # Not even asked about permission: an Unassigned server is out before the permission check.
        self.assertEqual(len(self.permission_profiles), 2)

    def test_a_server_that_left_the_resource_or_became_historical_is_skipped(self):
        self.servers[0].resource_id = 99
        self.servers[1].status = "HISTORICAL"
        _, (status, output, errors, outputs) = self.go()
        self.assertEqual([call["id"] for call in self.engine_calls], [3])
        self.assertEqual(outputs["servers"]["1"]["message"], "Skipped: no longer in this resource.")
        self.assertEqual(outputs["servers"]["2"]["message"], "Skipped: historical.")

    def test_a_server_that_is_gone_is_skipped(self):
        fetch = self._fetch
        with mock.patch.object(fleet, "_fetch_for_scan", side_effect=lambda i: None if i == 2 else fetch(i)):
            _, (status, output, errors, outputs) = self.go()
        self.assertEqual(outputs["servers"]["2"]["message"], "Skipped: no longer in CloudBolt.")
        self.assertEqual([call["id"] for call in self.engine_calls], [1, 3])


class InFlightAndNamesTests(Base):
    def spy_close(self):
        runs = []
        original = rsrc._Run.close

        def close(run):
            runs.append(run)
            return original(run)

        return runs, mock.patch.object(rsrc._Run, "close", close)

    def test_a_cancel_during_an_update_marks_that_server_and_skips_the_rest(self):
        self.engine[2] = CancelJob()
        runs, spy = self.spy_close()
        with spy, self.assertRaises(CancelJob):
            self.go()
        run = runs[0]
        self.assertEqual(run.results[1]["status"], "updated")
        self.assertEqual(run.results[2]["status"], "failed")
        self.assertEqual(run.results[2]["message"], rsrc.IN_FLIGHT)
        self.assertIn("may still finish", rsrc.IN_FLIGHT)
        self.assertEqual(run.results[3]["status"], "skipped")
        self.assertIn("web-02: The job was canceled while this server was updating.", "\n".join(run.lines))
        self.assertEqual(self.last["status"], "warning")
        self.assertEqual(self.last["counts"]["failed"], 1)
        self.assertEqual(self.last["counts"]["skipped"], 1)
        self.assertTrue(any(patch.GUEST_KEEPS_RUNNING in m for m, d, t in self.progress_calls))

    def test_a_cancel_in_the_first_server_skips_the_two_after_it(self):
        self.engine[1] = CancelJob()
        runs, spy = self.spy_close()
        with spy, self.assertRaises(CancelJob):
            self.go()
        self.assertEqual(runs[0].results[1]["message"], rsrc.IN_FLIGHT)
        self.assertEqual(self.last["counts"]["skipped"], 2)

    def test_an_error_in_the_fetch_of_one_server_skips_it_and_goes_on(self):
        def broken(server_id):
            if server_id == 2:
                raise RuntimeError("db down")
            return self._fetch(server_id)

        with mock.patch.object(fleet, "_fetch_for_scan", side_effect=broken):
            _, (status, output, errors, outputs) = self.go()
        self.assertEqual([call["id"] for call in self.engine_calls], [1, 3])
        self.assertEqual(outputs["servers"]["2"]["status"], "skipped")
        self.assertIn("db down", outputs["servers"]["2"]["message"])
        self.assertFalse(any(patch.GUEST_KEEPS_RUNNING in m for m, d, t in self.progress_calls))
        self.assertEqual(status, "WARNING")

    def test_the_error_text_is_cleaned_like_the_engine_cleans_it(self):
        def broken(server_id):
            raise RuntimeError("x" * 500)

        with mock.patch.object(fleet, "_fetch_for_scan", side_effect=broken):
            _, (status, output, errors, outputs) = self.go()
        self.assertLessEqual(len(outputs["servers"]["1"]["message"]), 300)
        self.assertEqual(self.engine_calls, [])

    def test_an_error_in_a_permission_check_skips_that_server_only(self):
        def boom(profile, server):
            if server.id == 1:
                raise RuntimeError("perm down")
            return True

        with mock.patch.object(hooks, "server_patch_permitted", side_effect=boom):
            _, (status, output, errors, outputs) = self.go()
        self.assertEqual([call["id"] for call in self.engine_calls], [2, 3])
        self.assertIn("perm down", outputs["servers"]["1"]["message"])

    def test_servers_the_owner_cannot_view_are_never_named(self):
        self.no_view.add(2)
        _, (status, output, errors, outputs) = self.go()
        text = json.dumps(outputs) + output + json.dumps(self.last)
        self.assertNotIn("web-02", text)
        self.assertEqual(outputs["servers"]["2"]["hostname"], "Server 2")
        self.assertEqual(outputs["servers"]["1"]["hostname"], "web-01")
        self.assertNotIn("web-02", " ".join(m for m, d, t in self.progress_calls))

    def test_the_not_attempted_lines_do_not_name_an_unviewable_server(self):
        self.no_view.add(3)
        self.engine[2] = record("failed", message="stopped")
        _, (status, output, errors, outputs) = self.go()
        self.assertNotIn("web-03", output + json.dumps(outputs))
        self.assertIn("Server 3: skipped", output)

    def test_the_not_attempted_lines_written_at_close_do_not_name_one_either(self):
        self.no_view.add(3)
        self.engine[2] = CancelJob()
        runs, spy = self.spy_close()
        with spy, self.assertRaises(CancelJob):
            self.go()
        self.assertEqual(runs[0].results[3]["hostname"], "Server 3")
        self.assertNotIn("web-03", "\n".join(runs[0].lines))


# ---------------------------------------------------------------- gates, budget, cancel, marker
class GateTests(Base):
    def test_patching_off_is_a_failure_that_touches_nothing(self):
        self.settings["patching"] = False
        _, (status, output, errors, outputs) = self.go()
        self.assertEqual((status, errors), ("FAILURE", patch.PATCHING_OFF))
        self.assertEqual(self.engine_calls, [])
        self.assertEqual(self.marker_writes, [])
        self.assertEqual(self.record_writes, [])

    def test_an_action_an_admin_switched_off_does_not_run(self):
        self.action_state = {"exists": True, "enabled": False, "action_id": 9}
        _, (status, output, errors, outputs) = self.go()
        self.assertEqual((status, errors), ("FAILURE", rsrc.ACTION_OFF))
        self.assertEqual(self.engine_calls, [])
        self.assertEqual(self.marker_writes, [])
        self.assertEqual(self.record_writes, [])

    def test_a_missing_action_or_an_unreadable_state_does_not_stop_the_run(self):
        self.action_state = {"exists": False, "enabled": False, "action_id": None}
        self.assertEqual(self.go()[1][0], "SUCCESS")
        with mock.patch.object(hooks, "resource_patch_action_state", side_effect=RuntimeError("x")):
            self.assertEqual(self.go(job=make_job(12))[1][0], "SUCCESS")

    def test_patching_off_is_reported_before_the_action_state(self):
        self.settings["patching"] = False
        self.action_state = {"exists": True, "enabled": False, "action_id": 9}
        self.assertEqual(self.go()[1][2], patch.PATCHING_OFF)

    def test_a_job_without_an_owner_updates_nothing(self):
        job = make_job(owner=None)
        _, (status, output, errors, outputs) = self.go(job=job)
        self.assertEqual(status, "FAILURE")
        self.assertIn("no owner", errors)
        self.assertEqual(self.engine_calls, [])
        self.assertEqual(self.marker_writes, [])

    def test_exactly_one_resource_is_needed(self):
        job = make_job()
        for found in ([], [types.SimpleNamespace(id=1), types.SimpleNamespace(id=2)]):
            status, output, errors, outputs = rsrc.run_resource_patch(job, resources=found)
            self.assertEqual((status, errors), ("FAILURE", rsrc.NO_RESOURCE))
        status, output, errors, outputs = rsrc.run_resource_patch(job)
        self.assertEqual(status, "FAILURE")
        self.assertEqual(self.engine_calls, [])

    def test_the_resources_keyword_works_like_the_resource_keyword(self):
        job = make_job()
        status, output, errors, outputs = rsrc.run_resource_patch(
            job, resources=[types.SimpleNamespace(id=RESOURCE_ID, name="Web tier")], restart_if_needed=True)
        self.assertEqual(status, "SUCCESS")

    def test_a_second_run_is_refused_and_leaves_the_first_alone(self):
        self.live(77)
        before = dict(self.marker)
        _, (status, output, errors, outputs) = self.go()
        self.assertEqual(status, "FAILURE")
        self.assertEqual(errors, "Job 77 is already running on this resource.")
        self.assertEqual(self.engine_calls, [])
        self.assertEqual(self.marker, before)
        self.assertEqual(self.record_writes, [])

    def test_a_scan_of_the_resource_also_refuses_it(self):
        self.live(78, kind="scan")
        _, (status, output, errors, outputs) = self.go()
        self.assertEqual((status, errors), ("FAILURE", "Job 78 is already running on this resource."))

    def test_a_stale_marker_does_not_refuse_it(self):
        self.marker = {"job_id": 5, "started": "x", "requested_by": "bob", "server_ids": [], "kind": "patch"}
        _, (status, output, errors, outputs) = self.go()
        self.assertEqual(status, "SUCCESS")
        self.assertEqual(self.marker_writes[0]["job_id"], 11)

    def test_a_rerun_of_the_same_job_is_let_in(self):
        self.live(11)
        _, (status, output, errors, outputs) = self.go()
        self.assertEqual(status, "SUCCESS")

    def test_an_unreadable_marker_means_no_run(self):
        with mock.patch.object(store, "read_resource_run", side_effect=store.StoreError("bad")):
            _, (status, output, errors, outputs) = self.go()
        self.assertEqual(status, "FAILURE")
        self.assertEqual(errors, fleet.RESOURCE_STATE_ERROR)
        self.assertEqual(self.engine_calls, [])


class BudgetTests(Base):
    def test_no_server_starts_when_its_worst_case_would_pass_the_job_limit(self):
        def slow(server):
            self.clock_now += 10000
            return record()

        for server in self.servers:
            self.engine[server.id] = slow
        _, (status, output, errors, outputs) = self.go()
        # Worst case 150 minutes: servers start at 0 and 10000 seconds, not at 20000 (20000 + 9000 > 27000).
        self.assertEqual([call["id"] for call in self.engine_calls], [1, 2])
        self.assertEqual(outputs["servers"]["3"]["status"], "skipped")
        self.assertIn("8 hour limit", outputs["servers"]["3"]["message"])
        self.assertIn("150 minutes", outputs["servers"]["3"]["message"])
        self.assertEqual(status, "WARNING")

    def test_the_worst_case_follows_the_settings(self):
        self.settings.update(patch_timeout_min=300, restart_wait_min=120)

        def slow(server):
            self.clock_now += 1
            return record()

        self.engine[1] = slow
        self.go()
        # 300 + 120 + 30 minutes is exactly 27000 seconds: server 1 fits at time 0, server 2 not a second later.
        self.assertEqual([call["id"] for call in self.engine_calls], [1])

    def test_a_cancel_between_servers_skips_the_rest(self):
        def cancel(server):
            self.cancelled = True
            return record()

        self.engine[1] = cancel
        _, (status, output, errors, outputs) = self.go()
        self.assertEqual([call["id"] for call in self.engine_calls], [1])
        self.assertEqual(self.ids_by_status(outputs, "skipped"), [2, 3])
        self.assertIn("canceled", outputs["servers"]["2"]["message"])
        self.assertEqual(status, "WARNING")
        self.assertIsNone(self.marker)

    def test_a_cancel_that_raises_clears_the_marker_writes_the_record_and_goes_on_raising(self):
        self.engine[2] = CancelJob()
        with self.assertRaises(CancelJob):
            self.go()
        self.assertIsNone(self.marker)
        self.assertEqual(len(self.record_writes), 1)
        self.assertEqual(self.last["status"], "warning")
        self.assertEqual(self.last["counts"], {"updated": 1, "no_updates": 0, "restart_pending": 0, "failed": 1, "skipped": 1})
        self.assertTrue(any(patch.GUEST_KEEPS_RUNNING in message for message, done, total in self.progress_calls))

    def test_the_marker_is_cleared_when_the_run_ends_in_an_error(self):
        with mock.patch.object(rsrc, "_execute", side_effect=RuntimeError("boom")):
            _, (status, output, errors, outputs) = self.go()
        self.assertEqual(status, "FAILURE")
        self.assertIn("boom", errors)
        self.assertIsNone(self.marker)
        self.assertEqual(self.last["status"], "failure")

    def test_the_marker_of_a_newer_run_is_not_cleared(self):
        def newer(server):
            self.marker = {"job_id": 99, "kind": "patch"}
            return record()

        self.engine[3] = newer
        self.go()
        self.assertEqual(self.marker, {"job_id": 99, "kind": "patch"})


class RecordTests(Base):
    def test_the_record_has_the_contracted_shape(self):
        self.engine[2] = record("restart_pending")
        self.go(restart=False)
        rec = self.last
        self.assertEqual(sorted(rec), ["at", "by", "counts", "job_id", "message", "restart_if_needed", "status"])
        self.assertEqual((rec["by"], rec["job_id"], rec["status"], rec["restart_if_needed"]),
                         ("alice", 11, "warning", False))
        self.assertEqual(rec["counts"], {"updated": 2, "no_updates": 0, "restart_pending": 1, "failed": 0, "skipped": 0})
        self.assertRegex(rec["at"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
        self.assertLessEqual(len(rec["message"]), 300)
        self.assertEqual(len(self.record_writes), 1)

    def test_a_success_is_recorded_as_success(self):
        self.go()
        self.assertEqual(self.last["status"], "success")
        self.assertEqual(self.last["counts"]["updated"], 3)

    def test_a_record_that_cannot_be_written_does_not_end_the_run(self):
        with mock.patch.object(store, "set_resource_patch", side_effect=store.StoreError("disk")):
            _, (status, output, errors, outputs) = self.go()
        self.assertEqual(status, "SUCCESS")
        self.assertIsNone(self.marker)

    def test_the_output_dict_is_json_ready(self):
        _, (status, output, errors, outputs) = self.go()
        json.dumps(outputs)


# ---------------------------------------------------------------- start_job
class StartJobTests(Base):
    def setUp(self):
        super().setUp()
        self.hook_calls = []
        self.created = types.SimpleNamespace(id=21, label="", save=lambda **k: None)
        self.linked = []
        outer = self

        class FakeHook:
            def run_as_job(self, **kw):
                outer.hook_calls.append(kw)
                return [outer.created]

        patcher = mock.patch.object(hooks, "resource_patch_hook", lambda: FakeHook())
        patcher.start()
        self.addCleanup(patcher.stop)
        self.resource = types.SimpleNamespace(id=RESOURCE_ID, name="Web tier",
                                              jobs=types.SimpleNamespace(add=self.linked.append))
        self.owner = types.SimpleNamespace(id=OWNER_ID)

    def test_it_starts_the_hook_and_labels_and_links_the_job(self):
        job = rsrc.start_job(self.owner, self.resource, restart_if_needed="false", parent_job="p")
        self.assertIs(job, self.created)
        self.assertEqual(self.hook_calls, [{"owner": self.owner, "resources": [self.resource], "parent_job": "p",
                                            "context": {"restart_if_needed": False}}])
        self.assertEqual(job.label, "Install updates: Web tier")
        self.assertEqual(self.linked, [job])

    def test_the_restart_flag_defaults_to_true(self):
        rsrc.start_job(self.owner, self.resource)
        self.assertIs(self.hook_calls[0]["context"]["restart_if_needed"], True)

    def test_patching_off_refuses(self):
        self.settings["patching"] = False
        refusal = rsrc.start_job(self.owner, self.resource)
        self.assertIsInstance(refusal, jobs.Refusal)
        self.assertEqual((refusal.message, refusal.status), (patch.PATCHING_OFF, 403))
        self.assertEqual(self.hook_calls, [])

    def test_a_live_run_refuses_with_409(self):
        self.live(77, kind="scan")
        refusal = rsrc.start_job(self.owner, self.resource)
        self.assertEqual((refusal.message, refusal.status), ("Job 77 is already running on this resource.", 409))
        self.assertEqual(self.hook_calls, [])

    def test_a_stale_marker_does_not_refuse(self):
        self.marker = {"job_id": 5, "kind": "patch"}
        self.assertIs(rsrc.start_job(self.owner, self.resource), self.created)

    def test_a_hook_that_creates_nothing_is_a_500(self):
        hook = types.SimpleNamespace(run_as_job=lambda **k: [])
        with mock.patch.object(hooks, "resource_patch_hook", lambda: hook):
            refusal = rsrc.start_job(self.owner, self.resource)
        self.assertEqual(refusal.status, 500)

    def test_an_unreadable_marker_is_a_500(self):
        with mock.patch.object(store, "read_resource_run", side_effect=store.StoreError("bad")):
            refusal = rsrc.start_job(self.owner, self.resource)
        self.assertEqual((refusal.message, refusal.status), (fleet.RESOURCE_STATE_ERROR, 500))

    def test_a_label_that_cannot_be_saved_still_returns_the_job(self):
        def broken(**kw):
            raise RuntimeError("db")

        self.created.save = broken
        self.assertIs(rsrc.start_job(self.owner, self.resource), self.created)


# ---------------------------------------------------------------- the offer and the condition
class OfferTests(Base):
    def setUp(self):
        super().setUp()
        self.resource = types.SimpleNamespace(id=RESOURCE_ID)
        self.profile = types.SimpleNamespace(id=OWNER_ID)

    def test_patchable_ids_keeps_the_order_and_the_rules(self):
        self.no_view.add(1)
        self.no_patch.add(3)
        self.servers.append(make_server(4))
        self.codes[4] = "powered_off"
        self.assertEqual(rsrc.patchable_ids(self.profile, self.resource, "linux"), [2])

    def test_patchable_ids_lists_every_server_that_passes(self):
        self.assertEqual(rsrc.patchable_ids(self.profile, self.resource, "linux", problems={}), [1, 2, 3])

    def test_nothing_viewable_is_nothing(self):
        self.no_view.update({1, 2, 3})
        self.assertEqual(rsrc.patchable_ids(self.profile, self.resource, "linux"), [])
        self.assertIs(rsrc.any_patchable(self.profile, self.resource, "linux"), False)

    def test_any_patchable_is_true_with_one_and_never_raises(self):
        self.assertIs(rsrc.any_patchable(self.profile, self.resource, "linux"), True)
        with mock.patch.object(fleet, "resource_servers", side_effect=RuntimeError("db")):
            self.assertIs(rsrc.any_patchable(self.profile, self.resource, "linux"), False)

    def test_any_patchable_is_false_when_no_server_passes(self):
        for server in self.servers:
            self.codes[server.id] = "unassigned"
        self.assertIs(rsrc.any_patchable(self.profile, self.resource, "linux"), False)


class ConditionTests(Base):
    def resource(self, items):
        queryset = FakeQuerySet(items)
        return types.SimpleNamespace(id=1, server_set=queryset), queryset

    def test_one_exists_query_over_live_supported_servers_outside_unassigned_groups(self):
        resource, queryset = self.resource([1])
        group = types.SimpleNamespace(unassigned=types.SimpleNamespace(ids=lambda: [5, 6]))
        with mock.patch.object(rsrc, "Group", group), \
                mock.patch.object(fleet, "supported_q", return_value="SUPPORTED"):
            self.assertIs(rsrc.has_candidates(resource), True)
        self.assertEqual(queryset.calls, [("exclude", (), {"status": "HISTORICAL"}), ("filter", ("SUPPORTED",), {}),
                                          ("exclude", (), {"group__in": [5, 6]})])

    def test_no_server_is_no_candidate(self):
        resource, queryset = self.resource([])
        group = types.SimpleNamespace(unassigned=types.SimpleNamespace(ids=lambda: []))
        with mock.patch.object(rsrc, "Group", group), \
                mock.patch.object(fleet, "supported_q", return_value="SUPPORTED"):
            self.assertIs(rsrc.has_candidates(resource), False)
        self.assertEqual(len(queryset.calls), 2)

    def test_an_error_is_false(self):
        resource = types.SimpleNamespace(id=1, server_set=None)
        self.assertIs(rsrc.has_candidates(resource), False)

    def should_display(self):
        namespace = {}
        path = os.path.join(PKG_DIR, "vuln_resource_patch_condition.py")
        with open(path, encoding="utf-8") as handle:
            exec(compile(handle.read(), path, "exec"), namespace)
        return namespace["should_display"]

    def modules(self, has_candidates):
        package = types.ModuleType(PACKAGE)
        if has_candidates is not None:
            package.vuln_patch_resource = types.SimpleNamespace(has_candidates=has_candidates)
        return {"xui": types.ModuleType("xui"), PACKAGE: package}

    def test_the_condition_file_asks_has_candidates(self):
        seen = []
        should_display = self.should_display()
        resource = object()
        with mock.patch.dict(sys.modules, self.modules(lambda r: seen.append(r) or True)):
            self.assertIs(should_display(resource), True)
            self.assertIs(should_display(resource, request=None), True)
        self.assertEqual(seen, [resource, resource])
        with mock.patch.dict(sys.modules, self.modules(lambda r: False)):
            self.assertIs(should_display(resource), False)

    def test_the_condition_file_fails_closed(self):
        should_display = self.should_display()

        def boom(resource):
            raise RuntimeError("db")

        with mock.patch.dict(sys.modules, self.modules(boom)):
            self.assertIs(should_display(object()), False)
        with mock.patch.dict(sys.modules, self.modules(None)):
            self.assertIs(should_display(object()), False)


# ---------------------------------------------------------------- the files
class ShimTests(unittest.TestCase):
    def read(self, name):
        with open(os.path.join(PKG_DIR, name), "rb") as handle:
            raw = handle.read()
        raw.decode("ascii")
        self.assertNotIn(b"\n", raw.replace(b"\r\n", b""), name)
        return raw.decode("ascii")

    def test_the_shim_has_exactly_the_one_token_line_the_patch_shim_has(self):
        token = 'RESTART_TOKEN = """' + OPEN + " restart_if_needed " + CLOSE + '"""'
        text = self.read("vuln_resource_patch_job.py")
        self.assertEqual(text.count(token), 1)
        self.assertEqual(self.read("vuln_patch_job.py").count(token), 1)
        rest = text.replace(token, "")
        self.assertNotIn(OPEN, rest)
        self.assertNotIn(CLOSE, rest)

    def test_the_other_new_files_have_no_double_braces(self):
        for name in ("vuln_resource_patch_condition.py", "vuln_patch_resource.py"):
            text = self.read(name)
            self.assertNotIn(OPEN, text, name)
            self.assertNotIn(CLOSE, text, name)

    def test_the_shim_lists_its_modules_in_dependency_order(self):
        text = self.read("vuln_resource_patch_job.py")
        namespace = {}
        exec(compile(text, "shim", "exec"), namespace)
        modules = [name.rsplit(".", 1)[1] for name in namespace["MODULES"]]
        self.assertEqual(modules[-3:], ["vuln_wintask", "vuln_patch", "vuln_patch_resource"])
        for earlier, later in (("vuln_store", "vuln_hooks"), ("vuln_jobs", "vuln_fleet"), ("vuln_fleet", "vuln_patch")):
            self.assertLess(modules.index(earlier), modules.index(later))
        patch_shim = {}
        exec(compile(self.read("vuln_patch_job.py"), "shim", "exec"), patch_shim)
        self.assertEqual(list(namespace["MODULES"][:-1]), list(patch_shim["MODULES"]))

    def test_run_reloads_the_modules_and_hands_over(self):
        namespace = {}
        exec(compile(self.read("vuln_resource_patch_job.py"), "shim", "exec"), namespace)
        reloaded = []
        handed = []

        def run_resource_patch(job, **kw):
            handed.append((job, kw))
            return "SUCCESS", "", "", {}

        fake = types.SimpleNamespace(run_resource_patch=run_resource_patch)
        importer = namespace["importlib"]
        with mock.patch.object(importer, "import_module", side_effect=lambda name: fake), \
                mock.patch.object(importer, "reload", side_effect=lambda module: reloaded.append(module)):
            result = namespace["run"]("JOB", restart_if_needed=True, resources=["r"])
        self.assertEqual(result, ("SUCCESS", "", "", {}))
        self.assertEqual(len(reloaded), len(namespace["MODULES"]))
        self.assertEqual(handed, [("JOB", {"restart_if_needed": True, "resources": ["r"]})])

    def test_the_new_python_files_are_ascii_with_crlf(self):
        for name in ("vuln_patch_resource.py", "vuln_resource_patch_job.py", "vuln_resource_patch_condition.py"):
            self.assertTrue(self.read(name).endswith("\r\n"), name)
            self.assertTrue(re.search(r"\r\n", self.read(name)))


if __name__ == "__main__":
    unittest.main()
