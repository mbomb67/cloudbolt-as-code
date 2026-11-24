"""
Checks for the update job of the Vulnerabilities XUI (vuln_patch.py, vuln_patch.sh, vuln_patch_job.py, vuln_api.py
and the handover edits to vuln_jobs.py) without CloudBolt: CloudBolt's modules are stubs, the findings store and the
server's state are in memory, and the guest is a scripted fake that answers each mode of the script.

    python -B -m unittest discover -s extensions/XUI-wqrj0e3p/tests -p "test_vuln_patch.py"

The stubs are in place only while this file imports the extension's modules (the same swap test_vuln_fleet.py makes,
with the modules this file needs), so another test file's stubs are not disturbed.
"""
import contextlib
import importlib
import os
import re
import shutil
import subprocess
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
    """Stands in for utilities.exceptions.TimeoutException."""


class FakeLogger:
    def __init__(self, *a):
        pass

    def __getattr__(self, name):
        return lambda *a, **k: None


def _module(name, **attrs):
    module = types.ModuleType(name)
    module.__dict__.update(attrs)
    return module


TMP = tempfile.mkdtemp(prefix="vuln-patch-test-")
STUBS = {}
EVENTS = []


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
_stub("utilities.events", add_server_event=lambda *a, **k: EVENTS.append((a, k)))
_stub("utilities.exceptions", TimeoutException=TimeoutException)
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
                 "vuln_fleet", "vuln_patch", "vuln_api")
        return {name: importlib.import_module(PACKAGE + "." + name) for name in names}
    finally:
        for key in [k for k in sys.modules if k == "xui" or k.startswith(PACKAGE) or k in STUBS]:
            del sys.modules[key]
        sys.modules.update(saved)


MODS = _import_extension()
store, db, tools, repo, hooks, jobs, fleet, patch, api = (MODS[n] for n in (
    "vuln_store", "vuln_db", "vuln_tools", "vuln_repo", "vuln_hooks", "vuln_jobs", "vuln_fleet", "vuln_patch",
    "vuln_api"))

# The template engine's markers, built here so this file has none of its own.
OPEN, CLOSE = b"{" * 2, b"}" * 2
BOOT_1 = "11111111-2222-3333-4444-555555555555"
BOOT_2 = "66666666-7777-8888-9999-000000000000"
RUN = "patch-20261124T101500Z-abc123"


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
        jobs=types.SimpleNamespace(add=lambda job: None),
    )
    data.update(over)
    return types.SimpleNamespace(**data)


def windows_server(**over):
    data = dict(os_family=types.SimpleNamespace(get_base_name=lambda: "Windows", name="Windows"),
                is_windows=lambda: True)
    data.update(over)
    return make_server(**data)


def solaris_server(**over):
    return make_server(os_family=types.SimpleNamespace(get_base_name=lambda: "Solaris", name="Solaris"), **over)


def out(**fields):
    """Script output: a CBVULN line per field, then the final done line."""
    lines = ["CBVULN|{}|{}".format(key, value) for key, value in fields.items()]
    return "\n".join(lines + ["CBVULN|done|1"]) + "\n"


class Guest:
    """The server: answers each call of the script by its MODE. A mode's answers are used in turn and the last one
    repeats; an exception in the list is raised."""

    def __init__(self):
        self.calls = []
        self.scripts = []
        self.queues = {}
        self.on_call = None

    def answers(self, mode, *items):
        self.queues[mode] = list(items)

    def __call__(self, server, script, timeout, job=None):
        mode = re.search(r"^MODE=(\w+)", script, re.M).group(1)
        self.calls.append(mode)
        self.scripts.append(script)
        if self.on_call:
            self.on_call(mode)
        queue = self.queues.get(mode)
        if not queue:
            raise AssertionError("no answer for mode " + mode)
        item = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(item, BaseException):
            raise item
        return item


def happy_guest():
    guest = Guest()
    guest.answers("probe", out(boot_id=BOOT_1))
    guest.answers("start", out(state="running", os="Rocky", distro="rhel"))
    guest.answers("poll", out(state="running", phase="Updating packages"),
                  out(state="done", updated=7, failed=0, exit=0, restart=0, kernel_running="k1", kernel_newest="k1"))
    guest.answers("oldkernels", out(removed=0, exit=0, kernel_running="k1", kernel_newest="k1"))
    guest.answers("cleanup", out())
    return guest


class Clock:
    def __init__(self):
        self.now = 0.0

    def sleep(self, seconds):
        self.now += seconds

    def clock(self):
        return self.now


class FakeHook:
    def __init__(self, job_id=11):
        self.calls = []
        self.job = types.SimpleNamespace(id=job_id, label="", save=lambda **k: None)

    def run_as_job(self, **kw):
        self.calls.append(kw)
        return [self.job]


def make_job(job_id=11, username="alice"):
    progress = []
    return types.SimpleNamespace(
        id=job_id, owner=types.SimpleNamespace(user=types.SimpleNamespace(username=username)),
        set_progress=lambda message, **k: progress.append(message), progress=progress,
    )


class Refused:
    def __init__(self, message="no", status=409):
        self.message, self.status = message, status


class Base(unittest.TestCase):
    """In-memory state of one server, a guest, a clock, and the settings the job reads."""

    def setUp(self):
        del EVENTS[:]
        self.state = {"active": None, "last": None, "history": []}
        self.settings = {"visibility": "linux", "scan_timeout_min": 20, "patch_timeout_min": 90,
                         "restart_wait_min": 30, "patching": True}
        self.guest = happy_guest()
        self.clock = Clock()
        self.markers = []
        self.cancelled = False
        self.active_jobs = {}
        self.patches = [
            mock.patch.dict(sys.modules, STUBS),
            mock.patch.object(store, "get_settings", side_effect=lambda: dict(self.settings)),
            mock.patch.object(store, "job_settings", return_value={"timeout_min": 20}),
            mock.patch.object(store, "start_lock", lambda server_id: contextlib.nullcontext()),
            mock.patch.object(store, "read_state", side_effect=lambda server_id: self.state),
            mock.patch.object(store, "_update", side_effect=lambda server_id, change: change(self.state)),
            mock.patch.object(store, "set_active", side_effect=self._set_active),
            mock.patch.object(store, "clear_active", side_effect=self._clear_active),
            mock.patch.object(tools, "status", return_value={"ready": True, "db": {"built": "b"}}),
            mock.patch.object(fleet, "default_base_url", return_value="https://cb.example.com"),
            mock.patch.object(fleet, "_cancelled", side_effect=lambda job: self.cancelled),
            mock.patch.object(jobs, "_execute", self.guest),
            mock.patch.object(jobs, "Job", self._job_model()),
            mock.patch.object(patch, "_sleep", self.clock.sleep),
            mock.patch.object(patch, "_clock", self.clock.clock),
            mock.patch.object(db, "server_summary", side_effect=self._summary),
        ]
        for patcher in self.patches:
            patcher.start()
            self.addCleanup(patcher.stop)
        self.summaries = [{"vulns": 10, "vuln_fixable": 8}, {"vulns": 2, "vuln_fixable": 0}]
        self.rescan_job = types.SimpleNamespace(id=99, status="SUCCESS")
        self.rescan_started = types.SimpleNamespace(id=99, wait_for_completion=lambda timeout: self.rescan_job)
        self.rescan_calls = []
        self.start_result = self.rescan_started

    def _set_active(self, server_id, marker):
        self.state["active"] = marker
        self.markers.append(dict(marker))

    def _clear_active(self, server_id, scan_id=None):
        active = self.state.get("active")
        if scan_id is None or (isinstance(active, dict) and active.get("scan_id") == scan_id):
            self.state["active"] = None

    def _summary(self, server_id):
        return self.summaries.pop(0) if self.summaries else None

    def _job_model(self):
        outer = self

        class Manager:
            def filter(self, **kw):
                job = outer.active_jobs.get(kw.get("id"))
                return types.SimpleNamespace(first=lambda: job)

        return types.SimpleNamespace(objects=Manager())

    def rescan(self):
        """Make jobs.start a spy answering with the scan job."""
        def start(profile, server, base_url, parent_job=None, handover_from=None):
            self.rescan_calls.append({"parent_job": parent_job, "handover_from": handover_from,
                                      "active": self.state.get("active")})
            return self.start_result

        patcher = mock.patch.object(jobs, "start", side_effect=start)
        patcher.start()
        self.addCleanup(patcher.stop)

    def go(self, servers=None, restart=True, job=None):
        job = job or make_job()
        self.rescan()
        return job, patch.run_patch(job, servers=servers or [make_server()], restart_if_needed=restart)


# ---------------------------------------------------------------- the restart flag
class RestartFlagTests(unittest.TestCase):
    def test_values(self):
        for value in (True, "True", "true", "1", "yes", "on"):
            self.assertIs(patch.parse_restart_flag(value, default=False), True, value)
        for value in (False, "false", "False", "0", "no", "off"):
            self.assertIs(patch.parse_restart_flag(value), False, value)

    def test_nothing_or_unreadable_is_the_default(self):
        self.assertIs(patch.parse_restart_flag(None), True)
        self.assertIs(patch.parse_restart_flag(None, default=False), False)
        self.assertIs(patch.parse_restart_flag("maybe"), True)
        self.assertIs(patch.parse_restart_flag("", default=False), False)


# ---------------------------------------------------------------- the guest script
class ScriptTests(unittest.TestCase):
    def path(self):
        return os.path.join(PKG_DIR, "vuln_patch.sh")

    def test_header_quotes_every_value(self):
        script = patch._linux_build_script("reboot", RUN, BOOT_1)
        for line in ("MODE=reboot\n", "RUN_ID=" + RUN + "\n", "BOOT_ID=" + BOOT_1 + "\n", "REBOOT_DELAY_S=20\n"):
            self.assertIn("\n" + line, script)
        self.assertIn("BOOT_ID=''\n", patch._linux_build_script("poll", RUN))

    def test_a_bad_run_id_or_boot_id_is_refused(self):
        for bad in ("x; rm -rf /", "short", "has space in it", "a" * 65, "id$(touch x)", "", None, "abc\nMODE=poll"):
            with self.assertRaises(patch.PatchFailed, msg=repr(bad)):
                patch._linux_build_script("poll", bad)
        for bad in ("; reboot", BOOT_1 + "x", "not-a-uuid", "AAAAAAAA-BBBB-CCCC-DDDD-EEEEEEEEEEEE"):
            with self.assertRaises(patch.PatchFailed, msg=repr(bad)):
                patch._linux_build_script("reboot", RUN, bad)
        with self.assertRaises(patch.PatchFailed):
            patch._linux_build_script("rm", RUN)

    def test_the_file_is_small_ascii_lf_and_has_no_template_marker(self):
        with open(self.path(), "rb") as handle:
            raw = handle.read()
        self.assertLess(len(raw), 32 * 1024)
        raw.decode("ascii")
        self.assertNotIn(b"\r", raw)
        self.assertNotIn(OPEN, raw)
        self.assertNotIn(CLOSE, raw)
        script = patch._linux_build_script("start", RUN)
        self.assertNotIn(OPEN.decode(), script)
        self.assertLess(len(script), 32 * 1024)

    def test_no_other_double_brace_in_the_new_files(self):
        for name in ("vuln_patch.py", "vuln_api.py", "vuln_patch.sh"):
            with open(os.path.join(PKG_DIR, name), "rb") as handle:
                raw = handle.read()
            raw.decode("ascii")
            self.assertNotIn(OPEN, raw, name)
        with open(os.path.join(PKG_DIR, "vuln_patch_job.py"), "rb") as handle:
            lines = [line for line in handle.read().decode("ascii").splitlines() if OPEN.decode() in line]
        self.assertEqual(lines, ['RESTART_TOKEN = """' + OPEN.decode() + ' restart_if_needed ' + CLOSE.decode() + '"""'])

    def test_the_job_file_reloads_the_patch_module(self):
        with open(os.path.join(PKG_DIR, "vuln_patch_job.py"), encoding="ascii") as handle:
            text = handle.read()
        self.assertIn('"xui.io_cloudbolt_vulnerabilities.vuln_patch"', text)
        self.assertIn("run_patch(job, **kwargs)", text)

    def test_the_script_passes_bash_n(self):
        bash = shutil.which("bash")
        if not bash:
            self.skipTest("bash is not installed")
        with open(self.path(), "rb") as handle:
            raw = handle.read()
        for script in (raw, patch._linux_build_script("poll", RUN).encode("ascii")):
            done = subprocess.run([bash, "-n"], input=script, capture_output=True, timeout=60)
            self.assertEqual(done.returncode, 0, done.stderr.decode(errors="replace"))

    def test_every_vendor_command_is_documented_where_it_is_used(self):
        with open(self.path(), encoding="ascii") as handle:
            text = handle.read()
        for url in ("https://dnf.readthedocs.io/en/latest/command_ref.html",
                    "https://dnf.readthedocs.io/en/latest/conf_ref.html",
                    "https://dnf-plugins-core.readthedocs.io/en/latest/needs_restarting.html",
                    "https://docs.aws.amazon.com/linux/al2023/ug/deterministic-upgrades.html",
                    "https://docs.aws.amazon.com/linux/al2023/ug/managing-repos-os-updates.html",
                    "https://manpages.ubuntu.com/manpages/noble/man8/apt-get.8.html",
                    "https://www.freedesktop.org/software/systemd/man/latest/systemd-run.html"):
            self.assertIn(url, text)
        self.assertIn("--oldinstallonly", text)
        self.assertIn("Remv", text)
        self.assertIn("Purg", text)


# ---------------------------------------------------------------- the output
class ParseTests(unittest.TestCase):
    def test_unknown_keys_and_other_lines_are_ignored(self):
        parsed = patch.parse_patch_output(
            "noise\nCBVULN|evil|x\nCBVULN|state|running\nCBVULN|phase|Updating packages\nCBVULN|done|1\n")
        self.assertEqual((parsed["state"], parsed["phase"], parsed["done"]), ("running", "Updating packages", True))
        self.assertNotIn("evil", parsed)
        self.assertIs(patch.parse_patch_output, patch._linux_parse)

    def test_ints_are_capped_and_a_later_line_wins(self):
        parsed = patch._linux_parse("CBVULN|updated|99999999999\nCBVULN|failed|-1\nCBVULN|exit|5000\n"
                                    "CBVULN|removed|abc\nCBVULN|updated|12\nCBVULN|updated|7\n")
        self.assertEqual((parsed["updated"], parsed["failed"], parsed["exit"], parsed["removed"]), (7, None, None, None))

    def test_values_outside_their_list_are_dropped(self):
        parsed = patch._linux_parse("CBVULN|state|sleeping\nCBVULN|distro|plan9\nCBVULN|restart|maybe\n"
                                    "CBVULN|boot_id|not-a-uuid\n")
        self.assertEqual((parsed["state"], parsed["distro"], parsed["restart"], parsed["boot_id"]), ("", "", "", ""))
        parsed = patch._linux_parse(out(state="died", distro="amzn2023", restart="unknown", boot_id=BOOT_1))
        self.assertEqual((parsed["state"], parsed["distro"], parsed["restart"], parsed["boot_id"]),
                         ("died", "amzn2023", "unknown", BOOT_1))

    def test_text_is_cleaned_and_notes_are_limited(self):
        parsed = patch._linux_parse("CBVULN|failure|see https://evil.example/x?t=1 now\n"
                                    + "".join("CBVULN|note|n{}\n".format(i) for i in range(9)))
        self.assertNotIn("evil.example", parsed["failure"])
        self.assertEqual(len(parsed["notes"]), patch.MAX_NOTES)
        parsed = patch._linux_parse("CBVULN|reason|" + "x" * 5000)
        self.assertLessEqual(len(parsed["reason"]), patch.MESSAGE_LEN)

    @unittest.skipUnless(shutil.which("bash"), "bash is not installed")
    def test_the_real_script_reports_a_missing_run(self):
        script = patch._linux_build_script("poll", "patch-nothing-here-0001")
        # Run the built launcher (an sh script) the way the guest does, from stdin.
        done = subprocess.run(["sh"], input=script.encode("ascii"), capture_output=True, timeout=60)
        parsed = patch._linux_parse(done.stdout.decode(errors="replace"))
        self.assertTrue(parsed["done"], done.stderr.decode(errors="replace"))
        self.assertEqual(parsed["state"], "missing")


# ---------------------------------------------------------------- eligibility
class EligibilityTests(Base):
    def test_a_linux_server_is_eligible(self):
        self.assertEqual(patch.eligibility(make_server()), "")

    def test_windows_needs_the_microsoft_data_for_the_rescan(self):
        with mock.patch.object(jobs, "msrc_ready", return_value=True):
            self.assertEqual(patch.eligibility(windows_server()), "")
        with mock.patch.object(jobs, "msrc_ready", return_value=False):
            self.assertEqual(patch.eligibility(windows_server()), "no_windows_data")

    def test_another_operating_system_is_unsupported(self):
        self.assertEqual(patch.eligibility(solaris_server()), "unsupported_os")

    def test_unassigned_and_powered_off(self):
        unassigned = make_server(group=types.SimpleNamespace(is_unassigned=True, name="Unassigned"))
        self.assertEqual(patch.eligibility(unassigned), "unassigned")
        self.assertEqual(patch.eligibility(make_server(power_status="POWEROFF")), "powered_off")
        self.assertEqual(patch.eligibility(make_server(status="HISTORICAL")), "historical")

    def test_the_scanner_must_be_ready_for_the_rescan(self):
        with mock.patch.object(tools, "status", return_value={"ready": False, "db": {"built": ""}}):
            self.assertEqual(patch.eligibility(make_server()), "not_set_up")
        with mock.patch.object(fleet, "default_base_url", return_value=""):
            self.assertEqual(patch.eligibility(make_server()), "no_site_url")

    def test_an_error_while_checking_is_unknown(self):
        with mock.patch.object(store, "get_settings", side_effect=RuntimeError("disk")):
            self.assertEqual(patch.eligibility(make_server()), "unknown")


# ---------------------------------------------------------------- start_job
class StartJobTests(Base):
    def setUp(self):
        super().setUp()
        self.hook = FakeHook()
        for patcher in (mock.patch.object(hooks, "patch_hook", create=True, return_value=self.hook),
                        mock.patch.object(jobs, "precheck", return_value=None)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_one_server(self):
        added = []
        server = make_server(jobs=types.SimpleNamespace(add=added.append))
        job = patch.start_job("owner", server=server, restart_if_needed="false")
        self.assertIs(job, self.hook.job)
        call = self.hook.calls[0]
        self.assertEqual((call["owner"], call["servers"], call["parent_job"]), ("owner", [server], None))
        self.assertEqual(call["context"], {"restart_if_needed": False})
        self.assertEqual(job.label, "Install updates: web-01")
        self.assertEqual(added, [job])

    def test_a_list_of_servers(self):
        added = []
        servers = [make_server(id=i, hostname="h{}".format(i), jobs=types.SimpleNamespace(add=added.append))
                   for i in (1, 2, 3)]
        job = patch.start_job("owner", servers=servers, parent_job="parent")
        self.assertEqual(self.hook.calls[0]["servers"], servers)
        self.assertEqual(self.hook.calls[0]["parent_job"], "parent")
        self.assertEqual(self.hook.calls[0]["context"], {"restart_if_needed": True})
        self.assertEqual(job.label, "Install updates: 3 servers")
        self.assertEqual(added, [job, job, job])

    def test_the_first_server_that_cannot_be_updated_refuses_the_whole_start(self):
        servers = [make_server(id=1), windows_server(id=2, hostname="win-01")]
        with mock.patch.object(jobs, "msrc_ready", return_value=False):
            refusal = patch.start_job("owner", servers=servers)
        self.assertEqual(refusal.status, 409)
        self.assertIn("win-01", refusal.message)
        self.assertEqual(self.hook.calls, [])

    def test_the_scan_prechecks_are_applied_per_server(self):
        with mock.patch.object(jobs, "precheck", return_value=jobs.Refusal("not ready", 409)):
            refusal = patch.start_job("owner", server=make_server())
        self.assertEqual(refusal.message, "not ready")
        self.assertEqual(self.hook.calls, [])

    def test_no_server_and_a_job_that_did_not_start(self):
        self.assertEqual(patch.start_job("owner").status, 400)
        self.hook.run_as_job = lambda **kw: []
        self.assertEqual(patch.start_job("owner", server=make_server()).status, 500)


# ---------------------------------------------------------------- one server, end to end
class FlowTests(Base):
    def test_success_with_no_restart_needed(self):
        job, result = self.go()
        status, output, errors, outputs = result
        self.assertEqual((status, errors), ("SUCCESS", ""))
        # No restart was needed, so none is pending: the old kernels go, without a restart.
        self.assertEqual(self.guest.calls, ["probe", "start", "poll", "poll", "oldkernels", "cleanup"])
        self.assertEqual(outputs["counts"], {"updated": 1, "no_updates": 0, "restart_pending": 0, "failed": 0, "skipped": 0})
        record = self.state["patch"]
        self.assertEqual(set(record), {"at", "by", "job_id", "status", "before", "after", "updated", "failed",
                                       "restarted", "restart_needed", "kernels_removed", "duration_s", "message", "family"})
        self.assertEqual((record["status"], record["by"], record["job_id"]), ("updated", "alice", 11))
        self.assertEqual((record["before"], record["after"]), ({"vulns": 10, "vuln_fixable": 8},
                                                               {"vulns": 2, "vuln_fixable": 0}))
        self.assertEqual((record["updated"], record["failed"], record["restarted"], record["restart_needed"],
                          record["kernels_removed"]), (7, 0, False, False, 0))
        self.assertEqual(record["duration_s"], 30)
        self.assertEqual(outputs["servers"]["7"]["hostname"], "web-01")
        self.assertEqual(outputs["servers"]["7"]["status"], "updated")
        self.assertEqual(output, "web-01: 7 packages updated, known vulnerabilities 10 to 2, with a fix 8 to 0.")

    def test_the_marker_is_written_with_kind_patch_and_cleared(self):
        seen = []
        self.guest.on_call = lambda mode: seen.append((mode, dict(self.state["active"] or {})))
        self.go()
        marker = self.markers[0]
        self.assertEqual(set(marker), {"job_id", "scan_id", "started", "requested_by", "kind"})
        self.assertEqual((marker["job_id"], marker["kind"], marker["requested_by"]), (11, "patch", "alice"))
        self.assertTrue(marker["scan_id"].startswith("patch-"))
        self.assertTrue(store.SCAN_ID_RE.fullmatch(marker["scan_id"]))
        self.assertTrue(all(active["kind"] == "patch" for mode, active in seen if mode != "cleanup"))
        self.assertIsNone(self.state["active"])
        self.assertIn(marker["scan_id"], self.guest.scripts[0])

    def test_the_rescan_is_a_handover_child_of_the_job(self):
        job, result = self.go()
        call = self.rescan_calls[0]
        self.assertIs(call["parent_job"], job)
        self.assertEqual(call["handover_from"], 11)
        self.assertEqual(call["active"]["kind"], "patch")

    def test_the_server_event_is_an_info_event(self):
        job, result = self.go()
        self.assertEqual(len(EVENTS), 1)
        args, kwargs = EVENTS[0]
        self.assertEqual(args[0], "INFO")
        self.assertEqual(kwargs["job"], job)

    def test_restart_needed_but_not_allowed_is_restart_pending_and_still_rescans(self):
        self.guest.answers("poll", out(state="done", updated=3, failed=0, exit=0, restart=1, restart_reason="kernel"))
        job, (status, output, errors, outputs) = self.go(restart=False)
        self.assertEqual(status, "WARNING")
        self.assertNotIn("reboot", self.guest.calls)
        self.assertEqual(self.state["patch"]["status"], "restart_pending")
        self.assertTrue(self.state["patch"]["restart_needed"])
        self.assertFalse(self.state["patch"]["restarted"])
        self.assertEqual(len(self.rescan_calls), 1)
        self.assertEqual(outputs["counts"]["restart_pending"], 1)
        self.assertIn("restart is needed", self.state["patch"]["message"])

    def restart_guest(self):
        self.guest.answers("poll", out(state="done", updated=3, failed=0, exit=0, restart=1))
        self.guest.answers("reboot", out(boot_id=BOOT_1, note="restart scheduled"))
        self.guest.answers("probe", out(boot_id=BOOT_1), RuntimeError("connection refused"), out(boot_id=BOOT_1),
                           RuntimeError("timed out"), out(boot_id=BOOT_2))
        self.guest.answers("oldkernels", out(removed=4, exit=0))

    def test_restart_waits_for_a_new_boot_id_tolerating_probe_errors(self):
        self.restart_guest()
        job, (status, output, errors, outputs) = self.go()
        self.assertEqual(status, "SUCCESS")
        calls = self.guest.calls
        self.assertEqual(calls[:4], ["probe", "start", "poll", "reboot"])
        self.assertEqual(calls.count("probe"), 5)
        self.assertEqual(calls[-2:], ["oldkernels", "cleanup"])
        record = self.state["patch"]
        self.assertEqual((record["status"], record["restarted"], record["kernels_removed"]), ("updated", True, 4))
        self.assertEqual(len(self.rescan_calls), 1)
        # The reboot call carries the boot id the server had before the update.
        reboot = self.guest.scripts[calls.index("reboot")]
        self.assertIn("BOOT_ID=" + BOOT_1 + "\n", reboot)

    def test_a_boot_id_that_never_changes_fails_at_the_deadline(self):
        self.restart_guest()
        self.guest.answers("probe", out(boot_id=BOOT_1))
        job, (status, output, errors, outputs) = self.go()
        self.assertEqual(status, "FAILURE")
        self.assertEqual(self.state["patch"]["status"], "failed")
        self.assertIn("did not come back within 30 minutes", self.state["patch"]["message"])
        self.assertNotIn("oldkernels", self.guest.calls)
        self.assertEqual(self.rescan_calls, [])
        self.assertIsNone(self.state["active"])
        self.assertGreater(self.clock.now, 30 * 60)
        self.assertEqual(self.state["patch"]["updated"], 3)

    def test_old_kernels_failing_does_not_fail_the_update(self):
        self.restart_guest()
        self.guest.answers("oldkernels", out(exit=1, failure="Error: nothing"))
        job, (status, output, errors, outputs) = self.go()
        self.assertEqual(status, "WARNING")
        self.assertEqual(self.state["patch"]["status"], "updated")
        self.assertIsNone(self.state["patch"]["kernels_removed"])
        self.assertIn("Old kernels were not removed", self.state["patch"]["message"])

    def test_twenty_lost_polls_fail_the_update(self):
        self.guest.answers("poll", RuntimeError("no route"))
        job, (status, output, errors, outputs) = self.go()
        self.assertEqual(status, "FAILURE")
        self.assertEqual(self.guest.calls.count("poll"), patch.LOST_POLLS)
        self.assertIn("lost contact", self.state["patch"]["message"])
        self.assertEqual(self.rescan_calls, [])

    def test_lost_polls_in_a_row_are_counted_not_in_total(self):
        answers = ([RuntimeError("blip")] * (patch.LOST_POLLS - 1) + [out(state="running")]
                   + [RuntimeError("blip")] * (patch.LOST_POLLS - 1)
                   + [out(state="done", updated=1, failed=0, exit=0, restart=0)])
        self.guest.answers("poll", *answers)
        job, (status, output, errors, outputs) = self.go()
        self.assertEqual(status, "SUCCESS")

    def test_a_poll_with_no_state_counts_as_lost(self):
        self.guest.answers("poll", out())
        job, (status, output, errors, outputs) = self.go()
        self.assertEqual(self.guest.calls.count("poll"), patch.LOST_POLLS)
        self.assertEqual(status, "FAILURE")

    def test_the_patch_time_limit(self):
        self.settings["patch_timeout_min"] = 10
        self.guest.answers("poll", out(state="running"))
        job, (status, output, errors, outputs) = self.go()
        self.assertEqual(status, "FAILURE")
        self.assertIn("did not finish within 10 minutes", self.state["patch"]["message"])
        self.assertIn("may still be running", self.state["patch"]["message"])
        self.assertIn("dnf history, or dpkg --configure -a", self.state["patch"]["message"])

    def test_a_worker_that_died_fails_the_update(self):
        self.guest.answers("poll", out(state="died", updated=2))
        job, (status, output, errors, outputs) = self.go()
        self.assertEqual(status, "FAILURE")
        record = self.state["patch"]
        self.assertEqual(record["status"], "failed")
        self.assertEqual(record["message"], patch.STOPPED)
        self.assertIn("dnf history, or dpkg --configure -a", record["message"])
        self.assertEqual(record["updated"], 2)
        self.assertEqual(errors, patch.STOPPED)
        self.assertIsNone(self.state["active"])

    def test_the_package_manager_failing_is_a_failure_with_its_reason(self):
        self.guest.answers("poll", out(state="done", updated=0, failed=2, exit=1, failure="Error: Failed to download"))
        job, (status, output, errors, outputs) = self.go()
        self.assertEqual(status, "FAILURE")
        self.assertIn("Failed to download", self.state["patch"]["message"])
        self.assertEqual(self.state["patch"]["failed"], 2)

    def test_start_is_refused_while_another_worker_runs(self):
        self.guest.answers("start", out(reason="another update is already running on this server"))
        job, (status, output, errors, outputs) = self.go()
        self.assertEqual(status, "FAILURE")
        self.assertNotIn("poll", self.guest.calls)
        self.assertIn("another update is already running", self.state["patch"]["message"])
        self.assertIsNone(self.state["active"])

    def test_an_unreadable_boot_id_changes_nothing(self):
        self.guest.answers("probe", out(boot_id=""))
        job, (status, output, errors, outputs) = self.go()
        self.assertEqual(status, "FAILURE")
        self.assertNotIn("start", self.guest.calls)

    def test_a_rescan_that_cannot_start_leaves_the_update_as_updated_with_a_warning(self):
        self.start_result = Refused("The scanner is busy.")
        job, (status, output, errors, outputs) = self.go()
        self.assertEqual(status, "WARNING")
        record = self.state["patch"]
        self.assertEqual((record["status"], record["after"]), ("updated", None))
        self.assertEqual(record["before"], {"vulns": 10, "vuln_fixable": 8})
        self.assertIn("installed and the rescan failed", record["message"])
        self.assertEqual(outputs["counts"], {"updated": 1, "no_updates": 0, "restart_pending": 0, "failed": 0, "skipped": 0})
        self.assertIsNone(self.state["active"])

    def test_a_rescan_that_fails_or_times_out_is_a_warning_too(self):
        self.rescan_job = types.SimpleNamespace(id=99, status="FAILURE")
        job, (status, output, errors, outputs) = self.go()
        self.assertEqual((status, self.state["patch"]["status"], self.state["patch"]["after"]), ("WARNING", "updated", None))

        def never(timeout):
            raise TimeoutException("too slow")

        self.rescan_started.wait_for_completion = never
        self.state.pop("patch")
        self.summaries[:] = [{"vulns": 1, "vuln_fixable": 1}]
        job, (status, output, errors, outputs) = self.go()
        self.assertEqual((status, self.state["patch"]["after"]), ("WARNING", None))
        self.assertIn("did not finish", self.state["patch"]["message"])

    def test_a_server_never_scanned_has_no_before_and_proceeds(self):
        self.summaries[:] = [None, {"vulns": 3, "vuln_fixable": 1}]
        job, (status, output, errors, outputs) = self.go()
        self.assertEqual(status, "SUCCESS")
        self.assertIsNone(self.state["patch"]["before"])
        self.assertEqual(self.state["patch"]["after"], {"vulns": 3, "vuln_fixable": 1})
        self.assertNotIn("known vulnerabilities", output)

    def test_the_marker_is_cleared_after_a_failure_and_after_a_cancel(self):
        self.guest.answers("poll", CancelJob())
        job = make_job()
        self.rescan()
        with self.assertRaises(CancelJob):
            patch.run_patch(job, servers=[make_server()], restart_if_needed=True)
        self.assertIsNone(self.state["active"])
        self.assertTrue(any("keeps running" in message for message in job.progress))
        self.assertEqual(self.guest.calls[-1], "cleanup")

    def test_a_server_that_is_held_by_another_job_is_refused_untouched(self):
        self.state["active"] = {"job_id": 5, "scan_id": "20261124T101500Z-aaaaaa", "started": "x", "requested_by": "bob"}
        self.active_jobs[5] = types.SimpleNamespace(id=5, is_active=lambda: True)
        job, (status, output, errors, outputs) = self.go()
        self.assertEqual(status, "FAILURE")
        self.assertEqual(self.guest.calls, [])
        self.assertEqual(self.state["active"]["job_id"], 5)
        self.assertIn("Job 5 is still running", output)
        self.assertNotIn("patch", self.state)

    def test_a_second_update_job_on_the_same_server_is_refused(self):
        job_a, (status, output, errors, outputs) = self.go()
        self.state["active"] = {"job_id": 11, "scan_id": "patch-x1234567", "started": "x", "requested_by": "alice",
                                "kind": "patch"}
        self.active_jobs[11] = types.SimpleNamespace(id=11, is_active=lambda: True)
        with self.assertRaises(patch.PatchFailed):
            patch._patch_one(make_job(12), make_server(), True, {})
        self.assertEqual(self.state["active"]["job_id"], 11)

    def test_a_server_of_another_family_raises_before_anything_is_touched(self):
        with self.assertRaises(patch.PatchFailed) as caught:
            patch._patch_one(make_job(), solaris_server(), True, {})
        self.assertEqual(str(caught.exception), patch.ONLY_SUPPORTED)
        self.assertEqual((self.guest.calls, self.markers), ([], []))

    def test_a_linux_server_is_dispatched_to_the_linux_runner(self):
        facts = {"updated": 1, "failed": 0, "restart_needed": False, "restarted": False, "kernels_removed": None,
                 "notes": []}
        self.rescan()
        with mock.patch.object(patch, "_linux_patch", return_value=facts) as linux, \
                mock.patch.object(patch, "_linux_cleanup") as cleanup:
            record = patch._patch_one(make_job(), make_server(), True, {})
        self.assertEqual(linux.call_count, 1)
        self.assertEqual(cleanup.call_count, 1)
        self.assertEqual((record["status"], record["hostname"]), ("updated", "web-01"))

    def test_the_progress_text_names_the_phase(self):
        job, result = self.go()
        self.assertTrue(any("Updating packages" in message for message in job.progress))


# ---------------------------------------------------------------- several servers
class BulkTests(Base):
    def servers(self):
        return [make_server(id=i, hostname="h{}".format(i)) for i in (1, 2, 3)]

    def test_the_next_server_runs_after_a_failure(self):
        polls = {"n": 0}
        original = self.guest.__call__

        def failing_second(server, script, timeout, job=None):
            if re.search(r"^MODE=probe", script, re.M):
                polls["n"] += 1
            if polls["n"] == 2 and re.search(r"^MODE=start", script, re.M):
                return out(reason="dnf is not installed on the server")
            return original(server, script, timeout, job)

        with mock.patch.object(jobs, "_execute", failing_second):
            self.summaries[:] = [{"vulns": 5, "vuln_fixable": 5}] * 6
            job, (status, output, errors, outputs) = self.go(servers=self.servers())
        self.assertEqual(status, "FAILURE")
        self.assertEqual(outputs["counts"], {"updated": 2, "no_updates": 0, "restart_pending": 0, "failed": 1, "skipped": 0})
        self.assertEqual(sorted(outputs["servers"]), ["1", "2", "3"])
        self.assertEqual(outputs["servers"]["2"]["status"], "failed")
        self.assertEqual(len(self.rescan_calls), 2)
        self.assertIn("h2: failed", output)

    def test_every_server_failing_is_a_failure(self):
        self.guest.answers("start", out(reason="no"))
        job, (status, output, errors, outputs) = self.go(servers=self.servers())
        self.assertEqual(status, "FAILURE")
        self.assertEqual(outputs["counts"]["failed"], 3)

    def test_the_budget_stops_new_servers(self):
        self.summaries[:] = [{"vulns": 5, "vuln_fixable": 5}] * 6
        # One server's worst case is (90 + 30 + 30) minutes = 9000 s; the first server takes 30 s.
        with mock.patch.object(patch, "JOB_LIMIT_S", 9020):
            job, (status, output, errors, outputs) = self.go(servers=self.servers())
        self.assertEqual(status, "WARNING")
        self.assertEqual(outputs["counts"], {"updated": 1, "no_updates": 0, "restart_pending": 0, "failed": 0, "skipped": 2})
        self.assertEqual(list(outputs["servers"]), ["1"])
        self.assertIn("2 servers were not started", output)
        self.assertIn("8 hour limit", output)

    def test_a_cancel_between_servers_stops_and_says_the_guest_keeps_running(self):
        self.summaries[:] = [{"vulns": 5, "vuln_fixable": 5}] * 6
        servers = self.servers()
        results = []

        def after_first(mode):
            if mode == "cleanup":
                self.cancelled = True

        self.guest.on_call = after_first
        job, (status, output, errors, outputs) = self.go(servers=servers)
        self.assertEqual(status, "WARNING")
        self.assertEqual(outputs["counts"], {"updated": 1, "no_updates": 0, "restart_pending": 0, "failed": 0, "skipped": 2})
        self.assertIn("keeps running", output)
        self.assertEqual(results, [])

    def test_a_restart_pending_server_is_counted(self):
        self.guest.answers("poll", out(state="done", updated=1, failed=0, exit=0, restart=1))
        self.summaries[:] = [{"vulns": 5, "vuln_fixable": 5}] * 4
        job, (status, output, errors, outputs) = self.go(servers=self.servers()[:2], restart=False)
        self.assertEqual(outputs["counts"], {"updated": 0, "no_updates": 0, "restart_pending": 2, "failed": 0, "skipped": 0})
        self.assertEqual(status, "WARNING")

    def test_no_server_is_a_failure(self):
        self.assertEqual(patch.run_patch(make_job())[0], "FAILURE")

    def test_the_servers_and_the_restart_flag_arrive_as_the_job_gives_them(self):
        self.rescan()
        server = make_server()
        status = patch.run_patch(make_job(), server=server, restart_if_needed="False")[0]
        self.assertEqual(status, "SUCCESS")


# ---------------------------------------------------------------- the handover in vuln_jobs.start
class HandoverTests(Base):
    def setUp(self):
        super().setUp()
        self.queued = []
        self.set_active_calls = []

        class Hook:
            def run_as_job(hook, **kw):
                self.queued.append(kw)
                return [types.SimpleNamespace(id=42, save=lambda **k: None)]

        for patcher in (
            mock.patch.object(repo, "targets", return_value={"upload_url": "u", "tools": {}, "token": "t"}),
            mock.patch.object(repo, "prepare"),
            mock.patch.object(hooks, "scan_hook", return_value=Hook()),
            mock.patch.object(store, "set_active", side_effect=lambda sid, marker: self.set_active_calls.append(marker)),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.server = make_server()
        self.server.jobs = types.SimpleNamespace(add=lambda job: None)

    def hold(self, kind="patch", job_id=11):
        marker = {"job_id": job_id, "scan_id": "patch-20261124T101500Z-abc123", "started": "2026-11-24T10:15:00Z",
                  "requested_by": "alice"}
        if kind:
            marker["kind"] = kind
        self.state["active"] = marker
        self.active_jobs[job_id] = types.SimpleNamespace(id=job_id, is_active=lambda: True)

    def start(self, **kw):
        return jobs.start(None, self.server, "https://cb.example.com", **kw)

    def test_a_handover_from_the_holding_patch_job_is_accepted_and_keeps_the_marker(self):
        self.hold()
        created = self.start(handover_from=11, parent_job="parent")
        self.assertEqual(created.id, 42)
        self.assertEqual(self.set_active_calls, [])
        self.assertEqual(self.state["active"]["kind"], "patch")
        self.assertEqual(self.queued[0]["parent_job"], "parent")

    def test_a_handover_skips_the_cooldown(self):
        self.hold()
        self.state["last"] = {"status": "completed", "finished": store.stamp()}
        self.assertEqual(self.start(handover_from=11).id, 42)

    def test_without_a_handover_the_same_marker_refuses(self):
        self.hold()
        refused = self.start()
        self.assertIsInstance(refused, jobs.Refusal)
        self.assertEqual(refused.status, 409)
        self.assertEqual(self.queued, [])

    def test_a_different_job_id_is_refused(self):
        self.hold()
        refused = self.start(handover_from=12)
        self.assertIsInstance(refused, jobs.Refusal)
        self.assertEqual((refused.status, self.queued), (409, []))

    def test_a_scan_marker_cannot_be_taken_over(self):
        self.hold(kind=None, job_id=11)
        self.assertEqual(self.start(handover_from=11).status, 409)
        self.assertEqual(self.queued, [])

    def test_a_handover_with_no_marker_is_refused(self):
        self.assertEqual(self.start(handover_from=11).status, 409)
        self.assertEqual(self.queued, [])

    def test_a_plain_start_still_sets_the_marker_and_keeps_the_cooldown(self):
        created = self.start()
        self.assertEqual(created.id, 42)
        self.assertEqual(len(self.set_active_calls), 1)
        self.assertNotIn("kind", self.set_active_calls[0])
        self.state["last"] = {"status": "completed", "finished": store.stamp()}
        refused = self.start()
        self.assertEqual(refused.status, 429)

    def test_precheck_is_what_start_refuses_with(self):
        self.assertIsNone(jobs.precheck(self.server, "https://cb"))
        self.assertEqual(jobs.precheck(self.server, "").message, jobs.NO_SITE_URL)
        self.assertEqual(jobs.precheck(make_server(status="PROVFAILED"), "https://cb").message, jobs.NOT_ACTIVE)
        refusal = self.start()
        self.assertEqual(refusal.id if hasattr(refusal, "id") else None, 42)
        bad = jobs.start(None, make_server(power_status="POWEROFF"), "https://cb")
        self.assertEqual((bad.message, bad.status), ("The server is not powered on.", 409))


# ---------------------------------------------------------------- the panel's active dict
class PanelActiveTests(Base):
    def job(self):
        return types.SimpleNamespace(id=11, is_active=lambda: True, get_absolute_url=lambda: "/jobs/11/",
                                     get_progress=lambda: (0, 0, types.SimpleNamespace(message="Updating")))

    def test_kind_and_title_for_an_update_and_for_a_scan(self):
        self.active_jobs[11] = self.job()
        self.state["active"] = {"job_id": 11, "scan_id": "patch-x1234567", "started": "t", "requested_by": "a",
                                "kind": "patch"}
        active = jobs.panel_active(7)
        self.assertEqual((active["kind"], active["title"], active["job_id"], active["message"]),
                         ("patch", "Installing updates", 11, "Updating"))
        self.assertEqual(active["job_url"], "/jobs/11/")
        self.state["active"] = {"job_id": 11, "scan_id": "20261124T101500Z-aaaaaa", "started": "t", "requested_by": "a"}
        active = jobs.panel_active(7)
        self.assertEqual((active["kind"], active["title"]), ("scan", "Scan running"))
        self.assertEqual(set(active), {"job_id", "job_url", "message", "started", "kind", "title"})


# ---------------------------------------------------------------- vuln_api
class ApiTests(Base):
    def setUp(self):
        super().setUp()
        self.hook = FakeHook()
        for patcher in (mock.patch.object(hooks, "patch_hook", create=True, return_value=self.hook),
                        mock.patch.object(jobs, "precheck", return_value=None)):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.ended = types.SimpleNamespace(id=11, status="SUCCESS", errors="")
        self.hook.job.wait_for_completion = lambda timeout: self.ended
        self.waited = []

    def test_refused(self):
        with mock.patch.object(jobs, "msrc_ready", return_value=False):
            result = api.install_updates(windows_server(hostname="win-01"), "owner")
        self.assertEqual(result["status"], "refused")
        self.assertIn("win-01", result["message"])
        self.assertIsNone(result["job"])

    def test_no_updates_is_completed_with_that_message(self):
        self.state["patch"] = {"job_id": 11, "status": "no_updates", "restarted": False, "message": ""}
        result = api.install_updates(make_server(hostname="web-01"), "owner")
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["message"], "No updates were available on web-01.")

    def test_timeout(self):
        def never(timeout):
            self.waited.append(timeout)
            raise TimeoutException("slow")

        self.hook.job.wait_for_completion = never
        result = api.install_updates(make_server(), "owner")
        self.assertEqual(result["status"], "timeout")
        self.assertIs(result["job"], self.hook.job)
        self.assertEqual(self.waited, [(90 + 30 + 20) * 60 + api.WAIT_MARGIN_SECONDS])
        result = api.install_updates(make_server(), "owner", timeout=120)
        self.assertEqual(self.waited[-1], 120)

    def test_completed_restart_pending_and_failed(self):
        record = {"job_id": 11, "status": "updated", "restarted": True, "message": ""}
        self.state["patch"] = record
        result = api.install_updates(make_server(), "owner", restart_if_needed=False)
        self.assertEqual(self.hook.calls[0]["context"], {"restart_if_needed": False})
        self.assertEqual((result["status"], result["patch"]), ("completed", record))
        self.assertIn("restarted", result["message"])
        record["status"] = "restart_pending"
        self.assertEqual(api.install_updates(make_server(), "owner")["status"], "restart_pending")
        record.update(status="failed", message="the server did not come back")
        result = api.install_updates(make_server(), "owner")
        self.assertEqual(result["status"], "failed")
        self.assertIn("did not come back", result["message"])

    def test_a_job_with_no_record_of_its_own_is_failed(self):
        self.state["patch"] = {"job_id": 3, "status": "updated"}
        self.ended = types.SimpleNamespace(id=11, status="FAILURE", errors="boom")
        result = api.install_updates(make_server(), "owner")
        self.assertEqual(result["status"], "failed")
        self.assertIn("boom", result["message"])

    def test_scan_and_wait_and_eligible(self):
        started = types.SimpleNamespace(id=55, wait_for_completion=lambda timeout: types.SimpleNamespace(
            id=55, status="SUCCESS", errors=""))
        with mock.patch.object(jobs, "start", return_value=started):
            self.state["last"] = {"scan_id": "20261124T101500Z-aaaaaa", "status": "completed"}
            result = api.scan_and_wait(make_server(), "owner")
            self.assertEqual((result["status"], result["scan"]["status"]), ("completed", "completed"))
            self.state["last"] = {"scan_id": "x", "status": "failed", "error": "no list"}
            self.assertEqual(api.scan_and_wait(make_server(), "owner")["status"], "failed")
        with mock.patch.object(jobs, "start", return_value=Refused("busy")):
            self.assertEqual(api.scan_and_wait(make_server(), "owner")["status"], "refused")
            self.assertEqual(api.start_scan(make_server(), "owner")["status"], "refused")
        with mock.patch.object(jobs, "start", return_value=started):
            self.assertEqual(api.start_scan(make_server(), "owner", parent_job="p")["status"], "started")
        self.assertEqual(api.eligible(make_server()), (True, ""))
        self.assertEqual(api.eligible(make_server(power_status="POWEROFF")), (False, "the server is powered off."))
        with mock.patch.object(fleet, "default_base_url", return_value=""):
            self.assertFalse(api.eligible(make_server())[0])


# ---------------------------------------------------------------- review fixes
class SettingGateTests(Base):
    def test_start_job_is_refused_when_patching_is_off(self):
        self.settings["patching"] = False
        hook = FakeHook()
        with mock.patch.object(hooks, "patch_hook", create=True, return_value=hook):
            refusal = patch.start_job("owner", server=make_server())
        self.assertEqual((refusal.message, refusal.status), (patch.PATCHING_OFF, 403))
        self.assertEqual(patch.PATCHING_OFF, "Installing updates is turned off in Vulnerability Settings.")
        self.assertEqual(hook.calls, [])

    def test_an_unreadable_setting_counts_as_off(self):
        with mock.patch.object(store, "get_settings", side_effect=RuntimeError("disk")):
            self.assertFalse(patch.patching_enabled())

    def test_run_patch_ends_in_failure_without_touching_a_server(self):
        self.settings["patching"] = False
        job, (status, output, errors, outputs) = self.go()
        self.assertEqual((status, errors, outputs), ("FAILURE", patch.PATCHING_OFF, {}))
        self.assertEqual((self.guest.calls, self.markers, self.rescan_calls), ([], [], []))
        self.assertNotIn("patch", self.state)

    def test_the_api_is_refused_too(self):
        self.settings["patching"] = False
        with mock.patch.object(hooks, "patch_hook", create=True, return_value=FakeHook()):
            result = api.install_updates(make_server(), "owner")
        self.assertEqual(result["status"], "refused")
        self.assertEqual(result["message"], patch.PATCHING_OFF)


class OutcomeTests(Base):
    def restart_guest(self, running, newest):
        self.guest.answers("poll", out(state="done", updated=3, failed=0, exit=0, restart=1))
        self.guest.answers("reboot", out(boot_id=BOOT_1))
        self.guest.answers("probe", out(boot_id=BOOT_1), out(boot_id=BOOT_2, kernel_running=running,
                                                              kernel_newest=newest))
        self.guest.answers("oldkernels", out(removed=2, exit=0))

    def test_a_restart_into_an_older_kernel_is_a_warning_and_cleanup_still_runs(self):
        self.restart_guest("5.14.0-1", "5.14.0-2")
        job, (status, output, errors, outputs) = self.go()
        self.assertEqual(status, "WARNING")
        record = self.state["patch"]
        self.assertEqual((record["status"], record["restarted"], record["kernels_removed"]), ("updated", True, 2))
        self.assertIn("Restarted, but the server is still running the older kernel.", record["message"])
        self.assertIn("oldkernels", self.guest.calls)

    def test_a_restart_into_the_newest_kernel_is_clean(self):
        self.restart_guest("5.14.0-2", "5.14.0-2")
        job, (status, output, errors, outputs) = self.go()
        self.assertEqual(status, "SUCCESS")
        self.assertEqual(self.state["patch"]["message"], "")

    def test_a_single_failure_puts_its_reason_in_errors(self):
        self.guest.answers("poll", out(state="done", updated=0, failed=1, exit=1, failure="Error: no space left"))
        job, (status, output, errors, outputs) = self.go()
        self.assertEqual(status, "FAILURE")
        self.assertIn("no space left", errors)
        self.assertNotEqual(errors, "No server was updated.")
        self.assertEqual(errors, self.state["patch"]["message"])

    def test_a_bulk_failure_has_a_count_and_the_first_reason(self):
        self.guest.answers("start", out(reason="dnf is not installed on the server"))
        servers = [make_server(id=i, hostname="h{}".format(i)) for i in (1, 2)]
        job, (status, output, errors, outputs) = self.go(servers=servers)
        self.assertEqual(status, "FAILURE")
        self.assertTrue(errors.startswith("2 of 2 servers failed. First failure (h1): "), errors)
        self.assertIn("dnf is not installed", errors)

    def test_a_partly_failed_bulk_run_still_reports_the_reason(self):
        original = self.guest.__call__
        starts = {"n": 0}

        def second_start_refused(server, script, timeout, job=None):
            if re.search(r"^MODE=start", script, re.M):
                starts["n"] += 1
                if starts["n"] == 2:
                    return out(reason="the disk is read-only")
            return original(server, script, timeout, job)

        self.summaries[:] = [{"vulns": 5, "vuln_fixable": 5}] * 6
        servers = [make_server(id=i, hostname="h{}".format(i)) for i in (1, 2)]
        with mock.patch.object(jobs, "_execute", second_start_refused):
            job, (status, output, errors, outputs) = self.go(servers=servers)
        self.assertEqual(status, "FAILURE")
        self.assertIn("1 of 2 servers failed", errors)
        self.assertIn("read-only", errors)

    def test_a_server_that_does_not_fit_in_the_time_left_is_skipped_with_a_message(self):
        self.settings.update(patch_timeout_min=240, restart_wait_min=120)
        # 240 + 120 + 30 minutes = 23400 s of worst case; the limit patched in below is 20000 s.
        servers = [make_server(id=i, hostname="h{}".format(i)) for i in (1, 2)]
        self.summaries[:] = [{"vulns": 5, "vuln_fixable": 5}] * 6
        with mock.patch.object(patch, "JOB_LIMIT_S", 20000):
            self.clock.now = 4000.0
            job, (status, output, errors, outputs) = self.go(servers=servers)
        self.assertEqual(outputs["counts"]["skipped"], 2)
        self.assertEqual(self.guest.calls, [])
        self.assertIn("390 minutes", output)
        self.assertEqual(status, "WARNING")


# ---------------------------------------------------------------- the shell's decision functions
SH_PATH = os.path.join(PKG_DIR, "vuln_patch.sh")


def shell(snippet):
    """Run snippet in bash after the script's functions are defined (everything before its dispatch), with stub
    commands defined as shell functions in the snippet. Nothing of the script's modes runs. Returns stdout."""
    with open(SH_PATH, encoding="ascii") as handle:
        text = handle.read()
    head = text[:text.index("# ---- dispatch")]
    program = "MODE=poll\nRUN_ID=patch-shell-test-0001\n" + head + "\n" + snippet + "\n"
    done = subprocess.run([shutil.which("bash")], input=program.encode("ascii"), capture_output=True, timeout=60)
    return done.stdout.decode(errors="replace")


@unittest.skipUnless(shutil.which("bash"), "bash is not installed")
# ---------------------------------------------------------------- the fix round
class OutcomeWordingTests(Base):
    def test_the_line_gives_every_fact_that_happened(self):
        self.guest.answers("poll", out(state="done", updated=1, failed=0, exit=0, restart=1))
        self.guest.answers("reboot", out(boot_id=BOOT_1))
        self.guest.answers("probe", out(boot_id=BOOT_1), out(boot_id=BOOT_2))
        self.guest.answers("oldkernels", out(removed=2, exit=0, kernel_running="k2", kernel_newest="k2"))
        job, (status, output, errors, outputs) = self.go()
        self.assertEqual(output, "web-01: 1 package updated, restarted, 2 old kernel packages removed, known "
                                 "vulnerabilities 10 to 2, with a fix 8 to 0.")

    def test_parts_that_did_not_happen_are_left_out(self):
        self.summaries[:] = [{"vulns": 4, "vuln_fixable": None}, {"vulns": 1, "vuln_fixable": None}]
        job, (status, output, errors, outputs) = self.go()
        self.assertEqual(output, "web-01: 7 packages updated, known vulnerabilities 4 to 1.")

    def test_a_pending_restart_is_said(self):
        self.guest.answers("poll", out(state="done", updated=3, failed=0, exit=0, restart=1))
        job, (status, output, errors, outputs) = self.go(restart=False)
        self.assertTrue(output.startswith("web-01: 3 packages updated, a restart is still needed, known "))


class NoUpdatesTests(Base):
    def test_nothing_installed_nothing_pending_is_no_updates_and_still_rescans(self):
        self.guest.answers("poll", out(state="done", updated=0, failed=0, exit=0, restart=0))
        job, (status, output, errors, outputs) = self.go()
        self.assertEqual((status, errors), ("SUCCESS", ""))
        self.assertEqual(output, "web-01: no updates were available.")
        record = self.state["patch"]
        self.assertEqual((record["status"], record["updated"], record["restarted"], record["message"]),
                         ("no_updates", 0, False, ""))
        self.assertEqual(len(self.rescan_calls), 1)
        self.assertEqual(record["after"], {"vulns": 2, "vuln_fixable": 0})
        self.assertEqual(outputs["counts"]["no_updates"], 1)
        self.assertEqual(outputs["counts"]["updated"], 0)
        self.assertIn("no updates were available", EVENTS[0][0][2])

    def test_a_failed_rescan_is_still_a_warning(self):
        self.guest.answers("poll", out(state="done", updated=0, failed=0, exit=0, restart=0))
        self.start_result = Refused("busy")
        job, (status, output, errors, outputs) = self.go()
        self.assertEqual(status, "WARNING")
        self.assertEqual(self.state["patch"]["status"], "no_updates")

    def test_a_run_that_only_removed_old_kernels_is_updated(self):
        self.guest.answers("poll", out(state="done", updated=0, failed=0, exit=0, restart=0))
        self.guest.answers("oldkernels", out(removed=3, exit=0, kernel_running="k1", kernel_newest="k1"))
        job, (status, output, errors, outputs) = self.go()
        self.assertEqual(self.state["patch"]["status"], "updated")
        self.assertEqual(self.state["patch"]["kernels_removed"], 3)
        self.assertEqual(output, "web-01: 0 packages updated, 3 old kernel packages removed, known vulnerabilities "
                                 "10 to 2, with a fix 8 to 0.")

    def test_a_run_that_only_restarted_is_updated(self):
        self.guest.answers("poll", out(state="done", updated=0, failed=0, exit=0, restart=1))
        self.guest.answers("reboot", out(boot_id=BOOT_1))
        self.guest.answers("probe", out(boot_id=BOOT_1), out(boot_id=BOOT_2))
        job, (status, output, errors, outputs) = self.go()
        self.assertEqual((status, self.state["patch"]["status"], self.state["patch"]["restarted"]),
                         ("SUCCESS", "updated", True))

    def test_a_pending_restart_is_not_no_updates(self):
        self.guest.answers("poll", out(state="done", updated=0, failed=0, exit=0, restart=1))
        job, (status, output, errors, outputs) = self.go(restart=False)
        self.assertEqual(self.state["patch"]["status"], "restart_pending")

    def test_failures_are_not_no_updates(self):
        self.guest.answers("poll", out(state="done", updated=0, failed=2, exit=0, restart=0))
        job, (status, output, errors, outputs) = self.go()
        self.assertEqual(self.state["patch"]["status"], "updated")

    def test_every_server_without_updates_keeps_the_job_successful(self):
        self.guest.answers("poll", out(state="done", updated=0, failed=0, exit=0, restart=0))
        self.summaries[:] = [{"vulns": 1, "vuln_fixable": 0}] * 4
        job, (status, output, errors, outputs) = self.go(servers=[make_server(id=1, hostname="a"),
                                                                  make_server(id=2, hostname="b")])
        self.assertEqual(status, "SUCCESS")
        self.assertEqual(output, "a: no updates were available.\nb: no updates were available.")

    def test_a_failed_server_next_to_one_without_updates_is_a_failure(self):
        self.guest.answers("poll", out(state="done", updated=0, failed=0, exit=0, restart=0))
        self.guest.answers("start", out(state="running"), out(reason="no"))
        job, (status, output, errors, outputs) = self.go(servers=[make_server(id=1, hostname="a"),
                                                                  make_server(id=2, hostname="b")])
        self.assertEqual(status, "FAILURE")


class OldKernelStepTests(Base):
    def test_it_runs_without_a_restart_when_none_was_needed(self):
        self.guest.answers("oldkernels", out(removed=2, exit=0, kernel_running="k1", kernel_newest="k1"))
        self.go()
        self.assertIn("oldkernels", self.guest.calls)
        self.assertNotIn("reboot", self.guest.calls)
        self.assertEqual(self.state["patch"]["kernels_removed"], 2)
        self.assertLess(self.guest.calls.index("poll"), self.guest.calls.index("oldkernels"))

    def test_it_never_runs_while_a_restart_is_pending(self):
        self.guest.answers("poll", out(state="done", updated=3, failed=0, exit=0, restart=1))
        self.go(restart=False)
        self.assertNotIn("oldkernels", self.guest.calls)

    def test_it_runs_after_a_confirmed_restart(self):
        self.guest.answers("poll", out(state="done", updated=3, failed=0, exit=0, restart=1))
        self.guest.answers("reboot", out(boot_id=BOOT_1))
        self.guest.answers("probe", out(boot_id=BOOT_1), out(boot_id=BOOT_2))
        self.go()
        self.assertGreater(self.guest.calls.index("oldkernels"), self.guest.calls.index("reboot"))

    def test_the_setting_turns_the_step_off(self):
        self.settings["remove_old_kernels"] = False
        job, (status, output, errors, outputs) = self.go()
        self.assertNotIn("oldkernels", self.guest.calls)
        self.assertEqual(status, "SUCCESS")
        self.assertIsNone(self.state["patch"]["kernels_removed"])

    def test_the_setting_off_is_said_after_a_restart_and_is_not_a_warning(self):
        self.settings["remove_old_kernels"] = False
        self.guest.answers("poll", out(state="done", updated=3, failed=0, exit=0, restart=1))
        self.guest.answers("reboot", out(boot_id=BOOT_1))
        self.guest.answers("probe", out(boot_id=BOOT_1), out(boot_id=BOOT_2))
        job, (status, output, errors, outputs) = self.go()
        self.assertNotIn("oldkernels", self.guest.calls)
        self.assertEqual(status, "SUCCESS")
        self.assertIn("Old kernels were kept.", self.state["patch"]["message"])

    def test_the_setting_defaults_to_on_when_it_is_missing_or_unreadable(self):
        for value in (None, "", "maybe"):
            self.settings["remove_old_kernels"] = value
            self.guest.calls[:] = []
            self.state["patch"] = None
            self.guest.queues["probe"] = [out(boot_id=BOOT_1)]
            self.guest.queues["poll"] = [out(state="done", updated=1, failed=0, exit=0, restart=0)]
            self.go()
            self.assertIn("oldkernels", self.guest.calls, repr(value))
        del self.settings["remove_old_kernels"]
        self.guest.calls[:] = []
        self.guest.queues["poll"] = [out(state="done", updated=1, failed=0, exit=0, restart=0)]
        self.go()
        self.assertIn("oldkernels", self.guest.calls)
        self.assertTrue(patch._flag({}, "remove_old_kernels", True))
        self.assertFalse(patch._flag({"remove_old_kernels": "false"}, "remove_old_kernels", True))

    def test_a_guest_note_about_a_skipped_cleanup_is_a_note_not_a_warning(self):
        self.guest.answers("oldkernels", out(removed=0, exit=0, kernel_running="k1", kernel_newest="k1",
                                             note="purging them would also remove other packages"))
        job, (status, output, errors, outputs) = self.go()
        self.assertEqual(status, "SUCCESS")
        self.assertIn("purging them would also remove other packages", self.state["patch"]["message"])
        self.assertEqual(self.state["patch"]["kernels_removed"], 0)

    def test_not_running_the_newest_kernel_is_a_warning(self):
        self.guest.answers("oldkernels", out(removed=0, exit=0, kernel_running="k1", kernel_newest="k2",
                                             note="the server is not running its newest kernel"))
        job, (status, output, errors, outputs) = self.go()
        self.assertEqual(status, "WARNING")
        message = self.state["patch"]["message"]
        self.assertIn(patch.OLDER_KERNEL, message)
        self.assertNotIn("the server is not running its newest kernel", message)

    def test_the_removed_count_is_the_guests(self):
        self.guest.answers("oldkernels", out(removed=5, exit=0, kernel_running="k1", kernel_newest="k1"))
        self.go()
        self.assertEqual(self.state["patch"]["kernels_removed"], 5)


class ShellTests(unittest.TestCase):
    RPM = "rpm() { printf '%s\\n' 'kernel 5.14.0-2.x86_64' 'kernel 5.14.0-1.x86_64'; }\n"

    def restart(self, dnf_body, kernel="5.14.0-1.x86_64", rpm=None):
        snippet = ("DISTRO=rhel\ndnf() { " + dnf_body + " }\n" + (rpm or self.RPM)
                   + "uname() { echo " + kernel + "; }\ncheck_restart\necho \"RESULT|$RESTART|$RESTART_REASON\"\n")
        for line in shell(snippet).splitlines():
            if line.startswith("RESULT|"):
                return line.split("|", 2)[1:]
        self.fail("no result")

    def test_a_missing_needs_restarting_plugin_is_not_read_as_restart_required(self):
        # dnf exits 1 for a command it does not know; that must not mean "restart".
        missing = "return 1;"
        self.assertEqual(self.restart(missing, kernel="5.14.0-2.x86_64")[0], "0")
        self.assertEqual(self.restart(missing, kernel="5.14.0-1.x86_64")[0], "1")
        self.assertEqual(self.restart(missing, rpm="rpm() { return 1; }\n")[0], "unknown")

    def test_a_working_plugin_is_trusted(self):
        plugin = ('case "$*" in "needs-restarting -h") return 0;; "needs-restarting -r") echo "Reboot is required"; '
                  'return %d;; esac;')
        self.assertEqual(self.restart(plugin % 1, kernel="5.14.0-2.x86_64")[0], "1")
        self.assertEqual(self.restart(plugin % 0, kernel="5.14.0-1.x86_64")[0], "0")
        # Any other exit status falls through to the kernel comparison.
        self.assertEqual(self.restart(plugin % 2, kernel="5.14.0-2.x86_64")[0], "0")
        self.assertEqual(self.restart(plugin % 2, kernel="5.14.0-1.x86_64")[0], "1")

    def test_the_run_folder_must_be_owned_by_the_running_user_with_mode_700(self):
        # stat and id are stubbed, so the answer does not depend on the file system the test runs on.
        with tempfile.TemporaryDirectory() as root:
            root = root.replace("\\", "/")
            results = {}
            for label, owner_mode in (("good", "0 700"), ("open", "0 755"), ("other_owner", "1000 700"),
                                      ("group", "0 770")):
                snippet = ("id() { echo 0; }\nstat() { echo '" + owner_mode + "'; }\n"
                           "if safe_dir " + root + "; then echo SAFE; else echo UNSAFE; fi\n"
                           "if safe_dir " + root + "/missing; then echo SAFE; else echo UNSAFE; fi\n")
                results[label] = shell(snippet).split()
        self.assertEqual(results["good"], ["SAFE", "UNSAFE"])
        for label in ("open", "other_owner", "group"):
            self.assertEqual(results[label], ["UNSAFE", "UNSAFE"], label)

    def test_a_link_is_never_a_safe_run_folder(self):
        with tempfile.TemporaryDirectory() as root:
            root = root.replace("\\", "/")
            snippet = ("id() { echo 0; }\nstat() { echo '0 700'; }\nmkdir {r}/target; ln -s {r}/target {r}/link 2>/dev/null\n"
                       "[ -L {r}/link ] || { echo NOLINK; exit 0; }\n"
                       "if safe_dir {r}/link; then echo SAFE; else echo UNSAFE; fi\n").replace("{r}", root)
            text = shell(snippet)
        if "NOLINK" in text:
            self.skipTest("symbolic links are not available")
        self.assertEqual(text.split(), ["UNSAFE"])

    def test_a_cleanup_refuses_through_a_link_and_removes_nothing(self):
        with tempfile.TemporaryDirectory() as root:
            root = root.replace("\\", "/")
            snippet = ("mkdir -m 700 {r}/target; touch {r}/target/keep; ln -s {r}/target {r}/link 2>/dev/null\n"
                       "[ -L {r}/link ] || { echo NOLINK; exit 0; }\n"
                       "RUN={r}/link\nmode_cleanup\n").replace("{r}", root)
            text = shell(snippet)
            if "NOLINK" in text:
                self.skipTest("symbolic links are not available")
            self.assertIn("CBVULN|reason|the run folder on the server is not safe to use", text)
            self.assertTrue(os.path.exists(os.path.join(root, "target", "keep")))

    def test_the_other_modes_refuse_an_unsafe_run_folder(self):
        with tempfile.TemporaryDirectory() as root:
            root = root.replace("\\", "/")
            for mode in ("mode_poll", "mode_oldkernels", "mode_cleanup"):
                snippet = ("id() { echo 0; }\nstat() { echo '0 755'; }\nneed_distro() { :; }\n"
                           "mkdir {r}/open; RUN={r}/open\ntouch {r}/open/keep\n{m}\n").replace(
                               "{r}", root).replace("{m}", mode)
                text = shell(snippet)
                self.assertIn("CBVULN|reason|the run folder on the server is not safe to use", text, mode)
                self.assertTrue(os.path.exists(os.path.join(root, "open", "keep")), mode)
                shutil.rmtree(os.path.join(root, "open"))

    def test_a_missing_run_folder_is_not_an_error_for_poll_and_cleanup(self):
        with tempfile.TemporaryDirectory() as root:
            root = root.replace("\\", "/")
            for mode, expect in (("mode_poll", "CBVULN|state|missing"), ("mode_cleanup", "CBVULN|done|1")):
                text = shell(("RUN={r}/none\n{m}\n").replace("{r}", root).replace("{m}", mode))
                self.assertIn(expect, text)
                self.assertNotIn("reason", text)


# ---------------------------------------------------------------- failure detail lines
def failed_poll(details, failure="Error: Transaction test error:"):
    """A poll answer for a package manager that failed, with detail lines (a key that may repeat)."""
    lines = ["CBVULN|state|done", "CBVULN|updated|0", "CBVULN|failed|1", "CBVULN|exit|1", "CBVULN|failure|" + failure]
    lines += ["CBVULN|detail|" + text for text in details]
    return "\n".join(lines + ["CBVULN|done|1"]) + "\n"


class DetailTests(Base):
    CONFLICTS = ["Error: Transaction test error:", "  file /usr/a conflicts with b-1", "  file /usr/c conflicts with d-2"]

    def test_the_parser_collects_detail_lines_cleaned_capped_and_limited(self):
        text = failed_poll(["see https://evil.example/x now"] + ["line {}".format(i) for i in range(20)] + ["x" * 500])
        parsed = patch._linux_parse(text)
        self.assertEqual(len(parsed["details"]), patch.MAX_DETAIL)
        self.assertNotIn("evil.example", parsed["details"][0])
        self.assertIn("detail", patch.KNOWN_KEYS)
        parsed = patch._linux_parse(failed_poll(["y" * 500]))
        self.assertLessEqual(len(parsed["details"][0]), patch.MESSAGE_LEN)
        self.assertEqual(patch._linux_parse(out(state="done"))["details"], [])

    def test_the_job_output_and_errors_carry_the_detail_the_record_does_not(self):
        self.guest.answers("poll", failed_poll(self.CONFLICTS))
        job, (status, output, errors, outputs) = self.go()
        self.assertEqual(status, "FAILURE")
        first = "The update failed on the server (exit code 1): Error: Transaction test error:"
        self.assertEqual(self.state["patch"]["message"], first)
        self.assertIn("file /usr/a conflicts with b-1", output)
        self.assertIn("file /usr/c conflicts with d-2", output)
        self.assertTrue(errors.startswith(first + "\n"), errors)
        self.assertIn("file /usr/c conflicts with d-2", errors)
        self.assertNotIn("detail", outputs["servers"]["7"])
        self.assertNotIn("conflicts", " ".join(str(value) for value in self.state["patch"].values()))

    def test_the_detail_text_is_capped_at_two_thousand_characters(self):
        self.guest.answers("poll", failed_poll(["z" * 290 + str(i) for i in range(12)]))
        job, (status, output, errors, outputs) = self.go()
        detail = errors.split("\n", 1)[1]
        self.assertLessEqual(len(detail), patch.DETAIL_TOTAL)
        self.assertGreater(len(detail), 1500)
        self.assertEqual(patch._detail_text(["a" * 3000]), "a" * patch.DETAIL_TOTAL)
        self.assertEqual(patch._detail_text(None), "")

    def test_a_bulk_failure_errors_carry_the_first_failures_detail(self):
        self.guest.answers("poll", failed_poll(self.CONFLICTS))
        servers = [make_server(id=i, hostname="h{}".format(i)) for i in (1, 2)]
        job, (status, output, errors, outputs) = self.go(servers=servers)
        self.assertTrue(errors.startswith("2 of 2 servers failed. First failure (h1): "), errors)
        self.assertEqual(errors.count("file /usr/a conflicts with b-1"), 1)
        self.assertEqual(output.count("file /usr/a conflicts with b-1"), 2)

    def test_a_failure_without_detail_lines_is_as_before(self):
        self.guest.answers("poll", out(state="done", updated=0, failed=1, exit=1, failure="Error: boom"))
        job, (status, output, errors, outputs) = self.go()
        self.assertEqual(errors, "The update failed on the server (exit code 1): Error: boom")


if __name__ == "__main__":
    unittest.main()
