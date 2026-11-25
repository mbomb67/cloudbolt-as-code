"""
Checks for the Servers view's routes (views.vuln_fleet_servers, vuln_fleet_server_filters, vuln_fleet_servers_select,
vuln_fleet_servers_patch and the shell view's URLs) without CloudBolt. The store (vuln_db), the reconcile and the
prerequisites (vuln_fleet), the action and the waiting servers (vuln_hooks), the scan state (vuln_jobs), the patch
module, the Server model and the permission check are stubbed here; the views and the helpers between them run
for real. Admin-only is tested with a stand-in for cbadmin_required that denies a non-admin the way CloudBolt's does.

    python -B -m unittest discover -s extensions/XUI-wqrj0e3p/tests -p "test_vuln_fleet_servers_views.py"
"""
import contextlib
import importlib
import os
import re
import sys
import types
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import test_vuln_fleet as base  # noqa: E402  (builds the stubs and imports the extension)

PATCH_KEY = base.PACKAGE + ".vuln_patch"
VIEW_STUBS = {}
Denied = type("PermissionDenied", (Exception,), {})


def _import_views():
    """views.py against stubs of Django, CloudBolt's extension decorators and the patch module."""
    def stub(name, **attrs):
        module = types.ModuleType(name)
        module.__dict__.update(attrs)
        VIEW_STUBS[name] = module
        return module

    def identity(*a, **k):
        if len(a) == 1 and callable(a[0]) and not k:
            return a[0]
        return lambda view: view

    def admin_only(view):
        def wrapper(request, *args, **kwargs):
            if not request.get_user_profile().is_cbadmin:
                raise Denied()
            return view(request, *args, **kwargs)
        wrapper.__name__ = view.__name__
        return wrapper

    class Q:
        def __init__(self, **kw):
            pass

        def __or__(self, other):
            return self

    def parse_restart_flag(value, default=True):
        if isinstance(value, bool):
            return value
        text = str(value).strip().lower() if value is not None else ""
        return {"1": True, "true": True, "0": False, "false": False}.get(text, default)

    stub("django.core")
    stub("django.core.exceptions", PermissionDenied=Denied)
    stub("django.core.paginator", Paginator=object)
    stub("django.db.models", Q=Q)
    stub("django.http", Http404=base.Http404, FileResponse=object, HttpResponse=object,
         JsonResponse=lambda data, status=200: types.SimpleNamespace(data=data, status=status))
    stub("django.shortcuts", render=lambda request, template, context=None: (template, context))
    stub("django.views")
    stub("django.views.decorators")
    stub("django.views.decorators.csrf", csrf_exempt=identity)
    stub("extensions")
    stub("extensions.views", admin_extension=identity, tab_extension=identity,
         TabExtensionDelegate=type("Delegate", (), {}))
    stub("resources")
    stub("resources.models", Resource=object)
    stub("utilities.middleware", login_not_required=identity)
    stub("utilities.permissions", cbadmin_required=admin_only)
    stub(PATCH_KEY, eligibility=lambda server, problems=None: "", parse_restart_flag=parse_restart_flag)
    names = set(base.STUBS) | set(VIEW_STUBS)

    def ours(key):
        return key == "xui" or key.startswith(base.PACKAGE) or key in names

    saved = {k: sys.modules[k] for k in list(sys.modules) if ours(k)}
    for key in saved:
        del sys.modules[key]
    sys.modules.update(base.STUBS)
    sys.modules.update(VIEW_STUBS)
    try:
        return importlib.import_module(base.PACKAGE + ".views")
    finally:
        for key in [k for k in sys.modules if ours(k)]:
            del sys.modules[key]
        sys.modules.update(saved)


views = _import_views()
SOURCE = os.path.join(base.EXT, "io_cloudbolt_vulnerabilities")
OK = {"enabled": True, "why": "", "action_label": "Install Updates", "restart_default": True,
      "requires_approval": False}
OFF = "Installing updates is turned off in Vulnerability Settings."
PROFILE = types.SimpleNamespace(is_cbadmin=True)
NOT_ADMIN = types.SimpleNamespace(is_cbadmin=False)
SORTS = {"hostname": "x", "high": "x", "fixable": "x"}


def reverse(name, args=None, **kwargs):
    return "/{}/{}".format(name, "".join(str(a) for a in args or []))


def server(server_id, status="ACTIVE", hostname=None):
    return types.SimpleNamespace(id=server_id, hostname=hostname or "host{}".format(server_id), status=status,
                                 get_absolute_url=lambda: "/servers/{}/".format(server_id))


def row(server_id):
    return {"server_id": server_id, "hostname": "host{}".format(server_id), "vulns": 1}


def page(ids, total=None, **extra):
    data = {"total": total if total is not None else len(ids), "page": 1, "page_size": 25, "pages": 1,
            "unavailable": False, "rows": [row(i) for i in ids]}
    data.update(extra)
    return data


class FakeManager:
    """Server.objects: records every filter call and answers with the servers asked for."""

    def __init__(self, servers):
        self.servers = servers
        self.calls = []
        self.related = []

    def filter(self, **kw):
        self.calls.append(kw)
        self.rows = [s for s in self.servers if s.id in kw["id__in"]]
        return self

    def select_related(self, *names):
        self.related.append(names)
        return self

    def prefetch_related(self, *names):
        return self

    def __iter__(self):
        return iter(self.rows)


class Job:
    def __init__(self, job_id):
        self.id = job_id

    def get_absolute_url(self):
        return "/jobs/{}/".format(job_id_of(self))


def job_id_of(job):
    return job.id


class Env:
    """Everything outside the views, replaced; each stub is an attribute so a test can change it and count calls."""

    def __init__(self, servers=(), bulk=OK, action=None, hidden=(), queued=None, eligible=None, permitted=True,
                 active=None, settings=None, max_servers=3, full_eligible=None, offered=True):
        self.servers = list(servers)
        self.manager = FakeManager(self.servers)
        self.offered = offered
        self.action = action or types.SimpleNamespace(
            run_hook_as_job=mock.Mock(return_value=[]),
            server_action_dict=mock.Mock(side_effect=lambda srv, request=None: (
                {"url": "/x/"} if (self.offered(srv) if callable(self.offered) else self.offered) else None)))
        self.bulk_mock = mock.Mock(return_value=(dict(bulk), self.action if bulk.get("enabled") else None))
        self.reconcile = mock.Mock(return_value={"hidden": list(hidden)})
        self.queued = mock.Mock(return_value=dict(queued or {}))
        self.eligible = dict(eligible or {})
        self.full_eligible = dict(full_eligible or {})
        self.quick = mock.Mock(side_effect=lambda srv, problems, unassigned, visibility: self.eligible.get(srv.id, ""))
        self.eligibility = mock.Mock(side_effect=lambda srv, problems=None: self.full_eligible.get(srv.id, ""))
        self.unassigned = mock.Mock(return_value={9})
        self.permitted = permitted
        self.permission = mock.Mock(side_effect=lambda profile, srv, action: (
            self.permitted(srv) if callable(self.permitted) else self.permitted))
        self.active = dict(active or {})
        self.states = {}
        self.read_state = mock.Mock(side_effect=lambda server_id: self.states.get(server_id, {}))
        self.active_job = mock.Mock(side_effect=lambda server_id, state=None: self.active.get(server_id))
        self.prerequisites = mock.Mock(return_value=("", ""))
        self.fleet_servers = mock.Mock(side_effect=lambda **kw: page([s.id for s in self.servers]))
        self.fleet_ids = mock.Mock(side_effect=lambda **kw: {
            "total": len(self.servers), "ids": [s.id for s in self.servers], "unavailable": False})
        self.options = mock.Mock(return_value={"group": [], "environment": [], "os_build": [], "owner": [],
                                               "has_no_owner": False, "has_no_os_build": False,
                                               "truncated": False, "unavailable": False})
        self.upgrade = mock.Mock()
        self.start_job = mock.Mock()
        self.settings = {"bulk_patch_max": max_servers, "visibility": "all"}
        self.settings.update(settings or {})
        self.profile = PROFILE
        self.stack = contextlib.ExitStack()

    def __enter__(self):
        enter = self.stack.enter_context
        perms = types.ModuleType("utilities.permissions")
        perms.server_action_permitted = self.permission
        enter(mock.patch.dict(sys.modules, {"utilities.permissions": perms}))
        enter(mock.patch.object(views, "reverse", reverse))
        enter(mock.patch.object(views, "Server", types.SimpleNamespace(objects=self.manager)))
        enter(mock.patch.object(views, "_upgrade_store", self.upgrade))
        enter(mock.patch.object(views.store, "get_settings", side_effect=lambda: dict(self.settings)))
        enter(mock.patch.object(views.vuln_db, "FLEET_SERVER_SORTS", SORTS, create=True))
        enter(mock.patch.object(views.vuln_db, "NONE_VALUE", "__none__", create=True))
        enter(mock.patch.object(views.vuln_db, "fleet_servers", self.fleet_servers, create=True))
        enter(mock.patch.object(views.vuln_db, "fleet_server_ids", self.fleet_ids, create=True))
        enter(mock.patch.object(views.vuln_db, "fleet_server_options", self.options, create=True))
        enter(mock.patch.object(views.vuln_fleet, "reconcile_servers", self.reconcile, create=True))
        enter(mock.patch.object(views.vuln_fleet, "CURRENT_STATUSES", ("ACTIVE", "MODIFY"), create=True))
        enter(mock.patch.object(views.vuln_fleet, "prerequisites", self.prerequisites))
        enter(mock.patch.object(views.vuln_fleet, "default_base_url", return_value="https://cb.example"))
        enter(mock.patch.object(views.vuln_hooks, "patch_bulk_action", self.bulk_mock, create=True))
        enter(mock.patch.object(views.vuln_hooks, "queued_update_servers", self.queued, create=True))
        enter(mock.patch.object(views.vuln_jobs, "active_job", self.active_job))
        enter(mock.patch.object(views.store, "read_state", self.read_state))
        enter(mock.patch.object(views.vuln_fleet, "quick_eligibility", self.quick, create=True))
        enter(mock.patch.object(views.vuln_fleet, "unassigned_ids", self.unassigned, create=True))
        enter(mock.patch.object(views.vuln_patch, "eligibility", self.eligibility, create=True))
        enter(mock.patch.object(views.vuln_patch, "start_job", self.start_job, create=True))
        return self

    def __exit__(self, *exc):
        return self.stack.__exit__(*exc)

    def call(self, view, method="GET", query=None, post=None, profile=None):
        profile = profile or self.profile
        request = types.SimpleNamespace(method=method, GET=query or {}, POST=post or {}, path="/x/",
                                        get_user_profile=lambda: profile)
        return view(request)


def marker(job_id, kind=None):
    job = types.SimpleNamespace(id=job_id, get_absolute_url=lambda: "/jobs/{}/".format(job_id))
    return ({"kind": kind} if kind else {}, job)


# ---------------------------------------------------------------- the list
class ServersListTests(unittest.TestCase):
    def test_the_query_string_reaches_the_store_exactly(self):
        with Env([server(1)], hidden=[5, 6]) as env:
            env.call(views.vuln_fleet_servers, query={
                "q": "  web  ", "kev": "1", "fix": "1", "group": "Team A", "environment": "Env B",
                "os_build": "__none__", "owner": "alice", "sort": "high", "dir": "desc", "page": "3",
                "page_size": "999"})
        env.fleet_servers.assert_called_once_with(
            search="web", kev_only=True, fix_only=True, group="Team A", environment="Env B", os_build="__none__",
            owner="alice", sort="high", direction="desc", page=3, page_size=25, hide_ids=[5, 6])

    def test_defaults_and_hostile_values(self):
        with Env([server(1)]) as env:
            env.call(views.vuln_fleet_servers, query={"sort": "s.kev; DROP TABLE servers", "dir": "sideways",
                                                      "group": "x" * 500, "q": "y" * 500})
            env.call(views.vuln_fleet_servers, query={"dir": "desc"})
            env.call(views.vuln_fleet_servers, query={"sort": "fixable", "dir": "ASC"})
        first, second, third = (c.kwargs for c in env.fleet_servers.call_args_list)
        self.assertEqual((first["sort"], first["direction"]), ("", "asc"))
        self.assertEqual(len(first["group"]), 200)
        self.assertEqual(len(first["search"]), 100)
        self.assertEqual((first["kev_only"], first["fix_only"], first["page"]), (False, False, 1))
        self.assertEqual((second["sort"], second["direction"]), ("", "asc"))
        self.assertEqual((third["sort"], third["direction"]), ("fixable", "asc"))

    def test_only_get_and_the_store_is_upgraded_then_reconciled(self):
        with Env([server(1)]) as env:
            self.assertEqual(env.call(views.vuln_fleet_servers, method="POST").status, 405)
            env.fleet_servers.assert_not_called()
            env.call(views.vuln_fleet_servers)
        env.upgrade.assert_called_once_with()
        env.reconcile.assert_called_once_with(force=False)

    def test_rows_get_their_url_and_the_bulk_object(self):
        with Env([server(1), server(2)]) as env:
            data = env.call(views.vuln_fleet_servers).data
        self.assertEqual([r["url"] for r in data["rows"]],
                         ["/servers/1/" + views.SERVER_TAB_HASH, "/servers/2/" + views.SERVER_TAB_HASH])
        self.assertEqual(data["bulk"], {
            "enabled": True, "why": "", "max": 3, "action_label": "Install Updates", "restart_default": True,
            "requires_approval": False, "run_url": "/vuln_fleet_servers_patch/", "select_url": "/vuln_fleet_servers_select/"})
        self.assertNotIn("dialog_url", data["bulk"])
        self.assertNotIn("action_id", data["bulk"])

    def test_costs_are_bounded(self):
        with Env([server(i) for i in range(1, 6)]) as env:
            env.call(views.vuln_fleet_servers)
        self.assertEqual(len(env.manager.calls), 1)
        self.assertEqual(env.bulk_mock.call_count, 1)
        self.assertEqual(env.prerequisites.call_count, 1)
        self.assertEqual(env.queued.call_count, 1)
        self.assertEqual(env.active_job.call_count, 5)
        self.assertEqual(env.reconcile.call_count, 1)
        self.assertEqual(env.fleet_servers.call_count, 1)
        self.assertEqual(env.manager.related,
                         [("group", "os_family__parent__parent", "resource_handler", "owner__user")])
        self.assertEqual(env.unassigned.call_count, 1)
        self.assertEqual(env.quick.call_count, 5)
        env.eligibility.assert_not_called()
        env.action.server_action_dict.assert_not_called()

    def test_each_reason_switches_bulk_off_and_rows_carry_no_patch(self):
        reasons = (OFF, "The Install Updates action does not exist yet. Turn on installing updates in "
                        "Vulnerability Settings.", "The Install Updates action is switched off.",
                   "The Install Updates action does not allow bulk operation. A CloudBolt admin can allow it on "
                   "the action.", "The Install Updates action cannot be run on several servers at once as it is "
                   "set up now.", "Install updates could not be checked. The CloudBolt application log has the "
                   "details.")
        for why in reasons:
            with Env([server(1)], bulk=dict(OK, enabled=False, why=why)) as env:
                data = env.call(views.vuln_fleet_servers).data
            self.assertEqual((data["bulk"]["enabled"], data["bulk"]["why"]), (False, why))
            self.assertEqual(data["bulk"]["max"], 3)
            self.assertNotIn("patch", data["rows"][0])
            self.assertIn("url", data["rows"][0])
            env.prerequisites.assert_not_called()
            env.queued.assert_not_called()
        self.assertIn("Vulnerability Settings", OFF)

    def test_row_states(self):
        servers = [server(i) for i in range(1, 10)]
        with Env(servers, eligible={3: "powered_off", 4: "unassigned", 5: "unsupported_os"},
                 permitted=lambda s: s.id != 6, queued={7: 55},
                 active={2: marker(41, "patch"), 8: marker(42)}) as env:
            data = env.call(views.vuln_fleet_servers).data
        patches = {r["server_id"]: r["patch"] for r in data["rows"]}
        self.assertEqual(patches[1], {"selectable": True, "why": "", "running": None})
        self.assertEqual(patches[2], {
            "selectable": False, "why": "A scan or update is already running on this server (job 41).",
            "running": {"job_id": 41, "job_url": "/jobs/41/", "kind": "patch", "queued": False}})
        self.assertEqual(patches[8]["running"], {"job_id": 42, "job_url": "/jobs/42/", "kind": "scan",
                                                 "queued": False})
        self.assertEqual(patches[7], {
            "selectable": False, "why": "Waiting in update job 55.",
            "running": {"job_id": 55, "job_url": "/job_detail/55", "kind": "patch", "queued": True}})
        self.assertEqual(patches[3]["why"], "Cannot install updates: powered off.")
        self.assertEqual(patches[4]["why"], "Cannot install updates: in an Unassigned group.")
        self.assertEqual(patches[5]["why"], "Cannot install updates: not Linux or Windows.")
        self.assertEqual(patches[6], {"selectable": False,
                                      "why": "You do not have permission to install updates on this server.",
                                      "running": None})
        for server_id in (3, 4, 5, 6, 9):
            self.assertIsNone(patches[server_id]["running"])
        self.assertTrue(patches[9]["selectable"])

    def test_an_active_marker_beats_the_queue_and_a_running_server_is_not_asked_further(self):
        with Env([server(1)], queued={1: 9}, active={1: marker(41, "patch")}, eligible={1: "powered_off"}) as env:
            patch = env.call(views.vuln_fleet_servers).data["rows"][0]["patch"]
        self.assertFalse(patch["running"]["queued"])
        env.quick.assert_not_called()

    def test_a_server_the_running_job_has_finished_is_selectable_again(self):
        with Env([server(1), server(2), server(3), server(4)], queued={1: 50, 2: 50, 3: 50, 4: 50}) as env:
            env.states[1] = {"patch": {"job_id": 50, "status": "updated"}}      # finished in this job
            env.states[3] = {"patch": {"job_id": 49, "status": "updated"}}      # an older job's record
            env.states[4] = {"patch": "unreadable"}                             # not a record
            patches = {r["server_id"]: r["patch"] for r in env.call(views.vuln_fleet_servers).data["rows"]}
        self.assertEqual(patches[1], {"selectable": True, "why": "", "running": None})
        self.assertEqual(patches[2]["why"], "Waiting in update job 50.")      # no record yet: still queued
        self.assertTrue(patches[2]["running"]["queued"])
        self.assertTrue(patches[3]["running"]["queued"])
        self.assertTrue(patches[4]["running"]["queued"])
        self.assertEqual(env.read_state.call_count, 4)

    def test_an_unreadable_state_keeps_the_server_queued(self):
        with Env([server(1)], queued={1: 50}) as env:
            env.read_state.side_effect = RuntimeError("state")
            patch = env.call(views.vuln_fleet_servers).data["rows"][0]["patch"]
        self.assertTrue(patch["running"]["queued"])

    def test_a_finished_server_of_a_running_job_can_be_submitted_again(self):
        job = types.SimpleNamespace(id=77, get_absolute_url=lambda: "/jobs/77/")
        with Env([server(1)], queued={1: 50}) as env:
            env.states[1] = {"patch": {"job_id": 50}}
            env.action.run_hook_as_job.return_value = [job]
            response = env.call(views.vuln_fleet_servers_patch, method="POST", post={"ids": "1"})
        self.assertEqual(response.status, 200)

    def test_a_missing_server_is_not_selectable_and_has_no_url(self):
        with Env([server(1)]) as env:
            env.fleet_servers.side_effect = lambda **kw: page([1, 2])
            env.reconcile.return_value = {"hidden": []}
            data = env.call(views.vuln_fleet_servers).data
        gone = data["rows"][1]
        self.assertNotIn("url", gone)
        self.assertEqual(gone["patch"], {"selectable": False, "why": "This server no longer exists.",
                                         "running": None})

    def test_an_unreadable_state_file_or_check_does_not_break_the_page(self):
        with Env([server(1), server(2)]) as env:
            env.active_job.side_effect = RuntimeError("state")
            data = env.call(views.vuln_fleet_servers).data
            self.assertTrue(data["rows"][0]["patch"]["selectable"])
            env.permission.side_effect = RuntimeError("perm")
            data = env.call(views.vuln_fleet_servers).data
        self.assertEqual(data["rows"][0]["patch"]["why"], "Cannot install updates: could not be checked.")

    def test_failing_prerequisites_switch_bulk_off_but_return_the_page(self):
        with Env([server(1)]) as env:
            env.prerequisites.side_effect = RuntimeError("tools")
            data = env.call(views.vuln_fleet_servers).data
        self.assertFalse(data["bulk"]["enabled"])
        self.assertIn("could not be checked", data["bulk"]["why"])
        self.assertEqual(len(data["rows"]), 1)

    def test_a_failing_server_query_switches_bulk_off(self):
        with Env([server(1)]) as env:
            env.manager.filter = mock.Mock(side_effect=RuntimeError("db"))
            data = env.call(views.vuln_fleet_servers).data
        self.assertFalse(data["bulk"]["enabled"])
        self.assertNotIn("url", data["rows"][0])

    def test_a_historical_row_forces_one_reconcile_and_one_new_read(self):
        with Env([server(1), server(2, status="HISTORICAL")]) as env:
            env.reconcile.side_effect = [{"hidden": []}, {"hidden": [2]}]

            def read(**kw):
                return page([1]) if 2 in kw["hide_ids"] else page([1, 2])

            env.fleet_servers.side_effect = read
            data = env.call(views.vuln_fleet_servers).data
        self.assertEqual([c.kwargs for c in env.reconcile.call_args_list], [{"force": False}, {"force": True}])
        self.assertEqual(env.fleet_servers.call_count, 2)
        self.assertEqual(env.fleet_servers.call_args_list[1].kwargs["hide_ids"], [2])
        self.assertEqual([r["server_id"] for r in data["rows"]], [1])
        self.assertEqual(len(env.manager.calls), 2)

    def test_a_missing_server_row_also_forces_one_reconcile_and_no_loop(self):
        with Env([server(1)]) as env:
            env.fleet_servers.side_effect = lambda **kw: page([1, 2])
            env.call(views.vuln_fleet_servers)
        self.assertEqual(env.reconcile.call_count, 2)
        self.assertEqual(env.fleet_servers.call_count, 2)

    def test_modifying_servers_are_current(self):
        with Env([server(1, status="MODIFY")]) as env:
            env.call(views.vuln_fleet_servers)
        self.assertEqual(env.reconcile.call_count, 1)

    def test_a_failing_reconcile_still_returns_the_page(self):
        with Env([server(1)]) as env:
            env.reconcile.side_effect = RuntimeError("orm")
            data = env.call(views.vuln_fleet_servers).data
        self.assertEqual(len(data["rows"]), 1)
        self.assertEqual(env.fleet_servers.call_args.kwargs["hide_ids"], [])

    def test_reconcile_helper_never_raises_and_parses_hidden(self):
        with Env() as env:
            env.reconcile.return_value = {"hidden": ["3", 4, "x", None]}
            self.assertEqual(views._reconcile(), [3, 4])
            env.reconcile.return_value = None
            self.assertEqual(views._reconcile(), [])
            env.reconcile.side_effect = RuntimeError("x")
            self.assertEqual(views._reconcile(force=True), [])

    def test_the_other_fleet_routes_reconcile_first(self):
        with Env() as env:
            with mock.patch.object(views, "_fleet_summary_state", return_value=({"servers": 1}, "ready")), \
                    mock.patch.object(views.vuln_tools, "status", return_value={}), \
                    mock.patch.object(views.vuln_db, "fleet_cves", return_value=page([])), \
                    mock.patch.object(views.vuln_db, "cve_servers", return_value=page([])), \
                    mock.patch.object(views.vuln_db, "fleet_cves", return_value=page([])):
                env.call(views.vuln_fleet_summary)
                env.call(views.vuln_fleet_cves)
                env.call(views.vuln_fleet_cve_servers, query={"cve": "CVE-2024-0001"})
        self.assertEqual(env.reconcile.call_count, 3)
        self.assertEqual(env.upgrade.call_count, 3)


# ---------------------------------------------------------------- filters
class FiltersTests(unittest.TestCase):
    def test_shape_and_none_entries(self):
        with Env(hidden=[4]) as env:
            env.options.return_value = {
                "group": ["A", "b"], "environment": ["E"], "os_build": ["RHEL 9"], "owner": ["Alice"],
                "has_no_owner": True, "has_no_os_build": True, "truncated": True, "unavailable": False}
            data = env.call(views.vuln_fleet_server_filters).data
        env.options.assert_called_once_with(hide_ids=[4])
        self.assertEqual(data, {
            "state": "ready", "group": ["A", "b"], "environment": ["E"],
            "os_build": [{"value": "RHEL 9", "label": "RHEL 9"}, {"value": "__none__", "label": "No OS build"}],
            "owner": [{"value": "Alice", "label": "Alice"}, {"value": "__none__", "label": "No owner"}],
            "truncated": True})
        env.upgrade.assert_called_once_with()

    def test_states(self):
        with Env() as env:
            self.assertEqual(env.call(views.vuln_fleet_server_filters).data["state"], "empty")
            env.options.return_value = {"unavailable": True}
            self.assertEqual(env.call(views.vuln_fleet_server_filters).data["state"], "unavailable")
            self.assertEqual(env.call(views.vuln_fleet_server_filters, method="POST").status, 405)


# ---------------------------------------------------------------- select all
class SelectTests(unittest.TestCase):
    def select(self, servers, total=None, max_servers=3, **env_kwargs):
        with Env(servers, max_servers=max_servers, **env_kwargs) as env:
            if total is not None:
                env.fleet_ids.side_effect = lambda **kw: {"total": total, "ids": [s.id for s in servers],
                                                           "unavailable": False}
            data = env.call(views.vuln_fleet_servers_select, query={"q": "web", "sort": "high", "dir": "desc"})
        return env, data

    def test_everything_fits(self):
        env, response = self.select([server(1), server(2), server(3)])
        self.assertEqual(response.status, 200)
        self.assertEqual(response.data, {
            "total": 3, "max": 3, "ids": [1, 2, 3], "hostnames": {"1": "host1", "2": "host2", "3": "host3"},
            "scanned": 3, "skipped": [], "skipped_count": 0, "complete": True})
        call = env.fleet_ids.call_args.kwargs
        self.assertEqual((call["search"], call["sort"], call["direction"], call["limit"]), ("web", "high", "desc", 12))

    def test_more_than_the_cap_takes_the_first_ones_in_order_and_is_not_complete(self):
        env, response = self.select([server(i) for i in (9, 4, 7, 1, 2)])
        self.assertEqual(response.data["ids"], [9, 4, 7])
        self.assertFalse(response.data["complete"])
        self.assertEqual((response.data["total"], response.data["scanned"]), (5, 3))

    def test_the_skipped_list_is_capped_at_twenty_but_counted_in_full(self):
        servers = [server(i) for i in range(1, 36)]
        env, response = self.select(servers, max_servers=100, eligible={i: "powered_off" for i in range(1, 31)})
        self.assertEqual(len(response.data["skipped"]), 20)
        self.assertEqual(response.data["skipped"][0], {"id": 1, "hostname": "host1", "why": "Cannot install updates: powered off."})
        self.assertEqual((response.data["skipped_count"], len(response.data["ids"])), (30, 5))
        self.assertTrue(response.data["complete"])
        self.assertEqual(env.fleet_ids.call_args.kwargs["limit"], 400)

    def test_scanned_is_bounded_and_a_longer_match_list_is_not_complete(self):
        servers = [server(i) for i in range(1, 13)]
        env, response = self.select(servers, total=20, eligible={s.id: "powered_off" for s in servers})
        self.assertEqual(env.fleet_ids.call_args.kwargs["limit"], 12)
        self.assertEqual((response.data["scanned"], response.data["ids"], response.data["complete"]),
                         (12, [], False))

    def test_a_server_missing_from_the_orm_is_skipped_with_its_id(self):
        with Env([server(1)]) as env:
            env.fleet_ids.side_effect = lambda **kw: {"total": 2, "ids": [1, 2], "unavailable": False}
            data = env.call(views.vuln_fleet_servers_select).data
        self.assertEqual(data["skipped"], [{"id": 2, "hostname": "Server 2", "why": "This server no longer exists."}])

    def test_bulk_off_is_a_409_with_the_reason(self):
        with Env([server(1)], bulk=dict(OK, enabled=False, why=OFF)) as env:
            response = env.call(views.vuln_fleet_servers_select)
        self.assertEqual((response.status, response.data), (409, {"error": OFF}))
        env.fleet_ids.assert_not_called()

    def test_hidden_ids_and_one_query(self):
        env, _ = self.select([server(1)], hidden=[8])
        self.assertEqual(env.fleet_ids.call_args.kwargs["hide_ids"], [8])
        self.assertEqual(len(env.manager.calls), 1)
        self.assertEqual(env.prerequisites.call_count, 1)

    def test_only_get(self):
        with Env() as env:
            self.assertEqual(env.call(views.vuln_fleet_servers_select, method="POST").status, 405)


# ---------------------------------------------------------------- the run
class PatchRouteTests(unittest.TestCase):
    def post(self, env, ids="1,2", restart=None):
        data = {"ids": ids}
        if restart is not None:
            data["restart"] = restart
        return env.call(views.vuln_fleet_servers_patch, method="POST", post=data)

    def test_a_job(self):
        job = types.SimpleNamespace(id=77, get_absolute_url=lambda: "/jobs/77/")
        servers = [server(1), server(2)]
        with Env(servers) as env:
            env.action.run_hook_as_job.return_value = [job]
            response = self.post(env, "2,1", "1")
        self.assertEqual((response.status, response.data), (
            200, {"state": "started", "job_id": 77, "job_url": "/jobs/77/", "servers": 2}))
        call = env.action.run_hook_as_job.call_args.kwargs
        self.assertIs(call["owner"], PROFILE)
        self.assertEqual([s.id for s in call["servers"]], [2, 1])
        self.assertIsNone(call["scheduled_time"])
        self.assertEqual(call["job_display_label"], "Install updates: 2 servers")
        context = call["context"]
        self.assertIs(context["restart_if_needed"], True)
        self.assertIs(context["bulk_changes"], True)
        self.assertEqual(context["__untrusted_expressions"], ["restart_if_needed"])
        self.assertEqual(context["__sensitive_keys"], [])
        self.assertEqual(env.action.run_hook_as_job.call_count, 1)
        env.start_job.assert_not_called()

    def test_the_restart_flag(self):
        job = types.SimpleNamespace(id=1, get_absolute_url=lambda: "/jobs/1/")
        for value, default, expect in (("0", True, False), ("1", False, True), (None, False, False),
                                       (None, True, True), ("maybe", False, False)):
            with Env([server(1)], bulk=dict(OK, restart_default=default)) as env:
                env.action.run_hook_as_job.return_value = [job]
                self.post(env, "1", value)
            self.assertIs(env.action.run_hook_as_job.call_args.kwargs["context"]["restart_if_needed"], expect,
                          (value, default))

    def test_one_server_label_is_its_hostname(self):
        job = types.SimpleNamespace(id=1, get_absolute_url=lambda: "/jobs/1/")
        with Env([server(1)]) as env:
            env.action.run_hook_as_job.return_value = [job]
            self.post(env, "1")
        self.assertEqual(env.action.run_hook_as_job.call_args.kwargs["job_display_label"], "Install updates: host1")

    def test_an_order_that_was_submitted(self):
        for status, approved in (("PENDING", False), ("ACTIVE", True)):
            order = types.SimpleNamespace(id=5, status=status)
            with Env([server(1), server(2)]) as env:
                env.action.run_hook_as_job.return_value = (order, "message")
                response = self.post(env)
            self.assertEqual((response.status, response.data), (200, {
                "state": "approval", "order_id": 5, "order_url": "/order_detail/5", "servers": 2,
                "submitted": True, "approved": approved}))

    def test_an_order_item_in_the_current_order(self):
        item = types.SimpleNamespace(order=types.SimpleNamespace(id=6, status="CART"))
        with Env([server(1), server(2)]) as env:
            env.action.run_hook_as_job.return_value = item
            response = self.post(env)
        self.assertEqual(response.data, {"state": "approval", "order_id": 6, "order_url": "/current_order/",
                                         "servers": 2, "submitted": False})

    def test_nothing_started_or_an_exception_is_a_500_with_one_text(self):
        text = "Install updates could not be started. The CloudBolt application log has the details."
        for result in ([], None, "x", (), [types.SimpleNamespace(id="a")]):
            with Env([server(1)]) as env:
                env.action.run_hook_as_job.return_value = result
                response = self.post(env, "1")
            self.assertEqual((response.status, response.data), (500, {"error": text}), result)
        with Env([server(1)]) as env:
            env.action.run_hook_as_job.side_effect = RuntimeError("quota")
            response = self.post(env, "1")
        self.assertEqual((response.status, response.data), (500, {"error": text}))
        with Env([server(1)]) as env:
            env.prerequisites.side_effect = RuntimeError("tools")
            self.assertEqual(self.post(env, "1").status, 500)
        env.action.run_hook_as_job.assert_not_called()

    def test_bulk_off_comes_first_even_with_bad_ids(self):
        with Env([server(1)], bulk=dict(OK, enabled=False, why=OFF)) as env:
            response = self.post(env, "nonsense")
        self.assertEqual((response.status, response.data), (409, {"error": OFF}))
        self.assertEqual(env.manager.calls, [])

    def test_id_parsing_limits(self):
        bad = ("", " ", "0", "-1", "+1", "a", "1,,2", "1,", ",1", "1.5", "1 2", "١", "1,2,3,4", "9" * 2001,
               "1," * 1200)
        for text in bad:
            with Env([server(i) for i in range(1, 5)]) as env:
                response = self.post(env, text)
            self.assertEqual((response.status, response.data), (400, {"error": "Select between 1 and 3 servers."}),
                             text[:20])
            self.assertEqual(env.manager.calls, [])
            env.action.run_hook_as_job.assert_not_called()
        with Env([server(1), server(2)]) as env:
            env.action.run_hook_as_job.return_value = [types.SimpleNamespace(id=1, get_absolute_url=lambda: "/j")]
            self.post(env, " 1, 2,2,1 ")
        self.assertEqual([s.id for s in env.action.run_hook_as_job.call_args.kwargs["servers"]], [1, 2])
        with Env([server(1)], max_servers=1) as env:
            self.assertEqual(self.post(env, "1,2").data, {"error": "Select between 1 and 1 servers."})

    def test_refused_servers_start_nothing(self):
        with Env([server(1), server(3), server(4)], eligible={1: "powered_off"}, queued={3: 12}, max_servers=5) as env:
            response = self.post(env, "1,2,3,4")
        self.assertEqual(response.status, 409)
        self.assertEqual(response.data["error"], "3 of the selected servers can no longer be updated.")
        self.assertEqual(response.data["refused"], [
            {"id": 1, "hostname": "host1", "why": "Cannot install updates: powered off."},
            {"id": 2, "hostname": "Server 2", "why": "This server no longer exists."},
            {"id": 3, "hostname": "host3", "why": "Waiting in update job 12."}])
        env.action.run_hook_as_job.assert_not_called()

    def test_a_busy_or_unpermitted_server_is_refused(self):
        with Env([server(1), server(2)], active={1: marker(8)}, permitted=lambda s: s.id != 2) as env:
            response = self.post(env)
        self.assertEqual([r["why"] for r in response.data["refused"]], [
            "A scan or update is already running on this server (job 8).",
            "You do not have permission to install updates on this server."])

    def test_the_run_costs_one_server_query_and_one_prerequisite_read(self):
        with Env([server(1), server(2)]) as env:
            env.action.run_hook_as_job.return_value = [types.SimpleNamespace(id=1, get_absolute_url=lambda: "/j")]
            self.post(env)
        self.assertEqual(len(env.manager.calls), 1)
        self.assertEqual(env.prerequisites.call_count, 1)
        self.assertEqual(env.queued.call_count, 1)

    def test_only_post(self):
        with Env([server(1)]) as env:
            self.assertEqual(env.call(views.vuln_fleet_servers_patch, method="GET").status, 405)
            env.bulk_mock.assert_not_called()

    def test_csrf_is_the_default_check_like_the_scan_route(self):
        with open(os.path.join(SOURCE, "views.py"), encoding="ascii", newline="") as handle:
            text = handle.read().replace("\r\n", "\n")
        exempt = re.findall(r"@csrf_exempt\ndef (\w+)", text)
        self.assertEqual(exempt, ["vuln_upload", "vuln_tool"])


class FullChecksTests(unittest.TestCase):
    def post(self, env, ids="1,2"):
        return env.call(views.vuln_fleet_servers_patch, method="POST", post={"ids": ids})

    def test_the_whole_eligibility_runs_only_at_submit_and_failures_are_refused(self):
        with Env([server(1), server(2)], full_eligible={2: "no_script_channel"}) as env:
            response = self.post(env)
        self.assertEqual(response.status, 409)
        self.assertEqual(response.data["refused"], [
            {"id": 2, "hostname": "host2", "why": "Cannot install updates: no way to run a script."}])
        self.assertEqual(env.eligibility.call_count, 2)
        env.action.run_hook_as_job.assert_not_called()

    def test_an_action_not_offered_for_a_server_is_refused(self):
        with Env([server(1), server(2)], offered=lambda s: s.id != 1) as env:
            response = self.post(env)
        self.assertEqual(response.data["refused"], [
            {"id": 1, "hostname": "host1", "why": "The Install Updates action is not offered for this server."}])
        self.assertEqual(env.action.server_action_dict.call_count, 2)
        env.action.run_hook_as_job.assert_not_called()

    def test_the_list_and_select_never_run_the_costly_checks(self):
        with Env([server(1), server(2)]) as env:
            env.call(views.vuln_fleet_servers)
            env.call(views.vuln_fleet_servers_select)
        env.eligibility.assert_not_called()
        env.action.server_action_dict.assert_not_called()

    def test_the_bulk_object_carries_requires_approval(self):
        with Env([server(1)], bulk=dict(OK, requires_approval=True)) as env:
            data = env.call(views.vuln_fleet_servers).data
        self.assertIs(data["bulk"]["requires_approval"], True)
        self.assertEqual(sorted(data["bulk"]), sorted([
            "enabled", "why", "max", "action_label", "restart_default", "requires_approval", "run_url",
            "select_url"]))


class Strict:
    """A server that raises when anything but the named attributes is read: proof that a check does not
    touch the database through any other relation."""

    def __init__(self, **allowed):
        self.__dict__["_allowed"] = allowed

    def __getattr__(self, name):
        allowed = self.__dict__["_allowed"]
        if name in allowed:
            return allowed[name]
        raise AssertionError("read " + name)


class QuickEligibilityTests(unittest.TestCase):
    def server(self, **over):
        family = types.SimpleNamespace(get_base_name=lambda: "Linux")
        data = dict(id=1, status="ACTIVE", group_id=5, power_status="POWERON", os_family=family,
                    tags=types.SimpleNamespace(all=lambda: []),
                    is_windows=lambda: bool(data["os_family"] and data["os_family"].get_base_name() == "Windows"))
        data.update(over)
        return Strict(**data)

    def check(self, server, problems=("", ""), unassigned=frozenset({9}), visibility="all"):
        return views.vuln_fleet.quick_eligibility(server, problems, unassigned, visibility)

    def test_a_good_server_reads_nothing_else(self):
        self.assertEqual(self.check(self.server()), "")

    def test_each_reason_in_the_order_of_the_full_check(self):
        self.assertEqual(self.check(self.server(os_family=None)), "unsupported_os")
        self.assertEqual(self.check(self.server(status="HISTORICAL")), "historical")
        self.assertEqual(self.check(self.server(status="MODIFY")), "not_active")
        self.assertEqual(self.check(self.server(group_id=9)), "unassigned")
        self.assertEqual(self.check(self.server(power_status="POWEROFF")), "powered_off")
        self.assertEqual(self.check(self.server(), problems=("not_set_up", "")), "not_set_up")
        self.assertEqual(self.check(self.server(), visibility="label"), "hidden")
        self.assertEqual(self.check(self.server(group_id=None)), "")

    def test_it_is_never_stricter_than_the_full_check_on_what_it_checks(self):
        # A server the quick check passes may still fail the full one; the reverse must not happen for the
        # same inputs: a server in no Unassigned group, active, on, supported is passed.
        self.assertEqual(self.check(self.server(group_id=5, power_status="POWERON")), "")


# ---------------------------------------------------------------- admin only and the shell
class AdminOnlyTests(unittest.TestCase):
    def test_a_non_admin_gets_403_on_every_route(self):
        for view, method in ((views.vuln_fleet_servers, "GET"), (views.vuln_fleet_server_filters, "GET"),
                             (views.vuln_fleet_servers_select, "GET"), (views.vuln_fleet_servers_patch, "POST")):
            with Env([server(1)]) as env:
                response = env.call(view, method=method, post={"ids": "1"}, profile=NOT_ADMIN)
            self.assertEqual(response.status, 403, view.__name__)
            self.assertEqual(response.data, {"error": "You do not have permission to do this."})
            env.fleet_servers.assert_not_called()
            env.action.run_hook_as_job.assert_not_called()
            env.reconcile.assert_not_called()
            env.bulk_mock.assert_not_called()

    def test_an_unexpected_error_is_json_without_a_traceback(self):
        with Env([server(1)]) as env:
            env.fleet_servers.side_effect = RuntimeError("secret detail")
            response = env.call(views.vuln_fleet_servers)
        self.assertEqual(response.status, 500)
        self.assertNotIn("secret", str(response.data))


class ShellTests(unittest.TestCase):
    def test_the_shell_gets_the_urls_of_the_new_routes(self):
        request = types.SimpleNamespace(method="GET", GET={}, get_user_profile=lambda: PROFILE)
        with mock.patch.object(views, "reverse", reverse), \
                mock.patch.object(views, "_settings_url", return_value="/settings/"):
            template, context = views.vulnerabilities(request)
        self.assertTrue(template.endswith("vuln_fleet.html"))
        self.assertEqual(context["urls"]["server_filters"], "/vuln_fleet_server_filters/")
        self.assertEqual(context["urls"]["servers_select"], "/vuln_fleet_servers_select/")
        self.assertEqual(context["urls"]["servers_patch"], "/vuln_fleet_servers_patch/")
        self.assertEqual(context["urls"]["servers"], "/vuln_fleet_servers/")
        self.assertEqual(context["page_error"], "")

    def test_the_urls_file_names_the_three_routes(self):
        with open(os.path.join(SOURCE, "urls.py"), encoding="ascii") as handle:
            text = handle.read()
        for path, name in (("vulnerabilities/fleet/server-filters/", "vuln_fleet_server_filters"),
                           ("vulnerabilities/fleet/servers/select/", "vuln_fleet_servers_select"),
                           ("vulnerabilities/fleet/servers/patch/", "vuln_fleet_servers_patch")):
            self.assertIn('"{}"'.format(path), text)
            self.assertIn('views.{},'.format(name), text)
            self.assertIn('name="{}"'.format(name), text)

    def test_no_double_braces_in_the_files_of_this_part(self):
        for name in ("views.py", "urls.py", "vuln_hooks.py", "vuln_store.py"):
            with open(os.path.join(SOURCE, name), encoding="ascii") as handle:
                self.assertNotIn("{{", handle.read(), name)


if __name__ == "__main__":
    unittest.main()
