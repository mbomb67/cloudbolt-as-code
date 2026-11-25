"""
Checks for the Install updates part of the Vulnerabilities panel (views._panel_context, vuln_panel.html and the
settings view) without CloudBolt. The patch module and the two vuln_hooks functions the panel calls are stubbed
here, so these tests do not depend on the patch engine. The template tests need Django itself and are skipped
when it is not installed.

    python -B -m unittest discover -s extensions/XUI-wqrj0e3p/tests -p "test_*.py"
"""
import contextlib
import importlib
import os
import sys
import types
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

try:  # the real Django, imported before the stubs of test_vuln_fleet.py are put in and taken out again
    import django.conf
    import django.template
    HAVE_DJANGO = True
except ImportError:
    HAVE_DJANGO = False

import test_vuln_fleet as base  # noqa: E402  (builds the stubs and imports the extension)

PATCH_KEY = base.PACKAGE + ".vuln_patch"
VIEW_STUBS = {}


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

    class Q:
        def __init__(self, **kw):
            pass

        def __or__(self, other):
            return self

    stub("django.core")
    stub("django.core.exceptions", PermissionDenied=type("PermissionDenied", (Exception,), {}))
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
    stub("utilities.permissions", cbadmin_required=identity)
    stub(PATCH_KEY, eligibility=lambda server: "")
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
TEMPLATES = os.path.join(base.EXT, "io_cloudbolt_vulnerabilities", "templates")
AT = "2026-11-24T10:00:00Z"
BUTTON = {"url": "/actions/9/dialog/7/", "label": "Install Updates", "action_id": 9}
SUMMARY = {"vulns": 12, "vuln_fixable": 9, "vuln_critical": 1, "vuln_high": 2, "vuln_medium": 3, "vuln_low": 6,
           "kev": 0, "packages": 300, "os_name": "Amazon Linux 2023"}


def record(**over):
    data = {"at": AT, "by": "alice", "job_id": 41, "status": "updated", "before": {"vulns": 12, "vuln_fixable": 9},
            "after": {"vulns": 3, "vuln_fixable": 0}, "updated": 20, "failed": 0, "restarted": True,
            "restart_needed": True, "kernels_removed": 1, "duration_s": 600, "message": ""}
    data.update(over)
    return data


def context(state=None, active=None, windows=False, eligibility="", button=BUTTON, summary=SUMMARY, denied=False):
    """_panel_context with everything outside it replaced; returns (context, the patch_button mock)."""
    patch_button = mock.Mock(return_value=None if denied else button)
    server = base.make_server()
    profile = types.SimpleNamespace(is_cbadmin=False)
    with contextlib.ExitStack() as stack:
        enter = stack.enter_context
        enter(mock.patch.object(views, "_upgrade_store"))
        enter(mock.patch.object(views.vuln_db, "server_summary", return_value=dict(summary) if summary else None))
        enter(mock.patch.object(views.store, "read_state", return_value=state or {}))
        enter(mock.patch.object(views.vuln_jobs, "panel_active", return_value=active))
        enter(mock.patch.object(views.vuln_jobs, "can_scan", return_value=True))
        enter(mock.patch.object(views.vuln_jobs, "in_unassigned_group", return_value=False))
        enter(mock.patch.object(views.vuln_jobs, "is_windows", return_value=windows))
        enter(mock.patch.object(views.vuln_tools, "status", return_value={"ready": True, "ready_windows": True}))
        enter(mock.patch.object(views, "reverse", lambda name, args=None, **k: "/%s/%s/" % (name, (args or [""])[0])))
        enter(mock.patch.object(views.vuln_patch, "eligibility", return_value=eligibility))
        enter(mock.patch.object(views.vuln_hooks, "patch_button", patch_button, create=True))
        ctx = views._panel_context(server, profile, "REQ")
    return ctx, patch_button


def render(vuln, error=None):
    from django.template import Context, Engine
    if not django.conf.settings.configured:
        django.conf.settings.configure(USE_I18N=False)
    engine = Engine(dirs=[TEMPLATES], libraries={"i18n": "django.templatetags.i18n"})
    base_vuln = {"denied": False, "windows": False, "can_scan": True, "ready": True, "summary": SUMMARY,
                 "active": None, "patch": {"offer": False, "url": "", "label": ""}, "patch_last": None, "key": "k"}
    base_vuln.update(vuln)
    return engine.get_template("vuln_panel.html").render(Context({"vuln": base_vuln, "error": error}))


class PanelContextTests(unittest.TestCase):
    def test_offer_is_built_from_the_button_and_the_request_is_passed_on(self):
        ctx, button = context()
        self.assertEqual(ctx["vuln"]["patch"], {"offer": True, "url": BUTTON["url"], "label": "Install Updates"})
        self.assertEqual(button.call_args.args[2], "REQ")

    def test_no_offer_while_a_job_is_active_and_the_button_is_not_asked(self):
        ctx, button = context(active={"job_id": 5, "kind": "patch", "title": "Installing updates"})
        self.assertFalse(ctx["vuln"]["patch"]["offer"])
        button.assert_not_called()

    def test_offer_for_an_eligible_windows_server(self):
        ctx, button = context(windows=True)
        self.assertEqual(ctx["vuln"]["patch"], {"offer": True, "url": BUTTON["url"], "label": "Install Updates"})
        button.assert_called_once()

    def test_no_offer_for_a_windows_server_that_is_not_eligible(self):
        ctx, button = context(windows=True, eligibility="unsupported_windows")
        self.assertFalse(ctx["vuln"]["patch"]["offer"])
        button.assert_not_called()

    def test_no_offer_for_windows_while_a_job_is_active(self):
        ctx, button = context(windows=True, active={"job_id": 5, "kind": "patch", "title": "Installing updates"})
        self.assertFalse(ctx["vuln"]["patch"]["offer"])
        button.assert_not_called()

    def test_no_offer_when_the_server_is_not_eligible(self):
        ctx, button = context(eligibility="unsupported_os")
        self.assertFalse(ctx["vuln"]["patch"]["offer"])
        button.assert_not_called()

    def test_no_offer_when_the_user_may_not_run_the_action(self):
        ctx, _ = context(denied=True)
        self.assertFalse(ctx["vuln"]["patch"]["offer"])

    def test_a_failing_patch_module_means_no_button_not_a_broken_panel(self):
        none = {"offer": False, "url": "", "label": ""}
        server, profile = base.make_server(), types.SimpleNamespace()
        with mock.patch.object(views.vuln_patch, "eligibility", side_effect=RuntimeError("boom")):
            self.assertEqual(views._patch_offer(server, profile, None), none)
        with mock.patch.object(views.vuln_patch, "eligibility", return_value=""), \
                mock.patch.object(views.vuln_hooks, "patch_button", side_effect=RuntimeError("boom"), create=True):
            self.assertEqual(views._patch_offer(server, profile, None), none)

    def test_patch_last_carries_the_record_and_the_job_link(self):
        ctx, _ = context(state={"patch": record()})
        last = ctx["vuln"]["patch_last"]
        self.assertEqual(last["status"], "updated")
        self.assertEqual(last["before"], {"vulns": 12, "vuln_fixable": 9})
        self.assertEqual(last["job_url"], "/job_detail/41/")

    def test_no_patch_record_gives_none(self):
        ctx, _ = context(state={})
        self.assertIsNone(ctx["vuln"]["patch_last"])

    def test_the_key_changes_with_the_patch_job_id(self):
        first, _ = context(state={"patch": record(job_id=41)})
        second, _ = context(state={"patch": record(job_id=42)})
        none, _ = context(state={})
        self.assertNotEqual(first["vuln"]["key"], second["vuln"]["key"])
        self.assertNotEqual(first["vuln"]["key"], none["vuln"]["key"])


@unittest.skipUnless(HAVE_DJANGO, "Django is not installed")
class TemplateTests(unittest.TestCase):
    ANCHOR = 'class="btn btn-sm btn-outline-primary open-dialog" href="/actions/9/dialog/7/"'

    def offer(self, **over):
        return dict({"patch": {"offer": True, "url": BUTTON["url"], "label": "Install Updates"}}, **over)

    def note(self, html):
        return html.split("data-vuln-patch")[1].split('class="vuln-bar"')[0]

    def test_the_anchor_opens_the_dialog_in_the_toolbar(self):
        html = render(self.offer())
        self.assertEqual(html.count(self.ANCHOR), 1)
        self.assertGreater(html.index(self.ANCHOR), html.index('class="vuln-bar"'))

    def test_the_anchor_is_in_the_first_scan_section_too(self):
        html = render(self.offer(summary=None))
        self.assertEqual(html.count(self.ANCHOR), 1)
        self.assertIn("No scan yet", html)

    def test_no_anchor_when_not_offered_or_when_active(self):
        self.assertNotIn("open-dialog", render({}))
        self.assertNotIn("open-dialog", render(self.offer(active={"job_id": 5, "kind": "scan"})))
        self.assertNotIn("open-dialog", render(self.offer(summary=None, active={"job_id": 5, "kind": "scan"})))

    def test_the_banner_title_follows_the_kind(self):
        patch = render({"active": {"job_id": 5, "kind": "patch", "started": AT, "message": "Installing"}})
        scan = render({"active": {"job_id": 5, "kind": "scan", "started": AT}})
        check = render({"active": {"job_id": 5, "kind": "scan"}, "windows": True})
        self.assertIn("Installing updates", patch)
        self.assertNotIn("Scan running", patch)
        self.assertIn("Scan running", scan)
        self.assertIn("Check running", check)

    def test_updated_line(self):
        html = render({"patch_last": dict(record(), job_url="/job_detail/41/")})
        self.assertIn('Updated <span data-vuln-time="%s" data-vuln-fmt="when">' % AT, html)
        self.assertIn("by alice.", html)
        self.assertIn("Known vulnerabilities 12 &rarr; 3.", html)
        self.assertIn("With a fix 9 &rarr; 0.", html)
        self.assertIn('<a class="vuln-link" href="/job_detail/41/">View job</a>', html)
        self.assertNotIn("A restart is pending", html)

    def test_null_numbers_are_dashes(self):
        html = render({"patch_last": dict(record(before=None, after=None), job_url="")})
        self.assertIn("Known vulnerabilities &ndash; &rarr; &ndash;.", html)
        self.assertIn("With a fix &ndash; &rarr; &ndash;.", html)
        self.assertNotIn("View job", self.note(html))
        html = render({"patch_last": dict(record(after=None), job_url="")})
        self.assertIn("Known vulnerabilities 12 &rarr; &ndash;.", html)

    def test_no_updates_line_in_both_copies(self):
        for extra in ({}, {"summary": None}):
            html = render(dict({"patch_last": dict(record(status="no_updates"), job_url="/job_detail/41/")}, **extra))
            self.assertIn('Checked <span data-vuln-time="%s" data-vuln-fmt="when">' % AT, html)
            self.assertIn("by alice.", html)
            self.assertIn("No updates were available.", html)
            self.assertIn("Known vulnerabilities 12 &rarr; 3.", html)
            self.assertIn('<a class="vuln-link" href="/job_detail/41/">View job</a>', html)
            self.assertNotIn("Updated <span", html)

    def test_updated_line_says_restarted_and_kernels_removed(self):
        html = self.note(render({"patch_last": dict(record(restarted=True, kernels_removed=2), job_url="")}))
        self.assertIn("The server was restarted. 2 old kernel packages were removed.", html)
        html = self.note(render({"patch_last": dict(record(restarted=False, kernels_removed=0), job_url="")}))
        self.assertNotIn("restarted", html)
        self.assertNotIn("kernel", html)
        html = self.note(render({"patch_last": dict(record(restarted=True, kernels_removed=None), job_url="")}))
        self.assertIn("The server was restarted.", html)
        self.assertNotIn("kernel", html)
        html = render({"summary": None, "patch_last": dict(record(restarted=True, kernels_removed=1), job_url="")})
        self.assertIn("The server was restarted. 1 old kernel package was removed.", html)
        for n in (1, 6):
            for extra in ({}, {"summary": None}):
                html = render(dict({"patch_last": dict(record(kernels_removed=n), job_url="")}, **extra))
                word = "package was" if n == 1 else "packages were"
                self.assertIn("%d old kernel %s removed." % (n, word), html)

    def test_restart_pending_line(self):
        html = render({"patch_last": dict(record(status="restart_pending"), job_url="")})
        self.assertIn("Updates installed <span", html)
        self.assertIn("A restart is pending.", html)
        self.assertIn("Known vulnerabilities 12 &rarr; 3.", html)

    def test_failed_line_escapes_the_stored_message(self):
        html = render({"patch_last": dict(record(status="failed", message="<script>alert(1)</script> & more"),
                                          job_url="")})
        self.assertIn("Updating failed <span", html)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt; &amp; more", html)
        self.assertNotIn("<script>alert(1)", html)
        self.assertNotIn("Known vulnerabilities 12", self.note(html))

    def test_the_note_shows_without_a_scan_summary(self):
        html = render({"summary": None, "patch_last": dict(record(status="failed", message="repo unreachable"),
                                                           job_url="")})
        self.assertEqual(html.count("data-vuln-patch"), 1)
        self.assertIn("Updating failed <span", html)
        self.assertIn("repo unreachable", html)
        html = render({"summary": None, "patch_last": dict(record(after=None), job_url="")})
        self.assertEqual(html.count("data-vuln-patch"), 1)
        self.assertIn("Known vulnerabilities 12 &rarr; &ndash;.", html)

    def test_the_note_is_once_in_the_scanned_case(self):
        html = render({"patch_last": dict(record(), job_url="")})
        self.assertEqual(html.count("data-vuln-patch"), 1)

    def test_the_user_name_is_escaped(self):
        html = render({"patch_last": dict(record(by="<b>x</b>"), job_url="")})
        self.assertIn("by &lt;b&gt;x&lt;/b&gt;.", html)
        self.assertNotIn("<b>x</b>", html)


class SettingsViewTests(unittest.TestCase):
    STATE = {"exists": True, "enabled": True, "action_id": 9}
    MISSING = {"exists": False, "enabled": False, "action_id": None}

    def post(self, apply_patching=None, patching=True, was=False, state=None, state_error=False, resource_state=None):
        request = types.SimpleNamespace(method="POST", POST={"x": "1"})
        clean = {"patching": patching, "scan_on_provision": False}
        if state_error:
            get_state = mock.Mock(side_effect=RuntimeError("boom"))
        else:
            get_state = mock.Mock(return_value=state or self.STATE)
        get_resource = mock.Mock(return_value=resource_state or self.STATE)
        with mock.patch.object(views.store, "validate_settings", return_value=(clean, {})),                 mock.patch.object(views.store, "save_settings"),                 mock.patch.object(views.store, "get_settings", return_value={"patching": was}),                 mock.patch.object(views.vuln_hooks, "apply_provision_setting", create=True),                 mock.patch.object(views.vuln_hooks, "apply_patching_setting", apply_patching or mock.Mock(),
                                  create=True),                 mock.patch.object(views.vuln_hooks, "patch_action_state", get_state, create=True),                 mock.patch.object(views.vuln_hooks, "resource_patch_action_state", get_resource, create=True),                 mock.patch.object(views.vuln_hooks, "ensure_schedules", return_value=[], create=True),                 mock.patch.object(views, "_schedule_rows", return_value=[]),                 mock.patch.object(views, "_scanner_context", return_value={}),                 mock.patch.object(views, "_fleet_url", return_value=""),                 mock.patch.object(views, "reverse", return_value="/setup/"):
            return views.settings(request)[1]

    def test_off_to_on_applies_it(self):
        apply = mock.Mock()
        ctx = self.post(apply, patching=True, was=False)
        apply.assert_called_once_with(True, create_only=False)
        self.assertEqual(ctx["warnings"], [])

    def test_on_to_on_with_both_actions_present_leaves_them_alone(self):
        apply = mock.Mock()
        self.post(apply, patching=True, was=True)
        apply.assert_not_called()

    def test_on_to_on_with_the_action_missing_applies_it_create_only(self):
        apply = mock.Mock()
        self.post(apply, patching=True, was=True, state=self.MISSING)
        apply.assert_called_once_with(True, create_only=True)

    def test_on_to_on_with_only_the_resource_action_missing_applies_it_create_only(self):
        apply = mock.Mock()
        self.post(apply, patching=True, was=True, resource_state=self.MISSING)
        apply.assert_called_once_with(True, create_only=True)

    def test_on_to_off_applies_false(self):
        apply = mock.Mock()
        self.post(apply, patching=False, was=True)
        apply.assert_called_once_with(False, create_only=False)

    def test_off_to_off_leaves_it_alone(self):
        apply = mock.Mock()
        self.post(apply, patching=False, was=False)
        apply.assert_not_called()

    def test_a_failure_is_a_warning_and_the_save_stands(self):
        # The server action did not follow (it does not exist after the failed apply); the resource action did.
        ctx = self.post(mock.Mock(side_effect=RuntimeError("no hook")), state=self.MISSING)
        self.assertEqual(ctx["warnings"], [views.PATCHING_ERROR])
        self.assertTrue(ctx["saved"])

    def test_the_action_state_is_in_the_context(self):
        self.assertEqual(self.post()["patch_action"], self.STATE)
        self.assertEqual(self.post(state_error=True, patching=False, was=False)["patch_action"], self.MISSING)


if __name__ == "__main__":
    unittest.main()
