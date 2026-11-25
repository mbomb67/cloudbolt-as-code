"""
Checks for the Windows engine wiring of the Compliance XUI, without CloudBolt:
CloudBolt's modules and oscap_windows are stubs.

    python -m unittest discover -s extensions/XUI-i4kzuy3y/tests -p "test_windows_wiring.py"
"""
import os
import sys
import tempfile
import types
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
EXT = os.path.dirname(HERE)


def _module(name, **attrs):
    module = types.ModuleType(name)
    module.__dict__.update(attrs)
    sys.modules[name] = module
    return module


def _install_stubs():
    tmp = tempfile.mkdtemp(prefix="oscap-test-")
    _module("django")
    _module("django.conf", settings=types.SimpleNamespace(PROSERV_DIR=tmp, CLOUDBOLT_OS_USER="u", CLOUDBOLT_OS_GROUP="g"))

    class Http404(Exception):
        pass

    _module("django.http", FileResponse=object, Http404=Http404, HttpResponseRedirect=object)
    _module("django.urls", reverse=lambda *a, **k: "/x/")
    _module("django.db", IntegrityError=Exception)
    _module("cbhooks")
    _module("cbhooks.models", CloudBoltHook=object)
    _module("infrastructure")
    _module("infrastructure.models", Server=object)
    _module("jobs")
    _module("jobs.models", Job=object)
    _module("resources")
    _module("resources.models", Resource=object)
    _module("utilities")
    _module("utilities.events", add_server_event=lambda *a, **k: None)

    class Logger:
        def __init__(self, *a):
            pass

        def __getattr__(self, name):
            return lambda *a, **k: None

    _module("utilities.logger", ThreadLogger=Logger)
    xui = _module("xui")
    xui.__path__ = [EXT]
    return tmp


_install_stubs()
import importlib  # noqa: E402

# A stand-in for the real engine, which another file provides.
windows_stub = _module("xui.io_cloudbolt_compliance.oscap_windows", run=lambda *a, **k: "")
store = importlib.import_module("xui.io_cloudbolt_compliance.oscap_store")
sys.modules["xui.io_cloudbolt_compliance"].oscap_windows = windows_stub
jobs = importlib.import_module("xui.io_cloudbolt_compliance.oscap_jobs")
oscap_resource = importlib.import_module("xui.io_cloudbolt_compliance.oscap_resource")
rollup = importlib.import_module("xui.io_cloudbolt_compliance.oscap_rollup")

SHA = "a537a0f9a39ccc2231818709f8bb5f8f9b9cbfc2ec0d44dbbfabbe35d4f558ba"

WINDOWS_OUTPUT = "\n".join([
    "OSCAP|engine|scc",
    "OSCAP|os_name|Microsoft Windows Server 2025 Datacenter",
    "OSCAP|oscap_version|5.15",
    "OSCAP|datastream|MS_Windows_Server_2025_STIG|1",
    "OSCAP|profile|MS_Windows_Server_2025_STIG|MAC-1_Classified|MAC-1 Classified",
    "OSCAP|profile|MS_Windows_Server_2025_STIG|MAC-3_Public|MAC-3 Public",
    "OSCAP|ready|1",
    "OSCAP|failed|high WN25-00-000001",
    "OSCAP|failed|medium SV-277982r1181944",
    "OSCAP|stored|report.html|yes|",
    "OSCAP|summary|ver=5.15~os=Windows Server 2025~ds=MS_Windows_Server_2025_STIG~prof=MAC-1_Classified"
    "~title=MAC-1 Classified~exit=2~score=88.5~dur=300~pass=210~fail=2~sev_high=1~sev_medium=1~final=1~ready=1"
    "~st_report=1~st_results=0~st_arf=0",
    "OSCAP|done|1",
])


def server(windows=False, linux=False, status="ACTIVE", tags=(), power="POWERON"):
    family = types.SimpleNamespace(get_base_name=lambda: "Linux" if linux else ("Windows" if windows else "Other"))
    return types.SimpleNamespace(
        id=7, hostname="web-01", status=status, os_family=family, power_status=power,
        is_windows=lambda: windows,
        tags=types.SimpleNamespace(all=lambda: [types.SimpleNamespace(name=t) for t in tags]),
    )


class FakeHooks:
    """A stand-in for CloudBoltHook: rows in a list, the way objects.filter(name=...).first() reads them."""

    rows = []

    class objects:
        @staticmethod
        def filter(name):
            found = [r for r in FakeHooks.rows if r.name == name]
            return types.SimpleNamespace(first=lambda: found[0] if found else None)

    def __init__(self, name="", description="", shared=False):
        self.name, self.description, self.shared, self.module_file = name, description, shared, ""

    def save(self):
        if self not in FakeHooks.rows:
            FakeHooks.rows.append(self)


class HookNameTests(unittest.TestCase):
    def setUp(self):
        FakeHooks.rows = []
        self.hooks = importlib.import_module("xui.io_cloudbolt_compliance.oscap_hooks")
        self.saved = self.hooks.CloudBoltHook
        self.hooks.CloudBoltHook = FakeHooks

    def tearDown(self):
        self.hooks.CloudBoltHook = self.saved

    def test_new_appliance_creates_the_new_name(self):
        hook = self.hooks.scan_hook()
        self.assertEqual(hook.name, "Compliance Scan")
        self.assertEqual(len(FakeHooks.rows), 1)

    def test_old_row_is_renamed_not_duplicated(self):
        old = FakeHooks("OpenSCAP Scan")
        old.module_file = "/old/place/oscap_job.py"
        FakeHooks.rows.append(old)
        hook = self.hooks.scan_hook()
        self.assertIs(hook, old)
        self.assertEqual(old.name, "Compliance Scan")
        self.assertTrue(old.module_file.endswith("oscap_job.py"))
        self.assertNotIn("old/place", old.module_file.replace("\\", "/"))
        self.assertEqual(len(FakeHooks.rows), 1)

    def test_resource_old_row_is_renamed(self):
        old = FakeHooks("OpenSCAP Resource Scan")
        FakeHooks.rows.append(old)
        self.assertEqual(self.hooks.resource_scan_hook().name, "Compliance Resource Scan")
        self.assertEqual(len(FakeHooks.rows), 1)

    def test_new_row_wins_over_old(self):
        new, old = FakeHooks("Compliance Scan"), FakeHooks("OpenSCAP Scan")
        FakeHooks.rows.extend([new, old])
        self.assertIs(self.hooks.scan_hook(), new)


class SettingsTests(unittest.TestCase):
    def data(self, **extra):
        base = {"backend": "local", "windows_default_profile": "MAC-1_Classified", "scc_version": "5.15",
                "scc_sha256": SHA, "scc_url": store.DEFAULT_SETTINGS["scc_url"]}
        base.update(extra)
        return base

    def test_defaults(self):
        clean, errors = store.validate_settings(self.data())
        self.assertEqual(errors, [])
        self.assertEqual(clean["scc_version"], "5.15")
        self.assertEqual(clean["scc_sha256"], SHA)
        self.assertEqual(clean["windows_default_profile"], "MAC-1_Classified")
        self.assertIn("{version}", clean["scc_url"])

    def test_empty_fields_fall_back(self):
        clean, errors = store.validate_settings({"backend": "local"})
        self.assertEqual(errors, [])
        self.assertEqual(clean["scc_version"], "5.15")
        self.assertEqual(clean["scc_sha256"], "")  # a blank checksum stays blank
        self.assertEqual(clean["scc_url"], store.DEFAULT_SETTINGS["scc_url"])

    def test_version(self):
        for good in ("5.15", "5.9.1", "latest", "LATEST"):
            clean, errors = store.validate_settings(self.data(scc_version=good, scc_sha256=""))
            self.assertEqual(errors, [], good)
            self.assertEqual(clean["scc_version"], good.lower())
        for bad in ("5", "5.x", "v5.15", "5.15.1.2", "5.15; rm", "latest "[:-1] + "x"):
            _clean, errors = store.validate_settings(self.data(scc_version=bad))
            self.assertTrue(errors, bad)

    def test_sha_ignored_for_latest(self):
        clean, errors = store.validate_settings(self.data(scc_version="latest"))
        self.assertEqual(errors, [])
        self.assertEqual(clean["scc_sha256"], "")

    def test_sha_must_be_64_hex_or_empty(self):
        clean, errors = store.validate_settings(self.data(scc_sha256=SHA.upper()))
        self.assertEqual((errors, clean["scc_sha256"]), ([], SHA))
        clean, errors = store.validate_settings(self.data(scc_sha256=""))
        self.assertEqual((errors, clean["scc_sha256"]), ([], ""))
        for bad in (SHA[:-1], SHA + "0", "g" * 64):
            _clean, errors = store.validate_settings(self.data(scc_sha256=bad))
            self.assertTrue(errors, bad)

    def test_url(self):
        ok = "https://mirror.example.com/scc.zip"
        clean, errors = store.validate_settings(self.data(scc_url=ok))
        self.assertEqual((errors, clean["scc_url"]), ([], ok))
        for bad in ("http://a.example/x.zip", "https://a.example/x y.zip", 'https://a.example/"x', "https://a.example/'x",
                    "https://a.example/x\x01", "ftp://a.example/x", "https://a.example/`x", "https://"):
            clean, errors = store.validate_settings(self.data(scc_url=bad))
            self.assertTrue(errors, repr(bad))
            self.assertEqual(clean["scc_url"], store.DEFAULT_SETTINGS["scc_url"])

    def test_windows_profile(self):
        clean, errors = store.validate_settings(self.data(windows_default_profile="MAC-2_Sensitive"))
        self.assertEqual((errors, clean["windows_default_profile"]), ([], "MAC-2_Sensitive"))
        for bad in ("MAC 1", "a;b", "x" * 101):
            _clean, errors = store.validate_settings(self.data(windows_default_profile=bad))
            self.assertTrue(errors, bad)

    def test_get_settings_repairs_saved_values(self):
        saved = {"scc_version": "nonsense", "scc_sha256": "zz", "scc_url": "http://x", "windows_default_profile": "a b"}
        with mock.patch.object(store, "_read_json", return_value=saved):
            cfg = store.get_settings()
        self.assertEqual(cfg["scc_version"], "5.15")
        self.assertEqual(cfg["scc_sha256"], "")
        self.assertEqual(cfg["scc_url"], store.DEFAULT_SETTINGS["scc_url"])
        self.assertEqual(cfg["windows_default_profile"], "MAC-1_Classified")
        with mock.patch.object(store, "_read_json", return_value={"scc_version": "latest", "scc_sha256": SHA}):
            self.assertEqual(store.get_settings()["scc_sha256"], "")
        with mock.patch.object(store, "_read_json", return_value={}):
            cfg = store.get_settings()
        self.assertEqual((cfg["scc_version"], cfg["scc_sha256"]), ("5.15", SHA))

    def test_job_settings_carry_scc(self):
        job_cfg = store.job_settings(store.get_settings())
        for key in ("windows_default_profile", "scc_version", "scc_sha256", "scc_url"):
            self.assertIn(key, job_cfg)


class TabTests(unittest.TestCase):
    def test_windows_shows_tab(self):
        self.assertTrue(jobs.shows_tab(server(windows=True), "linux"))
        self.assertTrue(jobs.shows_tab(server(linux=True), "linux"))
        self.assertFalse(jobs.shows_tab(server(), "linux"))

    def test_label_visibility(self):
        self.assertFalse(jobs.shows_tab(server(windows=True), "label"))
        self.assertTrue(jobs.shows_tab(server(windows=True, tags=("Compliance",)), "label"))
        self.assertTrue(jobs.shows_tab(server(windows=True, tags=("OpenScap",)), "label"))
        self.assertFalse(jobs.shows_tab(server(windows=True, tags=("Other",)), "label"))

    def test_historical_hidden(self):
        self.assertFalse(jobs.shows_tab(server(windows=True, status="HISTORICAL"), "linux"))

    def test_resource_eligible(self):
        self.assertTrue(oscap_resource.eligible(server(windows=True), "linux"))
        self.assertFalse(oscap_resource.eligible(server(windows=True), "label"))

    def test_openshift_vm_never_windows(self):
        vm = server(windows=True)
        vm.ovirtserverinfo = object()
        self.assertFalse(jobs._is_openshift_vm(vm))
        vm = server(linux=True)
        vm.ovirtserverinfo = object()
        self.assertTrue(jobs._is_openshift_vm(vm))


class StartTests(unittest.TestCase):
    def test_start_accepts_windows(self):
        created = types.SimpleNamespace(id=5, label="", save=lambda **k: None, get_absolute_url=lambda: "/job/5/")
        hook = mock.Mock()
        hook.run_as_job.return_value = [created]
        win = server(windows=True)
        win.jobs = types.SimpleNamespace(add=lambda job: None)
        cfg = dict(store.DEFAULT_SETTINGS, backend="s3", allow_install=True)
        with mock.patch.object(store, "get_settings", return_value=cfg), \
                mock.patch.object(store, "server_lock", mock.MagicMock()), \
                mock.patch.object(store, "read_index", return_value={"state": {}, "scans": []}), \
                mock.patch.object(store, "set_active"), \
                mock.patch.object(jobs.oscap_hooks, "scan_hook", return_value=hook), \
                mock.patch.object(jobs.oscap_repo, "upload_targets",
                                  return_value={"urls": {"report.html": "https://s3/x"}, "token": "", "region": "us-east-1"}), \
                mock.patch.object(jobs.oscap_repo, "location", return_value={"backend": "s3"}):
            result = jobs.start(types.SimpleNamespace(), win, "scan", "MS_Windows_Server_2025_STIG",
                                "MAC-1_Classified", True)
        self.assertIs(result, created)
        context = hook.run_as_job.call_args.kwargs["context"]
        self.assertFalse(context["oscap_pull"])
        self.assertTrue(context["oscap_cfg"]["allow_install"])
        self.assertEqual(context["oscap_cfg"]["scc_version"], "5.15")

    def test_start_still_checks_ids(self):
        refusal = jobs.start(types.SimpleNamespace(), server(windows=True), "scan", "bad name", "", False)
        self.assertIsInstance(refusal, jobs.Refusal)


class OutputTests(unittest.TestCase):
    def test_parse_windows_marker_text(self):
        parsed = jobs.parse_output(WINDOWS_OUTPUT)
        self.assertEqual(parsed["engine"], "scc")
        self.assertEqual(parsed["oscap_version"], "5.15")
        self.assertEqual(parsed["datastreams"][0]["file"], "MS_Windows_Server_2025_STIG")
        self.assertTrue(parsed["datastreams"][0]["recommended"])
        self.assertEqual([p["id"] for p in parsed["datastreams"][0]["profiles"]], ["MAC-1_Classified", "MAC-3_Public"])
        self.assertEqual([f["id"] for f in parsed["failed"]], ["WN25-00-000001", "SV-277982r1181944"])
        self.assertEqual(parsed["exit_code"], 2)

    def test_linux_output_is_oscap(self):
        self.assertEqual(jobs.parse_output("OSCAP|ready|1\nOSCAP|done|1")["engine"], "oscap")
        self.assertEqual(jobs.parse_output("OSCAP|engine|bogus")["engine"], "oscap")

    def test_state_and_scan_record_carry_engine(self):
        parsed = jobs.parse_output(WINDOWS_OUTPUT)
        state = jobs.state_from(parsed)
        self.assertEqual(state["engine"], "scc")
        self.assertEqual(state["oscap_version"], "5.15")
        scan = jobs.scan_summary(parsed, "20261123T101500Z-abc123", "now", None, "mike", "", "", {"backend": "s3"}, 300)
        self.assertEqual(scan["engine"], "scc")
        self.assertEqual(scan["status"], "completed")
        self.assertEqual(scan["datastream"], "MS_Windows_Server_2025_STIG")
        self.assertEqual(scan["profile_id"], "MAC-1_Classified")
        self.assertEqual(scan["counts"]["fail"], 2)
        self.assertEqual(scan["files"], {"report.html": True, "results.xml.gz": False, "arf.xml.gz": False})
        linux = jobs.scan_summary(jobs.parse_output("OSCAP|done|1"), "20261123T101500Z-abc123", "now", None, "m", "", "", {}, 1)
        self.assertEqual(linux["engine"], "oscap")
        self.assertEqual(jobs.state_from(jobs.parse_output("OSCAP|ready|1"))["engine"], "oscap")

    def test_rollup_does_not_assume_ssg_ids(self):
        rolled = rollup.rollup([
            {"id": 1, "hostname": "a", "score": 90, "fail": 2,
             "failed_rules": [{"id": "WN25-00-000001", "severity": "high"},
                              {"id": "xccdf_org.ssg_rule_x", "severity": "low"}]},
            {"id": 2, "hostname": "b", "score": 80, "fail": 1,
             "failed_rules": [{"id": "WN25-00-000001", "severity": "high"}]},
        ])
        self.assertEqual(rolled["unique_failed"], 2)
        self.assertEqual(rolled["on_every"], 1)


class RunTests(unittest.TestCase):
    def run_job(self, output=None, error=None, mode="scan", cfg_extra=None):
        win = server(windows=True)
        win.execute_script = mock.Mock(side_effect=AssertionError("a Windows server must not use execute_script"))
        calls = []

        def fake_run(srv, params, timeout, progress=None):
            calls.append((srv, params, timeout))
            progress("Downloading SCC")
            if error is not None:
                exc = RuntimeError(error)
                exc.output = output or ""
                raise exc
            return output

        cfg = store.job_settings(dict(store.DEFAULT_SETTINGS, allow_install=True, local_insecure_upload=False))
        cfg.update(cfg_extra or {})
        saved = {}
        job = mock.Mock()
        with mock.patch.object(windows_stub, "run", fake_run), \
                mock.patch.object(store, "read_index", return_value={"state": {}, "scans": []}), \
                mock.patch.object(store, "set_state", side_effect=lambda sid, state: saved.__setitem__("state", state)), \
                mock.patch.object(store, "add_scan", side_effect=lambda sid, scan: saved.__setitem__("scan", scan)), \
                mock.patch.object(jobs.oscap_repo, "prune"):
            result = jobs._run(
                job, win, mode, "MS_Windows_Server_2025_STIG", "MAC-1_Classified", "20261123T101500Z-abc123",
                dict(cfg, backend="s3"), True, "", {"report.html": "https://s3/r", "results.xml.gz": "https://s3/x"},
                "", {"backend": "s3"}, "now", "mike",
            )
        return result, calls, saved, job

    def test_scan_calls_engine_with_params(self):
        (status, message, errors, outputs), calls, saved, job = self.run_job(output=WINDOWS_OUTPUT)
        self.assertEqual(len(calls), 1)
        _srv, params, timeout = calls[0]
        self.assertEqual(timeout, 30 * 60)
        self.assertEqual(params, {
            "mode": "scan", "scan_id": "20261123T101500Z-abc123", "benchmark": "MS_Windows_Server_2025_STIG",
            "profile": "MAC-1_Classified", "default_profile": "MAC-1_Classified", "allow_install": True,
            "scc_version": "5.15", "scc_sha256": SHA, "scc_url": store.DEFAULT_SETTINGS["scc_url"],
            "uploads": {"report.html": "https://s3/r", "results.xml.gz": "https://s3/x"},
            "upload_token": "", "insecure": False,
        })
        self.assertEqual(saved["state"]["engine"], "scc")
        self.assertEqual(saved["scan"]["engine"], "scc")
        self.assertEqual(status, "WARNING")  # two of the three files were not stored
        job.set_progress.assert_any_call("Downloading SCC")

    def test_transport_failure_keeps_partial_output_and_engine(self):
        (status, _m, errors, _o), calls, saved, _job = self.run_job(
            output="OSCAP|engine|scc\nOSCAP|problem|no admin rights", error="The script exited with code 1.", mode="check")
        self.assertEqual(status, "WARNING")
        self.assertEqual(saved["state"]["engine"], "scc")
        self.assertEqual(saved["state"]["problem"], "no admin rights")

    def test_total_failure_still_records_scc(self):
        (status, _m, errors, _o), _calls, saved, _job = self.run_job(output="", error="timed out", mode="check")
        self.assertEqual(saved["state"]["engine"], "scc")
        self.assertFalse(saved["state"]["ready"])

    def test_bad_ids_never_reach_the_engine(self):
        with self.assertRaises(jobs.ScanError):
            jobs.windows_params("scan", "bad name", "", "20261123T101500Z-abc123", {"allow_install": False, "insecure": False}, {}, "")
        with self.assertRaises(jobs.ScanError):
            jobs.windows_params("scan", "", "a;b", "20261123T101500Z-abc123", {"allow_install": False, "insecure": False}, {}, "")


if __name__ == "__main__":
    unittest.main()
