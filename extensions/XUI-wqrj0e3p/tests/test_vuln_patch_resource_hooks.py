"""
Checks for the Install Updates on All Servers wiring of the Vulnerabilities XUI (vuln_hooks.py
apply_patching_setting, _apply_resource_action, resource_patch_button, resource_patch_action_state,
server_patch_permitted; vuln_api.install_updates_on_resource) without CloudBolt. It reuses the stubs and the
in-memory ORM of test_vuln_patch_hooks.py and adds a fake ResourceAction.

    python -B -m unittest extensions/XUI-wqrj0e3p/tests/test_vuln_patch_resource_hooks.py
"""
import contextlib
import importlib.util
import itertools
import os
import sys
import types
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import test_vuln_patch_hooks as base  # noqa: E402

hooks, store, PACKAGE, SOURCE = base.hooks, base.store, base.PACKAGE, base.SOURCE
_module = base._module


class World(base.World):
    """The fake database of test_vuln_patch_hooks.py plus ResourceAction."""

    def __init__(self, make_token_input=True):
        super().__init__(make_token_input)
        self.resource_actions = []
        self.resource_permitted = True
        self.listed = []
        world = self

        class RAction:
            def __init__(self, **kw):
                self.requires_approval = False
                self.auto_submit_order = False
                self.roles = []
                self.blueprints = []
                self.list_view_visible = True
                self.pk = next(world.ids)
                self.mapping_rows = []
                self.input_mappings = base.Manager(self.mapping_rows)
                self.__dict__.update(kw)

            def update_input_mappings(self):
                world.calls.append(("raction.update_input_mappings",))
                for row in self.hook.input_rows:
                    if not self.input_mappings.filter(hook_input=row):
                        self.mapping_rows.append(_Mapping(row, world))

            def save(self):
                world.calls.append(("raction.save", self.enabled))

        class ResourceAction:
            objects = types.SimpleNamespace(
                filter=lambda **kw: base.Rows(a for a in world.resource_actions
                                              if all(getattr(a, k) == v for k, v in kw.items())))

        def create_action(**kw):
            row = RAction(**kw)
            world.resource_actions.append(row)
            world.calls.append(("raction.create", dict(kw)))
            return row

        ResourceAction.objects.create = create_action
        self.ResourceAction = ResourceAction

    @contextlib.contextmanager
    def installed(self):
        cb = _module("cbhooks.models", CloudBoltHook=self.Hook, HookInput=self.HookInput,
                     ServerAction=self.ServerAction, ResourceAction=self.ResourceAction)
        orders = _module("orders.models", CustomFieldValue=self.CustomFieldValue)
        perms = _module("utilities.permissions",
                        server_action_permitted=lambda profile, server, action: self.permitted,
                        resource_action_permitted=lambda profile, resource, action: self.resource_permitted)
        package = _module(PACKAGE, vuln_store=store)
        patches = {"cbhooks.models": cb, "orders.models": orders, "utilities.permissions": perms,
                   PACKAGE: package, PACKAGE + ".vuln_store": store}
        with mock.patch.dict(sys.modules, patches), mock.patch.object(hooks, "CloudBoltHook", self.Hook):
            yield self


class _Mapping:
    def __init__(self, hook_input, world):
        self.hook_input = hook_input
        self.default_value = None
        self.world = world

    def save(self):
        self.world.calls.append(("mapping.save", self.default_value.value if self.default_value else None))


def resource(actions=None):
    """A fake resource whose available_actions returns what the test set."""
    holder = types.SimpleNamespace(id=5, name="web tier", group=object(), owner=None)
    holder.available_actions = lambda request=None: list(holder.listed)
    holder.listed = actions or []
    return holder


class ApplyResourceActionTests(unittest.TestCase):
    def test_off_with_no_hook_is_a_no_op(self):
        world = World()
        with world.installed():
            hooks.apply_patching_setting(False)
        self.assertEqual(world.calls, [])

    def test_first_enable_makes_the_resource_half_once(self):
        world = World()
        with world.installed():
            hooks.apply_patching_setting(True)
        names = [h.name for h in world.hooks]
        self.assertEqual(names, [hooks.PATCH_NAME, hooks.CONDITION_NAME, hooks.RESOURCE_PATCH_NAME,
                                 hooks.RESOURCE_CONDITION_NAME])
        self.assertEqual(len(world.actions), 1)
        self.assertEqual(len(world.resource_actions), 1)
        action = world.resource_actions[0]
        plugin, condition = world.hooks[2], world.hooks[3]
        self.assertIs(action.hook, plugin)
        self.assertIs(action.condition, condition)
        self.assertTrue(plugin.module_file.endswith("vuln_resource_patch_job.py"))
        self.assertTrue(condition.module_file.endswith("vuln_resource_patch_condition.py"))
        self.assertEqual(action.label, "Install Updates on All Servers")
        self.assertIs(action.enabled, True)
        self.assertIs(action.list_view_visible, False)
        self.assertIs(action.is_synchronous, False)
        self.assertEqual(action.extra_classes, "fas fa-download")
        self.assertEqual(action.blueprints, [])
        self.assertNotIn("blueprints", [c for c in world.calls if c[0] == "raction.create"][0][1])
        # One input only, turned into a checkbox, and one mapping with the default True.
        self.assertEqual(len(plugin.input_rows), 1)
        field = plugin.input_rows[0]
        self.assertEqual(field.name, "restart_if_needed_a{}".format(plugin.id))
        self.assertEqual((field.type, field.required, field.hide_if_default_value), ("BOOL", False, False))
        self.assertEqual(len(action.mapping_rows), 1)
        self.assertIs(action.mapping_rows[0].default_value.value, True)
        self.assertIs(action.mapping_rows[0].default_value.field, field)
        self.assertEqual(world.count("mapping.save"), 2)  # one per action, none on re-enable

    def test_second_enable_only_sets_enabled(self):
        world = World()
        with world.installed():
            hooks.apply_patching_setting(True)
            world.calls.clear()
            hooks.apply_patching_setting(True)
            self.assertEqual(world.calls, [])
            hooks.apply_patching_setting(False)
            self.assertEqual(sorted(world.calls), [("action.save", False), ("raction.save", False)])
            world.calls.clear()
            hooks.apply_patching_setting(False)
            self.assertEqual(world.calls, [])
            hooks.apply_patching_setting(True)
            self.assertEqual(sorted(world.calls), [("action.save", True), ("raction.save", True)])
        self.assertEqual((len(world.actions), len(world.resource_actions), len(world.hooks)), (1, 1, 4))

    def test_admin_owned_flags_survive_off_and_on(self):
        world = World()
        with world.installed():
            hooks.apply_patching_setting(True)
            action = world.resource_actions[0]
            action.requires_approval = True
            action.auto_submit_order = True
            action.label = "Patch tier"
            action.roles = ["ops"]
            action.list_view_visible = True
            action.condition = "admin's own"
            action.mapping_rows[0].default_value.value = False
            hooks.apply_patching_setting(False)
            hooks.apply_patching_setting(True)
        self.assertIs(action.requires_approval, True)
        self.assertIs(action.auto_submit_order, True)
        self.assertEqual(action.label, "Patch tier")
        self.assertEqual(action.roles, ["ops"])
        self.assertIs(action.list_view_visible, True)
        self.assertEqual(action.condition, "admin's own")
        self.assertIs(action.mapping_rows[0].default_value.value, False)
        self.assertEqual(world.count("mapping.save"), 2)  # one per action, none on re-enable
        self.assertEqual(world.count("raction.create"), 1)

    def test_appliance_with_the_server_action_gets_only_the_resource_action(self):
        world = World()
        with world.installed():
            hooks._apply_server_action(True)
            server_action = world.actions[0]
            world.calls.clear()
            hooks.apply_patching_setting(True)
        self.assertEqual(world.count("action.create"), 0)
        self.assertEqual(world.count("raction.create"), 1)
        self.assertEqual(world.count("action.save"), 0)
        self.assertIs(server_action.enabled, True)
        self.assertEqual(len(world.actions), 1)

    def test_create_only_keeps_a_switched_off_action_off_and_makes_the_missing_one(self):
        world = World()
        with world.installed():
            hooks._apply_server_action(True)
            world.actions[0].enabled = False
            world.calls.clear()
            hooks.apply_patching_setting(True, create_only=True)
            self.assertIs(world.actions[0].enabled, False)
            self.assertEqual(world.count("action.save"), 0)
            self.assertEqual(len(world.resource_actions), 1)
            world.resource_actions[0].enabled = False
            world.calls.clear()
            hooks.apply_patching_setting(True, create_only=True)
            self.assertEqual(world.calls, [])
            self.assertIs(world.resource_actions[0].enabled, False)
            hooks.apply_patching_setting(True)
            self.assertIs(world.resource_actions[0].enabled, True)
            self.assertIs(world.actions[0].enabled, True)

    def test_a_failing_half_does_not_hide_the_other(self):
        world = World()
        with world.installed():
            world.ResourceAction.objects.create = mock.Mock(side_effect=ValueError("boom"))
            with self.assertRaises(RuntimeError) as caught:
                hooks.apply_patching_setting(True)
            self.assertIn("resource", str(caught.exception))
            self.assertNotIn("server and", str(caught.exception))
        self.assertEqual(len(world.actions), 1)
        self.assertEqual(world.resource_actions, [])

        world = World()
        with world.installed():
            world.ServerAction.objects.create = mock.Mock(side_effect=ValueError("boom"))
            with self.assertRaises(RuntimeError) as caught:
                hooks.apply_patching_setting(True)
            self.assertIn("server", str(caught.exception))
        self.assertEqual(world.actions, [])
        self.assertEqual(len(world.resource_actions), 1)

    def test_both_failing_says_both(self):
        world = World()
        with world.installed():
            world.ServerAction.objects.create = mock.Mock(side_effect=ValueError("boom"))
            world.ResourceAction.objects.create = mock.Mock(side_effect=ValueError("boom"))
            with self.assertRaises(RuntimeError) as caught:
                hooks.apply_patching_setting(True)
        self.assertIn("server and resource", str(caught.exception))

    def test_an_action_made_meanwhile_is_not_doubled(self):
        world = World()
        with world.installed():
            hooks.apply_patching_setting(True)
            rival = world.resource_actions[0]
            world.resource_actions.clear()
            real = hooks.resource_patch_actions
            seen = []

            def racing(hook):
                seen.append(1)
                if len(seen) == 2:
                    world.resource_actions.append(rival)
                return real(hook)

            world.calls.clear()
            with mock.patch.object(hooks, "resource_patch_actions", racing):
                hooks.apply_patching_setting(True, create_only=True)
        self.assertEqual(len(world.resource_actions), 1)
        self.assertEqual(world.count("raction.create"), 0)

    def test_no_os_families_and_no_blueprints_are_touched(self):
        world = World()
        with world.installed():
            hooks.apply_patching_setting(True)
            hooks.apply_patching_setting(False)
            hooks.apply_patching_setting(True)
        text = " ".join(str(c) for c in world.calls)
        self.assertNotIn("os_famil", text)
        self.assertNotIn("blueprints", text)

    def test_state_reads_only(self):
        world = World()
        empty = {"exists": False, "enabled": False, "action_id": None}
        with world.installed():
            self.assertEqual(hooks.resource_patch_action_state(), empty)
            self.assertEqual(world.calls, [])
            hooks.apply_patching_setting(True)
            self.assertEqual(hooks.resource_patch_action_state(),
                             {"exists": True, "enabled": True, "action_id": world.resource_actions[0].pk})
            hooks.apply_patching_setting(False)
            self.assertIs(hooks.resource_patch_action_state()["enabled"], False)
        with mock.patch.object(hooks, "CloudBoltHook", mock.Mock(objects=mock.Mock(filter=mock.Mock(
                side_effect=RuntimeError("db"))))):
            self.assertEqual(hooks.resource_patch_action_state(), empty)

    def test_names_fit_core_and_files_exist(self):
        for name in (hooks.RESOURCE_PATCH_NAME, hooks.RESOURCE_CONDITION_NAME):
            self.assertLessEqual(len(name), 255)
        self.assertEqual(hooks.RESOURCE_ACTION_LABEL, "Install Updates on All Servers")
        self.assertEqual(len({hooks.PATCH_NAME, hooks.CONDITION_NAME, hooks.RESOURCE_PATCH_NAME,
                              hooks.RESOURCE_CONDITION_NAME}), 4)
        for name in (hooks.RESOURCE_PATCH_FILE, hooks.RESOURCE_CONDITION_FILE):
            self.assertTrue(name.startswith("vuln_") and name.endswith(".py"))


class ResourceButtonTests(unittest.TestCase):
    def setUp(self):
        self.world = World()
        with self.world.installed():
            hooks.apply_patching_setting(True)
        self.action = self.world.resource_actions[0]
        self.settings = {"patching": True}
        patcher = mock.patch.object(store, "get_settings", lambda: self.settings)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.listed = [{"action": self.action, "url": "/resource_actions/5/run_action/{}/".format(self.action.pk),
                        "label": "x", "extra_classes": ""}]

    def button(self):
        with self.world.installed():
            return hooks.resource_patch_button(resource(self.listed), object(), None)

    def test_shows_when_everything_allows_it(self):
        self.assertEqual(self.button(), {"url": "/resource_actions/5/run_action/{}/".format(self.action.pk),
                                         "label": "Install Updates on All Servers", "action_id": self.action.pk})

    def test_setting_off(self):
        self.settings = {"patching": False}
        self.assertIsNone(self.button())

    def test_action_disabled(self):
        self.action.enabled = False
        self.assertIsNone(self.button())

    def test_core_does_not_list_it(self):
        self.listed = [{"action": types.SimpleNamespace(pk=99), "url": "/x/"}]
        self.assertIsNone(self.button())
        self.listed = []
        self.assertIsNone(self.button())

    def test_permission_denied(self):
        self.world.resource_permitted = False
        self.assertIsNone(self.button())

    def test_a_new_tab_action_has_no_dialog(self):
        self.listed[0]["not_dialog"] = True
        self.assertIsNone(self.button())

    def test_never_raises(self):
        self.listed = "not a list"
        self.assertIsNone(self.button())
        with mock.patch.object(hooks, "CloudBoltHook", None):
            self.assertIsNone(hooks.resource_patch_button(resource(), object()))


class ServerPatchPermittedTests(unittest.TestCase):
    def test_follows_the_enabled_server_action(self):
        world = World()
        with world.installed():
            self.assertIs(hooks.server_patch_permitted(object(), base.server()), False)
            hooks.apply_patching_setting(True)
            self.assertIs(hooks.server_patch_permitted(object(), base.server()), True)
            world.permitted = False
            self.assertIs(hooks.server_patch_permitted(object(), base.server()), False)
            world.permitted = True
            hooks.apply_patching_setting(False)
            self.assertIs(hooks.server_patch_permitted(object(), base.server()), False)

    def test_never_raises(self):
        with mock.patch.object(hooks, "CloudBoltHook", None):
            self.assertIs(hooks.server_patch_permitted(object(), base.server()), False)


# ---------------------------------------------------------------- vuln_api.install_updates_on_resource
class FakeJob:
    def __init__(self, id=11, status="SUCCESS", errors=""):
        self.id, self.status, self.errors = id, status, errors
        self.waited = None
        self.ends = True

    def wait_for_completion(self, timeout=None):
        self.waited = timeout
        if not self.ends:
            from utilities.exceptions import TimeoutException
            raise TimeoutException()
        return self


class Refusal:
    id = None

    def __init__(self, message):
        self.message = message


def _load_api(started, record, ids, calls):
    class Timeout(Exception):
        pass

    fleet = _module(PACKAGE + ".vuln_fleet", _started_id=lambda s: getattr(s, "id", None),
                    default_base_url=lambda: "")
    jobs = _module(PACKAGE + ".vuln_jobs", MESSAGE_LEN=300, clean=lambda text, limit: text[:limit])
    patch = _module(PACKAGE + ".vuln_patch", DEFAULT_PATCH_TIMEOUT_MIN=90, DEFAULT_RESTART_WAIT_MIN=60,
                    _minutes=lambda cfg, key, default: cfg.get(key, default), _summary=lambda i: None)

    def start_job(owner, res, restart_if_needed=True, parent_job=None):
        calls.append(("start_job", owner, res, restart_if_needed, parent_job))
        return started

    def patchable_ids(profile, res, visibility, problems=None):
        calls.append(("patchable_ids", visibility))
        if isinstance(ids, Exception):
            raise ids
        return ids

    patch_resource = _module(PACKAGE + ".vuln_patch_resource", start_job=start_job, patchable_ids=patchable_ids)
    fake_store = _module(PACKAGE + ".vuln_store",
                         get_settings=lambda: {"visibility": "all", "scan_timeout_min": 30, "patch_timeout_min": 90,
                                               "restart_wait_min": 60},
                         read_resource_patch=lambda rid: record, read_state=lambda i: {},
                         job_settings=lambda: {"timeout_min": 30})
    package = _module(PACKAGE, __path__=[SOURCE], vuln_fleet=fleet, vuln_jobs=jobs, vuln_patch=patch,
                      vuln_patch_resource=patch_resource, vuln_store=fake_store)
    patches = {PACKAGE: package, PACKAGE + ".vuln_fleet": fleet, PACKAGE + ".vuln_jobs": jobs,
               PACKAGE + ".vuln_patch": patch, PACKAGE + ".vuln_patch_resource": patch_resource,
               PACKAGE + ".vuln_store": fake_store,
               "utilities.logger": _module("utilities.logger", ThreadLogger=base.FakeLogger),
               "utilities.exceptions": _module("utilities.exceptions", TimeoutException=Timeout)}
    patcher = mock.patch.dict(sys.modules, patches)
    patcher.start()
    path = os.path.join(SOURCE, "vuln_api.py")
    spec = importlib.util.spec_from_file_location(PACKAGE + ".vuln_api", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module, patcher


class InstallOnResourceTests(unittest.TestCase):
    def run_api(self, started=None, record=None, ids=(1, 2), **kw):
        calls = []
        started = started or FakeJob()
        api, patcher = _load_api(started, record, list(ids) if not isinstance(ids, Exception) else ids, calls)
        self.addCleanup(patcher.stop)
        self.calls = calls
        return api.install_updates_on_resource(resource(), "owner", **kw), started

    def record(self, job_id=11, status="success", **counts):
        full = {"updated": 0, "restart_pending": 0, "failed": 0, "skipped": 0}
        full.update(counts)
        return {"at": "x", "by": "u", "job_id": job_id, "status": status, "counts": full,
                "restart_if_needed": True, "message": "2 updated."}

    def test_completed(self):
        result, job = self.run_api(record=self.record(updated=2))
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["counts"], {"updated": 2, "no_updates": 0, "restart_pending": 0, "failed": 0, "skipped": 0})
        self.assertIs(result["job"], job)
        self.assertIn("web tier", result["message"])
        self.assertEqual(self.calls[0], ("start_job", "owner", self.calls[0][2], True, None))

    def test_restart_pending_when_none_failed(self):
        result, _ = self.run_api(record=self.record(status="warning", updated=1, restart_pending=1))
        self.assertEqual(result["status"], "restart_pending")
        self.assertEqual(result["counts"]["restart_pending"], 1)

    def test_failed_wins_over_restart_pending(self):
        result, _ = self.run_api(record=self.record(status="warning", updated=1, restart_pending=1, failed=1))
        self.assertEqual(result["status"], "failed")
        result, _ = self.run_api(record=self.record(status="failure"))
        self.assertEqual(result["status"], "failed")
        self.assertIn("2 updated.", result["message"])

    def test_refused(self):
        result, job = self.run_api(started=Refusal("Job 4 is already installing updates on the servers of this "
                                                   "resource."))
        self.assertEqual(result["status"], "refused")
        self.assertIsNone(result["counts"])
        self.assertIsNone(result["job"])
        self.assertIn("already installing", result["message"])

    def test_timeout(self):
        job = FakeJob()
        job.ends = False
        result, _ = self.run_api(started=job, record=None)
        self.assertEqual(result["status"], "timeout")
        self.assertIs(result["job"], job)

    def test_no_record_or_another_jobs_record_is_a_failure(self):
        result, _ = self.run_api(record=None)
        self.assertEqual(result["status"], "failed")
        self.assertIn("recorded no result", result["message"])
        result, _ = self.run_api(record=self.record(job_id=3, updated=2))
        self.assertEqual(result["status"], "failed")
        self.assertIsNone(result["counts"])

    def test_default_wait_is_servers_times_worst_case_capped(self):
        _, job = self.run_api(record=self.record(updated=2), ids=(1, 2))
        self.assertEqual(job.waited, 2 * (90 + 60 + 30) * 60 + 600)
        _, job = self.run_api(record=self.record(updated=2), ids=range(50))
        self.assertEqual(job.waited, 27000)
        _, job = self.run_api(record=self.record(updated=2), ids=())
        self.assertEqual(job.waited, (90 + 60 + 30) * 60 + 600)
        _, job = self.run_api(record=self.record(updated=2), ids=RuntimeError("db"))
        self.assertEqual(job.waited, (90 + 60 + 30) * 60 + 600)
        _, job = self.run_api(record=self.record(updated=2), timeout=100)
        self.assertEqual(job.waited, 100)

    def test_restart_flag_and_parent_job_pass_through(self):
        self.run_api(record=self.record(updated=2), restart_if_needed=False, parent_job="parent")
        self.assertEqual(self.calls[0][3:], (False, "parent"))

    def test_the_docstring_says_there_is_no_permission_check(self):
        api, patcher = _load_api(FakeJob(), None, [], [])
        self.addCleanup(patcher.stop)
        self.assertIn("no permission check of its own", api.install_updates_on_resource.__doc__)


class NoTokenTests(unittest.TestCase):
    def test_no_double_braces_in_the_files_this_package_writes(self):
        for name in ("vuln_hooks.py", "vuln_api.py"):
            with open(os.path.join(SOURCE, name), encoding="ascii") as handle:
                text = handle.read()
            self.assertNotIn(chr(123) * 2, text, name)

    def test_the_settings_page_names_the_resource_action(self):
        path = os.path.join(SOURCE, "templates", "vuln_settings.html")
        with open(path, encoding="ascii") as handle:
            html = handle.read()
        self.assertIn("Install Updates on All Servers", html)
        self.assertIn("resource_patch_action.exists", html)
        self.assertIn("resource_patch_action.enabled", html)

    def test_the_new_files_are_in_the_package(self):
        import json

        path = os.path.join(os.path.dirname(HERE), "XUI-wqrj0e3p_metadata.json")
        with open(path, encoding="utf-8") as handle:
            contents = json.load(handle)["package_contents"]
        for name in ("vuln_patch_resource.py", "vuln_resource_patch_job.py", "vuln_resource_patch_condition.py"):
            self.assertEqual(contents.count(name), 1, name)


if __name__ == "__main__":
    unittest.main()
