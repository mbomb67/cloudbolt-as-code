"""
Checks for the admin Servers view: the shell (vuln_fleet.html), the resource tab (vuln_resource_tab.html, which must
not get any of it) and the script they share (vuln_fleet_assets.html), without CloudBolt or a browser. The template
tests need Django itself and are skipped when it is not installed; the script checks are text checks, plus
`node --check` on the script body when node is on the PATH (skipped otherwise).

    python -B -m unittest discover -s extensions/XUI-wqrj0e3p/tests -p "test_vuln_fleet_servers_page.py"
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import test_vuln_patch_panel as panel  # noqa: E402  (imports the real Django before other tests stub it)

base = panel.base
HAVE_DJANGO = panel.HAVE_DJANGO
TEMPLATES = panel.TEMPLATES
ASSETS = os.path.join(TEMPLATES, "vuln_fleet_assets.html")

NEW_ATTRS = ("data-server-filters-url", "data-servers-select-url", "data-servers-patch-url")
ADMIN_URLS = {"summary": "/x/summary/", "cves": "/x/cves/", "cve_servers": "/x/cve_servers/", "servers": "/x/servers/",
              "server_filters": "/x/filters/", "servers_select": "/x/select/", "servers_patch": "/x/patch/"}


def read_script():
    with open(ASSETS, encoding="utf-8") as handle:
        text = handle.read()
    start = text.index("<script>") + len("<script>")
    return text[start:text.index("</script>")]


def render(name, ctx, parent=False):
    import django.conf
    from django.template import Context, Engine
    if not django.conf.settings.configured:
        django.conf.settings.configure(USE_I18N=False)
    dirs = [TEMPLATES, base.EXT]
    tmp = None
    if parent:  # the CloudBolt page layout the shell extends, reduced to its blocks
        tmp = tempfile.mkdtemp()
        os.makedirs(os.path.join(tmp, "v5", "default"))
        with open(os.path.join(tmp, "v5", "default", "body.html"), "w") as handle:
            handle.write("{% block title %}{% endblock %}{% block topnav %}{% endblock %}"
                         "{% block breadcrumb-item %}{% endblock %}{% block content %}{% endblock %}")
        dirs.insert(0, tmp)
    try:
        engine = Engine(dirs=dirs, libraries={"i18n": "django.templatetags.i18n"})
        return engine.get_template(name).render(Context(ctx))
    finally:
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)


@unittest.skipUnless(HAVE_DJANGO, "Django is not installed")
class ShellTests(unittest.TestCase):
    def shell(self, urls=None):
        return render("vuln_fleet.html", {"urls": ADMIN_URLS if urls is None else urls, "settings_url": "/s/",
                                         "page_error": "", "csrf_token": "tok123"}, parent=True)

    def test_root_carries_the_new_urls_and_the_csrf_token(self):
        html = self.shell()
        root = re.search(r'<div id="vuln-root"[^>]*>', html).group(0)
        self.assertIn('data-server-filters-url="/x/filters/"', root)
        self.assertIn('data-servers-select-url="/x/select/"', root)
        self.assertIn('data-servers-patch-url="/x/patch/"', root)
        self.assertIn('data-csrf="tok123"', root)
        self.assertIn('data-servers-url="/x/servers/"', root)

    def test_missing_urls_render_empty_so_the_script_stays_in_plain_mode(self):
        html = self.shell(urls={k: v for k, v in ADMIN_URLS.items() if k in ("summary", "cves", "cve_servers", "servers")})
        self.assertIn('data-server-filters-url=""', html)

    def servers_pane(self):
        html = self.shell()
        return re.search(r'<section data-vuln-pane="servers".*?</section>', html, re.S).group(0)

    def test_servers_pane_has_four_selects_both_checkboxes_and_the_search_text(self):
        pane = self.servers_pane()
        self.assertEqual(re.findall(r'data-vuln-filter="(\w+)"', pane), ["group", "os_build", "owner", "environment"])
        self.assertEqual(len(re.findall(r"<select ", pane)), 4)
        for label in ("Group: all", "OS build: all", "Owner: all", "Environment: all"):
            self.assertIn(label, pane)
        self.assertIn("data-vuln-kev", pane)
        self.assertIn("data-vuln-fix", pane)
        self.assertIn("With a fix only", pane)
        self.assertIn('placeholder="Search hostname, group, owner, environment or OS"', pane)
        self.assertEqual(len(re.findall(r'type="checkbox"', pane)), 2)

    def test_servers_pane_has_the_places_the_script_draws_into(self):
        pane = self.servers_pane()
        for marker in ("data-vuln-bulk", "data-vuln-selectline", 'data-vuln-act="clear-filters"', "data-vuln-table",
                       "data-vuln-pager"):
            self.assertIn(marker, pane)
        self.assertRegex(pane, r'<button[^>]*data-vuln-act="clear-filters"[^>]*hidden')

    def test_every_filter_select_has_a_label(self):
        pane = self.servers_pane()
        for sid in re.findall(r'<select [^>]*id="([^"]+)"', pane):
            self.assertIn('for="%s"' % sid, pane)

    def test_the_cve_pane_is_unchanged(self):
        html = self.shell()
        cves = re.search(r'<section data-vuln-pane="cves".*?</section>', html, re.S).group(0)
        self.assertNotIn("data-vuln-filter", cves)
        self.assertIn("data-vuln-sev", cves)


@unittest.skipUnless(HAVE_DJANGO, "Django is not installed")
class ResourceTabTests(unittest.TestCase):
    def tab(self):
        import types
        urls = {k: "/x/%s/" % k for k in ("summary", "cves", "cve_servers", "servers", "scan")}
        ctx = {"resource": types.SimpleNamespace(id=5, name="web"), "no_servers": False, "urls": urls,
               "shell": {"is_admin": False, "settings_url": "", "fleet_url": ""}, "csrf_token": "t"}
        return render("vuln_resource_tab.html", ctx)

    def test_the_resource_tab_has_none_of_the_admin_controls(self):
        html = self.tab()
        markup = re.sub(r"<script\b[^>]*>.*?</script\s*>", "", html, flags=re.S | re.I)   # the shared script names the attributes
        root = re.search(r'<div id="vuln-root"[^>]*>', markup).group(0)
        for attr in NEW_ATTRS:
            self.assertNotIn(attr, root)
        for marker in ("data-vuln-filter", "data-vuln-bulk", "data-vuln-selectline", "clear-filters"):
            self.assertNotIn(marker, markup)
        servers = re.search(r'<section data-vuln-pane="servers".*?</section>', markup, re.S).group(0)
        self.assertNotIn("<select", servers)
        self.assertNotIn("data-vuln-fix", servers)   # the Vulnerabilities pane keeps its own "With a fix" box
        self.assertIn('placeholder="Search hostname"', servers)
        self.assertIn('data-vuln-nohash="1"', root)

    def test_the_resource_tab_still_carries_what_it_always_had(self):
        html = self.tab()
        self.assertIn('data-vuln-scan-url="/x/scan/"', html)
        self.assertIn("data-vuln-patch-all", html)


class ScriptTextTests(unittest.TestCase):
    def setUp(self):
        self.script = read_script()

    def test_no_markup_is_built_from_strings(self):
        for banned in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", ".html("):
            self.assertNotIn(banned, self.script, banned)

    def test_no_template_marker_inside_the_script(self):
        self.assertNotIn("{{", self.script)
        self.assertNotIn("{%", self.script)
        self.assertNotIn("%}", self.script)

    def test_session_storage_only_inside_try(self):
        hits = [m.start() for m in re.finditer("sessionStorage", self.script)]
        self.assertTrue(hits, "the persistence is missing")
        for pos in hits:
            head = self.script[:pos]
            func = head[head.rindex("function "):]
            self.assertIn("try {", func, "sessionStorage outside a try block")
            self.assertGreater(func.rindex("try {"), -1)
        # and every try has its catch
        self.assertEqual(self.script.count("try {"), len(re.findall(r"\bcatch \(e\)", self.script)))

    def test_links_from_the_server_go_through_the_path_check(self):
        self.assertIn('typeof url === "string" && url.charAt(0) === "/" && url.charAt(1) !== "/"', self.script)
        self.assertIn("goodPath((serverBulk && serverBulk.run_url) || patchUrl)", self.script)
        self.assertIn("goodPath((serverBulk && serverBulk.select_url) || selectUrl)", self.script)
        self.assertIn("var url = goodPath(running.job_url)", self.script)
        self.assertIn("var url = goodPath(msg.url)", self.script)
        # an address is only ever set from a value that passed a check
        for m in re.finditer(r'setAttribute\("href", (\w+)\)', self.script):
            self.assertIn(m.group(1), ("url", "settings", "run.job_url", "patchLast.job_url", "patch.url"))

    def test_the_admin_controls_are_gated_on_the_filters_url(self):
        self.assertIn('var admin = !!rootEl.getAttribute("data-server-filters-url")', self.script)
        # the resource-tab column set is untouched
        self.assertIn('["Hostname", "OS", { t: "Critical" }, { t: "High" }, { t: "Exploited" }, "Software list"]', self.script)
        self.assertIn('build: function (rows, data) { return admin ? buildServersAdmin(rows, data) : buildServers(rows); }',
                      self.script)

    def test_sort_headers_are_buttons_with_aria_sort_on_the_th(self):
        section = self.script[self.script.index("function sortHead"):self.script.index("function buildServersAdmin")]
        self.assertIn('el("button", "vuln-sortbtn")', section)
        self.assertIn('th.setAttribute("aria-sort"', section)
        self.assertIn('"descending"', section)

    def test_checkboxes_are_labelled_and_the_header_one_can_be_indeterminate(self):
        self.assertIn('cb.setAttribute("aria-label", "Select " + host)', self.script)
        self.assertIn("head.indeterminate", self.script)
        self.assertIn('cb.setAttribute("title", why)', self.script)

    def test_the_sort_keys_match_the_contract(self):
        keys = re.search(r"var SORT_KEYS = \[(.*?)\]", self.script, re.S).group(1)
        self.assertEqual(re.findall(r'"(\w+)"', keys),
                         ["hostname", "group", "environment", "os", "owner", "critical", "high", "fixable", "exploited",
                          "inventory"])

    def test_column_order(self):
        cols = re.search(r"var SERVER_COLS = \[(.*?)\];", self.script, re.S).group(1)
        self.assertEqual(re.findall(r't: "([^"]+)"', cols),
                         ["Hostname", "Group", "Environment", "OS", "Owner", "Critical", "High", "With a fix", "Exploited",
                          "Software list"])

    def test_the_post_sends_ids_restart_and_the_token(self):
        section = self.script[self.script.index("function runBulk"):self.script.index("// ---- events ----")]
        self.assertIn('method: "POST"', section)
        self.assertIn('"X-CSRFToken": csrf', section)
        self.assertIn('ids: ids.join(",")', section)
        self.assertIn("restart: restart ? 1 : 0", section)

    def test_the_script_has_no_dialog_anchor_or_browser_confirm(self):
        self.assertNotIn("open-dialog", self.script)
        self.assertNotIn("confirm(", self.script.replace("bulk-confirm", "").replace("renderConfirm", "")
                         .replace("cancelConfirm", "").replace("openConfirm", "").replace("confirmOpen", "")
                         .replace("confirmBox", "").replace("confirmRefs", "").replace("confirmRestart", ""))

    def test_the_script_parses(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("node is not on the PATH")
        fd, path = tempfile.mkstemp(suffix=".js")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(self.script)
            result = subprocess.run([node, "--check", path], capture_output=True, text=True, timeout=60)
            self.assertEqual(result.returncode, 0, result.stderr)
        finally:
            os.unlink(path)


HARNESS = """
var window = { jQuery: null };
var document = { readyState: "complete", querySelectorAll: function () { return []; }, addEventListener: function () {} };
%s
"""
CASES = r"""
var t = globalThis.__t, out = {};
out.goodPath = [t.goodPath("/jobs/3/"), t.goodPath("//x"), t.goodPath("/" + String.fromCharCode(92) + "x"), t.goodPath("http://x"), t.goodPath(null)];
out.waiting = t.approvalMessage({ state: "approval", order_id: 7, order_url: "/orders/7/", submitted: true });
out.approved = t.approvalMessage({ state: "approval", order_id: 7, order_url: "/orders/7/", submitted: true, approved: true });
out.notSubmitted = t.approvalMessage({ state: "approval", order_id: 7, order_url: "/orders/7/", submitted: false });
out.badUrl = t.approvalMessage({ state: "approval", order_id: 7, order_url: "//evil/" }).url;
out.cap = t.capMessage(30);
out.sentences = [t.confirmSentence(3, false), t.confirmSentence(1, false), t.confirmSentence(3, true), t.confirmSentence(1, true)];
out.sigApproval = t.confirmSignature(["a"], false, false) !== t.confirmSignature(["a"], false, true);
out.sigSame = t.confirmSignature(["a", "b"], false) === t.confirmSignature(["a", "b"], false);
out.sigNames = t.confirmSignature(["a", "b"], false) !== t.confirmSignature(["a", "c"], false);
out.sigBusy = t.confirmSignature(["a"], false) !== t.confirmSignature(["a"], true);
out.sigOrder = t.confirmSignature(["a", "b"], false) !== t.confirmSignature(["ab"], false);
var q1 = JSON.stringify(t.serverQuery({ q: "", page: 1, kev: false, fix: false, group: "A", environment: "", os_build: "", owner: "", sort: "", dir: "asc" }, false));
var q2 = JSON.stringify(t.serverQuery({ q: "", page: 1, kev: false, fix: false, group: "B", environment: "", os_build: "", owner: "", sort: "", dir: "asc" }, false));
out.staleOnFilter = t.staleAnswer(q1, q2);
out.freshSame = t.staleAnswer(q1, q1);
out.pageIgnored = t.serverQuery({ q: "x", page: 3, kev: false, fix: false, group: "", environment: "", os_build: "", owner: "", sort: "", dir: "asc" }, false).page;
console.log(JSON.stringify(out));
"""


class ScriptBehaviorTests(unittest.TestCase):
    """The script's pure helpers, run in node (no DOM: the stateful wiring is checked as text below)."""

    @classmethod
    def setUpClass(cls):
        node = shutil.which("node")
        if not node:
            raise unittest.SkipTest("node is not on the PATH")
        script = read_script()
        tail = script.rindex("})();")
        names = "goodPath, approvalMessage, capMessage, confirmSignature, confirmSentence, staleAnswer, serverQuery"
        exports = "globalThis.__t = { " + ", ".join("%s: %s" % (n, n) for n in names.split(", ")) + " };" + chr(10)
        script = script[:tail] + exports + script[tail:]
        fd, path = tempfile.mkstemp(suffix=".js")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(HARNESS % script + CASES)
            result = subprocess.run([node, path], capture_output=True, text=True, timeout=60)
        finally:
            os.unlink(path)
        if result.returncode != 0:
            raise AssertionError(result.stderr)
        import json
        cls.out = json.loads(result.stdout.strip().splitlines()[-1])

    def test_goodpath_rejects_protocol_relative_and_backslash(self):
        self.assertEqual(self.out["goodPath"], ["/jobs/3/", "", "", "", ""])

    def test_the_three_approval_texts(self):
        self.assertEqual(self.out["waiting"]["text"], "Order 7 is waiting for approval before the updates start.")
        self.assertEqual(self.out["waiting"]["linkText"], "View order")
        self.assertEqual(self.out["approved"]["text"], "Order 7 was approved and the updates are starting.")
        self.assertEqual(self.out["approved"]["linkText"], "View order")
        self.assertEqual(self.out["notSubmitted"]["text"],
                         "Install updates was added to your current order. Submit the order to start it.")
        self.assertEqual(self.out["notSubmitted"]["linkText"], "Open current order")
        self.assertEqual(self.out["notSubmitted"]["url"], "/orders/7/")
        self.assertEqual(self.out["badUrl"], "")

    def test_the_confirmation_sentences(self):
        plain, single, approval, approval_one = self.out["sentences"]
        self.assertEqual(plain, "Install all available updates on 3 servers, one after another, in one job.")
        self.assertEqual(single, "Install all available updates on 1 server.")
        tail = (". This action needs approval: the request becomes an order and each server is updated by its own job "
                "once it is approved.")
        self.assertEqual(approval, "Install all available updates on 3 servers" + tail)
        self.assertEqual(approval_one, "Install all available updates on 1 server" + tail)
        self.assertTrue(self.out["sigApproval"])

    def test_the_cap_message(self):
        self.assertEqual(self.out["cap"], "At most 30 servers can be selected.")

    def test_a_late_select_answer_is_stale_after_a_filter_change(self):
        self.assertTrue(self.out["staleOnFilter"])
        self.assertFalse(self.out["freshSame"])

    def test_the_confirmation_panel_is_rebuilt_only_when_what_it_shows_changes(self):
        self.assertTrue(self.out["sigSame"])
        self.assertTrue(self.out["sigNames"])
        self.assertTrue(self.out["sigBusy"])
        self.assertTrue(self.out["sigOrder"])


class WiringTextTests(unittest.TestCase):
    """What a DOM-less test cannot run is pinned as text: the restart toggle is recorded, a redraw keeps an open
    panel, and a filter change drops the pending select-all request."""

    def setUp(self):
        self.script = read_script()

    def test_the_restart_choice_is_recorded_and_the_panel_is_not_rebuilt_when_unchanged(self):
        self.assertIn('on("change", "#vuln-bulk-restart", function () { confirmRestart = this.checked; })', self.script)
        section = self.script[self.script.index("function renderConfirm"):self.script.index("function renderMsg")]
        self.assertIn("confirmRefs.sig === sig", section)
        self.assertIn("restart.checked = confirmRestart", section)

    def test_a_filter_change_aborts_the_pending_select_all(self):
        section = self.script[self.script.index("function resetSelection"):self.script.index("function afterServers")]
        self.assertIn("old.abort()", section)
        self.assertIn("selectBusy = false", section)
        self.assertIn("staleAnswer(key,", self.script)

    def test_the_select_answer_that_is_unavailable_keeps_the_selection(self):
        section = self.script[self.script.index("function selectAll"):self.script.index("function openConfirm")]
        self.assertLess(section.index("data.unavailable"), section.index("selection.clear()"))
        self.assertIn("The results are being updated. Try again in a moment.", section)

    def test_a_failed_check_keeps_the_selection(self):
        section = self.script[self.script.index("function afterServers"):self.script.index("function clearFilters")]
        self.assertNotIn("selection.clear()", section)
        self.assertIn("were removed from the selection because they can no longer be updated", section)

    def test_the_sort_title_uses_the_displayed_label(self):
        self.assertIn('"Sort by " + col.t)', self.script)


if __name__ == "__main__":
    unittest.main()
