"""
Checks for the Install Updates wiring of the Vulnerabilities XUI (vuln_hooks.py apply_patching_setting,
patch_button and patch_action_state; the patching settings in vuln_store.py; vuln_patch_condition.py)
without CloudBolt: CloudBolt's modules are stubs and the ORM is a small in-memory fake that records every
write, so a test can say exactly what was created and what was left alone.

    python -B -m unittest extensions/XUI-wqrj0e3p/tests/test_vuln_patch_hooks.py

The stubs are in place only while this file imports the extension's modules, so another test file's stubs
are not disturbed. The ORM fakes are put in place per test.
"""
import contextlib
import importlib
import itertools
import os
import sys
import tempfile
import types
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
EXT = os.path.dirname(HERE)
PACKAGE = "xui.io_cloudbolt_vulnerabilities"
SOURCE = os.path.join(EXT, "io_cloudbolt_vulnerabilities")


class IntegrityError(Exception):
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


TMP = tempfile.mkdtemp(prefix="vuln-patch-hooks-test-")
STUBS = {}


def _stub(name, **attrs):
    STUBS[name] = _module(name, **attrs)
    return STUBS[name]


_stub("django")
_stub("django.conf", settings=types.SimpleNamespace(PROSERV_DIR=TMP, CLOUDBOLT_OS_USER="u", CLOUDBOLT_OS_GROUP="g"))
_stub("django.http", FileResponse=object, Http404=Exception, HttpResponse=object)
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
        names = ("vuln_store", "vuln_match", "vuln_db", "vuln_tools", "vuln_repo", "vuln_hooks", "vuln_jobs")
        return {name: importlib.import_module(PACKAGE + "." + name) for name in names}
    finally:
        for key in [k for k in sys.modules if k == "xui" or k.startswith(PACKAGE) or k in STUBS]:
            del sys.modules[key]
        sys.modules.update(saved)


MODS = _import_extension()
store, hooks, jobs = MODS["vuln_store"], MODS["vuln_hooks"], MODS["vuln_jobs"]


# ---------------------------------------------------------------- an in-memory ORM
class Rows(list):
    def first(self):
        return self[0] if self else None

    def exists(self):
        return bool(self)

    def all(self):
        return self


class Manager:
    def __init__(self, rows):
        self.rows = rows

    def filter(self, **kw):
        return Rows(r for r in self.rows if all(getattr(r, k, None) == v for k, v in kw.items()))

    def all(self):
        return Rows(self.rows)

    def select_for_update(self):
        return self


class World:
    """The fake database of one test and the log of every write made to it."""

    def __init__(self, make_token_input=True):
        self.calls = []
        self.hooks = []
        self.actions = []
        self.values = []
        self.ids = itertools.count(1)
        self.make_token_input = make_token_input
        world = self

        class Hook:
            objects = Manager(world.hooks)

            def __init__(self, **kw):
                self.id = None
                self.__dict__.update(kw)
                self.input_rows = []
                self.input_fields = Manager(self.input_rows)

            @property
            def pk(self):
                return self.id

            def save(self):
                first = self.id is None
                if first:
                    self.id = next(world.ids)
                    world.hooks.append(self)
                world.calls.append(("hook.save", self.name))
                # hook.save() makes a text input from the token in the plugin file.
                if first and self.name == hooks.PATCH_NAME and world.make_token_input:
                    HookInput.objects.create(hook=self, name="restart_if_needed_a{}".format(self.id),
                                             label="restart_if_needed", type="STR", required=True,
                                             hide_if_default_value=True, _from_token=True)

        class HookInput:
            objects = types.SimpleNamespace()

            def __init__(self, **kw):
                self.__dict__.update(kw)

            def save(self):
                world.calls.append(("input.save", self.name))

        def make_input(**kw):
            row = HookInput(**kw)
            kw["hook"].input_rows.append(row)
            if not kw.get("_from_token"):
                world.calls.append(("input.create", kw["name"]))
            return row

        HookInput.objects.create = make_input

        class Mapping:
            def __init__(self, hook_input):
                self.hook_input = hook_input
                self.default_value = None

            def save(self):
                world.calls.append(("mapping.save", self.default_value.value if self.default_value else None))

        class Action:
            def __init__(self, **kw):
                self.requires_approval = False
                self.auto_submit_order = False
                self.roles = []
                self.pk = next(world.ids)
                self.mapping_rows = []
                self.input_mappings = Manager(self.mapping_rows)
                self.__dict__.update(kw)

            def update_input_mappings(self):
                world.calls.append(("action.update_input_mappings",))
                for row in self.hook.input_rows:
                    if not self.input_mappings.filter(hook_input=row):
                        self.mapping_rows.append(Mapping(row))

            def save(self):
                world.calls.append(("action.save", self.enabled))

            def server_action_dict(self, server, request=None):
                return world.action_dict

        class ServerAction:
            objects = types.SimpleNamespace(
                filter=lambda **kw: Rows(a for a in world.actions if all(getattr(a, k) == v for k, v in kw.items())))

        def create_action(**kw):
            row = Action(**kw)
            world.actions.append(row)
            world.calls.append(("action.create", dict(kw)))
            return row

        ServerAction.objects.create = create_action

        class CustomFieldValue:
            objects = types.SimpleNamespace()

        def get_or_create(field, value):
            row = types.SimpleNamespace(field=field, value=value)
            world.values.append(row)
            world.calls.append(("value.get_or_create", field.name, value))
            return row, True

        CustomFieldValue.objects.get_or_create = get_or_create
        self.action_dict = {"url": "/server_actions/1/", "label": "Install Updates"}
        self.permitted = True
        self.Hook, self.HookInput, self.ServerAction, self.CustomFieldValue = Hook, HookInput, ServerAction, CustomFieldValue

    @contextlib.contextmanager
    def installed(self):
        cb = _module("cbhooks.models", CloudBoltHook=self.Hook, HookInput=self.HookInput,
                     ServerAction=self.ServerAction)
        orders = _module("orders.models", CustomFieldValue=self.CustomFieldValue)
        perms = _module("utilities.permissions", server_action_permitted=lambda profile, server, action: self.permitted)
        package = _module(PACKAGE, vuln_store=store)
        patches = {"cbhooks.models": cb, "orders.models": orders, "utilities.permissions": perms,
                   PACKAGE: package, PACKAGE + ".vuln_store": store}
        with mock.patch.dict(sys.modules, patches), mock.patch.object(hooks, "CloudBoltHook", self.Hook):
            yield self

    def count(self, kind):
        return len([c for c in self.calls if c[0] == kind])

    def actions_calls(self, kind):
        return [c for c in self.calls if c[0] == kind]


def server(**over):
    data = dict(id=7, hostname="web-01", group=types.SimpleNamespace(is_unassigned=False, name="Dev"),
                os_family=types.SimpleNamespace(get_base_name=lambda: "Linux", name="Linux"))
    data.update(over)
    return types.SimpleNamespace(**data)


# ---------------------------------------------------------------- the action
class ApplyPatchingTests(unittest.TestCase):
    def test_off_with_no_hook_is_a_no_op(self):
        world = World()
        with world.installed():
            hooks.apply_patching_setting(False)
        self.assertEqual(world.calls, [])

    def test_first_enable_makes_everything_once(self):
        world = World()
        with world.installed():
            hooks.apply_patching_setting(True)
        self.assertEqual([h.name for h in world.hooks], [hooks.PATCH_NAME, hooks.CONDITION_NAME])
        self.assertEqual(len(world.actions), 1)
        action = world.actions[0]
        patch = world.hooks[0]
        self.assertIs(action.hook, patch)
        self.assertIs(action.condition, world.hooks[1])
        self.assertEqual(action.label, "Install Updates")
        self.assertIs(action.enabled, True)
        self.assertIs(action.is_synchronous, False)
        self.assertTrue(world.hooks[0].module_file.endswith("vuln_patch_job.py"))
        self.assertTrue(world.hooks[1].module_file.endswith("vuln_patch_condition.py"))
        # One input: the token's text input turned into a checkbox, not a second one.
        self.assertEqual(len(patch.input_rows), 1)
        field = patch.input_rows[0]
        self.assertEqual(field.name, "restart_if_needed_a{}".format(patch.id))
        self.assertEqual(field.type, "BOOL")
        self.assertIs(field.required, False)
        self.assertIs(field.hide_if_default_value, False)
        # One mapping with the default True.
        self.assertEqual(len(action.mapping_rows), 1)
        self.assertIs(action.mapping_rows[0].hook_input, field)
        self.assertIs(action.mapping_rows[0].default_value.value, True)
        self.assertIs(action.mapping_rows[0].default_value.field, field)

    def test_the_input_is_made_when_the_token_pass_did_not(self):
        world = World(make_token_input=False)
        with world.installed():
            hooks.apply_patching_setting(True)
        patch = world.hooks[0]
        self.assertEqual(len(patch.input_rows), 1)
        self.assertEqual(world.count("input.create"), 1)
        field = patch.input_rows[0]
        self.assertEqual((field.type, field.required, field.hide_if_default_value), ("BOOL", False, False))
        self.assertIs(world.actions[0].mapping_rows[0].default_value.value, True)

    def test_no_os_families_call_is_made(self):
        world = World()
        with world.installed():
            hooks.apply_patching_setting(True)
            hooks.apply_patching_setting(False)
            hooks.apply_patching_setting(True)
        for hook in world.hooks:
            self.assertFalse(hasattr(hook, "os_families"))
        text = " ".join(str(c) for c in world.calls)
        self.assertNotIn("os_famil", text)
        import ast

        with open(os.path.join(SOURCE, "vuln_hooks.py"), encoding="ascii") as handle:
            tree = ast.parse(handle.read())
        for node in ast.walk(tree):
            self.assertNotEqual(getattr(node, "attr", None), "os_families")
            self.assertNotEqual(getattr(node, "arg", None), "os_families")
            self.assertNotEqual(getattr(node, "id", None), "os_families")

    def test_second_enable_creates_nothing_and_off_only_sets_enabled(self):
        world = World()
        with world.installed():
            hooks.apply_patching_setting(True)
            world.calls.clear()
            hooks.apply_patching_setting(True)
            self.assertEqual(world.calls, [])
            hooks.apply_patching_setting(False)
            self.assertEqual(world.calls, [("action.save", False)])
            self.assertIs(world.actions[0].enabled, False)
            world.calls.clear()
            hooks.apply_patching_setting(False)
            self.assertEqual(world.calls, [])
            hooks.apply_patching_setting(True)
            self.assertEqual(world.calls, [("action.save", True)])
        self.assertEqual(len(world.actions), 1)
        self.assertEqual(len(world.hooks), 2)

    def test_admin_owned_flags_survive_off_and_on(self):
        world = World()
        with world.installed():
            hooks.apply_patching_setting(True)
            action = world.actions[0]
            action.requires_approval = True
            action.auto_submit_order = True
            action.label = "Patch it"
            action.roles = ["ops"]
            condition = action.condition
            action.condition = "admin's own"
            action.mapping_rows[0].default_value.value = False
            hooks.apply_patching_setting(False)
            hooks.apply_patching_setting(True)
        self.assertIs(action.requires_approval, True)
        self.assertIs(action.auto_submit_order, True)
        self.assertEqual(action.label, "Patch it")
        self.assertEqual(action.roles, ["ops"])
        self.assertEqual(action.condition, "admin's own")
        self.assertIsNot(action.condition, condition)
        # The admin's default is not re-set, and no mapping is written on re-enable.
        self.assertIs(action.mapping_rows[0].default_value.value, False)
        self.assertEqual(world.count("mapping.save"), 1)
        self.assertEqual(world.count("value.get_or_create"), 1)

    def test_extra_classes_follow_the_exported_actions(self):
        self.assertEqual(hooks.PATCH_ACTION_ICON, "fas fa-download")
        root = os.path.abspath(os.path.join(EXT, "..", "..", "server_actions"))
        spellings = set()
        for folder in os.listdir(root):
            path = os.path.join(root, folder, folder + "_metadata.json")
            if os.path.exists(path):
                import json

                with open(path, encoding="utf-8") as handle:
                    spellings.add(json.load(handle).get("extra_classes", ""))
        spellings.discard("")
        self.assertTrue(spellings, "no exported server action carries extra_classes")
        for spelling in spellings:
            self.assertTrue(spelling.startswith(("fas ", "far ", "fab ")), spelling)
        self.assertTrue(hooks.PATCH_ACTION_ICON.startswith("fas fa-"))
        world = World()
        with world.installed():
            hooks.apply_patching_setting(True)
        self.assertEqual(world.actions[0].extra_classes, hooks.PATCH_ACTION_ICON)

    def test_a_failure_is_a_runtime_error(self):
        world = World()
        with world.installed():
            world.ServerAction.objects.create = mock.Mock(side_effect=ValueError("boom"))
            with self.assertRaises(RuntimeError):
                hooks.apply_patching_setting(True)

    def test_an_action_made_meanwhile_is_not_doubled(self):
        world = World()
        with world.installed():
            hooks.apply_patching_setting(True)
            rival = world.actions[0]
            world.actions.clear()
            real = hooks.patch_actions
            seen = []

            def racing(hook):
                seen.append(1)
                if len(seen) == 2:
                    world.actions.append(rival)      # the other request made it after our first look
                return real(hook)

            world.actions.clear()
            with mock.patch.object(hooks, "patch_actions", racing):
                world.calls.clear()
                hooks.apply_patching_setting(True)
        self.assertEqual(len(world.actions), 1)
        self.assertEqual(world.count("action.create"), 0)

    def test_state_reads_only(self):
        world = World()
        with world.installed():
            self.assertEqual(hooks.patch_action_state(), {"exists": False, "enabled": False, "action_id": None})
            self.assertEqual(world.calls, [])
            hooks.apply_patching_setting(True)
            state = hooks.patch_action_state()
            self.assertEqual(state, {"exists": True, "enabled": True, "action_id": world.actions[0].pk})
            hooks.apply_patching_setting(False)
            self.assertEqual(hooks.patch_action_state()["enabled"], False)
        with mock.patch.object(hooks, "CloudBoltHook", mock.Mock(objects=mock.Mock(filter=mock.Mock(
                side_effect=RuntimeError("db"))))):
            self.assertEqual(hooks.patch_action_state(), {"exists": False, "enabled": False, "action_id": None})


class PatchButtonTests(unittest.TestCase):
    def setUp(self):
        self.world = World()
        with self.world.installed():
            hooks.apply_patching_setting(True)
        self.settings = {"patching": True}
        self.patcher = mock.patch.object(store, "get_settings", lambda: self.settings)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

    def button(self):
        with self.world.installed():
            return hooks.patch_button(server(), object(), None)

    def test_shows_when_everything_allows_it(self):
        self.assertEqual(self.button(), {"url": "/server_actions/1/", "label": "Install Updates",
                                         "action_id": self.world.actions[0].pk})

    def test_setting_off(self):
        self.settings = {"patching": False}
        self.assertIsNone(self.button())

    def test_action_disabled(self):
        self.world.actions[0].enabled = False
        self.assertIsNone(self.button())

    def test_condition_hides_it(self):
        self.world.action_dict = None
        self.assertIsNone(self.button())

    def test_permission_denied(self):
        self.world.permitted = False
        self.assertIsNone(self.button())

    def test_never_raises(self):
        self.world.action_dict = "not a dict"
        self.assertIsNone(self.button())
        with mock.patch.object(hooks, "CloudBoltHook", None):
            self.assertIsNone(hooks.patch_button(server(), object()))


# ---------------------------------------------------------------- settings
class PatchSettingsTests(unittest.TestCase):
    def test_defaults(self):
        self.assertIs(store.DEFAULT_SETTINGS["patching"], False)
        self.assertEqual(store.DEFAULT_SETTINGS["restart_wait_min"], 60)
        self.assertEqual(store.DEFAULT_SETTINGS["patch_timeout_min"], 90)
        clean, errors = store.validate_settings({})
        self.assertEqual(errors, {})
        self.assertEqual((clean["patching"], clean["restart_wait_min"], clean["patch_timeout_min"]),
                         (False, 60, 90))
        with mock.patch.object(store, "_read_json", return_value={}):
            result = store.get_settings()
        self.assertEqual((result["patching"], result["restart_wait_min"], result["patch_timeout_min"]),
                         (False, 60, 90))

    def test_remove_old_kernels_setting(self):
        self.assertIs(store.DEFAULT_SETTINGS["remove_old_kernels"], True)
        with mock.patch.object(store, "_read_json", return_value={"patching": True}):
            self.assertIs(store.get_settings()["remove_old_kernels"], True)
        with mock.patch.object(store, "_read_json", return_value={"remove_old_kernels": False}):
            self.assertIs(store.get_settings()["remove_old_kernels"], False)
        self.assertIs(store.validate_settings({"remove_old_kernels": "1"})[0]["remove_old_kernels"], True)
        self.assertIs(store.validate_settings({})[0]["remove_old_kernels"], False)
        self.assertIs(store.validate_settings({"remove_old_kernels": "0"})[0]["remove_old_kernels"], False)

    def test_the_settings_page_has_the_checkbox(self):
        path = os.path.join(SOURCE, "templates", "vuln_settings.html")
        with open(path, encoding="ascii") as handle:
            html = handle.read()
        self.assertIn('name="remove_old_kernels"', html)
        self.assertIn("Remove old kernels after a restart", html)

    def test_patching_flag_parsing(self):
        for value, expect in (("1", True), ("on", True), ("true", True), ("", False), ("0", False), ("off", False)):
            clean, errors = store.validate_settings({"patching": value})
            self.assertIs(clean["patching"], expect, value)
            self.assertNotIn("patching", errors)

    def test_valid_values(self):
        clean, errors = store.validate_settings({"patching": "1", "restart_wait_min": "120", "patch_timeout_min": "10"})
        self.assertEqual(errors, {})
        self.assertEqual((clean["patching"], clean["restart_wait_min"], clean["patch_timeout_min"]),
                         (True, 120, 10))

    def test_bad_ints_are_errors_and_clamped(self):
        for key, bad, expect in (("restart_wait_min", "4", 5), ("restart_wait_min", "121", 120),
                                 ("restart_wait_min", "soon", 60), ("restart_wait_min", "", 60),
                                 ("patch_timeout_min", "9", 10), ("patch_timeout_min", "241", 240),
                                 ("patch_timeout_min", "-1", 10), ("patch_timeout_min", "\u00b2", 90)):
            clean, errors = store.validate_settings({key: bad})
            self.assertIn(key, errors, (key, bad))
            self.assertEqual(clean[key], expect, (key, bad))
        self.assertEqual(store.validate_settings({"restart_wait_min": "4"})[1]["restart_wait_min"],
                         "Minutes to wait for a server to come back after a restart must be a whole number "
                         "from 5 to 120.")

    def test_get_settings_clamps(self):
        saved = {"patching": 1, "restart_wait_min": 999, "patch_timeout_min": "x"}
        with mock.patch.object(store, "_read_json", return_value=saved):
            result = store.get_settings()
        self.assertIs(result["patching"], True)
        self.assertEqual(result["restart_wait_min"], 120)
        self.assertEqual(result["patch_timeout_min"], 90)
        with mock.patch.object(store, "_read_json", return_value={"patch_timeout_min": 1}):
            self.assertEqual(store.get_settings()["patch_timeout_min"], 10)


# ---------------------------------------------------------------- the display condition
def _load_condition():
    path = os.path.join(SOURCE, "vuln_patch_condition.py")
    namespace = {}
    with open(path, encoding="ascii") as handle:
        source = handle.read()
    exec(compile(source, path, "exec"), namespace)
    return source, namespace["should_display"]


class ConditionTests(unittest.TestCase):
    def setUp(self):
        self.source, self.should_display = _load_condition()
        fake = _module(PACKAGE + ".vuln_jobs", in_unassigned_group=jobs.in_unassigned_group,
                       is_linux=jobs.is_linux, is_windows=jobs.is_windows)
        patcher = mock.patch.dict(sys.modules, {PACKAGE: _module(PACKAGE), PACKAGE + ".vuln_jobs": fake,
                                                "xui": _module("xui"),
                                                "xui.io_cloudbolt_vulnerabilities": _module(
                                                    "xui.io_cloudbolt_vulnerabilities", vuln_jobs=fake)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_linux_is_shown(self):
        self.assertIs(self.should_display(server()), True)
        self.assertIs(self.should_display(server(), None), True)

    def test_windows_is_shown(self):
        windows = types.SimpleNamespace(get_base_name=lambda: "Windows", name="Windows")
        self.assertIs(self.should_display(server(os_family=windows, is_windows=lambda: True)), True)

    def test_windows_in_an_unassigned_group_is_hidden(self):
        windows = types.SimpleNamespace(get_base_name=lambda: "Windows", name="Windows")
        self.assertIs(self.should_display(server(os_family=windows, is_windows=lambda: True,
                                    group=types.SimpleNamespace(is_unassigned=True))), False)

    def test_an_error_in_the_windows_check_hides_it(self):
        def boom(server):
            raise RuntimeError("no family")
        fake = _module(PACKAGE + ".vuln_jobs", in_unassigned_group=jobs.in_unassigned_group,
                       is_linux=lambda s: False, is_windows=boom)
        with mock.patch.dict(sys.modules, {"xui.io_cloudbolt_vulnerabilities": _module(
                "xui.io_cloudbolt_vulnerabilities", vuln_jobs=fake)}):
            self.assertIs(self.should_display(server()), False)

    def test_the_texts_say_linux_and_windows(self):
        self.assertIn("Windows Server 2022 or 2025", hooks.PATCH_DESCRIPTION)
        self.assertNotIn("not Windows", hooks.CONDITION_DESCRIPTION)
        self.assertIn("Linux and Windows", hooks.CONDITION_DESCRIPTION)

    def test_no_or_other_os_family_is_hidden(self):
        self.assertIs(self.should_display(server(os_family=None)), False)
        other = types.SimpleNamespace(get_base_name=lambda: "Solaris", name="Solaris")
        self.assertIs(self.should_display(server(os_family=other)), False)

    def test_unassigned_group_is_hidden(self):
        self.assertIs(self.should_display(server(group=types.SimpleNamespace(is_unassigned=True))), False)

    def test_an_error_hides_it(self):
        broken = types.SimpleNamespace(id=1)     # no group at all: in_unassigned_group fails closed
        self.assertIs(self.should_display(broken), False)
        with mock.patch.dict(sys.modules, {"xui.io_cloudbolt_vulnerabilities": None}):
            self.assertIs(self.should_display(server()), False)

    def test_the_file_is_small_ascii_and_has_no_double_braces(self):
        self.assertLess(len(self.source), 1400)
        self.assertTrue(self.source.isascii())
        self.assertNotIn("{{", self.source)
        self.assertNotIn("}}", self.source)


class NoTokenTests(unittest.TestCase):
    def test_no_double_braces_in_the_files_of_this_package(self):
        for name in ("vuln_hooks.py", "vuln_patch_condition.py"):
            with open(os.path.join(SOURCE, name), encoding="ascii") as handle:
                self.assertNotIn("{{", handle.read(), name)


if __name__ == "__main__":
    unittest.main()
