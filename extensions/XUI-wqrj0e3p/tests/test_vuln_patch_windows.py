"""
Checks for the Windows side of the update job of the Vulnerabilities XUI (vuln_patch_windows.ps1 and the _windows_*
functions of vuln_patch.py) without CloudBolt and without a Windows guest: the guest is a scripted fake that stands in
for vuln_wintask's inline, start, poll and cleanup, and the findings store and the server's state are in memory (the
harness of test_vuln_patch.py).

    python -B -m unittest discover -s extensions/XUI-wqrj0e3p/tests -p "test_vuln_patch_windows.py"

With Windows PowerShell 5.1 (powershell.exe) the worker script is parsed (never run: it would use Windows Update on
this machine), three of its pure helper functions are run in a child process, and the probe script, which only
reads, is run here.
"""
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
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import test_vuln_patch as base  # noqa: E402  (the harness; its test classes are not imported by name)

patch, jobs, store = base.patch, base.jobs, base.store
wintask = patch.vuln_wintask
PKG_DIR = base.PKG_DIR
WORKER = os.path.join(PKG_DIR, "vuln_patch_windows.ps1")
BOOT_A = "20261124100000"
BOOT_B = "20261124101500"
OPEN, CLOSE = base.OPEN.decode(), base.CLOSE.decode()
POWERSHELL = shutil.which("powershell.exe")


def probe(boot=BOOT_A, build="20348", ccm="0", product="3", uptime=100000):
    """A probe reply as vuln_wintask.inline returns it: the build, the product type and the uptime travel in the
    build value ("20348;3;100000")."""
    packed = build + ";" + product + (";" + str(uptime) if uptime is not None else "")
    return {"boot": boot, "build": packed, "ccm": ccm}


def lines(**fields):
    """Worker lines for the keys given, in order (a key listed twice is not possible: use raw())."""
    return ["CBVULN|{}|{}".format(key, value) for key, value in fields.items()]


def finished(updated=2, failed=0, result=2, restart=0, more=0, items=None, extra=None, found=None):
    """The lines a worker prints for a normal install, ending with done."""
    out = ["CBVULN|phase|Searching for updates", "CBVULN|restart_before|0",
           "CBVULN|found|{}".format(found if found is not None else updated + failed), "CBVULN|skipped|0"]
    out += ["CBVULN|item|{}".format(item) for item in (items or [])]
    out += ["CBVULN|updated|{}".format(updated), "CBVULN|failed|{}".format(failed), "CBVULN|result|{}".format(result),
            "CBVULN|hresult|0x00000000", "CBVULN|restart|{}".format(restart)]
    if more is not None:
        out.append("CBVULN|more|{}".format(more))
    return out + list(extra or []) + ["CBVULN|done|1"]


class WinGuest:
    """The fake of vuln_wintask: one queue per call kind; a queue's answers are used in turn and the last one
    repeats; an exception in a queue is raised."""

    def __init__(self):
        self.probes = [probe()]
        self.reboots = [{"reboot": "0"}]
        self.polls = [{"lines": finished(), "alive": False}]
        self.start_error = None
        self.calls = []
        self.started = []
        self.cleaned = []
        self.reboot_scripts = []

    @staticmethod
    def _next(queue):
        item = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(item, BaseException):
            raise item
        return item

    def inline(self, server, script):
        if "shutdown.exe" in script:
            self.calls.append("reboot")
            self.reboot_scripts.append(script)
            return dict(self._next(self.reboots))
        self.calls.append("probe")
        return dict(self._next(self.probes))

    def start(self, server, run_id, worker_text, limit_s):
        self.calls.append("start")
        self.started.append({"run_id": run_id, "worker": worker_text, "limit_s": limit_s})
        if self.start_error:
            raise self.start_error

    def poll(self, server, run_id, offset, budget=None):
        self.calls.append("poll")
        item = self._next(self.polls)
        return {"lines": list(item["lines"]), "next": offset + len(item["lines"]), "more": item.get("more", False),
                "alive": item.get("alive", True)}

    def cleanup(self, server, run_id):
        self.calls.append("cleanup")
        self.cleaned.append(run_id)


class WinBase(base.Base):
    def setUp(self):
        super().setUp()
        self.win = WinGuest()
        for patcher in (mock.patch.object(jobs, "msrc_ready", return_value=True),
                        mock.patch.object(wintask, "inline", self.win.inline),
                        mock.patch.object(wintask, "start", self.win.start),
                        mock.patch.object(wintask, "poll", self.win.poll),
                        mock.patch.object(wintask, "cleanup", self.win.cleanup)):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.server = base.windows_server(hostname="win-01")

    def run_one(self, restart=True, server=None):
        job, result = self.go(servers=[server or self.server], restart=restart)
        return job, result[0], result[1], result[2], result[3]

    def record(self):
        return self.state["patch"]


# ---------------------------------------------------------------- eligibility
class WindowsEligibilityTests(WinBase):
    def test_a_windows_server_is_eligible(self):
        self.assertEqual(patch.eligibility(self.server), "")

    def test_no_microsoft_data(self):
        with mock.patch.object(jobs, "msrc_ready", return_value=False):
            self.assertEqual(patch.eligibility(self.server), "no_windows_data")

    def test_a_windows_version_the_data_does_not_cover(self):
        self.state["last"] = {"status": "failed", "reason": jobs.UNSUPPORTED_WINDOWS}
        self.assertEqual(patch.eligibility(self.server), "unsupported_windows")

    def test_unassigned_powered_off_and_historical(self):
        unassigned = base.windows_server(group=types.SimpleNamespace(is_unassigned=True, name="Unassigned"))
        self.assertEqual(patch.eligibility(unassigned), "unassigned")
        self.assertEqual(patch.eligibility(base.windows_server(power_status="POWEROFF")), "powered_off")
        self.assertEqual(patch.eligibility(base.windows_server(status="HISTORICAL")), "historical")

    def test_another_operating_system(self):
        self.assertEqual(patch.eligibility(base.solaris_server()), "unsupported_os")

    def test_the_linux_prerequisite_does_not_apply_to_windows_and_the_other_way_round(self):
        with mock.patch.object(base.tools, "status", return_value={"ready": False, "db": {"built": ""}}):
            self.assertEqual(patch.eligibility(self.server), "")
            self.assertEqual(patch.eligibility(base.make_server()), "not_set_up")

    def test_the_caller_can_pass_the_prerequisites(self):
        self.assertEqual(patch.eligibility(self.server, ("not_set_up", "no_windows_data")), "no_windows_data")
        self.assertEqual(patch.eligibility(base.make_server(), ("not_set_up", "")), "not_set_up")

    def test_start_job_accepts_a_windows_server(self):
        hook = base.FakeHook()
        with mock.patch.object(base.hooks, "patch_hook", create=True, return_value=hook), \
                mock.patch.object(jobs, "precheck", return_value=None):
            created = patch.start_job("owner", server=self.server)
        self.assertIs(created, hook.job)


# ---------------------------------------------------------------- the worker's output
class ParseTests(unittest.TestCase):
    def test_a_normal_install(self):
        parsed = patch._windows_parse(finished(updated=2, items=["2;KB5001234;2026-11 Cumulative Update",
                                                                  "2;KB5009999;Servicing stack"], more=3))
        self.assertEqual((parsed["found"], parsed["skipped"], parsed["updated"], parsed["failed"], parsed["result"]),
                         (2, 0, 2, 0, 2))
        self.assertEqual((parsed["restart"], parsed["restart_before"], parsed["more"], parsed["done"]), ("0", "0", 3, True))
        self.assertEqual(parsed["hresult"], "0x00000000")
        self.assertEqual(parsed["items"][0], {"result": 2, "kb": "KB5001234", "title": "2026-11 Cumulative Update"})

    def test_each_result_code(self):
        for code in (2, 3, 4, 5):
            parsed = patch._windows_parse(["CBVULN|result|{}".format(code), "CBVULN|item|{};KB1234567;t".format(code)])
            self.assertEqual((parsed["result"], parsed["items"][0]["result"]), (code, code))
        parsed = patch._windows_parse(["CBVULN|result|6", "CBVULN|item|9;KB1234567;t", "CBVULN|item|x;KB1234567;t"])
        self.assertEqual((parsed["result"], parsed["items"]), (None, []))

    def test_a_refusal(self):
        parsed = patch._windows_parse(["CBVULN|reason|This server is patched by Configuration Manager.", "CBVULN|done|1"])
        self.assertEqual(parsed["reason"], "This server is patched by Configuration Manager.")
        self.assertTrue(parsed["done"])

    def test_garbage_and_unknown_keys_are_ignored(self):
        parsed = patch._windows_parse(["noise", "CBVULN|evil|x", "CBVULN|", "CBVULN|found", "CBWT|next|3", "",
                                       "CBVULN|restart|maybe", "CBVULN|hresult|nothex", "CBVULN|updated|abc",
                                       "CBVULN|failed|-4", "CBVULN|found|99999999999"])
        self.assertEqual((parsed["restart"], parsed["hresult"], parsed["updated"], parsed["failed"], parsed["found"]),
                         ("", "", None, None, None))
        self.assertNotIn("evil", parsed)
        self.assertFalse(parsed["done"])

    def test_text_input_works_too(self):
        parsed = patch._windows_parse("x\nCBVULN|found|4\nCBVULN|done|1\n")
        self.assertEqual((parsed["found"], parsed["done"]), (4, True))

    def test_a_later_line_wins(self):
        parsed = patch._windows_parse(["CBVULN|updated|7", "CBVULN|updated|3", "CBVULN|phase|a", "CBVULN|phase|b"])
        self.assertEqual((parsed["updated"], parsed["phase"]), (3, "b"))

    def test_caps(self):
        many = ["CBVULN|item|2;KB1234567;t{}".format(i) for i in range(80)]
        notes = ["CBVULN|note|n{}".format(i) for i in range(9)]
        parsed = patch._windows_parse(many + notes + ["CBVULN|failure|see https://evil.example/x?t=1 now",
                                                      "CBVULN|reason|" + "x" * 5000,
                                                      "CBVULN|item|2;KB1;" + "y" * 500])
        self.assertEqual(len(parsed["items"]), patch.MAX_ITEMS)
        self.assertEqual(len(parsed["notes"]), patch.MAX_NOTES)
        self.assertNotIn("evil.example", parsed["failure"])
        self.assertLessEqual(len(parsed["reason"]), patch.MESSAGE_LEN)
        parsed = patch._windows_parse(["CBVULN|item|2;KB1;" + "y" * 500])
        self.assertLessEqual(len(parsed["items"][0]["title"]), 120)
        self.assertEqual(parsed["items"][0]["kb"], "")

    def test_lines_can_be_added_poll_by_poll(self):
        parsed = patch._windows_parse(["CBVULN|found|2"])
        again = patch._windows_parse(["CBVULN|updated|2", "CBVULN|done|1"], parsed)
        self.assertIs(again, parsed)
        self.assertEqual((parsed["found"], parsed["updated"], parsed["done"]), (2, 2, True))


class BootTests(unittest.TestCase):
    def test_a_real_restart_changes_the_boot_id(self):
        self.assertTrue(patch.boot_changed(BOOT_A, BOOT_B))
        self.assertTrue(patch.boot_changed(BOOT_B, BOOT_A))

    def test_a_few_seconds_are_the_same_boot(self):
        self.assertFalse(patch.boot_changed(BOOT_A, BOOT_A))
        self.assertFalse(patch.boot_changed(BOOT_A, "20261124100009"))
        self.assertFalse(patch.boot_changed("20261124100009", BOOT_A))
        self.assertTrue(patch.boot_changed(BOOT_A, "20261124100011"))

    def test_a_value_that_is_not_a_boot_id_is_unchanged(self):
        for bad in ("", None, "abc", "20261324100000", "2026112410000"):
            self.assertFalse(patch.boot_changed(BOOT_A, bad), repr(bad))
            self.assertFalse(patch.boot_changed(bad, BOOT_A), repr(bad))


# ---------------------------------------------------------------- the flow
class FlowTests(WinBase):
    def test_success_with_no_restart_needed(self):
        job, status, output, errors, outputs = self.run_one()
        self.assertEqual((status, errors), ("SUCCESS", ""))
        self.assertEqual(output, "win-01: 2 updates installed, known vulnerabilities 10 to 2, with a fix 8 to 0.")
        record = self.record()
        self.assertEqual((record["status"], record["family"], record["updated"], record["failed"]),
                         ("updated", "windows", 2, 0))
        self.assertEqual((record["restarted"], record["restart_needed"], record["kernels_removed"]), (False, False, None))
        self.assertEqual((record["before"], record["after"]), ({"vulns": 10, "vuln_fixable": 8},
                                                               {"vulns": 2, "vuln_fixable": 0}))
        self.assertEqual(len(self.win.started), 1)
        run_id = self.win.started[0]["run_id"]
        self.assertTrue(patch.RUN_ID_RE.fullmatch(run_id))
        self.assertEqual(self.win.cleaned, [run_id])
        self.assertEqual(self.win.started[0]["limit_s"], 90 * 60)
        self.assertEqual(self.win.calls[:2], ["probe", "start"])
        self.assertNotIn("reboot", self.win.calls)
        self.assertEqual(self.rescan_calls[0]["handover_from"], job.id)
        self.assertEqual(self.rescan_calls[0]["active"]["kind"], patch.PATCH_KIND)
        self.assertIsNone(self.state["active"])
        self.assertEqual([marker["kind"] for marker in self.markers], [patch.PATCH_KIND])

    def test_the_worker_is_the_real_script_with_an_empty_config(self):
        self.run_one()
        worker = self.win.started[0]["worker"]
        self.assertTrue(worker.startswith("$Cfg=@{\n}\n"))
        self.assertIn("Microsoft.Update.Session", worker)

    def test_progress_names_the_phases(self):
        self.win.polls = [{"lines": ["CBVULN|phase|Downloading updates"], "alive": True},
                          {"lines": finished(), "alive": False}]
        job, status, _, _, _ = self.run_one()
        self.assertIn("win-01: Downloading updates", job.progress)
        self.assertEqual(status, "SUCCESS")

    def test_a_restart_that_is_needed_but_not_allowed_leaves_restart_pending(self):
        self.win.polls = [{"lines": finished(restart=1), "alive": False}]
        _, status, output, _, _ = self.run_one(restart=False)
        self.assertEqual(status, "WARNING")
        record = self.record()
        self.assertEqual((record["status"], record["restart_needed"], record["restarted"]), ("restart_pending", True, False))
        self.assertIn("A restart is needed to finish; the server was not restarted.", record["message"])
        self.assertNotIn("reboot", self.win.calls)
        self.assertIn("a restart is still needed", output)

    def test_a_restart_waits_for_a_new_boot_time(self):
        self.win.polls = [{"lines": finished(restart=1), "alive": False}]
        self.win.probes = [probe(BOOT_A), probe(BOOT_A), probe(BOOT_A), probe("20261124100004"), probe(BOOT_B, uptime=60)]
        _, status, output, _, _ = self.run_one()
        self.assertEqual(status, "SUCCESS")
        record = self.record()
        self.assertEqual((record["status"], record["restarted"], record["restart_needed"]), ("updated", True, True))
        self.assertEqual(self.win.calls.count("reboot"), 1)
        self.assertIn("'/r','/t','20'", self.win.reboot_scripts[0])
        self.assertIn("restarted", output)
        self.assertGreater(self.clock.now, 3 * patch.WIN_PROBE_S)

    def test_probe_errors_while_the_server_restarts_are_tolerated(self):
        self.win.polls = [{"lines": finished(restart=1), "alive": False}]
        self.win.probes = [probe(BOOT_A), probe(BOOT_A), wintask.WinTaskError("timed out"),
                           {"terr": "probe: not ready"}, {}, probe(BOOT_B, uptime=60)]
        _, status, _, _, _ = self.run_one()
        self.assertEqual((status, self.record()["restarted"]), ("SUCCESS", True))

    def test_a_server_that_never_gets_a_new_boot_time_fails(self):
        self.win.polls = [{"lines": finished(restart=1), "alive": False}]
        self.win.probes = [probe(BOOT_A)]
        _, status, _, errors, _ = self.run_one()
        self.assertEqual(status, "FAILURE")
        self.assertIn("did not come back within 30 minutes", errors)
        record = self.record()
        self.assertEqual((record["status"], record["restart_needed"], record["restarted"], record["updated"]),
                         ("failed", True, False, 2))
        self.assertGreater(self.clock.now, 30 * 60)
        self.assertEqual(self.rescan_calls, [])

    def test_a_raising_reboot_call_is_tolerated(self):
        self.win.polls = [{"lines": finished(restart=1), "alive": False}]
        self.win.reboots = [wintask.WinTaskError("the connection closed")]
        self.win.probes = [probe(BOOT_A), probe(BOOT_A), probe(BOOT_B, uptime=60)]
        _, status, _, _, _ = self.run_one()
        self.assertEqual((status, self.record()["restarted"]), ("SUCCESS", True))

    def test_a_reboot_reply_with_a_non_zero_exit_fails(self):
        self.win.polls = [{"lines": finished(restart=1), "alive": False}]
        for reply in ({"reboot": "5"}, {"terr": "reboot: access denied"}):
            self.win.reboots = [reply]
            self.win.probes = [probe(BOOT_A)]
            self.state["patch"] = None
            _, status, _, errors, _ = self.run_one()
            self.assertEqual(status, "FAILURE", reply)
            self.assertIn("The restart could not be scheduled", errors)

    def test_a_server_that_restarted_after_the_install_is_not_restarted_again(self):
        # The worker logged done, the server restarted before the next poll; the poll then reads done.
        self.win.polls = [wintask.WinTaskError("lost"), wintask.WinTaskError("lost"),
                          {"lines": finished(restart=1), "alive": False}]
        self.win.probes = [probe(BOOT_A), probe(BOOT_B, uptime=60)]
        _, status, _, _, _ = self.run_one()
        self.assertEqual(status, "SUCCESS")
        self.assertEqual((self.record()["restarted"], self.record()["restart_needed"]), (True, True))
        self.assertNotIn("reboot", self.win.calls)

    def test_twenty_lost_polls_in_a_row_fail(self):
        self.win.polls = [wintask.WinTaskError("connection refused")]
        _, status, _, errors, _ = self.run_one()
        self.assertEqual(status, "FAILURE")
        self.assertIn("lost contact with the server for 20 checks in a row", errors)
        self.assertEqual(self.win.calls.count("poll"), patch.LOST_POLLS + 1)
        # The task state is unknown, so nothing is cleaned up.
        self.assertEqual(self.win.cleaned, [])

    def test_a_poll_that_works_again_resets_the_lost_count(self):
        lost = [wintask.WinTaskError("blip")] * (patch.LOST_POLLS - 1)
        self.win.polls = lost + [{"lines": ["CBVULN|phase|Installing updates"], "alive": True}] + lost + \
            [{"lines": finished(), "alive": False}]
        _, status, _, _, _ = self.run_one()
        self.assertEqual(status, "SUCCESS")

    def test_a_task_that_is_gone_after_a_restart_is_reported_as_that(self):
        self.win.polls = [{"lines": ["CBVULN|phase|Installing updates"], "alive": True},
                          {"lines": [], "alive": False}]
        self.win.probes = [probe(BOOT_A), probe(BOOT_B, uptime=60)]
        _, status, _, errors, _ = self.run_one()
        self.assertEqual(status, "FAILURE")
        self.assertIn(patch.RESTARTED_WINDOWS, errors)
        self.assertEqual(self.win.cleaned, [self.win.started[0]["run_id"]])

    def test_a_task_that_is_gone_without_a_restart_is_stopped(self):
        self.win.polls = [{"lines": ["CBVULN|phase|Installing updates"], "alive": True},
                          {"lines": [], "alive": False}]
        for probes in ([probe(BOOT_A)], [probe(BOOT_A), wintask.WinTaskError("down")]):
            self.win.probes = probes
            self.win.polls = [{"lines": ["CBVULN|phase|Installing updates"], "alive": True},
                              {"lines": [], "alive": False}]
            self.state["patch"] = None
            _, status, _, errors, _ = self.run_one()
            self.assertEqual(status, "FAILURE")
            self.assertIn(patch.STOPPED_WINDOWS, errors)

    def test_one_quiet_poll_is_not_a_death(self):
        self.win.polls = [{"lines": [], "alive": False}, {"lines": finished(), "alive": False}]
        _, status, _, _, _ = self.run_one()
        self.assertEqual(status, "SUCCESS")

    def test_more_lines_are_read_without_waiting(self):
        self.win.polls = [{"lines": ["CBVULN|phase|Installing updates"], "alive": True, "more": True},
                          {"lines": finished(), "alive": False}]
        _, status, _, _, _ = self.run_one()
        self.assertEqual(status, "SUCCESS")
        self.assertEqual(self.clock.now, patch.WIN_POLL_S)

    def test_the_time_limit(self):
        self.win.polls = [{"lines": ["CBVULN|phase|Installing updates"], "alive": True}]
        self.settings["patch_timeout_min"] = 5
        _, status, _, errors, _ = self.run_one()
        self.assertEqual(status, "FAILURE")
        self.assertIn("did not finish within 5 minutes and may still be running", errors)
        # The task is still running: its folder is left in place.
        self.assertEqual(self.win.cleaned, [])

    def test_rescan_failure_is_updated_with_a_warning_and_no_after(self):
        self.start_result = base.Refused("scanner busy")
        _, status, _, _, _ = self.run_one()
        self.assertEqual(status, "WARNING")
        record = self.record()
        self.assertEqual((record["status"], record["after"], record["updated"]), ("updated", None, 2))
        self.assertIn("The updates were installed and the rescan failed", record["message"])

    def test_no_updates_available(self):
        self.win.polls = [{"lines": ["CBVULN|restart_before|0", "CBVULN|found|0", "CBVULN|skipped|0", "CBVULN|done|1"],
                           "alive": False}]
        _, status, output, _, outputs = self.run_one()
        self.assertEqual(status, "SUCCESS")
        record = self.record()
        self.assertEqual((record["status"], record["updated"], record["failed"], record["message"]),
                         ("no_updates", 0, 0, ""))
        self.assertEqual(output, "win-01: no updates were available.")
        self.assertEqual(outputs["counts"]["no_updates"], 1)
        # The rescan still runs, so the numbers are current.
        self.assertEqual(len(self.rescan_calls), 1)
        self.assertEqual(record["after"], {"vulns": 2, "vuln_fixable": 0})

    def test_updates_that_need_a_person_are_not_installed(self):
        self.win.polls = [{"lines": ["CBVULN|found|3", "CBVULN|skipped|3", "CBVULN|done|1"], "alive": False}]
        self.run_one()
        record = self.record()
        self.assertEqual((record["status"], record["updated"]), ("updated", 0))
        # Only the specific note: the one about people and license agreements.
        self.assertEqual(record["message"], "3 updates were skipped because they need user input or a license agreement.")

    def test_skipped_updates_are_noted(self):
        self.win.polls = [{"lines": finished(extra=["CBVULN|skipped|1"]), "alive": False}]
        self.run_one()
        self.assertIn("1 update was skipped because it needs user input or a license agreement.",
                      self.record()["message"])

    def test_succeeded_with_errors_lists_the_failed_updates(self):
        items = ["2;KB1000001;a", "3;KB1000002;b", "4;KB1000003;c", "5;-;d"]
        self.win.polls = [{"lines": finished(updated=1, failed=3, result=3, items=items), "alive": False}]
        _, status, output, _, _ = self.run_one()
        self.assertEqual(status, "WARNING")
        record = self.record()
        self.assertEqual((record["status"], record["updated"], record["failed"]), ("updated", 1, 3))
        self.assertIn("These updates failed: KB1000002, KB1000003, d.", record["message"])

    def test_at_most_five_failed_updates_are_listed(self):
        items = ["3;KB10000{:02d};t".format(i) for i in range(8)]
        self.win.polls = [{"lines": finished(updated=0, failed=8, result=3, items=items), "alive": False}]
        self.run_one()
        message = self.record()["message"]
        self.assertIn("These updates failed: KB1000000, KB1000001, KB1000002, KB1000003, KB1000004 and 3 more.", message)

    def test_a_failed_or_aborted_install_is_a_failure_with_the_counts(self):
        for code in (4, 5):
            self.win.polls = [{"lines": finished(updated=1, failed=1, result=code,
                                                 items=["2;KB1000001;a", "4;KB1000002;b"]), "alive": False}]
            self.state["patch"] = None
            _, status, _, errors, _ = self.run_one()
            self.assertEqual(status, "FAILURE", code)
            self.assertIn("Windows Update could not install the updates (result {}".format(code), errors)
            self.assertIn("KB1000002", errors)
            record = self.record()
            self.assertEqual((record["status"], record["updated"], record["failed"]), ("failed", 1, 1))
            self.assertEqual(self.rescan_calls, [])

    def test_the_worker_can_fail_with_an_hresult(self):
        self.win.polls = [{"lines": ["CBVULN|failure|Windows Update failed: Access is denied.", "CBVULN|hresult|0x80070005",
                                     "CBVULN|done|1"], "alive": False}]
        _, status, _, errors, _ = self.run_one()
        self.assertEqual(status, "FAILURE")
        self.assertIn("The update failed: Windows Update failed: Access is denied. (0x80070005)", errors)

    def test_the_worker_can_refuse(self):
        self.win.polls = [{"lines": ["CBVULN|reason|This server is patched by Configuration Manager.",
                                     "CBVULN|done|1"], "alive": False}]
        _, status, _, errors, _ = self.run_one()
        self.assertEqual((status, errors), ("FAILURE", "This server is patched by Configuration Manager."))

    def test_more_updates_are_a_note(self):
        self.win.polls = [{"lines": finished(more=4), "alive": False}]
        _, status, _, _, _ = self.run_one()
        self.assertEqual(status, "WARNING")
        self.assertIn("4 more updates are available; run the update again.", self.record()["message"])

    def test_a_restart_that_was_pending_beforehand_is_a_note(self):
        lines_ = finished()
        lines_[1] = "CBVULN|restart_before|1"
        self.win.polls = [{"lines": lines_, "alive": False}]
        self.run_one()
        self.assertIn("A restart was already pending before the update started.", self.record()["message"])

    def test_the_record_has_the_family_and_the_same_keys_as_linux(self):
        self.run_one()
        self.assertEqual(set(self.record()), {"at", "by", "job_id", "status", "before", "after", "updated", "failed",
                                              "restarted", "restart_needed", "kernels_removed", "duration_s", "message",
                                              "family"})


class RefusalTests(WinBase):
    def test_configuration_manager_is_refused_before_anything_is_staged(self):
        self.win.probes = [probe(ccm="1")]
        _, status, _, errors, _ = self.run_one()
        self.assertEqual((status, errors), ("FAILURE", "This server is patched by Configuration Manager."))
        self.assertEqual(self.win.started, [])
        self.assertEqual(self.win.calls.count("start"), 0)
        self.assertEqual(self.rescan_calls, [])
        self.assertIsNone(self.state["active"])

    def test_a_build_the_scan_cannot_match_is_refused(self):
        for build in ("17763", "22631", "19045"):
            self.win.probes = [probe(build=build)]
            self.state["patch"] = None
            _, status, _, errors, _ = self.run_one()
            self.assertEqual(status, "FAILURE", build)
            self.assertIn("Windows build {} is not one this extension updates".format(build), errors)
            self.assertEqual(self.win.started, [])

    def test_both_supported_builds_are_accepted(self):
        for build in ("20348", "26100"):
            self.win.probes = [probe(build=build)]
            self.summaries = [{"vulns": 10, "vuln_fixable": 8}, {"vulns": 2, "vuln_fixable": 0}]
            self.state["patch"] = None
            _, status, _, _, _ = self.run_one()
            self.assertEqual(status, "SUCCESS", build)

    def test_an_unreadable_build_or_boot_time_changes_nothing(self):
        for reply, text in ((probe(build=""), "build could not be read"), (probe(boot=""), "boot time could not be read")):
            self.win.probes = [reply]
            self.state["patch"] = None
            _, status, _, errors, _ = self.run_one()
            self.assertEqual(status, "FAILURE")
            self.assertIn(text, errors)
            self.assertEqual(self.win.started, [])

    def test_a_probe_that_cannot_run(self):
        for reply in (wintask.WinTaskError("boom"), {"terr": "probe: nope"}, {}):
            self.win.probes = [reply]
            self.state["patch"] = None
            _, status, _, errors, _ = self.run_one()
            self.assertEqual(status, "FAILURE")
            self.assertEqual(self.win.started, [])

    def test_a_start_that_fails(self):
        self.win.start_error = wintask.WinTaskError("Could not start the update on the server: launch: access denied")
        _, status, _, errors, _ = self.run_one()
        self.assertEqual(status, "FAILURE")
        self.assertIn("The update did not start: Could not start the update on the server", errors)
        self.assertEqual(self.win.calls.count("poll"), 1)
        self.assertEqual(self.win.cleaned, [self.win.started[0]["run_id"]])

    def test_an_unassigned_server_is_never_touched(self):
        server = base.windows_server(group=types.SimpleNamespace(is_unassigned=True, name="Unassigned"))
        _, status, _, errors, _ = self.run_one(server=server)
        self.assertEqual(status, "FAILURE")
        self.assertEqual(self.win.calls, [])
        self.assertEqual(self.markers, [])

    def test_the_patching_setting_gates_the_engine(self):
        self.settings["patching"] = False
        _, status, _, errors, _ = self.run_one()
        self.assertEqual((status, errors), ("FAILURE", patch.PATCHING_OFF))
        self.assertEqual(self.win.calls, [])

    def test_a_server_held_by_another_job_is_refused(self):
        self.active_jobs[5] = types.SimpleNamespace(id=5, is_active=lambda: True)
        self.state["active"] = {"job_id": 5, "scan_id": "scan-x", "kind": "scan"}
        _, status, _, errors, _ = self.run_one()
        self.assertEqual(status, "FAILURE")
        self.assertIn("Job 5 is still running", errors)
        self.assertEqual(self.win.calls, [])


class CleanupTests(WinBase):
    def test_a_running_task_keeps_its_folder(self):
        self.win.polls = [{"lines": [], "alive": True}]
        patch._windows_cleanup(self.server, "patch-20261124-abcdef01")
        self.assertEqual(self.win.cleaned, [])

    def test_a_finished_task_is_cleaned(self):
        self.win.polls = [{"lines": [], "alive": False}]
        patch._windows_cleanup(self.server, "patch-20261124-abcdef01")
        self.assertEqual(self.win.cleaned, ["patch-20261124-abcdef01"])

    def test_an_unreachable_server_keeps_its_folder_and_nothing_raises(self):
        self.win.polls = [wintask.WinTaskError("down")]
        patch._windows_cleanup(self.server, "patch-20261124-abcdef01")
        self.assertEqual(self.win.cleaned, [])


class ConstantTests(unittest.TestCase):
    def test_both_systems_restart_after_twenty_seconds(self):
        self.assertEqual((patch.REBOOT_DELAY_S, patch.WIN_REBOOT_DELAY_S), (20, 20))
        self.assertIn("'20'", patch._reboot_script())
        self.assertNotIn("@@", patch._reboot_script())

    def test_the_linux_script_accepts_that_delay(self):
        with open(os.path.join(PKG_DIR, "vuln_patch.sh"), encoding="ascii") as handle:
            text = handle.read()
        match = re.search(r'\[ "\$REBOOT_DELAY_S" -le (\d+) \]', text)
        self.assertGreaterEqual(int(match.group(1)), patch.REBOOT_DELAY_S)

    def test_the_restart_wait_falls_back_to_an_hour(self):
        self.assertEqual(patch.DEFAULT_RESTART_WAIT_MIN, 60)


# ---------------------------------------------------------------- the scripts
class ScriptFileTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(WORKER, "rb") as handle:
            cls.raw = handle.read()
        cls.text = cls.raw.decode("ascii")
        cls.code = wintask.build_worker(WORKER, {})

    def test_the_file_is_ascii_lf_and_has_no_double_brace(self):
        self.assertNotIn(b"\r", self.raw)
        self.assertNotIn(OPEN.encode(), self.raw)
        self.assertNotIn(CLOSE.encode(), self.raw)
        for name in ("vuln_patch.py",):
            with open(os.path.join(PKG_DIR, name), "rb") as handle:
                data = handle.read()
            data.decode("ascii")
            self.assertNotIn(OPEN.encode(), data, name)

    def test_the_vendor_pages_are_cited(self):
        for page in ("nn-wuapi-iupdatesession", "nf-wuapi-iupdatesearcher-search", "nn-wuapi-iupdate",
                     "nf-wuapi-iupdate-accepteula", "nn-wuapi-iinstallationbehavior", "nn-wuapi-iupdatecollection",
                     "nf-wuapi-iupdatedownloader-download", "nn-wuapi-iupdateinstaller", "nn-wuapi-iupdateinstaller2",
                     "nn-wuapi-iinstallationresult", "nn-wuapi-iupdateinstallationresult", "ne-wuapi-operationresultcode",
                     "nn-wuapi-isysteminformation"):
            self.assertIn("https://learn.microsoft.com/en-us/windows/win32/api/wuapi/" + page, self.text)
        with open(os.path.join(PKG_DIR, "vuln_patch.py"), encoding="ascii") as handle:
            source = handle.read()
        self.assertIn("https://learn.microsoft.com/en-us/windows/win32/cimwin32prov/win32-operatingsystem", source)
        self.assertIn("https://learn.microsoft.com/en-us/windows-server/administration/windows-commands/shutdown", source)

    def test_the_code_never_changes_the_update_source_or_downloads_anything_itself(self):
        for word in ("ServerSelection", "AddService", "PSWindowsUpdate", "Invoke-WebRequest", "Invoke-RestMethod",
                     "DownloadFile", "curl", "Install-Module", "WebClient", "BITS"):
            self.assertNotIn(word, self.code, word)

    def test_the_code_has_what_the_contract_names(self):
        for word in ("CcmExec", "This server is patched by Configuration Manager.", "Microsoft.Update.SystemInfo",
                     "Microsoft.Update.Session", "IsInstalled=0 and Type='Software' and IsHidden=0",
                     "CanRequestUserInput", "AcceptEula()", "IsDownloaded", "ForceQuiet=$true",
                     "AllowSourcePrompts=$false", "GetUpdateResult", "RebootRequired", "CBVULN|"):
            self.assertIn(word, self.code, word)
        self.assertTrue(self.code.rstrip().endswith("exit 0"))
        self.assertNotIn("Write-Error", self.code)
        self.assertNotIn("[Console]::Error", self.code)

    def test_cb_done_is_the_last_line_written(self):
        self.assertEqual(self.code.rstrip().splitlines()[-2], "try{Cb done 1}catch{}")

    def test_the_stripped_worker_fits_the_staging(self):
        self.assertLessEqual(len(self.code), wintask.MAX_WORKER_CHARS)
        self.assertNotIn("\n#", self.code)
        scripts = wintask.stage_scripts("patch-20261124-abcdef01", wintask.encode_worker(self.code))
        self.assertLessEqual(len(scripts), wintask.MAX_CHUNKS)
        for script, _ in scripts:
            self.assertTrue(wintask.script_fits(script))

    def test_the_inline_scripts_fit_the_winrm_limit(self):
        for name, script in (("probe", patch.PROBE), ("reboot", patch._reboot_script())):
            self.assertTrue(script.isascii(), name)
            self.assertTrue(wintask.script_fits(script), name)
            self.assertLessEqual(len(script), wintask.MAX_SCRIPT_CHARS, name)
            self.assertNotIn(OPEN, script, name)
            self.assertNotIn("\n#", script, name)
            self.assertTrue(script.rstrip().endswith("exit 0"), name)

    def test_nothing_user_controlled_is_in_the_inline_scripts(self):
        self.assertEqual(re.findall(r"@@\w+@@", patch.PROBE), [])
        self.assertEqual(re.findall(r"@@\w+@@", patch.REBOOT), ["@@DELAY@@"])


# ---------------------------------------------------------------- the fix round
class ProductTypeTests(WinBase):
    def test_a_workstation_is_refused_before_anything_is_staged(self):
        # Windows 11 24H2 has build 26100, like Windows Server 2025.
        self.win.probes = [probe(build="26100", product="1")]
        _, status, _, errors, _ = self.run_one()
        self.assertEqual((status, errors), ("FAILURE", "Only Windows Server 2022 and 2025 are supported."))
        self.assertEqual(self.win.started, [])

    def test_an_unreadable_product_type_refuses_too(self):
        for reply in (probe(product=""), probe(product="9"), {"boot": BOOT_A, "build": "20348", "ccm": "0"}):
            self.win.probes = [reply]
            self.state["patch"] = None
            _, status, _, errors, _ = self.run_one()
            self.assertEqual((status, errors), ("FAILURE", patch.ONLY_SERVER), reply)
            self.assertEqual(self.win.started, [])

    def test_servers_and_domain_controllers_are_accepted(self):
        for product in ("2", "3"):
            self.win.probes = [probe(product=product)]
            self.summaries = [{"vulns": 10, "vuln_fixable": 8}, {"vulns": 2, "vuln_fixable": 0}]
            self.state["patch"] = None
            _, status, _, _, _ = self.run_one()
            self.assertEqual(status, "SUCCESS", product)

    def test_the_probe_reply_is_read_in_parts(self):
        parsed = patch._windows_probe_reply({"boot": BOOT_A, "build": "20348;3;86400", "ccm": "0"})
        self.assertEqual((parsed["build"], parsed["product"], parsed["uptime"]), ("20348", "3", 86400))
        parsed = patch._windows_probe_reply({"boot": BOOT_A, "build": "20348;3;-5", "ccm": "1"})
        self.assertEqual((parsed["uptime"], parsed["ccm"]), (None, "1"))
        parsed = patch._windows_probe_reply({"boot": "x", "build": "abc;3;x", "ccm": "0"})
        self.assertEqual((parsed["boot"], parsed["build"], parsed["uptime"]), ("", "", None))


class UptimeTests(WinBase):
    def test_a_moved_boot_id_with_a_long_uptime_is_not_a_restart(self):
        # The clock was stepped: the boot id moved by 15 minutes but the server has been up for a day.
        self.assertFalse(patch._restarted(BOOT_A, {"boot": BOOT_B, "uptime": 86400}, 0))

    def test_a_moved_boot_id_with_a_short_uptime_is_a_restart(self):
        self.clock.now = 600
        self.assertTrue(patch._restarted(BOOT_A, {"boot": BOOT_B, "uptime": 60}, 0))
        self.assertTrue(patch._restarted(BOOT_A, {"boot": BOOT_B, "uptime": 600 + 119}, 0))
        self.assertFalse(patch._restarted(BOOT_A, {"boot": BOOT_B, "uptime": 600 + 120}, 0))

    def test_a_missing_uptime_proves_nothing(self):
        self.assertFalse(patch._restarted(BOOT_A, {"boot": BOOT_B, "uptime": None}, 0))
        self.assertFalse(patch._restarted(BOOT_A, {"boot": BOOT_B}, 0))
        self.assertFalse(patch._restarted(BOOT_A, {}, 0))

    def test_an_unchanged_boot_id_is_never_a_restart(self):
        self.assertFalse(patch._restarted(BOOT_A, {"boot": BOOT_A, "uptime": 5}, 0))

    def test_a_clock_step_alone_never_records_already_restarted(self):
        # The probe before the reboot shows a moved boot id with the old, long uptime: the reboot is still sent.
        self.win.polls = [{"lines": finished(restart=1), "alive": False}]
        self.win.probes = [probe(BOOT_A), probe(BOOT_B, uptime=100000), probe(BOOT_B, uptime=100000),
                           probe(BOOT_B, uptime=60)]
        _, status, _, _, _ = self.run_one()
        self.assertEqual(status, "SUCCESS")
        self.assertEqual(self.win.calls.count("reboot"), 1)
        self.assertTrue(self.record()["restarted"])

    def test_a_clock_step_never_reads_as_a_restart_for_a_gone_task(self):
        self.win.polls = [{"lines": ["CBVULN|phase|Installing updates"], "alive": True}, {"lines": [], "alive": False}]
        self.win.probes = [probe(BOOT_A), probe(BOOT_B, uptime=100000)]
        _, status, _, errors, _ = self.run_one()
        self.assertEqual(status, "FAILURE")
        self.assertIn(patch.STOPPED_WINDOWS, errors)

    def test_the_probe_script_prints_the_uptime(self):
        self.assertIn("[DateTime]::UtcNow", patch.PROBE)
        self.assertIn("ProductType", patch.PROBE)


class RestartResendTests(WinBase):
    def setUp(self):
        super().setUp()
        self.win.polls = [{"lines": finished(restart=1), "alive": False}]

    def test_a_raising_reboot_that_never_restarts_is_sent_again_then_fails_early(self):
        self.win.reboots = [wintask.WinTaskError("the connection closed")]
        self.win.probes = [probe(BOOT_A)]
        _, status, _, errors, _ = self.run_one()
        self.assertEqual(status, "FAILURE")
        self.assertIn("The restart could not be started.", errors)
        self.assertEqual(self.win.calls.count("reboot"), 2)
        # Well before the 30 minute deadline: 3 + 3 probes.
        self.assertLess(self.clock.now, 10 * 60)
        self.assertEqual(self.record()["status"], "failed")
        self.assertTrue(self.record()["restart_needed"])
        self.assertEqual(self.rescan_calls, [])

    def test_the_second_reboot_can_work(self):
        self.win.reboots = [wintask.WinTaskError("closed"), {"reboot": "0"}]
        self.win.probes = [probe(BOOT_A), probe(BOOT_A), probe(BOOT_A), probe(BOOT_A), probe(BOOT_A),
                           probe(BOOT_B, uptime=30)]
        _, status, _, _, _ = self.run_one()
        self.assertEqual((status, self.record()["restarted"]), ("SUCCESS", True))
        self.assertEqual(self.win.calls.count("reboot"), 2)

    def test_a_failed_probe_restarts_the_count(self):
        # The server is going down: probes that fail are not "unchanged".
        self.win.reboots = [wintask.WinTaskError("closed")]
        self.win.probes = [probe(BOOT_A), probe(BOOT_A), probe(BOOT_A), probe(BOOT_A), wintask.WinTaskError("down"),
                           probe(BOOT_A), probe(BOOT_A), probe(BOOT_B, uptime=30)]
        _, status, _, _, _ = self.run_one()
        self.assertEqual(status, "SUCCESS")
        self.assertEqual(self.win.calls.count("reboot"), 1)

    def test_a_reboot_that_went_through_is_not_resent(self):
        self.win.reboots = [{"reboot": "0"}]
        self.win.probes = [probe(BOOT_A)]
        _, status, _, errors, _ = self.run_one()
        self.assertEqual(status, "FAILURE")
        self.assertIn("did not come back within 30 minutes", errors)
        self.assertEqual(self.win.calls.count("reboot"), 1)


class FixRoundFlowTests(WinBase):
    def test_a_pending_restart_is_recorded_when_the_install_failed(self):
        for code in (4, 5):
            self.win.polls = [{"lines": finished(updated=1, failed=1, result=code, restart=1,
                                                 items=["2;KB1000001;a", "4;KB1000002;b"]), "alive": False}]
            self.state["patch"] = None
            _, status, _, _, _ = self.run_one()
            self.assertEqual(status, "FAILURE", code)
            self.assertTrue(self.record()["restart_needed"], code)
            self.assertFalse(self.record()["restarted"])

    def test_a_pending_restart_is_recorded_when_the_worker_failed(self):
        self.win.polls = [{"lines": ["CBVULN|restart|1", "CBVULN|failure|x", "CBVULN|done|1"], "alive": False}]
        self.run_one()
        self.assertTrue(self.record()["restart_needed"])

    def test_the_skipped_note_is_said_once(self):
        self.win.polls = [{"lines": ["CBVULN|found|3", "CBVULN|skipped|3", "CBVULN|done|1"], "alive": False}]
        self.run_one()
        self.assertEqual(self.record()["message"].count("skipped"), 1)
        self.assertNotIn("offered", self.record()["message"])

    def test_the_line_for_a_restart(self):
        self.win.polls = [{"lines": finished(updated=1, restart=1), "alive": False}]
        self.win.probes = [probe(BOOT_A), probe(BOOT_A), probe(BOOT_B, uptime=60)]
        _, _, output, _, _ = self.run_one()
        self.assertEqual(output, "win-01: 1 update installed, restarted, known vulnerabilities 10 to 2, with a fix 8 to 0.")

    def test_the_api_reports_no_updates_as_completed(self):
        self.state["patch"] = {"job_id": 11, "status": "no_updates", "restarted": False, "message": ""}
        hook = base.FakeHook()
        hook.job.wait_for_completion = lambda timeout: types.SimpleNamespace(id=11, status="SUCCESS", errors="")
        with mock.patch.object(base.hooks, "patch_hook", create=True, return_value=hook), \
                mock.patch.object(jobs, "precheck", return_value=None):
            result = base.api.install_updates(self.server, "owner")
        self.assertEqual((result["status"], result["message"]), ("completed", "No updates were available on win-01."))


class WorkerFixRoundStaticTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.code = wintask.build_worker(WORKER, {})

    def test_a_status_value_is_capped_at_900_characters(self):
        self.assertIn("$v.Length -gt 900){$v=$v.Substring(0,900)}", self.code)
        self.assertNotIn("3000", self.code)

    def test_the_failure_reports_the_innermost_hresult(self):
        self.assertIn("$x=$_.Exception;while($x.InnerException){$x=$x.InnerException}", self.code)
        self.assertIn("Cb hresult (Hx $x.HResult)", self.code)
        self.assertNotIn("Hx $_.Exception.HResult", self.code)


DRIVER = r"""
param([string]$Path,[string]$Mode,[string]$Log)
$ErrorActionPreference='Stop'
$t=$null;$e=$null
$ast=[System.Management.Automation.Language.Parser]::ParseFile($Path,[ref]$t,[ref]$e)
if($Mode -eq 'parse'){
'errors='+@($e).Count
foreach($x in @($e)){'E:'+$x.Message}
exit 0
}
$fs=$ast.FindAll({param($n) ($n -is [System.Management.Automation.Language.FunctionDefinitionAst]) -and (@('Cb','Rc','Hx') -contains $n.Name)},$true)
'functions='+(@($fs|ForEach-Object{$_.Name}|Sort-Object) -join ',')
$text=($fs|ForEach-Object{$_.Extent.Text}) -join "`n"
. ([scriptblock]::Create($text))
$LogF=$Log
$U8=New-Object Text.UTF8Encoding($false)
'rc2='+(Rc 2)
'rc0='+(Rc 0)
'rc5='+(Rc 5)
'rc6='+(Rc 6)
'rcneg='+(Rc -1)
'rctext='+(Rc 'abc')
'rcnull='+(Rc $null)
'hx1='+(Hx -2145124318)
'hx2='+(Hx 0)
'hx3='+(Hx 'zz')
'hx5='+(Hx -2147024891)
Cb 'k' ('a|b'+"`n"+'c'+[char]0xe9+'d'+"`t"+'e')
Cb 'long' ('x'*5000)
Cb 'empty' ''
Cb 'num' 7
"""


@unittest.skipUnless(POWERSHELL and sys.platform == "win32", "Windows PowerShell is not available")
class PowerShellTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="vuln-patch-ps-")
        cls.driver = os.path.join(cls.tmp, "driver.ps1")
        with open(cls.driver, "w", encoding="ascii", newline="\n") as handle:
            handle.write(DRIVER)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def ps(self, path, *args):
        done = subprocess.run([POWERSHELL, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", path]
                              + list(args), capture_output=True, timeout=180)
        return done.returncode, done.stdout.decode("utf-8", errors="replace"), done.stderr.decode("utf-8", errors="replace")

    def drive(self, path, mode, log=""):
        code, out_, err = self.ps(self.driver, "-Path", path, "-Mode", mode, "-Log", log)
        self.assertEqual((code, err.strip()), (0, ""), out_)
        return dict(line.split("=", 1) for line in out_.splitlines() if "=" in line and not line.startswith("E:")), out_

    def write(self, name, text):
        path = os.path.join(self.tmp, name)
        with open(path, "w", encoding="ascii", newline="\n") as handle:
            handle.write(text)
        return path

    def test_the_worker_parses(self):
        values, out_ = self.drive(WORKER, "parse")
        self.assertEqual(values["errors"], "0", out_)

    def test_the_stripped_worker_parses(self):
        path = self.write("worker.ps1", wintask.build_worker(WORKER, {}))
        values, out_ = self.drive(path, "parse")
        self.assertEqual(values["errors"], "0", out_)

    def test_the_inline_scripts_parse(self):
        for name, script in (("probe.ps1", patch.PROBE), ("reboot.ps1", patch._reboot_script())):
            values, out_ = self.drive(self.write(name, script), "parse")
            self.assertEqual(values["errors"], "0", name + out_)

    def test_the_helper_functions(self):
        log = os.path.join(self.tmp, "status.log")
        values, out_ = self.drive(WORKER, "funcs", log)
        self.assertEqual(values["functions"], "Cb,Hx,Rc", out_)
        self.assertEqual([values[k] for k in ("rc2", "rc0", "rc5", "rc6", "rcneg", "rctext", "rcnull")],
                         ["2", "0", "5", "4", "4", "4", "0"])
        self.assertEqual([values[k] for k in ("hx1", "hx2", "hx3", "hx5")],
                         ["0x80240022", "0x00000000", "0x00000000", "0x80070005"])
        with open(log, "rb") as handle:
            data = handle.read()
        self.assertFalse(data.startswith(b"\xef\xbb\xbf"))
        self.assertTrue(data.endswith(b"\n"))
        rows = data.decode("ascii").split("\n")[:-1]
        self.assertEqual(rows[0], "CBVULN|k|a/b c d e")
        self.assertEqual(rows[1], "CBVULN|long|" + "x" * 900)
        self.assertEqual(rows[2], "CBVULN|empty|")
        self.assertEqual(rows[3], "CBVULN|num|7")
        self.assertEqual(len(rows), 4)

    def test_the_probe_runs_here_and_reads_only(self):
        code, out_, err = self.ps(self.write("probe.ps1", patch.PROBE))
        self.assertEqual((code, err.strip()), (0, ""), out_)
        reply = dict(line.strip().split("|", 2)[1:] for line in out_.splitlines() if line.startswith("CBWT|"))
        self.assertNotIn("terr", reply, out_)
        self.assertTrue(re.fullmatch(r"[0-9]{14}", reply["boot"]), reply)
        self.assertTrue(re.fullmatch(r"[0-9]{3,6};[1-3];[0-9]+", reply["build"]), reply)
        parsed = patch._windows_probe_reply(wintask._split(out_)[1])
        self.assertIn(parsed["product"], ("1", "2", "3"))
        self.assertGreater(parsed["uptime"], 0)
        self.assertIn(reply["ccm"], ("0", "1"))
        self.assertEqual(wintask._split(out_)[1]["boot"], reply["boot"])


if __name__ == "__main__":
    unittest.main()
