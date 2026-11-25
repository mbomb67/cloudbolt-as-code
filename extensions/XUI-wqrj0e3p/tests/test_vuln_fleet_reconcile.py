"""
Checks for vuln_fleet.reconcile_servers, the call a web request makes before it reads the Servers list, without
CloudBolt: the ORM, the findings store and the state files are replaced. The stubs and fakes are the ones of
test_vuln_fleet.py, which loads the extension's modules against them.

    python -B -m unittest discover -s extensions/XUI-wqrj0e3p/tests -p "test_vuln_fleet_reconcile.py"
"""
import contextlib
import types
import unittest
from unittest import mock

from test_vuln_fleet import (Base, db, fake_group_model, fake_server_model, fleet, jobs, make_server, repo, store)


def live(server_id, status="ACTIVE", **names):
    """A server as the sweep reads it: the values_list fields are attributes under their query names."""
    attrs = {"hostname": "host-%d" % server_id, "group__name": "Group", "environment__name": "Env",
             "owner__user__first_name": "", "owner__user__last_name": "", "owner__user__username": "",
             "os_build__name": ""}
    attrs.update(names)
    return make_server(id=server_id, status=status, **attrs)


def stored_names(server_id, **over):
    row = {"hostname": "host-%d" % server_id, "group_name": "Group", "environment": "Env", "owner": "",
           "os_build": ""}
    row.update(over)
    return row


class ReconcileBase(Base):
    def setUp(self):
        super().setUp()
        fleet._reconcile_state.update(stamp=None, result=None)
        self.addCleanup(fleet._reconcile_state.update, stamp=None, result=None)
        self.events = []
        self.now = [1000.0]

    def run_reconcile(self, known, servers, unassigned=(), active=(), attributes=None, fail=False, group_fails=False,
                      forget_fails=(), repo_fails=(), update_fails=False, **kwargs):
        forgot = {"db": [], "repo": [], "state": []}
        updates = []
        sweeps = []
        model = fake_server_model(servers, fail=fail)
        real_filter = model.objects.filter

        def filter_(**kw):
            self.events.append("sweep")
            sweeps.append(sorted(kw.get("id__in", [])))
            return real_filter(**kw)

        model.objects.filter = filter_

        def forget_server(server_id):
            self.events.append("forget")
            if server_id in forget_fails:
                raise RuntimeError("database is locked")
            forgot["db"].append(server_id)

        def forget_repo(server_id):
            if server_id in repo_fails:
                raise RuntimeError("disk full")
            forgot["repo"].append(server_id)

        def active_job(server_id, state=None):
            return ({"job_id": 1}, object()) if server_id in active else None

        def update(rows):
            self.events.append("update")
            if update_fails:
                raise RuntimeError("readonly database")
            updates.append(rows)
            return len(rows)

        if group_fails:
            class Broken:
                @staticmethod
                def ids():
                    raise RuntimeError("no")

            group = types.SimpleNamespace(unassigned=Broken)
        else:
            group = fake_group_model(unassigned)
        known_rows = known if isinstance(known, Exception) else [{"server_id": i} for i in known]
        patches = [
            mock.patch.object(fleet, "Server", model),
            mock.patch.object(fleet, "Group", group),
            mock.patch.object(fleet, "_clock", side_effect=lambda: self.now[0]),
            mock.patch.object(db, "known_servers", create=True,
                              side_effect=known_rows if isinstance(known, Exception) else None,
                              return_value=None if isinstance(known, Exception) else known_rows),
            mock.patch.object(db, "server_attributes", create=True, return_value=attributes or {}),
            mock.patch.object(db, "update_server_attributes", create=True, side_effect=update),
            mock.patch.object(db, "forget_server", create=True, side_effect=forget_server),
            mock.patch.object(store, "forget_state", side_effect=lambda i: forgot["state"].append(i)),
            mock.patch.object(repo, "forget", side_effect=forget_repo),
            mock.patch.object(jobs, "active_job", side_effect=active_job),
        ]
        self.patches = contextlib.ExitStack()
        for patcher in patches:
            self.patches.enter_context(patcher)
        self.addCleanup(self.patches.close)
        self.forgot, self.updates, self.sweeps = forgot, updates, sweeps
        return fleet.reconcile_servers(**kwargs)


class PurgeTests(ReconcileBase):
    def test_historical_missing_and_unassigned_are_forgotten_and_decom_is_only_hidden(self):
        servers = [live(1, "HISTORICAL"), live(2, "DECOM"), live(4, group_id=99), live(5), live(6, "MODIFY")]
        # 3 has a stored row and no Server at all; 4 is in the Unassigned group.
        result = self.run_reconcile([1, 2, 3, 4, 5, 6], servers, unassigned=[99])
        self.assertEqual(sorted(self.forgot["db"]), [1, 3, 4])
        self.assertEqual(sorted(self.forgot["repo"]), [1, 3, 4])
        self.assertEqual(sorted(self.forgot["state"]), [1, 3, 4])
        # A failed delete returns a server from DECOM to ACTIVE, so it is never purged on that status.
        self.assertEqual(result, {"checked": 6, "forgotten": 3, "refreshed": 0, "hidden": [2], "skipped": False})

    def test_the_constants_say_what_is_current_and_what_is_purged(self):
        self.assertEqual(fleet.CURRENT_STATUSES, ("ACTIVE", "MODIFY"))
        self.assertEqual(fleet.PURGE_STATUSES, ("HISTORICAL",))
        self.assertEqual((fleet.RECONCILE_MIN_S, fleet.MAX_FORGET_PER_CALL), (15, 50))

    def test_active_and_modify_are_kept_and_listed(self):
        result = self.run_reconcile([1, 2], [live(1, "ACTIVE"), live(2, "MODIFY")])
        self.assertEqual(self.forgot["db"], [])
        self.assertEqual((result["forgotten"], result["hidden"]), (0, []))

    def test_prov_and_provfailed_are_kept_but_hidden(self):
        servers = [live(1, "PROV"), live(2, "PROVFAILED"), live(3), live(4, "HISTORICAL")]
        result = self.run_reconcile([1, 2, 3, 4], servers)
        self.assertEqual(self.forgot["db"], [4])
        self.assertEqual(result["hidden"], [1, 2])
        self.assertEqual(result["forgotten"], 1)

    def test_a_server_with_a_scan_running_is_not_forgotten_and_is_hidden(self):
        result = self.run_reconcile([1, 2], [live(1, "HISTORICAL"), live(2, "HISTORICAL")], active=[1])
        self.assertEqual(self.forgot["db"], [2])
        self.assertEqual((result["forgotten"], result["hidden"]), (1, [1]))

    def test_more_than_max_forget_forgets_the_first_ones_and_hides_the_rest(self):
        ids = [1, 2, 3, 4, 5]
        result = self.run_reconcile(ids, [live(i, "HISTORICAL") for i in ids] + [live(6)], max_forget=2)
        self.assertEqual(sorted(self.forgot["db"]), [1, 2])
        self.assertEqual((result["forgotten"], result["hidden"]), (2, [3, 4, 5]))

    def test_the_default_cap_is_fifty_per_call(self):
        ids = list(range(1, 61))
        result = self.run_reconcile(ids, [live(i, "HISTORICAL") for i in ids])
        self.assertEqual((result["forgotten"], len(result["hidden"])), (50, 10))
        self.assertEqual(result["hidden"], list(range(51, 61)))

    def test_a_purge_that_raises_leaves_the_id_hidden_and_does_not_stop_the_others(self):
        servers = [live(1, "HISTORICAL"), live(2, "HISTORICAL"), live(3, "HISTORICAL"), live(4)]
        result = self.run_reconcile([1, 2, 3, 4], servers, forget_fails=[1], repo_fails=[3])
        # The files go before the findings row: 1's files went and its row stayed (retried by the next sweep), and
        # 3 failed on its first step, so nothing of it was touched.
        self.assertEqual(self.forgot["db"], [2])
        self.assertEqual(self.forgot["repo"], [1, 2])
        self.assertEqual(self.forgot["state"], [1, 2])
        self.assertEqual((result["forgotten"], result["hidden"]), (1, [1, 3]))

    def test_a_server_current_again_when_the_purge_starts_is_kept_and_hidden(self):
        class Flip:
            """The sweep sees the server HISTORICAL; the re-read just before the delete sees it ACTIVE."""
            def __init__(self):
                self.calls = 0

            def __iter__(self):
                self.calls += 1
                return iter([live(1, "HISTORICAL" if self.calls == 1 else "ACTIVE")])

        flip = Flip()
        result = self.run_reconcile([1], flip)
        self.assertEqual(flip.calls, 2)
        self.assertEqual(self.forgot, {"db": [], "repo": [], "state": []})
        self.assertEqual((result["forgotten"], result["hidden"]), (0, [1]))

    def test_a_failed_reread_deletes_nothing(self):
        class Breaks:
            def __init__(self):
                self.calls = 0

            def __iter__(self):
                self.calls += 1
                if self.calls > 1:
                    raise RuntimeError("database is gone")
                return iter([live(1, "HISTORICAL")])

        result = self.run_reconcile([1], Breaks())
        self.assertEqual(self.forgot, {"db": [], "repo": [], "state": []})
        self.assertEqual((result["forgotten"], result["hidden"]), (0, [1]))

    def test_an_emptied_listing_keeps_the_last_hidden_list(self):
        self.run_reconcile([1, 2], [live(1, "PROV"), live(2)])
        self.now[0] += 20
        with mock.patch.object(db, "known_servers", create=True, return_value=[]):
            again = fleet.reconcile_servers()
        self.assertEqual((again["hidden"], again["skipped"]), ([1], True))
        self.assertEqual(fleet.reconcile_servers(force=True)["hidden"], [1])

    def test_nothing_stored_means_no_query(self):
        result = self.run_reconcile([], [], fail=True)
        self.assertEqual(result, {"checked": 0, "forgotten": 0, "refreshed": 0, "hidden": [], "skipped": False})
        self.assertEqual(self.sweeps, [])

    def test_an_orm_failure_forgets_nothing(self):
        result = self.run_reconcile([1, 2], [live(1, "HISTORICAL")], fail=True)
        self.assertEqual(self.forgot, {"db": [], "repo": [], "state": []})
        self.assertEqual(result, {"checked": 0, "forgotten": 0, "refreshed": 0, "hidden": [], "skipped": True})

    def test_a_group_query_failure_forgets_nothing(self):
        result = self.run_reconcile([1], [live(1, "HISTORICAL")], group_fails=True)
        self.assertEqual(self.forgot["db"], [])
        self.assertTrue(result["skipped"])

    def test_an_unreadable_store_listing_forgets_nothing(self):
        result = self.run_reconcile(RuntimeError("busy"), [live(1, "HISTORICAL")])
        self.assertEqual(self.forgot["db"], [])
        self.assertTrue(result["skipped"])
        self.assertEqual(self.sweeps, [])

    def test_an_unexpected_error_inside_never_escapes(self):
        with mock.patch.object(fleet, "_forget", side_effect=RuntimeError("boom")):
            result = self.run_reconcile([1], [live(1, "HISTORICAL")])
        self.assertEqual(result, {"checked": 0, "forgotten": 0, "refreshed": 0, "hidden": [], "skipped": True})

    def test_the_sweep_is_chunked_and_finishes_before_anything_is_written(self):
        ids = list(range(1, 1202))
        servers = [live(i, "HISTORICAL" if i in (3, 1201) else "ACTIVE") for i in ids]
        result = self.run_reconcile(ids, servers, attributes={1: stored_names(1, group_name="Old")})
        self.assertEqual([len(chunk) for chunk in self.sweeps[:3]], [500, 500, 201])
        self.assertEqual(sorted(i for chunk in self.sweeps[:3] for i in chunk), ids)
        # The three chunks, the one re-read of the two ids about to be purged, then the writes.
        self.assertEqual(self.sweeps[3], [3, 1201])
        self.assertEqual(self.events, ["sweep"] * 4 + ["forget", "forget", "update"])
        self.assertEqual((result["checked"], result["forgotten"], result["refreshed"]), (1201, 2, 1))

    def test_the_sweep_runs_in_the_calling_thread_only(self):
        from test_vuln_fleet import VIOLATIONS
        self.run_reconcile([1, 2], [live(1), live(2, "HISTORICAL")])
        self.assertEqual(VIOLATIONS, [])


class ThrottleTests(ReconcileBase):
    def test_a_second_call_inside_the_window_answers_skipped_without_touching_the_orm(self):
        servers = [live(1, "PROV"), live(2, "HISTORICAL"), live(3)]
        first = self.run_reconcile([1, 2, 3], servers)
        self.assertEqual((first["skipped"], first["forgotten"], first["hidden"]), (False, 1, [1]))
        calls = len(self.sweeps)
        self.now[0] += 14.9
        second = fleet.reconcile_servers()
        self.assertEqual(len(self.sweeps), calls)
        self.assertEqual(self.forgot["db"], [2])
        self.assertTrue(second["skipped"])
        self.assertEqual(second["hidden"], [1])             # what the last sweep hid stays hidden
        self.assertEqual((second["checked"], second["forgotten"]), (0, 0))
        second["hidden"].append(99)                         # the answer is a copy
        self.assertEqual(fleet.reconcile_servers()["hidden"], [1])

    def test_after_the_window_it_runs_again(self):
        self.run_reconcile([1, 2], [live(1), live(2, "HISTORICAL")])
        calls = len(self.sweeps)
        self.now[0] += 15
        again = fleet.reconcile_servers()
        self.assertFalse(again["skipped"])
        self.assertGreater(len(self.sweeps), calls)

    def test_force_overrides_the_window(self):
        self.run_reconcile([1], [live(1)])
        forced = fleet.reconcile_servers(force=True)
        self.assertFalse(forced["skipped"])
        self.assertEqual(len(self.sweeps), 2)

    def test_a_failed_sweep_counts_for_the_window_too_and_keeps_the_last_hidden(self):
        self.run_reconcile([1, 2], [live(1, "PROV"), live(2)])
        self.now[0] += 20
        with mock.patch.object(fleet.Server.objects, "filter", side_effect=RuntimeError("gone")):
            failed = fleet.reconcile_servers()
        self.assertEqual((failed["skipped"], failed["hidden"], failed["forgotten"]), (True, [1], 0))
        calls = len(self.sweeps)
        self.now[0] += 1
        self.assertTrue(fleet.reconcile_servers()["skipped"])
        self.assertEqual(len(self.sweeps), calls)


class RefreshTests(ReconcileBase):
    def test_changed_names_of_current_servers_are_written_and_unchanged_ones_are_not(self):
        servers = [
            live(5, hostname="web-05", group__name="Group 2", environment__name="Env 2",
                 owner__user__first_name="Ann", owner__user__last_name="Lee", owner__user__username="al",
                 os_build__name="RHEL 9"),
            live(6),                                             # same as stored: no write
            live(7, owner__user__username="bob"),                # username only
            live(8, "MODIFY", hostname="resized"),
            live(9, "PROV", hostname="not-listed"),              # not current: never refreshed
            live(10),                                            # no stored attributes: nothing to compare
        ]
        attributes = {
            5: stored_names(5, group_name="Group 1"),
            6: stored_names(6),
            7: stored_names(7, owner="Old Owner"),
            8: stored_names(8),
            9: stored_names(9),
        }
        result = self.run_reconcile([5, 6, 7, 8, 9, 10], servers, attributes=attributes)
        self.assertEqual(len(self.updates), 1)
        by_id = {row["server_id"]: row for row in self.updates[0]}
        self.assertEqual(sorted(by_id), [5, 7, 8])
        self.assertEqual(by_id[5], {"server_id": 5, "hostname": "web-05", "group_name": "Group 2",
                                    "environment": "Env 2", "owner": "Ann Lee", "os_build": "RHEL 9"})
        self.assertEqual((by_id[7]["owner"], by_id[8]["hostname"]), ("bob", "resized"))
        self.assertEqual(result["refreshed"], 3)

    def test_an_owner_who_was_removed_is_written_as_empty(self):
        result = self.run_reconcile([1], [live(1)], attributes={1: stored_names(1, owner="Ann Lee", os_build="X")})
        self.assertEqual(self.updates[0][0]["owner"], "")
        self.assertEqual(self.updates[0][0]["os_build"], "")
        self.assertEqual(result["refreshed"], 1)

    def test_missing_names_compare_as_empty(self):
        servers = [live(1, group__name=None, environment__name=None, os_build__name=None)]
        attributes = {1: dict(stored_names(1), group_name=None, environment=None, owner=None, os_build=None)}
        self.run_reconcile([1], servers, attributes=attributes)
        self.assertEqual(self.updates, [])

    def test_nothing_is_written_when_the_stored_names_cannot_be_read(self):
        result = self.run_reconcile([1], [live(1, hostname="renamed")], attributes={})
        self.assertEqual(self.updates, [])
        self.assertEqual(result["refreshed"], 0)

    def test_a_failing_write_does_not_undo_the_purge(self):
        servers = [live(1, "HISTORICAL"), live(2, hostname="renamed")]
        result = self.run_reconcile([1, 2], servers, attributes={2: stored_names(2)}, update_fails=True)
        self.assertEqual(self.forgot["db"], [1])
        self.assertEqual((result["forgotten"], result["refreshed"], result["hidden"], result["skipped"]),
                         (1, 0, [], False))

    def test_nothing_is_written_when_nothing_differs(self):
        self.run_reconcile([1, 2], [live(1), live(2)], attributes={1: stored_names(1), 2: stored_names(2)})
        self.assertEqual(self.events, ["sweep"])


if __name__ == "__main__":
    unittest.main()
