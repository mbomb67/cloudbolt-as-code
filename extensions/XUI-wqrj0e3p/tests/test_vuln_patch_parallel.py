"""
Checks for the parallel Install updates run (vuln_patch.run_patch with the setting "Servers updated at the same time")
without CloudBolt: the per-server work (_patch_one) is a fake that blocks on events the test controls, so the number of
servers in each phase is counted exactly and nothing depends on timing.

    python -B -m unittest discover -s extensions/XUI-wqrj0e3p/tests -p "test_vuln_patch_parallel.py"

The stubs, the in-memory store and the fakes are those of test_vuln_patch.py.
"""
import contextlib
import queue
import threading
import time
import unittest
from unittest import mock

import test_vuln_patch as base

patch, store, fleet, jobs, db = base.patch, base.store, base.fleet, base.jobs, base.db
ORIGINAL_SLEEP = patch._sleep
WAIT = 15        # seconds a test waits for something that must happen; never reached when the code is right


def result(host, status="updated", **over):
    data = {"status": status, "message": "", "hostname": host, "warning": False, "updated": 3, "failed": 0,
            "restarted": False, "restart_needed": False, "kernels_removed": None, "before": None, "after": None,
            "family": "linux"}
    data.update(over)
    return data


class Phases:
    """A fake _patch_one that counts the servers in its update phase and in its rescan, and lets the test hold each
    one. By default nothing blocks; hold_update / hold_rescan name the hosts that wait for release()."""

    def __init__(self, rescan=True):
        self.lock = threading.Lock()
        self.updating = self.rescanning = 0
        self.peak_updating = self.peak_rescanning = 0
        self.starts = queue.Queue()
        self.at_rescan = queue.Queue()
        self.order = []
        self.threads = {}
        self.thread_jobs = {}
        self.peak_threads = 0
        self.gates = {}
        self.hold_update = set()
        self.hold_rescan = set()
        self.raises = {}
        self.rescan = rescan

    def note_threads(self):
        alive = sum(1 for t in threading.enumerate() if t.name.startswith("vuln-patch-"))
        with self.lock:
            self.peak_threads = max(self.peak_threads, alive)

    def gate(self, key):
        with self.lock:
            return self.gates.setdefault(key, threading.Event())

    def release(self, host, phase="update"):
        self.gate((host, phase)).set()

    def release_all(self):
        for host in list(self.hold_update):
            self.release(host, "update")
        for host in list(self.hold_rescan):
            self.release(host, "rescan")

    def __call__(self, job, server, restart, cfg):
        host = server.hostname
        with self.lock:
            self.updating += 1
            self.peak_updating = max(self.peak_updating, self.updating)
            self.order.append(host)
            self.threads[host] = threading.get_ident()
            self.thread_jobs[host] = (job, getattr(threading.current_thread(), "job", None))
        self.note_threads()
        self.starts.put(host)
        try:
            if host in self.hold_update and not self.gate((host, "update")).wait(WAIT):
                raise AssertionError("the test never released " + host)
            if host in self.raises:
                raise self.raises[host]
        finally:
            with self.lock:
                self.updating -= 1
        if self.rescan:
            with patch._rescan_turn(job, server):
                with self.lock:
                    self.rescanning += 1
                    self.peak_rescanning = max(self.peak_rescanning, self.rescanning)
                self.note_threads()
                self.at_rescan.put(host)
                try:
                    if host in self.hold_rescan and not self.gate((host, "rescan")).wait(WAIT):
                        raise AssertionError("the test never released the rescan of " + host)
                finally:
                    with self.lock:
                        self.rescanning -= 1
        return result(host)


class ParallelBase(base.Base):
    def setUp(self):
        super().setUp()
        self.settings["patch_parallel"] = 3
        self.settings["rematch_workers"] = 1
        self.phases = Phases()
        self.closed = []
        for patcher in (
            mock.patch.object(patch, "_patch_one", self.phases),
            mock.patch.object(patch, "_close_connection", lambda: self.closed.append(threading.get_ident())),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(self.phases.release_all)

    def servers(self, count=7):
        return [base.make_server(id=i, hostname="h{}".format(i)) for i in range(1, count + 1)]

    def start(self, servers=None, job=None):
        """Run run_patch in a thread of its own and return (thread, job, box) so the test can watch it."""
        job = job or base.make_job()
        box = {}

        def target():
            try:
                box["result"] = patch.run_patch(job, servers=servers or self.servers(), restart_if_needed=True)
            except BaseException as exc:
                box["error"] = exc
            box["workers_alive"] = [t.name for t in threading.enumerate() if t.name.startswith("vuln-patch-")]

        thread = threading.Thread(target=target, daemon=True)
        thread.start()
        return thread, job, box

    def finish(self, thread, box):
        thread.join(WAIT)
        self.assertFalse(thread.is_alive(), "run_patch did not return")
        self.assertNotIn("error", box, box.get("error"))
        return box["result"]

    def take(self, source, count=1):
        return [source.get(timeout=WAIT) for _ in range(count)]


class SlotTests(ParallelBase):
    def test_no_more_than_the_setting_update_at_once_and_all_finish(self):
        servers = self.servers(7)
        self.phases.hold_update = {s.hostname for s in servers}
        thread, job, box = self.start(servers)
        self.assertEqual(sorted(self.take(self.phases.starts, 3)), ["h1", "h2", "h3"])
        # The fourth cannot start while three hold the slots; one short look is the only wait of the file.
        self.assertRaises(queue.Empty, self.phases.starts.get, timeout=0.3)
        self.assertEqual(self.phases.updating, 3)
        self.phases.release_all()
        status, output, errors, outputs = self.finish(thread, box)
        self.assertEqual(self.phases.peak_updating, 3)
        self.assertEqual((status, errors), ("SUCCESS", ""))
        self.assertEqual(outputs["counts"]["updated"], 7)
        self.assertEqual(sorted(outputs["servers"]), [str(i) for i in range(1, 8)])

    def test_the_sentences_and_the_servers_keep_the_order_given_whichever_finishes_first(self):
        servers = self.servers(3)
        self.phases.hold_update = {"h1", "h2", "h3"}
        thread, job, box = self.start(servers)
        self.take(self.phases.starts, 3)
        for host in ("h3", "h2", "h1"):     # the last one finishes first
            self.phases.release(host)
        status, output, errors, outputs = self.finish(thread, box)
        self.assertEqual([line.split(":")[0] for line in output.splitlines()], ["h1", "h2", "h3"])
        self.assertEqual(list(outputs["servers"]), ["1", "2", "3"])

    def test_the_server_k_of_n_line_is_written_when_the_server_starts(self):
        servers = self.servers(4)
        self.settings["patch_parallel"] = 2
        self.phases.hold_update = {"h1", "h2", "h3", "h4"}
        thread, job, box = self.start(servers)
        self.take(self.phases.starts, 2)
        self.assertEqual(sorted(m for m in job.progress if m.startswith("Server ")),
                         ["Server 1 of 4: h1", "Server 2 of 4: h2"])
        self.phases.release_all()
        self.finish(thread, box)
        self.assertEqual(sorted(m for m in job.progress if m.startswith("Server ")),
                         ["Server {} of 4: h{}".format(i, i) for i in (1, 2, 3, 4)])

    def test_every_worker_closes_its_own_database_connection(self):
        thread, job, box = self.start(self.servers(5))
        self.finish(thread, box)
        self.assertEqual(len(set(self.closed)), 5)         # every worker thread, at least once, when it ends
        self.assertNotIn(thread.ident, self.closed)
        self.assertEqual(self.closed[-1] in set(self.phases.threads.values()), True)

    def test_a_worker_works_on_its_own_copy_of_the_server(self):
        copies = []

        def own(server):
            copy = base.make_server(id=server.id, hostname=server.hostname)
            copies.append((server, copy))
            return copy

        seen = []
        original = self.phases

        def spy(job, server, restart, cfg):
            seen.append(server)
            return original(job, server, restart, cfg)

        with mock.patch.object(patch, "_own_server", own), mock.patch.object(patch, "_patch_one", spy):
            thread, job, box = self.start(self.servers(3))
            self.finish(thread, box)
        self.assertEqual(len(copies), 3)
        self.assertTrue(all(any(s is copy for s in seen) and not any(s is orig for s in seen) for orig, copy in copies))

    def test_the_servers_are_read_again_by_id_with_a_fallback(self):
        server = base.make_server(id=4)
        fresh = base.make_server(id=4, hostname="fresh")
        with mock.patch.object(jobs, "Server", types_namespace(objects=Manager(fresh)), create=True):
            self.assertIs(patch._own_server(server), fresh)
        with mock.patch.object(jobs, "Server", types_namespace(objects=Manager(None)), create=True):
            self.assertIs(patch._own_server(server), server)
        with mock.patch.object(jobs, "Server", object, create=True):          # no manager at all
            self.assertIs(patch._own_server(server), server)


def types_namespace(**kw):
    import types
    return types.SimpleNamespace(**kw)


class Manager:
    def __init__(self, found):
        self.found = found

    def filter(self, **kw):
        return types_namespace(first=lambda: self.found)


class FailureTests(ParallelBase):
    def test_an_unexpected_exception_is_that_servers_failure_and_the_others_finish(self):
        self.phases.raises = {"h3": RuntimeError("boom")}
        thread, job, box = self.start(self.servers(5))
        status, output, errors, outputs = self.finish(thread, box)
        self.assertEqual(status, "FAILURE")
        self.assertEqual(outputs["counts"], {"updated": 4, "no_updates": 0, "restart_pending": 0, "failed": 1,
                                             "skipped": 0})
        self.assertEqual(outputs["servers"]["3"]["status"], "failed")
        self.assertEqual(outputs["servers"]["3"]["message"], "The update failed: boom")
        self.assertIn("h3: failed. The update failed: boom", output)
        self.assertIn("1 of 5 servers failed. First failure (h3)", errors)
        self.assertEqual(len(outputs["servers"]), 5)

    def test_a_base_exception_in_a_worker_is_a_failure_too(self):
        self.phases.raises = {"h2": SystemExit("bye")}
        thread, job, box = self.start(self.servers(3))
        status, output, errors, outputs = self.finish(thread, box)
        self.assertEqual((status, outputs["servers"]["2"]["status"]), ("FAILURE", "failed"))
        self.assertEqual(outputs["counts"]["updated"], 2)

    def test_a_server_that_cannot_be_read_again_is_a_failure_not_a_lost_server(self):
        with mock.patch.object(patch, "_own_server", side_effect=lambda s: 1 / 0 if s.id == 2 else s):
            thread, job, box = self.start(self.servers(3))
            status, output, errors, outputs = self.finish(thread, box)
        self.assertEqual(outputs["servers"]["2"]["status"], "failed")
        self.assertEqual(len(outputs["servers"]), 3)

    def test_a_failure_does_not_stop_the_next_servers_and_the_status_rules_hold(self):
        self.phases.raises = {"h1": patch.PatchFailed("The server cannot be updated: powered off.")}
        thread, job, box = self.start(self.servers(3))
        status, output, errors, outputs = self.finish(thread, box)
        self.assertEqual(status, "FAILURE")
        self.assertEqual(outputs["servers"]["1"]["message"], "The server cannot be updated: powered off.")
        self.assertEqual(outputs["counts"]["updated"], 2)

    def test_a_warning_gives_warning(self):
        def warn(job, server, restart, cfg):
            return result(server.hostname, "restart_pending", warning=True, restart_needed=True)

        with mock.patch.object(patch, "_patch_one", warn):
            thread, job, box = self.start(self.servers(3))
            status, output, errors, outputs = self.finish(thread, box)
        self.assertEqual((status, outputs["counts"]["restart_pending"]), ("WARNING", 3))


class CancelAndLimitTests(ParallelBase):
    def test_a_cancel_starts_no_new_server_and_the_ones_in_flight_finish(self):
        self.settings["patch_parallel"] = 2
        self.phases.hold_update = {"h1", "h2"}
        thread, job, box = self.start(self.servers(5))
        self.take(self.phases.starts, 2)
        self.cancelled = True
        self.phases.release_all()
        status, output, errors, outputs = self.finish(thread, box)
        self.assertEqual(status, "WARNING")
        self.assertEqual(outputs["counts"], {"updated": 2, "no_updates": 0, "restart_pending": 0, "failed": 0,
                                             "skipped": 3})
        self.assertEqual(sorted(outputs["servers"]), ["1", "2"])
        self.assertEqual(self.phases.order, ["h1", "h2"])
        self.assertIn("Stopped early because the job was canceled. 3 not started.", output)
        self.assertIn("keeps running", output)

    def test_the_time_limit_is_checked_when_each_server_is_about_to_start(self):
        # One server's worst case is (90 + 30 + 30) minutes = 9000 s, plus 1800 s (one scan, 20 minutes and the
        # margin) for the server that is ahead of the second one in the rescan queue. Two start at 0; the clock
        # is at 5000 when a slot frees, with nothing in flight.
        self.settings["patch_parallel"] = 2
        second_started, clock_set = threading.Event(), threading.Event()

        def timed(job, server, restart, cfg):
            self.phases.order.append(server.hostname)
            if server.hostname == "h1":
                second_started.wait(WAIT)
                self.clock.now = 5000
                clock_set.set()
            else:
                second_started.set()
                clock_set.wait(WAIT)
            return result(server.hostname)

        with mock.patch.object(patch, "_patch_one", timed), mock.patch.object(patch, "JOB_LIMIT_S", 10850):
            thread, job, box = self.start(self.servers(4))
            status, output, errors, outputs = self.finish(thread, box)
        self.assertEqual(status, "WARNING")
        self.assertEqual(outputs["counts"]["updated"], 2)
        self.assertEqual(outputs["counts"]["skipped"], 2)
        self.assertEqual(sorted(outputs["servers"]), ["1", "2"])
        self.assertIn("2 servers were not started", output)
        self.assertIn("8 hour limit", output)

    def test_the_job_thread_waits_for_its_workers_and_a_cancel_aborts_them(self):
        """A CancelJobException raised into the job's thread (here from the cancel check) aborts the workers at their
        next wait; the exception goes on."""
        self.settings["patch_parallel"] = 2
        waits = []

        def sleeper(job, server, restart, cfg):
            self.phases.starts.put(server.hostname)
            try:
                patch._sleep(30)         # the worker's wait: the abort ends it
            except BaseException as exc:
                if patch._is_abort(exc):
                    waits.append(server.hostname)
                raise
            return result(server.hostname)

        with mock.patch.object(patch, "_patch_one", sleeper), mock.patch.object(patch, "_sleep", ORIGINAL_SLEEP),                 mock.patch.object(patch, "CANCEL_CHECK_S", 0.05):
            calls = {"n": 0}

            def cancel_check(job):
                calls["n"] += 1
                if calls["n"] > 2:
                    raise base.CancelJob()
                return False

            with mock.patch.object(fleet, "_cancelled", side_effect=cancel_check):
                thread, job, box = self.start(self.servers(4))
                self.take(self.phases.starts, 2)
                thread.join(WAIT)
        self.assertFalse(thread.is_alive())
        self.assertIsInstance(box.get("error"), base.CancelJob)
        self.assertEqual(sorted(waits), ["h1", "h2"])
        self.assertEqual(box["workers_alive"], [])       # the job thread waited for them before it ended
        self.assertIn(patch.GUEST_KEEPS_RUNNING, job.progress)


class SerialTests(ParallelBase):
    def test_one_at_a_time_runs_in_the_job_thread_in_the_order_given(self):
        self.settings["patch_parallel"] = 1
        caller = {}

        def run():
            caller["id"] = threading.get_ident()
            return patch.run_patch(base.make_job(), servers=self.servers(4), restart_if_needed=True)

        status, output, errors, outputs = run()
        self.assertEqual(self.phases.order, ["h1", "h2", "h3", "h4"])
        self.assertEqual(set(self.phases.threads.values()), {caller["id"]})
        self.assertEqual(self.phases.peak_updating, 1)
        self.assertEqual(self.closed, [])
        self.assertEqual((status, outputs["counts"]["updated"]), ("SUCCESS", 4))

    def test_one_server_never_starts_a_thread(self):
        self.settings["patch_parallel"] = 5
        caller = threading.get_ident()
        patch.run_patch(base.make_job(), servers=self.servers(1), restart_if_needed=True)
        self.assertEqual(set(self.phases.threads.values()), {caller})

    def test_a_serial_run_stops_at_a_cancel_between_servers(self):
        self.settings["patch_parallel"] = 1

        def cancel_after_first(job, server, restart, cfg):
            self.cancelled = True
            return result(server.hostname)

        with mock.patch.object(patch, "_patch_one", cancel_after_first):
            status, output, errors, outputs = patch.run_patch(base.make_job(), servers=self.servers(3),
                                                              restart_if_needed=True)
        self.assertEqual((status, outputs["counts"]["updated"], outputs["counts"]["skipped"]), ("WARNING", 1, 2))


class RescanTests(ParallelBase):
    def test_rescans_are_limited_to_the_matches_at_once_setting(self):
        self.settings["patch_parallel"] = 5
        self.settings["rematch_workers"] = 2
        servers = self.servers(5)
        self.phases.hold_rescan = {s.hostname for s in servers}
        thread, job, box = self.start(servers)
        self.take(self.phases.at_rescan, 2)
        self.assertRaises(queue.Empty, self.phases.at_rescan.get, timeout=0.3)
        self.assertEqual(self.phases.rescanning, 2)
        self.assertTrue(any("waiting for its turn to scan again" in m for m in job.progress))
        self.phases.release_all()
        status, output, errors, outputs = self.finish(thread, box)
        self.assertEqual(self.phases.peak_rescanning, 2)
        self.assertEqual(outputs["counts"]["updated"], 5)

    def test_a_server_waiting_for_its_rescan_does_not_hold_an_update_slot(self):
        self.settings["patch_parallel"] = 2
        self.settings["rematch_workers"] = 1
        servers = self.servers(4)
        self.phases.hold_rescan = {"h1", "h2", "h3", "h4"}
        thread, job, box = self.start(servers)
        # h1 and h2 take the two slots, then both reach their rescan: one holds the limiter, the other waits.
        # Only then can h3 start, though neither rescan has finished. The fourth waits: at most
        # parallel + rescans = 3 servers are in flight.
        self.assertEqual(sorted(self.take(self.phases.starts, 3)), ["h1", "h2", "h3"])
        self.assertRaises(queue.Empty, self.phases.starts.get, timeout=0.3)
        self.take(self.phases.at_rescan, 1)
        self.assertEqual(self.phases.rescanning, 1)
        self.phases.release_all()
        status, output, errors, outputs = self.finish(thread, box)
        self.assertEqual(self.phases.peak_rescanning, 1)
        self.assertEqual(outputs["counts"]["updated"], 4)
        self.assertLessEqual(self.phases.peak_threads, 3)

    def test_a_rescan_waiting_for_its_turn_leaves_when_the_job_is_canceled(self):
        share = patch._Share(1, 1)
        share.rescans.acquire()                         # someone else holds the limiter
        share.abort.set()
        released = []
        me = threading.current_thread()
        me.vuln_share, me.vuln_release = share, lambda: released.append(True)
        try:
            with mock.patch.object(patch, "CANCEL_CHECK_S", 0.01):
                with self.assertRaises(patch._Aborted):
                    with patch._rescan_turn(base.make_job(), base.make_server()):
                        self.fail("must not run")
        finally:
            del me.vuln_share, me.vuln_release
        self.assertEqual(released, [True])


class ProgressTests(unittest.TestCase):
    def test_progress_writes_of_one_run_never_overlap(self):
        inside, worst, writes = [0], [0], []
        lock = threading.Lock()

        def set_progress(message, **kw):
            inside[0] += 1
            worst[0] = max(worst[0], inside[0])
            threading.Event().wait(0.002)       # widen the window in which a second writer would show
            writes.append(message)
            inside[0] -= 1

        job = base.types.SimpleNamespace(id=1, set_progress=set_progress)

        def writer(n):
            threading.current_thread().vuln_progress_lock = lock      # what a run puts on its threads
            for i in range(15):
                patch._progress(job, "h{}: {}".format(n, i))

        threads = [threading.Thread(target=writer, args=(n,)) for n in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(WAIT)
        self.assertEqual(worst[0], 1)
        self.assertEqual(len(writes), 90)

    def test_the_scan_helpers_take_the_runs_lock_and_a_thread_without_one_takes_none(self):
        held = []
        lock = threading.Lock()

        def set_progress(message, **kw):
            held.append(lock.locked())

        job = base.types.SimpleNamespace(set_progress=set_progress)
        jobs._progress(job, "no run")
        me = threading.current_thread()
        me.vuln_progress_lock = lock
        try:
            jobs._progress(job, "x")
            fleet._progress(job, "y")
            fleet._progress(job, "z", done=1, total=2)
        finally:
            del me.vuln_progress_lock
        self.assertEqual(held, [False, True, True, True])
        self.assertFalse(hasattr(jobs, "PROGRESS_LOCK"))      # no per-run state in a module global


class SettingTests(unittest.TestCase):
    def test_default_range_and_clamp(self):
        self.assertEqual(store.DEFAULT_SETTINGS["patch_parallel"], 5)
        self.assertEqual(store.PATCH_PARALLEL_RANGE, (1, 20))
        for saved, expect in ((0, 1), (21, 20), (8, 8), ("x", 5), (None, 5), (20, 20), (1, 1)):
            with mock.patch.object(store, "_read_json", return_value={"patch_parallel": saved}):
                self.assertEqual(store.get_settings()["patch_parallel"], expect, saved)
        with mock.patch.object(store, "_read_json", return_value={}):
            self.assertEqual(store.get_settings()["patch_parallel"], 5)

    def test_validation(self):
        self.assertEqual(store.validate_settings({})[0]["patch_parallel"], 5)
        clean, errors = store.validate_settings({"patch_parallel": "20"})
        self.assertEqual((clean["patch_parallel"], errors), (20, {}))
        for bad, expect in (("0", 1), ("21", 20), ("many", 5), ("", 5)):
            clean, errors = store.validate_settings({"patch_parallel": bad})
            self.assertEqual(clean["patch_parallel"], expect, bad)
            self.assertEqual(errors["patch_parallel"],
                             "Servers updated at the same time must be a whole number from 1 to 20.")

    def test_the_run_reads_the_setting_clamped(self):
        self.assertEqual(patch._parallel({"patch_parallel": 7}), 7)
        self.assertEqual(patch._parallel({"patch_parallel": 99}), 20)
        self.assertEqual(patch._parallel({"patch_parallel": 0}), 5)
        self.assertEqual(patch._parallel({"patching": True}), 5)    # read, and the key simply absent
        self.assertEqual(patch._parallel({}), 1)                    # the settings could not be read
        self.assertEqual(patch._parallel(None), 1)
        self.assertEqual(patch._parallel({"patch_parallel": "x"}), 1)


class NoTokenTests(unittest.TestCase):
    def test_no_double_braces_in_the_changed_python_files(self):
        import os
        for name in ("vuln_patch.py", "vuln_jobs.py", "vuln_fleet.py", "vuln_store.py", "vuln_hooks.py"):
            with open(os.path.join(base.PKG_DIR, name), encoding="utf-8") as handle:
                text = handle.read()
            self.assertNotIn("{{", text, name)
            self.assertNotIn("}}", text, name)


class Hand:
    """Run one function in a thread of its own the way a worker runs: the run's share on the thread object."""

    def __init__(self, share, released=None):
        self.share, self.released = share, released if released is not None else []
        self.box = {}

    def run(self, function, *args):
        def target():
            me = threading.current_thread()
            me.vuln_share, me.vuln_release = self.share, lambda: self.released.append(True)
            try:
                self.box["value"] = function(*args)
            except BaseException as exc:
                self.box["error"] = exc

        self.thread = threading.Thread(target=target, daemon=True)
        self.thread.start()
        return self

    def join(self):
        self.thread.join(WAIT)
        assert not self.thread.is_alive(), "the thread did not end"
        return self.box


def copy_function(function):
    """A new function object with the same code and globals, as a reload of the module makes."""
    import types
    new = types.FunctionType(function.__code__, function.__globals__, function.__name__, function.__defaults__,
                             function.__closure__)
    return new


class ReloadTests(ParallelBase):
    """The job files reload this module in the same process. A run must not depend on anything a reload replaces."""

    def swap(self, stack, names=("_checkpoint", "_share", "_is_abort")):
        old = patch._Aborted
        for name in names:
            stack.enter_context(mock.patch.object(patch, name, copy_function(getattr(patch, name))))
        stack.enter_context(mock.patch.object(patch, "_Aborted", type("_Aborted", (BaseException,), {"vuln_abort": True})))
        stack.enter_context(mock.patch.object(patch, "_Share", type("_Share", (), {})))
        return old

    def test_slot_hand_back_and_results_survive_a_reload_in_the_middle(self):
        self.settings["patch_parallel"] = 2
        self.settings["rematch_workers"] = 3
        servers = self.servers(3)
        self.phases.hold_update = {"h1", "h2"}
        self.phases.hold_rescan = {"h1", "h2"}
        thread, job, box = self.start(servers)
        self.take(self.phases.starts, 2)
        with contextlib.ExitStack() as stack:
            self.swap(stack)
            self.phases.release("h1")
            self.phases.release("h2")
            # h1 and h2 reach their rescan and hand their slots back: h3 can start though they still hold their rescans
            self.assertEqual(self.take(self.phases.starts, 1), ["h3"])
            self.take(self.phases.at_rescan, 2)
            self.phases.release_all()
            status, output, errors, outputs = self.finish(thread, box)
        self.assertEqual((status, outputs["counts"]["updated"]), ("SUCCESS", 3))

    def test_a_cancel_after_a_reload_still_aborts_the_workers(self):
        self.settings["patch_parallel"] = 2
        stop = threading.Event()
        seen = []

        def sleeper(job, server, restart, cfg):
            self.phases.starts.put(server.hostname)
            try:
                patch._sleep(30)
            except BaseException as exc:
                seen.append(patch._is_abort(exc))
                raise
            return result(server.hostname)

        def cancel_check(job):
            if stop.is_set():
                raise base.CancelJob()
            return False

        with mock.patch.object(patch, "_patch_one", sleeper), mock.patch.object(patch, "_sleep", ORIGINAL_SLEEP), \
                mock.patch.object(patch, "CANCEL_CHECK_S", 0.05), mock.patch.object(fleet, "_cancelled", cancel_check):
            thread, job, box = self.start(self.servers(4))
            self.take(self.phases.starts, 2)
            with contextlib.ExitStack() as stack:
                self.swap(stack, ("_sleep", "_checkpoint", "_share", "_is_abort"))
                stop.set()
                thread.join(WAIT)
        self.assertFalse(thread.is_alive())
        self.assertIsInstance(box.get("error"), base.CancelJob)
        self.assertEqual(seen, [True, True])
        self.assertEqual(box["workers_alive"], [])

    def test_an_abort_of_an_earlier_load_is_not_a_failed_server(self):
        old = type("_Aborted", (BaseException,), {"vuln_abort": True})
        finished = [None]
        share = patch._Share(1, 1)
        share.slots.acquire()
        share.begin()
        hand = Hand(share)
        with mock.patch.object(patch, "_work_on", side_effect=old()):
            hand.run(patch._worker, share, None, base.make_job(), base.make_server(), 0, 1, True, {}, finished)
            box = hand.join()
        self.assertNotIn("error", box)
        self.assertEqual(finished, [None])
        self.assertEqual(share.in_flight(), 0)


class CheckpointTests(base.Base):
    def setUp(self):
        super().setUp()
        self.share = patch._Share(1, 1)
        self.rescan()

    def test_an_abort_between_the_last_poll_and_the_restart_sends_no_restart(self):
        self.guest.answers("poll", base.out(state="running", phase="Updating packages"),
                           base.out(state="done", updated=7, failed=0, exit=0, restart=1, kernel_running="k1",
                                    kernel_newest="k2"))
        polls = []

        def on_call(mode):
            if mode == "poll":
                polls.append(1)
                if len(polls) == 2:
                    self.share.abort.set()      # the user cancels while the last poll is on its way

        self.guest.on_call = on_call
        box = Hand(self.share).run(patch._patch_one, base.make_job(), base.make_server(), True, dict(self.settings)).join()
        self.assertTrue(patch._is_abort(box["error"]))
        self.assertNotIn("reboot", self.guest.calls)
        self.assertNotIn("oldkernels", self.guest.calls)
        self.assertIn("cleanup", self.guest.calls)          # the cleanup that must follow an abort still runs
        self.assertIsNone(self.state["active"])             # the marker is cleared
        self.assertNotIn("patch", self.state)               # no record
        self.assertEqual(base.EVENTS, [])                   # no history event

    def test_an_abort_before_the_update_starts_sends_nothing_to_the_guest(self):
        self.share.abort.set()
        box = Hand(self.share).run(patch._patch_one, base.make_job(), base.make_server(), True, dict(self.settings)).join()
        self.assertTrue(patch._is_abort(box["error"]))
        self.assertNotIn("start", self.guest.calls)
        self.assertNotIn("probe", self.guest.calls)

    def test_an_aborted_run_writes_no_record_and_no_event(self):
        self.share.abort.set()
        record = {"status": "updated", "message": "m"}
        box = Hand(self.share).run(patch._finish, base.make_job(), base.make_server(), record).join()
        self.assertTrue(patch._is_abort(box["error"]))
        self.assertNotIn("patch", self.state)
        self.assertEqual(base.EVENTS, [])

    def test_the_serial_run_has_no_checkpoint(self):
        patch._checkpoint()          # no run on this thread: nothing happens


class RescanWaitTests(base.Base):
    def setUp(self):
        super().setUp()
        self.share = patch._Share(1, 1)
        self.child = base.types.SimpleNamespace(id=99, status="RUNNING", saved=[],
                                                save=lambda **kw: self.child.saved.append(kw))
        self.active_jobs[99] = self.child
        self.started = base.types.SimpleNamespace(id=99, wait_for_completion=lambda timeout: self.fail("core's wait"))

    def test_a_worker_in_the_rescan_wait_leaves_at_once_on_an_abort_and_does_not_cancel_the_child(self):
        self.share.abort.set()
        with mock.patch.object(patch, "_sleep", ORIGINAL_SLEEP), mock.patch.object(patch, "RESCAN_POLL_S", 60):
            began = time.monotonic()
            box = Hand(self.share).run(patch._wait_for_child, self.started, 3600).join()
            took = time.monotonic() - began
        self.assertTrue(patch._is_abort(box["error"]))
        self.assertLess(took, 10)
        self.assertEqual(self.child.saved, [])
        self.assertEqual(self.child.status, "RUNNING")

    def test_a_worker_returns_the_child_when_it_ends(self):
        self.child.status = "SUCCESS"
        box = Hand(self.share).run(patch._wait_for_child, self.started, 3600).join()
        self.assertIs(box["value"], self.child)

    def test_a_worker_cancels_the_child_and_times_out_at_the_limit(self):
        box = Hand(self.share).run(patch._wait_for_child, self.started, 100).join()
        self.assertIsInstance(box["error"], base.TimeoutException)
        self.assertEqual(self.child.status, "TO_CANCEL")

    def test_the_serial_run_uses_cores_wait(self):
        ended = object()
        started = base.types.SimpleNamespace(id=99, wait_for_completion=lambda timeout: ended)
        self.assertIs(patch._wait_for_child(started, 5), ended)


class GraceTests(unittest.TestCase):
    class FakeThread:
        def __init__(self, raises):
            self.raises, self.joins, self.alive = raises, 0, True

        def is_alive(self):
            return self.alive

        def join(self, timeout=None):
            self.joins += 1
            if self.joins <= self.raises:
                raise base.CancelJob()          # core raises its cancel into the job thread again
            self.alive = False

    def test_repeated_cancels_during_the_wait_do_not_cut_it_short(self):
        share = patch._Share(1, 1)
        fake = self.FakeThread(raises=3)
        patch._grace(share, [(0, fake)], [base.make_server()])
        self.assertTrue(share.abort.is_set())
        self.assertFalse(fake.alive)
        self.assertEqual(fake.joins, 4)

    def test_the_wait_ends_at_the_limit_and_logs_the_busy_servers(self):
        share = patch._Share(1, 1)

        class Stuck:
            def is_alive(self):
                return True

            def join(self, timeout=None):
                pass

        with mock.patch.object(patch, "ABORT_WAIT_S", 0.05), mock.patch.object(patch.logger, "warning", create=True) as warn:
            patch._grace(share, [(0, Stuck())], [base.make_server(hostname="busy-1")])
        self.assertTrue(any("busy-1" in str(call) for call in warn.call_args_list))

    def test_the_wait_is_long_enough_for_one_guest_call(self):
        self.assertGreaterEqual(patch.ABORT_WAIT_S, 120)


class BoundTests(ParallelBase):
    def test_threads_and_updates_are_bounded_with_a_long_rescan_queue(self):
        self.settings["patch_parallel"] = 2
        self.settings["rematch_workers"] = 1
        servers = self.servers(12)
        self.phases.hold_rescan = {s.hostname for s in servers}
        thread, job, box = self.start(servers)
        for _ in range(12):
            host = self.take(self.phases.at_rescan)[0]
            self.phases.release(host, "rescan")
        status, output, errors, outputs = self.finish(thread, box)
        self.assertLessEqual(self.phases.peak_threads, 3)
        self.assertLessEqual(self.phases.peak_updating, 2)
        self.assertEqual(self.phases.peak_rescanning, 1)
        self.assertEqual((status, outputs["counts"]["updated"]), ("SUCCESS", 12))

    def test_the_queue_allowance_of_the_time_check(self):
        self.assertEqual(patch._queue_s(0, 1), 0)
        scan = 20 * 60 + patch.WAIT_MARGIN_S
        self.assertEqual(patch._queue_s(5, 1), 5 * scan)
        self.assertEqual(patch._queue_s(5, 2), 3 * scan)

    def test_a_server_that_would_meet_a_rescan_queue_past_the_limit_is_not_started(self):
        # 9000 s for one server; with one server in flight ahead of it, one more scan (1800 s) in the worst case.
        job = base.make_job()
        with mock.patch.object(patch, "JOB_LIMIT_S", 10000):
            self.assertEqual(patch._stop_reason(job, 0, 9000, patch._queue_s(0, 1)), (False, False))
            self.assertEqual(patch._stop_reason(job, 0, 9000, patch._queue_s(1, 1)), (False, True))
            self.assertEqual(patch._stop_reason(job, 0, 9000, patch._queue_s(1, 2)), (False, True))

    def test_zero_or_none_matches_at_once_means_the_default_one_rescan(self):
        for value in (0, None):
            with self.subTest(value=value):
                self.settings["rematch_workers"] = value
                self.phases = Phases()
                servers = self.servers(3)
                self.phases.hold_rescan = {s.hostname for s in servers}
                with mock.patch.object(patch, "_patch_one", self.phases):
                    thread, job, box = self.start(servers)
                    self.take(self.phases.at_rescan, 1)
                    self.assertRaises(queue.Empty, self.phases.at_rescan.get, timeout=0.2)
                    self.phases.release_all()
                    status, output, errors, outputs = self.finish(thread, box)
                self.assertEqual(self.phases.peak_rescanning, 1)
                self.assertEqual(outputs["counts"]["updated"], 3)

    def test_a_worker_that_dies_while_waiting_for_the_rescan_permit_releases_everything(self):
        self.settings["patch_parallel"] = 3
        self.settings["rematch_workers"] = 1
        self.phases.hold_rescan = {"h1"}
        self.phases.hold_update = {"h2", "h3"}
        real = patch._progress

        def progress(job, message):
            if "waiting for its turn" in message:
                raise RuntimeError("progress failed")
            return real(job, message)

        with mock.patch.object(patch, "_progress", progress):
            thread, job, box = self.start(self.servers(3))
            self.assertEqual(self.take(self.phases.starts, 1), ["h1"])
            self.take(self.phases.at_rescan, 1)             # h1 holds the only rescan permit
            self.phases.release("h2")
            self.phases.release("h3")
            self.take(self.phases.starts, 2)
            self.phases.release("h1", "rescan")
            status, output, errors, outputs = self.finish(thread, box)
        self.assertEqual(outputs["servers"]["1"]["status"], "updated")
        self.assertEqual([outputs["servers"][k]["status"] for k in ("2", "3")], ["failed", "failed"])
        self.assertEqual(status, "FAILURE")

    def test_a_thread_that_cannot_start_is_that_servers_failure(self):
        real_start = threading.Thread.start

        def start(thread):
            if thread.name == "vuln-patch-2":
                raise RuntimeError("can't start new thread")
            return real_start(thread)

        with mock.patch.object(threading.Thread, "start", start):
            thread, job, box = self.start(self.servers(4))
            status, output, errors, outputs = self.finish(thread, box)
        self.assertEqual(outputs["servers"]["2"]["status"], "failed")
        self.assertEqual(outputs["servers"]["2"]["message"], "The update could not be started on this server.")
        self.assertEqual(outputs["counts"], {"updated": 3, "no_updates": 0, "restart_pending": 0, "failed": 1,
                                             "skipped": 0})
        self.assertNotIn("canceled", output)

    def test_a_server_listed_twice_is_updated_once_serial_or_parallel(self):
        for parallel in (1, 3):
            with self.subTest(parallel=parallel):
                self.settings["patch_parallel"] = parallel
                self.phases = Phases()
                servers = self.servers(2) + [base.make_server(id=1, hostname="again")]
                with mock.patch.object(patch, "_patch_one", self.phases):
                    status, output, errors, outputs = patch.run_patch(base.make_job(), servers=servers,
                                                                      restart_if_needed=True)
                self.assertEqual(sorted(self.phases.order), ["h1", "h2"])
                self.assertEqual(sorted(outputs["servers"]), ["1", "2"])

    def test_a_worker_has_the_job_and_the_run_on_its_thread(self):
        job = base.make_job()
        thread, _, box = self.start(self.servers(2), job=job)
        self.finish(thread, box)
        for given, on_thread in self.phases.thread_jobs.values():
            self.assertIs(given, on_thread)         # the worker's job is the one core's thread lookup finds
            self.assertEqual(given.id, job.id)


class RealParallelTests(base.Base):
    """The real _patch_one for three servers at once, the guest faked as the serial tests fake it: markers, the
    handover to the rescan, the limiter and clear_active all run in parallel."""

    def setUp(self):
        super().setUp()
        self.settings.update(patch_parallel=3, rematch_workers=1)
        ids = (1, 2, 3)
        self.states = {i: {"active": None, "last": None, "history": []} for i in ids}
        self.guests = {i: base.happy_guest() for i in ids}
        self.seen = {}
        self.lock = threading.Lock()
        self.inside = [0, 0]
        for patcher in (
            mock.patch.object(store, "read_state", side_effect=lambda sid: self.states[sid]),
            mock.patch.object(store, "_update", side_effect=lambda sid, change: change(self.states[sid])),
            mock.patch.object(store, "set_active", side_effect=lambda sid, marker: self.states[sid].__setitem__("active", marker)),
            mock.patch.object(store, "clear_active", side_effect=self.clear),
            mock.patch.object(jobs, "_execute", lambda server, script, timeout, job=None: self.guests[server.id](server, script, timeout, job)),
            mock.patch.object(db, "server_summary", return_value={"vulns": 5, "vuln_fixable": 5}),
            mock.patch.object(jobs, "start", side_effect=self.start_rescan),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def clear(self, sid, scan_id=None):
        active = self.states[sid].get("active")
        if scan_id is None or (isinstance(active, dict) and active.get("scan_id") == scan_id):
            self.states[sid]["active"] = None

    def start_rescan(self, profile, server, base_url, parent_job=None, handover_from=None):
        with self.lock:
            self.inside[0] += 1
            self.inside[1] = max(self.inside[1], self.inside[0])
            self.seen[server.id] = {"handover": handover_from, "marker": dict(self.states[server.id]["active"] or {})}
        try:
            return self.rescan_started
        finally:
            with self.lock:
                self.inside[0] -= 1

    def test_three_servers_update_together_through_the_real_flow(self):
        servers = [base.make_server(id=i, hostname="h{}".format(i)) for i in (1, 2, 3)]
        status, output, errors, outputs = patch.run_patch(base.make_job(), servers=servers, restart_if_needed=True)
        self.assertEqual((status, errors), ("SUCCESS", ""))
        self.assertEqual(outputs["counts"]["updated"], 3)
        self.assertEqual([line.split(":")[0] for line in output.splitlines()], ["h1", "h2", "h3"])
        for i in (1, 2, 3):
            self.assertIsNone(self.states[i]["active"])                       # every marker was cleared
            self.assertEqual(self.states[i]["patch"]["status"], "updated")    # and every record written
            self.assertEqual(self.seen[i]["handover"], 11)                    # the rescan was a handover from the job
            self.assertEqual(self.seen[i]["marker"]["kind"], "patch")         # while the server held its own marker
            self.assertIn("cleanup", self.guests[i].calls)
        self.assertEqual(len(base.EVENTS), 3)
        self.assertEqual(self.inside[1], 1)                                   # one rescan at a time


if __name__ == "__main__":
    unittest.main()
