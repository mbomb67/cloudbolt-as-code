"""
Checks for the resource-wide Install updates part of the Vulnerabilities resource tab (views.vuln_resource_summary,
the settings view, vuln_resource_tab.html and the script in vuln_fleet_assets.html) without CloudBolt. The patch
pieces the views call (vuln_hooks.resource_patch_button, vuln_patch_resource, store.read_resource_patch) are
stubbed here. The template tests need Django itself and are skipped when it is not installed.

    python -B -m unittest discover -s extensions/XUI-wqrj0e3p/tests -p "test_*.py"
"""
import contextlib
import os
import re
import sys
import types
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import test_vuln_patch_panel as panel  # noqa: E402  (imports views against stubs)

views = panel.views
base = panel.base
HAVE_DJANGO = panel.HAVE_DJANGO
TEMPLATES = panel.TEMPLATES
AT = "2026-11-24T10:00:00Z"
URL = "/resource_actions/5/run_action/9/"
BUTTON = {"url": URL, "label": "Install Updates on All Servers", "action_id": 9}
RESOURCE = types.SimpleNamespace(id=5, name="web")
PROFILE = types.SimpleNamespace(is_cbadmin=False)
RUN = {"job_id": 3, "job_url": "/jobs/3/", "started": AT, "done": 1, "total": 4, "kind": "patch",
       "message": "Server 2 of 4: web-02"}


def patch_record(**over):
    data = {"at": AT, "by": "alice", "job_id": 41, "status": "success",
            "counts": {"updated": 3, "restart_pending": 1, "failed": 0, "skipped": 2},
            "restart_if_needed": True, "message": ""}
    data.update(over)
    return data


def summary(run=None, ids=(1, 2), button=BUTTON, patchable=(1, 2), record=None, button_error=None,
            patchable_error=None, method="GET", query=None):
    """views.vuln_resource_summary with everything outside it replaced; returns (data, button mock, patchable mock)."""
    button_mock = mock.Mock(return_value=button, side_effect=button_error)
    module = types.ModuleType(base.PACKAGE + ".vuln_patch_resource")
    module.any_patchable = mock.Mock(return_value=bool(patchable), side_effect=patchable_error)
    package = types.ModuleType(base.PACKAGE)
    package.__path__ = []
    package.vuln_patch_resource = module
    request = types.SimpleNamespace(method=method, GET=query or {})
    with contextlib.ExitStack() as stack:
        enter = stack.enter_context
        enter(mock.patch.dict(sys.modules, {base.PACKAGE: package, base.PACKAGE + ".vuln_patch_resource": module}))
        enter(mock.patch.object(views, "_resource_gate", return_value=(RESOURCE, PROFILE, list(ids))))
        enter(mock.patch.object(views, "_upgrade_store"))
        enter(mock.patch.object(views.vuln_fleet, "resource_run", return_value=run))
        enter(mock.patch.object(views.vuln_fleet, "any_scannable", return_value=True))
        enter(mock.patch.object(views.vuln_db, "set_summary", return_value=None))
        enter(mock.patch.object(views.vuln_tools, "status", return_value={}))
        enter(mock.patch.object(views.store, "get_settings", return_value={"visibility": "all"}))
        enter(mock.patch.object(views.store, "read_resource_patch", return_value=record, create=True))
        enter(mock.patch.object(views, "reverse", lambda name, args=None, **k: "/%s/%s/" % (name, (args or [""])[0])))
        enter(mock.patch.object(views.vuln_hooks, "resource_patch_button", button_mock, create=True))
        response = views.vuln_resource_summary(request, 5)
    return response.data, button_mock, module.any_patchable


class SummaryTests(unittest.TestCase):
    def test_patch_offer(self):
        data, button, patchable = summary()
        self.assertEqual(data["patch"], {"offer": True, "url": URL, "label": BUTTON["label"]})
        self.assertEqual(button.call_args.args[0], RESOURCE)
        patchable.assert_called_once()
        self.assertEqual(patchable.call_args.args[:2], (PROFILE, RESOURCE))

    def test_patch_is_null_while_a_run_is_live_and_nothing_is_asked(self):
        data, button, patchable = summary(run=RUN, record=patch_record())
        self.assertIsNone(data["patch"])
        self.assertIsNone(data["patch_last"])
        self.assertEqual(data["run"], RUN)
        button.assert_not_called()
        patchable.assert_not_called()

    def test_patch_is_null_without_permission_or_with_the_setting_off(self):
        data, _, patchable = summary(button=None)
        self.assertIsNone(data["patch"])
        patchable.assert_not_called()

    def test_patch_is_null_when_the_button_has_no_site_path(self):
        for url in ("", "https://evil.example/x"):
            data, _, _ = summary(button={"url": url, "label": "x", "action_id": 1})
            self.assertIsNone(data["patch"])

    def test_patch_is_null_without_patchable_servers(self):
        data, _, _ = summary(patchable=())
        self.assertIsNone(data["patch"])

    def test_patch_is_null_when_the_viewer_sees_no_servers(self):
        data, button, _ = summary(ids=(), record=patch_record())
        self.assertIsNone(data["patch"])
        self.assertIsNone(data["patch_last"])
        button.assert_not_called()

    def test_a_failing_patch_piece_means_no_button_and_an_intact_answer(self):
        for kwargs in ({"button_error": RuntimeError("boom")}, {"patchable_error": RuntimeError("boom")}):
            data, _, _ = summary(**kwargs)
            self.assertIsNone(data["patch"])
            self.assertIn("state", data)
            self.assertIn("can_scan", data)

    def test_a_missing_patch_module_means_no_button(self):
        request = types.SimpleNamespace(method="GET", GET={})
        with mock.patch.object(views.vuln_hooks, "resource_patch_button", return_value=BUTTON, create=True), \
                mock.patch.dict(sys.modules):
            for key in [k for k in sys.modules if k.startswith(base.PACKAGE)]:
                del sys.modules[key]
            self.assertIsNone(views._resource_patch_offer(RESOURCE, PROFILE, request, "all"))

    def test_patch_last_shape_and_job_link(self):
        data, _, _ = summary(record=patch_record())
        self.assertEqual(data["patch_last"], {"at": AT, "by": "alice", "status": "success", "updated": 3,
                                              "restart_pending": 1, "failed": 0, "skipped": 2, "no_updates": 0,
                                              "job_url": "/job_detail/41/"})

    def test_patch_last_is_null_without_a_record_or_status_and_survives_bad_counts(self):
        self.assertIsNone(summary(record=None)[0]["patch_last"])
        self.assertIsNone(summary(record={"at": AT})[0]["patch_last"])
        last = summary(record=patch_record(counts={"updated": "x", "failed": -2}, job_id=None))[0]["patch_last"]
        self.assertEqual((last["updated"], last["failed"], last["job_url"]), (0, 0, ""))

    def test_run_one_stays_only_the_run(self):
        data, button, patchable = summary(run=RUN, query={"run": "1"})
        self.assertEqual(data, {"run": RUN})
        button.assert_not_called()
        patchable.assert_not_called()

    def test_run_kind_and_message_pass_through(self):
        data, _, _ = summary(run=dict(RUN, kind="scan", message="x"))
        self.assertEqual(data["run"]["kind"], "scan")
        self.assertEqual(data["run"]["message"], "x")

    def test_only_get_is_allowed(self):
        data, _, _ = summary(method="POST")
        self.assertEqual(data, {"error": "Use GET."})


class SettingsTests(unittest.TestCase):
    OK = {"exists": True, "enabled": True, "action_id": 9}
    MISSING = {"exists": False, "enabled": False, "action_id": None}

    def post(self, patching=True, was=True, server=OK, resource=OK, apply=None):
        request = types.SimpleNamespace(method="POST", POST={"x": "1"})
        clean = {"patching": patching, "scan_on_provision": False}
        if isinstance(resource, Exception):
            resource_state = mock.Mock(side_effect=resource)
        else:
            resource_state = mock.Mock(return_value=resource)
        apply = apply or mock.Mock()
        with mock.patch.object(views.store, "validate_settings", return_value=(clean, {})), \
                mock.patch.object(views.store, "save_settings"), \
                mock.patch.object(views.store, "get_settings", return_value={"patching": was}), \
                mock.patch.object(views.vuln_hooks, "apply_provision_setting", create=True), \
                mock.patch.object(views.vuln_hooks, "apply_patching_setting", apply, create=True), \
                mock.patch.object(views.vuln_hooks, "patch_action_state", return_value=server, create=True), \
                mock.patch.object(views.vuln_hooks, "resource_patch_action_state", resource_state, create=True), \
                mock.patch.object(views.vuln_hooks, "ensure_schedules", return_value=[], create=True), \
                mock.patch.object(views, "_schedule_rows", return_value=[]), \
                mock.patch.object(views, "_scanner_context", return_value={}), \
                mock.patch.object(views, "_fleet_url", return_value=""), \
                mock.patch.object(views, "reverse", return_value="/setup/"):
            return views.settings(request)[1], apply

    def test_a_missing_resource_action_is_created_when_the_setting_is_unchanged(self):
        ctx, apply = self.post(resource=self.MISSING)
        apply.assert_called_once_with(True, create_only=True)
        self.assertEqual(ctx["warnings"], [])

    def test_a_changed_setting_applies_without_create_only(self):
        _, apply = self.post(was=False)
        apply.assert_called_once_with(True, create_only=False)
        _, apply = self.post(patching=False, was=True)
        apply.assert_called_once_with(False, create_only=False)

    def test_everything_present_leaves_it_alone(self):
        _, apply = self.post()
        apply.assert_not_called()

    def test_an_unreadable_resource_state_does_not_trigger_an_apply(self):
        _, apply = self.post(resource=RuntimeError("boom"))
        apply.assert_not_called()

    def test_off_never_creates(self):
        _, apply = self.post(patching=False, was=False, resource=self.MISSING)
        apply.assert_not_called()

    def test_the_resource_action_state_is_in_the_context(self):
        ctx, _ = self.post()
        self.assertEqual(ctx["resource_patch_action"], self.OK)
        ctx, _ = self.post(resource=RuntimeError("boom"), was=False, patching=False)
        self.assertEqual(ctx["resource_patch_action"], self.MISSING)

    def test_a_failure_names_the_action_that_did_not_follow(self):
        boom = mock.Mock(side_effect=RuntimeError("one half failed"))
        ctx, _ = self.post(was=False, resource=self.MISSING, apply=boom)
        self.assertEqual(ctx["warnings"], [views.RESOURCE_PATCHING_ERROR])
        ctx, _ = self.post(was=False, server=self.MISSING, resource=self.OK, apply=boom)
        self.assertEqual(ctx["warnings"], [views.PATCHING_ERROR])
        ctx, _ = self.post(was=False, server=self.MISSING, resource=self.MISSING, apply=boom)
        self.assertEqual(ctx["warnings"], [views.PATCHING_ERROR, views.RESOURCE_PATCHING_ERROR])
        ctx, _ = self.post(was=False, apply=boom)
        self.assertEqual(ctx["warnings"], [views.PATCHING_ERROR])
        self.assertTrue(ctx["saved"])


def read(name):
    with open(os.path.join(TEMPLATES, name), encoding="utf-8") as handle:
        return handle.read()


class SourceTests(unittest.TestCase):
    def test_the_tab_anchor_opens_the_dialog_and_has_no_act(self):
        html = read("vuln_resource_tab.html")
        anchors = re.findall(r"<a [^>]*data-vuln-patch-all[^>]*>", html)
        self.assertEqual(len(anchors), 1)
        self.assertIn("open-dialog", anchors[0])
        self.assertIn("hidden", anchors[0])
        self.assertNotIn("data-vuln-act", anchors[0])
        self.assertLess(html.index('data-vuln-act="scan-all"'), html.index("<a class=\"btn btn-sm btn-outline-primary open-dialog\""))
        self.assertLess(html.index("<div data-vuln-run hidden>"), html.index("<div data-vuln-patch-last hidden>"))

    def test_the_search_page_has_neither_the_link_nor_the_line(self):
        html = read("vuln_fleet.html")
        self.assertNotIn("data-vuln-patch-all", html)
        self.assertNotIn("data-vuln-patch-last", html)

    def test_the_script_never_builds_html_from_stored_values(self):
        script = read("vuln_fleet_assets.html")
        self.assertNotIn("innerHTML", script)
        self.assertNotIn("insertAdjacentHTML", script)
        self.assertNotIn(".html(", script)
        self.assertIn("textContent", script)

    def test_the_script_guards_the_patch_line_and_link_to_resource_mode_and_site_paths(self):
        script = read("vuln_fleet_assets.html")
        self.assertIn("patch = scanUrl && data && data.patch ? data.patch : null;", script)
        self.assertIn("patchLast = scanUrl && data && data.patch_last ? data.patch_last : null;", script)
        self.assertIn('patch.url.charAt(0) === "/"', script)
        self.assertIn('String(patchLast.job_url).charAt(0) === "/"', script)

    def test_the_link_is_not_handled_by_the_script_and_the_hash_is_untouched(self):
        script = read("vuln_fleet_assets.html")
        section = script[script.index("function syncPatch"):script.index("function applyRun")]
        self.assertNotIn("location.hash", section)
        self.assertNotIn("preventDefault", section)
        self.assertNotIn(".on(", section)
        self.assertNotIn("addEventListener", section)


@unittest.skipUnless(HAVE_DJANGO, "Django is not installed")
class TabRenderTests(unittest.TestCase):
    def render(self, no_servers=False):
        import django.conf
        from django.template import Context, Engine
        if not django.conf.settings.configured:
            django.conf.settings.configure(USE_I18N=False)
        engine = Engine(dirs=[TEMPLATES, base.EXT], libraries={"i18n": "django.templatetags.i18n"})
        urls = {k: "/x/%s/" % k for k in ("summary", "cves", "cve_servers", "servers", "scan")}
        ctx = {"resource": RESOURCE, "no_servers": no_servers, "urls": urls,
               "shell": {"is_admin": False, "settings_url": "", "fleet_url": ""}, "csrf_token": "t"}
        return engine.get_template("vuln_resource_tab.html").render(Context(ctx))

    def test_the_rendered_tab_has_the_hidden_link_and_the_hidden_line(self):
        html = self.render()
        link = re.search(r"<a [^>]*data-vuln-patch-all[^>]*>[^<]*</a>", html).group(0)
        self.assertIn("Install updates on all servers", link)
        self.assertIn("hidden", link)
        self.assertNotIn("data-vuln-act", link)
        self.assertIn("<div data-vuln-patch-last hidden></div>", html)
        self.assertIn('data-vuln-nohash="1"', html)

    def test_no_link_for_a_viewer_without_servers(self):
        html = self.render(no_servers=True)
        self.assertNotIn('<a class="btn btn-sm btn-outline-primary open-dialog"', html)
        self.assertNotIn("<div data-vuln-patch-last", html)


if __name__ == "__main__":
    unittest.main()
