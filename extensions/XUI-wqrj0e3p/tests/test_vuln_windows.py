"""
Checks for the Windows scan path of the Vulnerabilities XUI (vuln_scan_windows.ps1, and the Windows branches
of vuln_jobs.py and vuln_fleet.py) without CloudBolt. The CloudBolt stubs and the imported modules are the
ones test_vuln_fleet.py builds; vuln_msrc (the Microsoft data) is real but its entry points are replaced
where a test needs a canned answer. Nothing here runs on a real Windows server.

    python -B -m unittest discover -s extensions/XUI-wqrj0e3p/tests -p "test_*.py"
"""
import base64
import contextlib
import gzip
import json
import os
import sys
import importlib
import shutil
import subprocess
import tempfile
import types
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import test_vuln_fleet as base  # noqa: E402  (builds the stubs and imports the extension)

MODS = base.MODS
store, vmatch, db, tools, repo, hooks, jobs, fleet = (MODS[n] for n in (
    "vuln_store", "vuln_match", "vuln_db", "vuln_tools", "vuln_repo", "vuln_hooks", "vuln_jobs", "vuln_fleet"))
msrc = jobs.vuln_msrc
make_server = base.make_server

MS_STAMP = "2026-11-24.0a1b2c3d4e"   # a Microsoft index stamp: a date, a dot, a hash
GRYPE_STAMP = "2026-11-23T05:00:00Z"

OUTPUT = "\r\n".join([
    "CBVULN|os|Microsoft Windows NT 10.0.26100.0",
    "CBVULN|current_build|26100",
    "CBVULN|ubr|33650",
    "CBVULN|installation_type|Server",
    "CBVULN|edition_id|ServerDatacenter",
    "CBVULN|display_version|24H2",
    "CBVULN|product_name|Windows Server 2022 Datacenter",
    "CBVULN|hotfixes|KB5131077,KB5044284,KB5042881",
    "CBVULN|done|1",
    "",
])


def windows_server(**over):
    family = types.SimpleNamespace(get_base_name=lambda: "Windows", name="Windows")
    data = dict(os_family=family, is_windows=lambda: True, get_credentials=lambda **kw: {"username": "Administrator",
                                                                                         "password": "secret"})
    data.update(over)
    return make_server(**data)


def finding(cve="CVE-2026-0001", kev=True, severity="Critical", fix="10.0.26100.33822", kb="KB5131080"):
    return {
        "pkg_name": "Windows Server 2025", "pkg_type": "windows", "pkg_version": "10.0.26100.33650", "path": kb,
        "vuln_id": cve, "cves": [{"cve": cve, "kev": kev}], "severity": severity,
        "sev_rank": vmatch.sev_rank(severity), "fix_state": "fixed", "fix_version": fix, "kev": kev, "epss": None,
        "risk": None, "data_source": "https://msrc.microsoft.com/update-guide/vulnerability/" + cve,
    }


FACTS = {"kind": "windows-build", "current_build": "26100", "ubr": 33650, "build": "10.0.26100.33650",
         "installation_type": "Server", "edition_id": "ServerDatacenter", "display_version": "24H2",
         "product_name": "Windows Server 2022 Datacenter", "hotfixes": ["KB5131077", "KB5044284", "KB5042881"]}


def matched(*findings):
    return {"matches": list(findings), "db_built": MS_STAMP, "track": "hotpatch", "product": "Windows Server 2025"}


# ---------------------------------------------------------------- the script
class ScriptTests(unittest.TestCase):
    def test_the_sent_text_is_plain_ascii_without_comments_and_ends_exit_0(self):
        script = jobs.build_windows_script()
        self.assertTrue(script.isascii())
        self.assertTrue(script.endswith("exit 0\n"))
        self.assertNotIn("\r", script)
        for line in script.split("\n"):
            self.assertFalse(line.startswith("#"), line)
            self.assertEqual(line, line.strip())
        self.assertNotIn("{" + "{", script)
        self.assertNotIn("}" + "}", script)

    def test_everything_is_in_a_try_and_nothing_goes_to_stderr(self):
        script = jobs.build_windows_script()
        self.assertEqual(script.count("try{"), script.count("}catch{"))
        self.assertGreaterEqual(script.count("try{"), 2)
        for forbidden in ("Write-Error", "Write-Warning", "Write-Host", "[Console]::Error", "throw ", "2>"):
            self.assertNotIn(forbidden, script)
        self.assertIn("$ErrorActionPreference='Stop'", script)
        # The build is reported even when the installed updates cannot be read: the update list has its own try.
        self.assertLess(script.index("CurrentBuild"), script.index("Get-HotFix"))
        self.assertIn("HKLM:\\SOFTWARE\\Microsoft\\Windows NT\\CurrentVersion", script)

    def test_it_prints_exactly_the_keys_the_parser_reads(self):
        script = jobs.build_windows_script()
        for key in ("os", "current_build", "ubr", "installation_type", "edition_id", "display_version",
                    "product_name", "hotfixes", "reason", "note", "done"):
            self.assertIn(key, jobs.WINDOWS_KEYS)
            self.assertIn("Cb " + key + " ", script)
        self.assertIn("'CBVULN|'", script)

    def test_the_real_script_fits_the_winrm_limit_with_room_to_spare(self):
        script = jobs.build_windows_script()
        size = jobs.windows_script_size(script)
        self.assertEqual(size, len(base64.b64encode(script.encode("utf-16-le"))) + 27)
        self.assertTrue(jobs.windows_script_fits(script))
        # The limit is 8000; the real script uses well under two thirds of it (3831 when measured).
        self.assertLess(size, 5000, size)

    def test_a_script_over_the_limit_is_refused_with_a_clear_message(self):
        big = "x" * 3000
        self.assertFalse(jobs.windows_script_fits(big))
        self.assertTrue(jobs.windows_script_fits("x" * 2900))
        real_open = open

        def fake_open(path, *a, **k):
            if str(path).endswith(jobs.WINDOWS_SCRIPT_FILE):
                import io
                return io.StringIO("$a='" + big + "'\n")
            return real_open(path, *a, **k)

        with mock.patch("builtins.open", fake_open):
            with self.assertRaises(jobs.ScanError) as caught:
                jobs.build_windows_script()
        self.assertIn("too long", str(caught.exception))

    def test_non_ascii_or_a_template_marker_is_refused(self):
        real_open = open
        for text in ("$a='caf\u00e9'\n", "$a='" + "{" + "{ x }" + "}'\n"):
            def fake_open(path, *a, _t=text, **k):
                if str(path).endswith(jobs.WINDOWS_SCRIPT_FILE):
                    import io
                    return io.StringIO(_t)
                return real_open(path, *a, **k)

            with mock.patch("builtins.open", fake_open):
                with self.assertRaises(jobs.ScanError):
                    jobs.build_windows_script()

    def test_the_file_itself_is_ascii_and_has_no_double_braces(self):
        path = os.path.join(base.EXT, "io_cloudbolt_vulnerabilities", jobs.WINDOWS_SCRIPT_FILE)
        with open(path, "rb") as handle:
            data = handle.read()
        data.decode("ascii")
        self.assertNotIn(b"{" + b"{", data)
        self.assertNotIn(b"}" + b"}", data)


# ---------------------------------------------------------------- the output
class ParseTests(unittest.TestCase):
    def test_a_full_output_becomes_the_facts(self):
        parsed = jobs.parse_windows_output(OUTPUT)
        self.assertTrue(parsed["done"])
        self.assertEqual(jobs.windows_facts(parsed), FACTS)

    def test_crlf_and_blank_lines_and_other_text_are_fine(self):
        text = "PS C:\\> prompt\r\n\r\n" + OUTPUT + "\r\nsome trailing text\r\n"
        self.assertEqual(jobs.windows_facts(jobs.parse_windows_output(text)), FACTS)

    def test_garbage_gives_no_facts(self):
        for text in ("", "nothing here", "CBVULN", "CBVULN|", "CBVULN|current_build", "\x00\x01\xff" * 50,
                     "CBVULN|unknown|1\nCBVULN|current_build|abc\nCBVULN|ubr|x"):
            parsed = jobs.parse_windows_output(text)
            self.assertIsNone(jobs.windows_facts(parsed), text)

    def test_missing_keys_are_empty_not_errors(self):
        parsed = jobs.parse_windows_output("CBVULN|current_build|20348\nCBVULN|ubr|0\nCBVULN|done|1")
        facts = jobs.windows_facts(parsed)
        self.assertEqual((facts["current_build"], facts["ubr"], facts["build"]), ("20348", 0, "10.0.20348.0"))
        self.assertEqual((facts["installation_type"], facts["edition_id"], facts["hotfixes"]), ("", "", []))

    def test_the_build_must_be_digits_and_the_revision_a_whole_number(self):
        self.assertIsNone(jobs.windows_facts(jobs.parse_windows_output(
            "CBVULN|current_build|26100;x\nCBVULN|ubr|33650")))
        self.assertIsNone(jobs.windows_facts(jobs.parse_windows_output(
            "CBVULN|current_build|26100\nCBVULN|ubr|-3")))
        self.assertIsNone(jobs.windows_facts(jobs.parse_windows_output(
            "CBVULN|current_build|26100\nCBVULN|ubr|1.5")))

    def test_a_reason_line_is_kept_and_a_value_with_a_url_is_cleaned(self):
        parsed = jobs.parse_windows_output(
            "CBVULN|reason|Reading the Windows version failed: see https://x.example/a?token=abc\nCBVULN|done|1")
        self.assertIn("<url>", parsed["reason"])
        self.assertNotIn("https://", parsed["reason"])
        self.assertIsNone(jobs.windows_facts(parsed))

    def test_update_ids_are_checked_deduplicated_and_capped(self):
        ids = ",".join(["kb5131077", "KB5131077", "KB12", "XX1234567", "KB5044284; rm -rf", "KB5042881", ""])
        parsed = jobs.parse_windows_output("CBVULN|hotfixes|" + ids)
        self.assertEqual(parsed["hotfixes"], ["KB5131077", "KB5042881"])
        many = ",".join("KB{}".format(1000000 + n) for n in range(900))
        self.assertEqual(len(jobs.parse_windows_output("CBVULN|hotfixes|" + many)["hotfixes"]), jobs.MAX_HOTFIXES)

    def test_a_note_is_collected_up_to_the_limit(self):
        text = "\n".join("CBVULN|note|n{}".format(i) for i in range(20))
        self.assertEqual(len(jobs.parse_windows_output(text)["notes"]), jobs.MAX_NOTES)

    def test_the_output_is_bounded(self):
        text = "x" * (jobs.MAX_OUTPUT * 3) + "\n" + OUTPUT
        self.assertEqual(jobs.windows_facts(jobs.parse_windows_output(text)), FACTS)

    def test_the_linux_parser_does_not_read_windows_keys(self):
        # The Linux parser reads "os" and "done" as it always did and ignores the rest.
        parsed = jobs.parse_output(OUTPUT)
        self.assertEqual((parsed["packages"], parsed["reason"], parsed["upload"]), (None, "", ""))


class StoredFactsTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="vuln-win-")

    def write(self, name, data):
        path = os.path.join(self.dir, name)
        with open(path, "wb") as handle:
            handle.write(data)
        return path

    def test_the_stored_list_round_trips_and_says_windows(self):
        data = jobs.encode_windows_facts(FACTS)
        self.assertEqual(data[:2], b"\x1f\x8b")
        kind, facts = jobs.inventory_kind(self.write("w.gz", data))
        self.assertEqual((kind, facts), ("windows", FACTS))
        self.assertTrue(gzip.decompress(data).startswith(b'{"kind":"windows-build",'))

    def test_a_syft_list_a_damaged_file_and_a_missing_file_are_linux(self):
        syft = gzip.compress(json.dumps({"artifacts": [], "distro": {"name": "ubuntu"}}).encode())
        self.assertEqual(jobs.inventory_kind(self.write("s.gz", syft)), ("linux", None))
        self.assertEqual(jobs.inventory_kind(self.write("plain.gz", b"{}")), ("linux", None))
        self.assertEqual(jobs.inventory_kind(self.write("bad.gz", b"\x1f\x8b\x08garbage")), ("linux", None))
        self.assertEqual(jobs.inventory_kind(self.write("empty.gz", b"")), ("linux", None))
        self.assertEqual(jobs.inventory_kind(os.path.join(self.dir, "none.gz")), ("linux", None))

    def test_a_syft_list_is_never_read_past_the_first_bytes(self):
        class Boom(Exception):
            pass

        class Handle:
            reads = []

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self, n=-1):
                Handle.reads.append(n)
                return b'{"artifacts":[{"name":"x"}'

        with mock.patch.object(jobs.gzip, "open", return_value=Handle()):
            self.assertEqual(jobs.inventory_kind("x"), ("linux", None))
        self.assertEqual(Handle.reads, [jobs.KIND_PEEK])

    def test_the_os_name_is_built_from_the_facts_not_from_the_product_name(self):
        self.assertEqual(jobs.windows_os_name(FACTS), "Windows Server 2025 Datacenter, 24H2, build 26100.33650")
        core = dict(FACTS, installation_type="Server Core", edition_id="ServerStandard", current_build="20348",
                    ubr=1, display_version="")
        self.assertEqual(jobs.windows_os_name(core), "Windows Server 2022 Standard, build 20348.1")
        odd = dict(FACTS, current_build="19045", edition_id="Professional")
        self.assertEqual(jobs.windows_os_name(odd),
                         "Windows Server 2022 Datacenter Professional, 24H2, build 19045.33650")


# ---------------------------------------------------------------- which servers
class WhichServersTests(base.Base):
    def test_is_windows_uses_the_core_method_and_tolerates_a_server_without_it(self):
        self.assertTrue(jobs.is_windows(windows_server()))
        self.assertFalse(jobs.is_windows(make_server()))
        self.assertFalse(jobs.is_windows(make_server(is_windows=lambda: False)))
        self.assertTrue(jobs.is_supported(windows_server()))
        self.assertTrue(jobs.is_supported(make_server()))
        other = make_server(os_family=types.SimpleNamespace(get_base_name=lambda: "Other", name="Other"))
        self.assertFalse(jobs.is_supported(other))

    def test_the_tab_shows_on_windows_and_labels_still_narrow_it(self):
        self.assertTrue(jobs.shows_tab(windows_server(), "linux"))
        self.assertFalse(jobs.shows_tab(windows_server(), "label"))
        tagged = windows_server(tags=types.SimpleNamespace(all=lambda: [types.SimpleNamespace(name="Vulnerabilities")]))
        self.assertTrue(jobs.shows_tab(tagged, "label"))
        self.assertFalse(jobs.shows_tab(windows_server(status="HISTORICAL"), "linux"))

    def test_eligibility_keeps_every_check_for_windows(self):
        self.assertEqual(fleet.eligibility(windows_server(), "linux"), "")
        self.assertEqual(fleet.eligibility(windows_server(power_status="POWEROFF"), "linux"), "powered_off")
        self.assertEqual(fleet.eligibility(windows_server(), "label"), "hidden")
        self.assertEqual(fleet.eligibility(windows_server(resource_handler=None), "linux"), "no_script_channel")
        self.assertEqual(fleet.eligibility(windows_server(get_preferred_ip=lambda: ""), "linux"), "no_address")
        self.assertEqual(fleet.eligibility(windows_server(get_credentials=lambda **kw: {}), "linux"), "no_credentials")
        tech = windows_server(use_tech_specific_script_execution=lambda: True, get_preferred_ip=lambda: "",
                              get_credentials=lambda **kw: {})
        self.assertEqual(fleet.eligibility(tech, "linux"), "")
        unassigned = windows_server(group=types.SimpleNamespace(is_unassigned=True, name="Unassigned"))
        self.assertEqual(fleet.eligibility(unassigned, "linux"), "unassigned")

    def test_unsupported_os_has_its_reason_in_words(self):
        other = make_server(os_family=types.SimpleNamespace(get_base_name=lambda: "Other", name="Other"))
        self.assertEqual(fleet.eligibility(other, "linux"), "unsupported_os")
        self.assertEqual(fleet.REASONS["unsupported_os"], "not Linux or Windows")
        self.assertNotIn("not_linux", fleet.REASONS)

    def test_an_openshift_vm_is_never_a_windows_server(self):
        self.assertFalse(jobs._is_openshift_vm(windows_server(ovirtserverinfo=object())))
        self.assertTrue(jobs._is_openshift_vm(make_server(ovirtserverinfo=object())))
        # And it goes through the ordinary Windows checks, not the API tunnel branch.
        server = windows_server(ovirtserverinfo=object(), resource_handler=None)
        self.assertEqual(fleet.eligibility(server, "linux"), "no_script_channel")


# ---------------------------------------------------------------- start
class StartTests(base.Base):
    def setUp(self):
        super().setUp()
        self.queued = {}
        self.marker = {}
        self.targets = mock.Mock(side_effect=AssertionError("a Windows scan makes no upload links"))
        outer = self

        class Hook:
            def run_as_job(self, **kw):
                outer.queued.update(kw)
                return [types.SimpleNamespace(id=42, save=lambda **k: None)]

        self.patches = [
            mock.patch.object(tools, "status", return_value={"ready": False, "ready_windows": True,
                                                              "db": {"built": ""}}),
            mock.patch.object(msrc, "ready", return_value=True),
            mock.patch.object(store, "job_settings", return_value={"timeout_min": 20}),
            mock.patch.object(store, "start_lock", lambda server_id: contextlib.nullcontext()),
            mock.patch.object(store, "read_state", return_value={"active": None, "last": None, "history": []}),
            mock.patch.object(store, "set_active", side_effect=lambda server_id, m: self.marker.update(m)),
            mock.patch.object(repo, "targets", self.targets),
            mock.patch.object(repo, "prepare"),
            mock.patch.object(hooks, "scan_hook", return_value=Hook()),
        ]
        for patcher in self.patches:
            patcher.start()
            self.addCleanup(patcher.stop)

    def server(self, **over):
        return windows_server(jobs=types.SimpleNamespace(add=lambda j: None), **over)

    def test_a_windows_server_starts_without_grype_and_without_a_site_url(self):
        created = jobs.start(None, self.server(), "")
        self.assertEqual(created.id, 42)
        context = self.queued["context"]
        self.assertEqual((context["vuln_upload_url"], context["vuln_token"], context["vuln_tools"]), ("", "", {}))
        self.assertIs(context["vuln_pull"], False)
        self.assertEqual(self.marker["requested_by"], "schedule")
        self.targets.assert_not_called()
        repo.prepare.assert_called_once()

    def test_without_the_microsoft_data_the_refusal_names_it_and_the_button(self):
        with mock.patch.object(msrc, "ready", return_value=False):
            refused = jobs.start(None, self.server(), "https://cb.example.com")
        self.assertIsInstance(refused, jobs.Refusal)
        self.assertEqual(refused.status, 409)
        self.assertIn("Microsoft security data", refused.message)
        self.assertIn("Set up scanner", refused.message)
        self.assertEqual(self.queued, {})

    def test_a_windows_server_on_openshift_is_not_pulled_through_the_tunnel(self):
        jobs.start(None, self.server(ovirtserverinfo=object()), "")
        self.assertIs(self.queued["context"]["vuln_pull"], False)

    def test_the_other_refusals_still_apply(self):
        self.assertEqual(jobs.start(None, self.server(power_status="POWEROFF"), "").status, 409)
        unassigned = self.server(group=types.SimpleNamespace(is_unassigned=True, name="Unassigned"))
        self.assertEqual(jobs.start(None, unassigned, "").message, jobs.UNASSIGNED)
        self.assertEqual(jobs.start(None, self.server(status="PROVFAILED"), "").message, jobs.NOT_ACTIVE)
        other = make_server(os_family=types.SimpleNamespace(get_base_name=lambda: "Other", name="Other"))
        refused = jobs.start(None, other, "https://cb")
        self.assertEqual(refused.message, "Vulnerability scanning supports Linux and Windows servers only.")

    def test_a_linux_server_still_needs_the_scanner_and_the_site_url_and_not_the_microsoft_data(self):
        linux = make_server(jobs=types.SimpleNamespace(add=lambda j: None))
        self.assertEqual(jobs.start(None, linux, "https://cb").message, jobs.NOT_SET_UP)
        with mock.patch.object(tools, "status", return_value={"ready": True, "db": {"built": "b"}}), \
                mock.patch.object(msrc, "ready", return_value=False):
            self.assertEqual(jobs.start(None, linux, "").message, jobs.NO_SITE_URL)
            self.targets.side_effect = None
            self.targets.return_value = {"upload_url": "u", "tools": {}, "token": "t"}
            self.assertEqual(jobs.start(None, linux, "https://cb").id, 42)
        self.assertEqual(self.queued["context"]["vuln_upload_url"], "u")


# ---------------------------------------------------------------- the run
class RunTests(base.Base):
    def setUp(self):
        super().setUp()
        self.root = tempfile.mkdtemp(prefix="vuln-winrun-")
        self.replaced = []
        self.recorded = []
        self.events = []
        self.pruned = []
        self.calls = []
        self.output = OUTPUT
        self.raises = None
        self.match_result = matched(finding(), finding("CVE-2026-0002", False, "High", kb="KB5131081"))
        self.match_error = None
        self.facts_seen = []
        outer = self

        def execute_script(**kw):
            outer.calls.append(kw)
            if outer.raises is not None:
                raise outer.raises
            return outer.output

        self.server = windows_server(execute_script=execute_script, power_status="POWERON")

        def fake_match(facts, index=None):
            outer.facts_seen.append(dict(facts))
            if outer.match_error:
                raise outer.match_error
            return outer.match_result

        def boom(*a, **k):
            raise AssertionError("a Windows scan never uses the scanner or a match slot")

        self.patches = [
            mock.patch.object(store, "INVENTORY_DIR", os.path.join(self.root, "inventories")),
            mock.patch.object(store, "ROOT", self.root),
            mock.patch.object(store, "own", lambda *a, **k: None),
            mock.patch.object(store, "ensure_dir", lambda path: os.makedirs(path, exist_ok=True) or path),
            mock.patch.object(repo, "_room_for", lambda size: True),
            mock.patch.object(msrc, "ready", return_value=True),
            mock.patch.object(msrc, "match", side_effect=fake_match),
            mock.patch.object(tools, "match", side_effect=boom),
            mock.patch.object(store, "match_slot", side_effect=boom),
            mock.patch.object(tools, "status", return_value={"ready": False, "db": {"built": ""}}),
            mock.patch.object(db, "replace_server", create=True,
                              side_effect=lambda row, parsed: self.replaced.append((dict(row), dict(parsed))) or
                              {"findings": len(parsed["matches"])}),
            mock.patch.object(store, "record_scan", side_effect=lambda sid, entry: self.recorded.append((sid, entry))),
            mock.patch.object(jobs, "add_server_event", side_effect=lambda *a, **k: self.events.append(a)),
            mock.patch.object(store, "clear_active"),
            mock.patch.object(jobs, "Server", base.fake_server_model([self.server])),
        ]
        for patcher in self.patches:
            patcher.start()
            self.addCleanup(patcher.stop)
        self.scan_id = store.new_scan_id()
        repo.prepare(self.server.id, self.scan_id)

    def run_it(self):
        began = store.now_utc()
        return jobs._run_windows(None, self.server, self.scan_id, store.stamp(), began)

    def test_a_scan_end_to_end(self):
        status, message, errors, outputs = self.run_it()
        self.assertEqual((status, errors), ("SUCCESS", ""))
        # The script went through execute_script as PowerShell text with no sudo and the short timeout.
        call = self.calls[0]
        self.assertEqual(call["script_contents"], jobs.build_windows_script())
        self.assertNotIn("run_with_sudo", call)
        self.assertEqual((call["timeout"], call["show_streaming_output"]), (110, False))
        # The facts were matched as the script reported them.
        self.assertEqual(self.facts_seen, [FACTS])
        # And stored, gzipped, as the server's software list at the normal inventory path.
        path = repo.inventory_path(self.server.id, self.scan_id)
        self.assertIsNotNone(path)
        self.assertEqual(jobs.inventory_kind(path), ("windows", FACTS))
        # The findings: the Microsoft stamp is the row's db_built, the packages are the installed updates.
        row, parsed = self.replaced[0]
        self.assertEqual(parsed["db_built"], MS_STAMP)
        self.assertEqual(len(parsed["matches"]), 2)
        self.assertEqual(row["packages"], 3)
        self.assertEqual(row["os_name"], "Windows Server 2025 Datacenter, 24H2, build 26100.33650")
        self.assertEqual((row["server_id"], row["hostname"], row["scan_id"]), (7, "web-01", self.scan_id))
        self.assertEqual(self.recorded[0][1]["status"], "completed")
        self.assertEqual(self.recorded[0][1]["packages"], 3)
        self.assertEqual(outputs["findings"], 2)
        self.assertEqual((outputs["critical"], outputs["high"], outputs["kev"], outputs["cves"]), (1, 1, 1, 2))
        self.assertIn("Windows build 10.0.26100.33650", message)
        self.assertEqual(len(self.events), 1)

    def test_a_server_with_no_installed_update_list_stores_zero_packages_not_a_made_up_one(self):
        self.output = OUTPUT.replace("KB5131077,KB5044284,KB5042881", "")
        status, message, errors, outputs = self.run_it()
        self.assertEqual(status, "SUCCESS")
        self.assertEqual(self.replaced[0][0]["packages"], 0)
        self.assertEqual(self.recorded[0][1]["packages"], 0)
        self.assertEqual(outputs["packages"], 0)
        self.assertIn("installed updates could not be read", message)
        self.assertNotIn("0 installed update", message)

    def test_one_installed_update_is_said_in_the_singular(self):
        self.output = OUTPUT.replace("KB5131077,KB5044284,KB5042881", "KB5131077")
        message = self.run_it()[1]
        self.assertEqual(self.replaced[0][0]["packages"], 1)
        self.assertIn("1 installed update,", message)

    def test_the_older_stored_list_is_pruned_after_the_save(self):
        old = store.new_scan_id() + "0"
        os.makedirs(os.path.join(store.INVENTORY_DIR, "7"), exist_ok=True)
        open(os.path.join(store.INVENTORY_DIR, "7", old + ".sbom.json.gz"), "wb").close()
        self.run_it()
        self.assertEqual(sorted(os.listdir(os.path.join(store.INVENTORY_DIR, "7"))), [self.scan_id + ".sbom.json.gz"])

    def test_a_reason_from_the_script_is_the_failure_and_nothing_is_stored_or_saved(self):
        self.output = "CBVULN|reason|Reading the Windows version failed: Access is denied\nCBVULN|done|1"
        with self.assertRaises(jobs.ScanFailed) as caught:
            self.run_it()
        self.assertIn("Access is denied", str(caught.exception))
        self.assertIn("did not report its Windows build", str(caught.exception))
        self.assertEqual((self.replaced, self.recorded, self.facts_seen), ([], [], []))
        self.assertIsNone(repo.inventory_path(self.server.id, self.scan_id))

    def test_a_failed_call_gives_its_message_even_when_stdout_is_lost(self):
        exc = RuntimeError("The command line is too long.")
        exc.output = "stderr text only"
        self.output = ""
        self.raises = exc
        with self.assertRaises(jobs.ScanFailed) as caught:
            self.run_it()
        self.assertIn("The command line is too long", str(caught.exception))

    def test_a_call_that_fails_but_carries_the_lines_is_still_read(self):
        exc = RuntimeError("exit 1")
        exc.output = OUTPUT
        self.raises = exc
        self.assertEqual(self.run_it()[0], "SUCCESS")

    def test_a_failure_without_an_output_attribute_gets_a_generic_sentence_not_the_exception(self):
        self.raises = ConnectionError("secret-host: bad password hunter2")
        with self.assertRaises(jobs.ScanFailed) as caught:
            self.run_it()
        self.assertNotIn("hunter2", str(caught.exception))
        self.assertIn("could not be run", str(caught.exception))

    def test_a_handler_that_cannot_run_scripts_is_said_so(self):
        self.raises = NotImplementedError()
        with self.assertRaises(jobs.ScanFailed) as caught:
            self.run_it()
        self.assertIn("cannot run scripts", str(caught.exception))

    def test_no_output_and_a_cut_off_output_have_their_own_sentences(self):
        self.output = ""
        with self.assertRaises(jobs.ScanFailed) as caught:
            self.run_it()
        self.assertIn("no output", str(caught.exception))
        self.output = "CBVULN|os|x"
        with self.assertRaises(jobs.ScanFailed) as caught:
            self.run_it()
        self.assertIn("did not finish", str(caught.exception))

    def test_an_unsupported_windows_build_fails_with_the_matcher_sentence(self):
        self.match_error = msrc.MsrcError("This Windows version has no published server updates here.",
                                          "build 17763", reason="unsupported_windows")
        with self.assertRaises(jobs.ScanFailed) as caught:
            self.run_it()
        self.assertEqual(str(caught.exception), "This Windows version has no published server updates here.")
        self.assertEqual(self.replaced, [])

    def test_missing_microsoft_data_fails_before_the_script_runs(self):
        with mock.patch.object(msrc, "ready", return_value=False):
            with self.assertRaises(jobs.ScanFailed) as caught:
                self.run_it()
        self.assertEqual(str(caught.exception), jobs.NOT_SET_UP_WINDOWS)
        self.assertEqual(self.calls, [])

    def test_a_server_that_is_off_is_not_run(self):
        self.server.power_status = "POWEROFF"
        with self.assertRaises(jobs.ScanFailed):
            self.run_it()
        self.assertEqual(self.calls, [])

    def test_a_job_that_already_stored_its_list_cannot_run_again(self):
        self.run_it()
        with self.assertRaises(jobs.ScanFailed) as caught:
            self.run_it()
        self.assertIn("cannot run again", str(caught.exception))

    def test_the_script_that_is_too_long_fails_the_scan_with_a_clear_message(self):
        with mock.patch.object(jobs, "build_windows_script", side_effect=jobs.ScanError("The Windows script is too long.")):
            with self.assertRaises(jobs.ScanFailed) as caught:
                self.run_it()
        self.assertIn("too long", str(caught.exception))
        self.assertEqual(self.calls, [])

    def test_run_scan_takes_the_windows_branch_without_upload_links_and_clears_the_marker(self):
        status, message, errors, outputs = jobs.run_scan(
            None, vuln_server_id=7, vuln_scan_id=self.scan_id, vuln_cfg={"timeout_min": 20}, vuln_upload_url="",
            vuln_tools={}, vuln_token="", vuln_pull=False, vuln_started=store.stamp(), vuln_requested_by="schedule")
        self.assertEqual((status, errors), ("SUCCESS", ""))
        store.clear_active.assert_called_once_with(7, self.scan_id)

    def test_run_scan_records_a_failed_windows_scan_and_discards_the_file(self):
        self.output = "CBVULN|reason|no\nCBVULN|done|1"
        with mock.patch.object(repo, "discard") as discard:
            status, message, errors, outputs = jobs.run_scan(
                None, vuln_server_id=7, vuln_scan_id=self.scan_id, vuln_cfg={"timeout_min": 20},
                vuln_started=store.stamp())
        self.assertEqual(status, "FAILURE")
        self.assertIn("did not report its Windows build", errors)
        discard.assert_called_once_with(7, self.scan_id)
        self.assertEqual(self.recorded[0][1]["status"], "failed")

    def run_scan(self):
        return jobs.run_scan(None, vuln_server_id=7, vuln_scan_id=self.scan_id, vuln_cfg={"timeout_min": 20},
                             vuln_started=store.stamp())

    def test_the_failed_entry_carries_the_reason_code_and_the_message(self):
        self.match_error = msrc.MsrcError("This Windows version has no published server updates here.",
                                          "build 17763", reason="unsupported_windows")
        status, message, errors, outputs = self.run_scan()
        self.assertEqual(status, "FAILURE")
        entry = self.recorded[0][1]
        self.assertEqual((entry["status"], entry["reason"]), ("failed", "unsupported_windows"))
        self.assertIn("no published server updates", entry["error"])
        self.assertEqual(self.replaced, [])

    def test_other_failures_carry_no_reason(self):
        self.match_error = msrc.MsrcError("The Microsoft security data is not on this appliance yet.",
                                          reason="not_ready")
        self.run_scan()
        self.assertNotIn("reason", self.recorded[0][1])
        self.match_error = None
        self.recorded.clear()
        self.output = "CBVULN|reason|no\nCBVULN|done|1"
        self.run_scan()
        self.assertNotIn("reason", self.recorded[0][1])

    def test_a_later_good_scan_writes_an_entry_without_it(self):
        self.match_error = msrc.MsrcError("Not covered.", reason="unsupported_windows")
        self.run_scan()
        self.assertEqual(self.recorded[0][1]["reason"], "unsupported_windows")
        self.match_error = None
        self.recorded.clear()
        self.scan_id = store.new_scan_id()
        repo.prepare(self.server.id, self.scan_id)
        self.run_scan()
        entry = self.recorded[0][1]
        self.assertEqual(entry["status"], "completed")
        self.assertNotIn("reason", entry)


# ---------------------------------------------------------------- the nightly re-match
class RematchTests(base.Base):
    def setUp(self):
        super().setUp()
        self.root = tempfile.mkdtemp(prefix="vuln-winrematch-")
        self.replaced = []
        self.linux_matched = []
        self.windows_matched = []
        self.slots = []
        self.updated = []
        self.update_error = None
        self.msrc_up = True
        self.grype_up = True
        self.listed = {}
        self.job = types.SimpleNamespace(id=500, set_progress=lambda *a, **k: None)
        self.known_rows = []
        self.windows_error = None

    def windows_list(self, name="w.gz", facts=FACTS):
        path = os.path.join(self.root, name)
        with open(path, "wb") as handle:
            handle.write(jobs.encode_windows_facts(facts))
        return path

    def run_it(self, servers=None):
        servers = servers or [make_server(id=i, hostname="h{}".format(i)) for i in (1, 2, 3, 4, 5, 6)]

        def inventory_path(server_id, scan_id):
            return self.listed.get(server_id)

        def fake_match(path, out):
            self.linux_matched.append(os.path.basename(path))
            with open(out, "w", encoding="utf-8") as handle:
                json.dump({"matches": []}, handle)

        def msrc_match(facts, index=None):
            base.main_thread_only("vuln_msrc.match")
            self.windows_matched.append(facts["build"])
            if self.windows_error:
                raise self.windows_error
            return {"matches": [finding()], "db_built": MS_STAMP, "track": "standard", "product": "x"}

        @contextlib.contextmanager
        def slot(slots, wait_s=None, on_wait=None):
            self.slots.append(slots)
            yield

        def update_microsoft(progress=None):
            self.updated.append("msrc")
            if self.update_error:
                raise self.update_error
            return {"updated": True, "built": MS_STAMP}

        def replace(row, parsed):
            base.main_thread_only("replace_server")
            self.replaced.append((dict(row), dict(parsed)))
            return dict(row, unchanged=False)

        def summary(server_id):
            row = next(r for r in self.known_rows if r["server_id"] == server_id)
            return dict(row, group_name="g", environment="e", packages=3, inventory_at="2026-11-01T00:00:00Z",
                        os_name="x")

        patches = [
            mock.patch.object(fleet, "Server", base.fake_server_model(servers)),
            mock.patch.object(fleet, "Job", base.fake_job_model(status="RUNNING")),
            mock.patch.object(fleet, "forget_stale", return_value={"forgotten": 0, "kept": 0}),
            mock.patch.object(tools, "status", return_value={"ready": self.grype_up, "db": {"built": GRYPE_STAMP}}),
            mock.patch.object(tools, "update_database", side_effect=lambda p=None: self.updated.append("grype")),
            mock.patch.object(tools, "update_microsoft", side_effect=update_microsoft, create=True),
            mock.patch.object(tools, "match", side_effect=fake_match),
            mock.patch.object(store, "match_slot", side_effect=slot),
            mock.patch.object(store, "get_settings", return_value={"rematch_workers": 1, "inventory_parallel": 2,
                                                                   "scan_on_provision": False, "visibility": "linux"}),
            mock.patch.object(store, "start_lock", lambda server_id: contextlib.nullcontext()),
            mock.patch.object(store, "WORK_DIR", os.path.join(self.root, "work")),
            mock.patch.object(store, "ensure_dir", lambda path: os.makedirs(path, exist_ok=True)),
            mock.patch.object(msrc, "ready", side_effect=lambda: self.msrc_up),
            mock.patch.object(msrc, "status", side_effect=lambda: {"built": MS_STAMP if self.msrc_up else "",
                                                                   "present": self.msrc_up}),
            mock.patch.object(msrc, "load_index", return_value={"stamp": MS_STAMP}),
            mock.patch.object(msrc, "match", side_effect=msrc_match),
            mock.patch.object(repo, "inventory_path", side_effect=inventory_path),
            mock.patch.object(jobs, "active_job", return_value=None),
            mock.patch.object(db, "known_servers", create=True, side_effect=lambda: self.known_rows),
            mock.patch.object(db, "server_summary", create=True, side_effect=summary),
            mock.patch.object(db, "replace_server", create=True, side_effect=replace),
            mock.patch.object(db, "prune_orphans", create=True),
            mock.patch.object(db, "rebuild_fleet", create=True),
        ]
        with contextlib.ExitStack() as stack:
            for patcher in patches:
                stack.enter_context(patcher)
            return fleet.run_rematch(self.job)

    def rows(self):
        # 1: Windows, old stamp. 2: Windows, current. 3: Linux, old. 4: Windows whose stamp is the Grype build.
        # 5: Linux whose stamp is the Microsoft stamp. 6: Linux, current.
        def row(server_id, built):
            return {"server_id": server_id, "scan_id": "s{}".format(server_id), "db_built": built,
                    "matched_at": "2026-11-2{}T01:00:00Z".format(server_id), "hostname": "h"}

        self.known_rows = [row(1, "old"), row(2, MS_STAMP), row(3, "old"), row(4, GRYPE_STAMP), row(5, MS_STAMP),
                           row(6, GRYPE_STAMP)]
        self.listed = {1: self.windows_list("1.gz"), 2: self.windows_list("2.gz"), 3: "/lists/3.gz",
                       4: self.windows_list("4.gz"), 5: "/lists/5.gz", 6: "/lists/6.gz"}

    def test_each_row_is_compared_with_the_stamp_of_its_own_kind(self):
        self.rows()
        status, output, errors, outputs = self.run_it()
        self.assertEqual((status, errors), ("SUCCESS", ""))
        # Windows 1 (old stamp) and 4 (carries the Grype stamp, which is not its own) are re-matched; 2 is current.
        self.assertEqual(sorted(self.windows_matched), ["10.0.26100.33650", "10.0.26100.33650"])
        self.assertEqual(sorted(r["server_id"] for r, _ in self.replaced), [1, 3, 4, 5])
        # Linux 3 (old) and 5 (carries the Microsoft stamp) are re-matched; 6 is current.
        self.assertEqual(sorted(self.linux_matched), ["3.gz", "5.gz"])
        by_id = {r["server_id"]: (r, p) for r, p in self.replaced}
        self.assertEqual(by_id[1][0]["db_built"], MS_STAMP)
        self.assertEqual(by_id[1][1]["db_built"], MS_STAMP)
        self.assertEqual(len(by_id[1][1]["matches"]), 1)
        self.assertEqual(by_id[3][0]["db_built"], GRYPE_STAMP)
        self.assertEqual(outputs["rematched"], 4)
        self.assertEqual(outputs["db_built"], GRYPE_STAMP)
        self.assertEqual(outputs["windows_built"], MS_STAMP)
        self.assertIn(GRYPE_STAMP, output)
        self.assertIn(MS_STAMP, output)

    def test_a_windows_row_never_takes_a_match_slot_or_runs_the_scanner(self):
        self.rows()
        self.known_rows = [r for r in self.known_rows if r["server_id"] in (1, 4)]
        status, output, errors, outputs = self.run_it()
        self.assertEqual(status, "SUCCESS")
        self.assertEqual((self.slots, self.linux_matched), ([], []))
        self.assertEqual(len(self.windows_matched), 2)
        self.assertEqual(base.VIOLATIONS, [])

    def test_the_orm_and_the_microsoft_match_stay_in_the_main_thread(self):
        self.rows()
        self.run_it()
        self.assertEqual(base.VIOLATIONS, [])

    def test_both_data_sources_are_updated_by_the_nightly_job(self):
        self.rows()
        self.run_it()
        self.assertEqual(sorted(self.updated), ["grype", "msrc"])

    def test_without_grype_the_windows_rows_are_still_matched_and_linux_is_counted(self):
        self.rows()
        self.grype_up = False
        status, output, errors, outputs = self.run_it()
        self.assertEqual(status, "SUCCESS")
        self.assertEqual(self.updated, ["msrc"])
        self.assertEqual(self.linux_matched, [])
        self.assertEqual(sorted(r["server_id"] for r, _ in self.replaced), [1, 4])
        self.assertEqual(outputs["skipped"], {"not_set_up": 3})     # Linux 3, 5 and 6
        self.assertNotIn("windows_built", {k: v for k, v in outputs.items() if k == "db_built" and v})
        self.assertEqual(outputs["db_built"], "")

    def test_without_the_microsoft_data_linux_is_matched_and_windows_is_counted(self):
        self.rows()
        self.msrc_up = False
        self.update_error = msrc.MsrcError("No route to Microsoft.")
        status, output, errors, outputs = self.run_it()
        self.assertEqual(status, "SUCCESS")
        self.assertEqual(sorted(self.linux_matched), ["3.gz", "5.gz"])
        self.assertEqual(self.windows_matched, [])
        self.assertEqual(outputs["skipped"], {"no_windows_data": 3})     # Windows 1, 2 and 4
        self.assertIn("Microsoft security data could not be updated", output)
        self.assertNotIn("windows_built", outputs)

    def test_neither_source_ready_is_a_failure_with_the_scanner_sentence(self):
        self.rows()
        self.grype_up = False
        self.msrc_up = False
        status, output, errors, outputs = self.run_it()
        self.assertEqual((status, errors), ("FAILURE", jobs.NOT_SET_UP))
        self.assertEqual(self.updated, [])

    def test_a_failed_microsoft_update_with_data_already_there_matches_with_it(self):
        self.rows()
        self.update_error = msrc.MsrcError("No route to Microsoft.")
        status, output, errors, outputs = self.run_it()
        self.assertEqual(status, "SUCCESS")
        self.assertIn("the data already there was used", output)
        self.assertEqual(len(self.windows_matched), 2)

    def test_one_windows_failure_among_others_is_a_success_and_all_failing_is_not(self):
        self.rows()
        self.windows_error = msrc.MsrcError("This Windows version has no published server updates here.",
                                            reason="unsupported_windows")
        self.known_rows = [r for r in self.known_rows if r["server_id"] in (1, 3)]
        status, output, errors, outputs = self.run_it()
        self.assertEqual((status, outputs["rematched"], outputs["failed"]), ("SUCCESS", 1, 1))
        self.known_rows = [r for r in self.known_rows if r["server_id"] == 1]
        status, output, errors, outputs = self.run_it()
        self.assertEqual(status, "FAILURE")

    def test_a_windows_server_that_stops_qualifying_is_dropped(self):
        self.rows()
        self.known_rows = [r for r in self.known_rows if r["server_id"] == 1]
        self.run_it([make_server(id=1, status="HISTORICAL")])
        self.assertEqual(self.windows_matched, [])

    def linux_only(self):
        def row(server_id):
            return {"server_id": server_id, "scan_id": "s{}".format(server_id), "db_built": GRYPE_STAMP,
                    "matched_at": "2026-11-2{}T01:00:00Z".format(server_id), "hostname": "h"}

        self.known_rows = [row(3), row(6)]
        self.listed = {3: "/lists/3.gz", 6: "/lists/6.gz"}

    def test_a_linux_only_appliance_that_never_set_up_the_microsoft_data_does_not_try_the_download(self):
        self.linux_only()
        self.msrc_up = False
        status, output, errors, outputs = self.run_it()
        self.assertEqual(status, "SUCCESS")
        self.assertEqual(self.updated, ["grype"])
        self.assertNotIn("Microsoft", output)

    def test_a_failed_download_is_not_added_as_a_note_for_that_appliance(self):
        self.linux_only()
        self.msrc_up = False
        self.update_error = msrc.MsrcError("No route to Microsoft.")
        status, output, errors, outputs = self.run_it()
        self.assertEqual((status, self.updated), ("SUCCESS", ["grype"]))
        self.assertNotIn("could not be updated", output)

    def test_it_is_downloaded_when_the_data_was_set_up_before(self):
        self.linux_only()
        self.msrc_up = True
        self.run_it()
        self.assertEqual(sorted(self.updated), ["grype", "msrc"])

    def test_it_is_downloaded_when_a_known_server_was_matched_against_it(self):
        self.linux_only()
        self.msrc_up = False
        self.known_rows.append({"server_id": 1, "scan_id": "s1", "db_built": MS_STAMP,
                                "matched_at": "2026-11-21T01:00:00Z", "hostname": "w"})
        self.listed[1] = self.windows_list("1.gz")
        self.run_it()
        self.assertIn("msrc", self.updated)

    def test_the_decision_itself(self):
        with mock.patch.object(msrc, "status", return_value={"present": False}):
            self.assertTrue(fleet._microsoft_in_use([{"db_built": MS_STAMP}]))
            self.assertFalse(fleet._microsoft_in_use([{"db_built": GRYPE_STAMP}, {"db_built": ""}, {}]))
            self.assertFalse(fleet._microsoft_in_use([]))
        with mock.patch.object(msrc, "status", return_value={"present": True}):
            self.assertTrue(fleet._microsoft_in_use([]))
        with mock.patch.object(msrc, "status", side_effect=RuntimeError("x")):
            self.assertTrue(fleet._microsoft_in_use([{"db_built": MS_STAMP}]))
            self.assertFalse(fleet._microsoft_in_use([{"db_built": GRYPE_STAMP}]))


# ---------------------------------------------------------------- weekly inventory, resource scan, provisioning
class FleetBase(base.Base):
    def setUp(self):
        super().setUp()
        self.started = []
        self.status_of = {}
        self.grype_up = False
        self.msrc_up = True
        self.settings = {"inventory_parallel": 2, "visibility": "linux", "rematch_workers": 1,
                         "scan_on_provision": True}

    def fake_start(self, profile, server, base_url, parent_job=None):
        self.started.append((server.id, base_url))
        job_id = 1000 + server.id
        self.status_of[job_id] = "SUCCESS"
        return types.SimpleNamespace(id=job_id)

    def patches(self, servers, base_url=""):
        return [
            mock.patch.object(fleet, "candidates", return_value=servers),
            mock.patch.object(fleet, "Server", base.fake_server_model(servers)),
            mock.patch.object(fleet, "Job", base.fake_job_model(statuses=lambda: dict(self.status_of))),
            mock.patch.object(fleet, "forget_stale", return_value={"forgotten": 0, "kept": 0}),
            mock.patch.object(fleet, "default_base_url", return_value=base_url),
            mock.patch.object(fleet.time, "sleep"),
            mock.patch.object(tools, "status", side_effect=lambda: {"ready": self.grype_up, "db": {"built": "b"}}),
            mock.patch.object(msrc, "ready", side_effect=lambda: self.msrc_up),
            mock.patch.object(store, "get_settings", return_value=self.settings),
            mock.patch.object(jobs, "start", side_effect=self.fake_start),
            mock.patch.object(db, "known_servers", create=True, return_value=[]),
        ]


class InventoryTests(FleetBase):
    def run_it(self, servers, base_url=""):
        with contextlib.ExitStack() as stack:
            for patcher in self.patches(servers, base_url):
                stack.enter_context(patcher)
            return fleet.run_inventory(types.SimpleNamespace(id=500, set_progress=lambda *a, **k: None))

    def test_a_windows_fleet_runs_without_grype_and_without_a_site_url(self):
        servers = [windows_server(id=i, hostname="w{}".format(i)) for i in (2, 4)]
        status, output, errors, outputs = self.run_it(servers, base_url="")
        self.assertEqual([entry[0] for entry in self.started], [2, 4])
        self.assertEqual((status, outputs["scanned"], outputs["skipped"]), ("SUCCESS", 2, {}))

    def test_a_linux_server_beside_them_is_skipped_with_a_reason_not_failed(self):
        servers = [windows_server(id=2), make_server(id=4), make_server(id=6)]
        status, output, errors, outputs = self.run_it(servers, base_url="")
        self.assertEqual([entry[0] for entry in self.started], [2])
        self.assertEqual(outputs["skipped"], {"not_set_up": 2})
        self.assertEqual((status, outputs["failed"]), ("SUCCESS", 0))
        self.assertIn("2 scanner not set up", output)

    def test_linux_without_a_site_url_is_skipped_but_windows_runs(self):
        self.grype_up = True
        servers = [make_server(id=4), windows_server(id=2)]
        status, output, errors, outputs = self.run_it(servers, base_url="")
        self.assertEqual([entry[0] for entry in self.started], [2])
        self.assertEqual(outputs["skipped"], {"no_site_url": 1})

    def test_windows_without_the_microsoft_data_is_skipped_but_linux_runs(self):
        self.grype_up = True
        self.msrc_up = False
        servers = [make_server(id=4), windows_server(id=2)]
        status, output, errors, outputs = self.run_it(servers, base_url="https://cb")
        self.assertEqual([entry[0] for entry in self.started], [4])
        self.assertEqual(outputs["skipped"], {"no_windows_data": 1})

    def test_nothing_can_run_is_the_old_failure(self):
        self.msrc_up = False
        status, output, errors, outputs = self.run_it([windows_server(id=2)], base_url="")
        self.assertEqual((status, errors), ("FAILURE", jobs.NOT_SET_UP))
        self.grype_up = True
        status, output, errors, outputs = self.run_it([windows_server(id=2)], base_url="")
        self.assertEqual((status, errors), ("FAILURE", jobs.NO_SITE_URL))
        self.assertEqual(self.started, [])


class ProvisionTests(FleetBase):
    def run_it(self, servers, base_url=""):
        job = types.SimpleNamespace(id=9, status="SUCCESS", owner=object(),
                                    server_set=types.SimpleNamespace(all=lambda: list(servers)))
        with contextlib.ExitStack() as stack:
            for patcher in self.patches(servers, base_url):
                stack.enter_context(patcher)
            return fleet.run_provision(job)

    def test_a_new_windows_server_is_scanned_without_grype_or_a_site_url(self):
        status, output, errors, outputs = self.run_it([windows_server(id=3, hostname="w3")])
        self.assertEqual((status, errors), ("SUCCESS", ""))
        self.assertEqual(self.started, [(3, "")])
        self.assertIn("w3 (job 1003)", output)

    def test_a_new_linux_server_is_skipped_with_its_reason(self):
        status, output, errors, outputs = self.run_it([make_server(id=3), windows_server(id=5, hostname="w5")])
        self.assertEqual([entry[0] for entry in self.started], [5])
        self.assertIn("1 scanner not set up", output)

    def test_nothing_runs_when_neither_kind_can(self):
        self.msrc_up = False
        status, output, errors, outputs = self.run_it([windows_server(id=3)])
        self.assertEqual(output, "No scan was started: the scanner is not set up yet.")
        self.assertEqual(self.started, [])


class ResourceScanTests(base.ResourceBase):
    def setUp(self):
        super().setUp()
        self.msrc_up = True
        self.grype_up = False
        patchers = [
            mock.patch.object(tools, "status", side_effect=lambda: {"ready": self.grype_up, "db": {"built": "b"}}),
            mock.patch.object(msrc, "ready", side_effect=lambda: self.msrc_up),
        ]
        for patcher in patchers:
            patcher.start()
            self.addCleanup(patcher.stop)
        self.queued = []

        class Hook:
            def run_as_job(hook_self, **kw):
                self.queued.append(kw)
                return [types.SimpleNamespace(id=77, save=lambda **k: None)]

        patcher = mock.patch.object(hooks, "resource_scan_hook", return_value=Hook())
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_the_start_is_allowed_for_windows_without_grype_or_a_site_url(self):
        job = fleet.start_resource_scan(self.owner, self.resource([]), [1, 2], "")
        self.assertEqual(job.id, 77)

    def test_the_start_is_refused_when_neither_kind_can_run(self):
        self.msrc_up = False
        refusal = fleet.start_resource_scan(self.owner, self.resource([]), [1], "")
        self.assertEqual((refusal.status, refusal.message), (409, jobs.NOT_SET_UP))
        self.grype_up = True
        refusal = fleet.start_resource_scan(self.owner, self.resource([]), [1], "")
        self.assertEqual(refusal.message, jobs.NO_SITE_URL)

    def test_the_job_skips_a_linux_server_it_cannot_scan_and_scans_the_windows_one(self):
        started = []

        def start(profile, server, url, parent_job=None):
            started.append((server.id, url))
            return types.SimpleNamespace(id=900 + server.id)

        servers = [windows_server(id=1, resource_id=5), make_server(id=2, resource_id=5)]
        job = types.SimpleNamespace(id=61, owner=self.owner, set_progress=lambda *a, **k: None)
        self.table.add(61, active=True)
        statuses = {901: "SUCCESS"}
        with contextlib.ExitStack() as stack:
            for patcher in (
                mock.patch.object(fleet, "Server", base.fake_server_model(servers)),
                mock.patch.object(fleet, "Job", base.fake_job_model(statuses=lambda: dict(statuses))),
                mock.patch.object(fleet, "default_base_url", return_value=""),
                mock.patch.object(fleet.time, "sleep"),
                mock.patch.object(fleet, "UserProfile", types.SimpleNamespace(objects=types.SimpleNamespace(
                    select_related=lambda *a: types.SimpleNamespace(filter=lambda **k: types.SimpleNamespace(
                        first=lambda: self.owner))))),
                mock.patch.object(store, "get_settings", return_value={"visibility": "linux",
                                                                       "inventory_parallel": 2}),
                mock.patch.object(jobs, "start", side_effect=start),
            ):
                stack.enter_context(patcher)
            status, output, errors, outputs = fleet.run_resource_scan(
                job, vuln_resource_id=5, vuln_server_ids=[1, 2], vuln_requested_by="alice")
        self.assertEqual(started, [(1, "")])
        self.assertEqual(outputs["skipped"], {"not_set_up": 1})
        self.assertEqual(status, "SUCCESS")



# ---------------------------------------------------------------- the update list (script and parsing)
def _powershell():
    return shutil.which("powershell") if sys.platform == "win32" else None


def _run_encoded(script):
    """A script run once in local Windows PowerShell 5.1 the way WinRM runs it (an encoded command)."""
    encoded = base64.b64encode(script.encode("utf-16-le")).decode()
    done = subprocess.run([_powershell(), "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
                          capture_output=True, text=True, timeout=300)
    assert done.returncode == 0, done.stderr
    return done.stdout


class UpdateListScriptTests(unittest.TestCase):
    def test_get_hotfix_and_the_servicing_packages_are_read_each_in_its_own_try(self):
        lines = jobs.build_windows_script().split("\n")
        hotfix = [line for line in lines if "Get-HotFix" in line]
        packages = [line for line in lines if "Component Based Servicing\\Packages" in line]
        self.assertEqual((len(hotfix), len(packages)), (1, 1))
        for line in hotfix + packages:
            self.assertTrue(line.startswith("try{"), line)
            self.assertIn("}catch{Cb note (", line)
        self.assertNotEqual(hotfix[0], packages[0])
        # The id comes out of the package name, which looks like Package_for_KB5131077~31bf3856ad364e35~amd64~~...
        self.assertIn("KB\\d{4,9}", packages[0])

    def test_at_most_400_ids_in_lines_that_end_at_a_comma_and_a_note_says_when_the_list_was_cut(self):
        script = jobs.build_windows_script()
        self.assertIn("Select-Object -First 400", script)
        self.assertIn("-gt 2900", script)          # a hotfixes line is closed before it reaches Cb's 3000 cut
        self.assertIn("if($n -gt 400){Cb note (", script)
        self.assertLess(2900, jobs.WINDOWS_LINE_CUT)

    def test_the_build_is_reported_before_and_apart_from_both_update_sources(self):
        script = jobs.build_windows_script()
        self.assertLess(script.index("Cb product_name"), script.index("Get-HotFix"))
        self.assertLess(script.index("Get-HotFix"), script.index("Component Based Servicing"))
        # The build is in the first try block, which ends before either source starts.
        self.assertLess(script.index("}catch{Cb reason"), script.index("Get-HotFix"))

    def test_the_script_size_has_margin_under_the_limit(self):
        size = jobs.windows_script_size(jobs.build_windows_script())
        self.assertLess(size, 0.65 * jobs.WINDOWS_MAX_ENCODED, size)

    @unittest.skipUnless(_powershell(), "needs Windows PowerShell")
    def test_run_in_local_powershell_it_reports_the_build_the_updates_and_done(self):
        parsed = jobs.parse_windows_output(_run_encoded(jobs.build_windows_script()))
        self.assertTrue(parsed["done"])
        self.assertEqual(parsed["reason"], "")
        facts = jobs.windows_facts(parsed)
        self.assertIsNotNone(facts)
        self.assertTrue(all(jobs.HOTFIX_RE.fullmatch(item) for item in facts["hotfixes"]))

    @unittest.skipUnless(_powershell(), "needs Windows PowerShell")
    def test_run_in_local_powershell_450_ids_come_out_as_400_in_whole_lines_with_a_note(self):
        fake = "$h=@(1000000..1000449|ForEach-Object{'KB'+$_})\n"
        script = jobs.build_windows_script().replace("$h=@()\n", fake, 1)
        self.assertNotEqual(script, jobs.build_windows_script())
        text = _run_encoded(script)
        lines = [line for line in text.splitlines() if line.startswith("CBVULN|hotfixes|")]
        self.assertGreaterEqual(len(lines), 2)
        for line in lines:
            value = line.split("|", 2)[2]
            self.assertLess(len(value), jobs.WINDOWS_LINE_CUT)
            self.assertTrue(all(jobs.HOTFIX_RE.fullmatch(item) for item in value.split(",")), value[-30:])
        parsed = jobs.parse_windows_output(text)
        self.assertEqual(len(parsed["hotfixes"]), jobs.MAX_HOTFIXES)
        # The machine's own updates are added to the 450 fake ones, so the total is 450 or more.
        self.assertTrue(any(note.startswith("Only 400 of 4") for note in parsed["notes"]), parsed["notes"])


class UpdateListParseTests(unittest.TestCase):
    def test_several_hotfixes_lines_add_up_without_repeats_and_stop_at_400(self):
        text = "CBVULN|hotfixes|KB5131077,KB5044284\nCBVULN|hotfixes|KB5044284,KB5042881\n"
        self.assertEqual(jobs.parse_windows_output(text)["hotfixes"], ["KB5131077", "KB5044284", "KB5042881"])
        lines = "\n".join("CBVULN|hotfixes|" + ",".join("KB{}".format(2000000 + 300 * n + k) for k in range(300))
                          for n in range(3))
        self.assertEqual(len(jobs.parse_windows_output(lines)["hotfixes"]), jobs.MAX_HOTFIXES)

    def test_a_line_cut_by_the_script_limit_loses_its_possibly_half_id(self):
        # Ids of 9 characters plus a comma: a value cut at 3000 characters ends in the middle of an id.
        full = ",".join("KB{}".format(3000000 + n) for n in range(400))
        value = full[:jobs.WINDOWS_LINE_CUT]
        listed = value.split(",")
        self.assertLess(len(listed[-1]), 9)                  # the cut tail is not a whole id here
        parsed = jobs.parse_windows_output("CBVULN|hotfixes|" + value)
        self.assertEqual(parsed["hotfixes"], listed[:-1])
        # A value that did not reach the cut keeps its last id.
        short = ",".join(listed[:50])
        self.assertEqual(jobs.parse_windows_output("CBVULN|hotfixes|" + short)["hotfixes"], listed[:50])

    def test_a_truncated_tail_never_adds_an_id_that_is_not_a_whole_one(self):
        for tail in ("KB51", "KB", "K", "KB5131077;", "KB5131077 x", "kb"):
            parsed = jobs.parse_windows_output("CBVULN|hotfixes|KB5044284," + tail)
            self.assertEqual(parsed["hotfixes"], ["KB5044284"], tail)


# ---------------------------------------------------------------- unsupported Windows versions
UNSUPPORTED_LAST = {"scan_id": "x", "status": "failed", "reason": "unsupported_windows", "error": "no"}


class UnsupportedWindowsTests(base.Base):
    def eligible(self, server, last):
        with mock.patch.object(store, "read_state", return_value={"active": None, "last": last, "history": []}):
            return fleet.eligibility(server, "linux")

    def test_a_windows_server_whose_last_scan_was_not_covered_is_skipped_with_its_own_reason(self):
        self.assertEqual(self.eligible(windows_server(), UNSUPPORTED_LAST), "unsupported_windows")
        self.assertEqual(fleet.REASONS["unsupported_windows"], "a Windows version this extension does not cover")

    def test_it_is_only_that_kind_of_failure_on_a_windows_server(self):
        self.assertEqual(self.eligible(windows_server(), None), "")
        self.assertEqual(self.eligible(windows_server(), dict(UNSUPPORTED_LAST, status="completed")), "")
        self.assertEqual(self.eligible(windows_server(), dict(UNSUPPORTED_LAST, reason="")), "")
        other = {"status": "failed", "error": "This Windows version has no published server updates here."}
        self.assertEqual(self.eligible(windows_server(), other), "")
        self.assertEqual(self.eligible(make_server(), UNSUPPORTED_LAST), "")

    def test_a_state_that_cannot_be_read_does_not_skip_the_server(self):
        with mock.patch.object(store, "read_state", side_effect=store.StoreError("unreadable")):
            self.assertEqual(fleet.eligibility(windows_server(), "linux"), "")

    def test_the_other_reasons_come_first(self):
        self.assertEqual(self.eligible(windows_server(power_status="POWEROFF"), UNSUPPORTED_LAST), "powered_off")

    def test_the_weekly_inventory_and_the_scan_after_provisioning_start_no_job_for_it(self):
        started = []

        def fake_start(profile, server, base_url, parent_job=None):
            started.append(server.id)
            return types.SimpleNamespace(id=1000 + server.id)

        servers = [windows_server(id=2), windows_server(id=3)]
        statuses = {1003: "SUCCESS"}

        def read_state(server_id):
            return {"active": None, "history": [], "last": UNSUPPORTED_LAST if server_id == 2 else None}

        job = types.SimpleNamespace(id=500, set_progress=lambda *a, **k: None, status="SUCCESS", owner=object(),
                                    server_set=types.SimpleNamespace(all=lambda: list(servers)))
        patches = [
            mock.patch.object(fleet, "candidates", return_value=servers),
            mock.patch.object(fleet, "Server", base.fake_server_model(servers)),
            mock.patch.object(fleet, "Job", base.fake_job_model(statuses=lambda: dict(statuses))),
            mock.patch.object(fleet, "forget_stale", return_value={"forgotten": 0, "kept": 0}),
            mock.patch.object(fleet, "default_base_url", return_value=""),
            mock.patch.object(fleet.time, "sleep"),
            mock.patch.object(tools, "status", return_value={"ready": False, "db": {"built": ""}}),
            mock.patch.object(msrc, "ready", return_value=True),
            mock.patch.object(store, "read_state", side_effect=read_state),
            mock.patch.object(store, "get_settings", return_value={"inventory_parallel": 2, "visibility": "linux",
                                                                   "rematch_workers": 1, "scan_on_provision": True}),
            mock.patch.object(jobs, "start", side_effect=fake_start),
            mock.patch.object(db, "known_servers", create=True, return_value=[]),
        ]
        with contextlib.ExitStack() as stack:
            for patcher in patches:
                stack.enter_context(patcher)
            status, output, errors, outputs = fleet.run_inventory(job)
            self.assertEqual(started, [3])
            self.assertEqual(outputs["skipped"], {"unsupported_windows": 1})
            self.assertIn("1 a Windows version this extension does not cover", output)
            started.clear()
            output = fleet.run_provision(job)[1]
            self.assertEqual(started, [3])
            self.assertIn("a Windows version this extension does not cover", output)

    def test_the_tab_button_still_starts_a_scan_of_such_a_server(self):
        queued = {}

        class Hook:
            def run_as_job(self, **kw):
                queued.update(kw)
                return [types.SimpleNamespace(id=42, save=lambda **k: None)]

        server = windows_server(jobs=types.SimpleNamespace(add=lambda j: None))
        with mock.patch.object(msrc, "ready", return_value=True), \
                mock.patch.object(store, "job_settings", return_value={"timeout_min": 20}), \
                mock.patch.object(store, "start_lock", lambda server_id: contextlib.nullcontext()), \
                mock.patch.object(store, "read_state", return_value={"active": None, "last": UNSUPPORTED_LAST,
                                                                      "history": []}), \
                mock.patch.object(store, "set_active"), mock.patch.object(repo, "prepare"), \
                mock.patch.object(hooks, "scan_hook", return_value=Hook()):
            self.assertEqual(jobs.start(None, server, "").id, 42)
        self.assertTrue(queued)


# ---------------------------------------------------------------- setup reporting
class SetupReportTests(base.Base):
    MSRC_OK = {"updated": True, "built": "2026-11-10.26edfc27ff"}
    MSRC_BAD = {"updated": False, "built": "", "error": "The Microsoft security data could not be reached"}
    GRYPE = {"grype": "v1", "syft": "v2", "db": {"updated": True, "built": "2026-11-10T01:00:00Z"}}

    def run_it(self, result=None, error=None):
        recorded = []
        with mock.patch.object(tools, "setup", side_effect=error, return_value=result), \
                mock.patch.object(tools, "record_setup_error", side_effect=recorded.append), \
                mock.patch.object(jobs, "_write_setup_marker"), \
                mock.patch.object(jobs, "_progress"):
            return jobs.run_setup(types.SimpleNamespace(id=1)), recorded

    def failing(self, msrc_result, public="Could not reach github.com."):
        exc = tools.ToolError(public, "detail")
        exc.msrc = msrc_result
        return exc

    def test_both_worked_says_both(self):
        (status, output, errors, outputs), recorded = self.run_it(dict(self.GRYPE, msrc=self.MSRC_OK))
        self.assertEqual((status, errors, recorded), ("SUCCESS", "", []))
        self.assertIn("The scanner is ready", output)
        self.assertIn("The Microsoft security data was downloaded (2026-11-10).", output)
        self.assertEqual(outputs["msrc_built"], "2026-11-10.26edfc27ff")

    def test_microsoft_current_and_microsoft_failed_with_grype_fine_are_reported_but_do_not_fail_it(self):
        (status, output, errors, outputs), _ = self.run_it(dict(self.GRYPE, msrc=dict(self.MSRC_OK, updated=False)))
        self.assertEqual(status, "SUCCESS")
        self.assertIn("is already current (2026-11-10)", output)
        (status, output, errors, outputs), _ = self.run_it(dict(self.GRYPE, msrc=self.MSRC_BAD))
        self.assertEqual((status, errors), ("SUCCESS", ""))
        self.assertIn("could not be downloaded: The Microsoft security data could not be reached.", output)
        self.assertEqual(outputs["msrc_error"], self.MSRC_BAD["error"])

    def test_no_microsoft_part_reported_is_the_old_message(self):
        (status, output, errors, outputs), _ = self.run_it(dict(self.GRYPE))
        self.assertEqual(status, "SUCCESS")
        self.assertNotIn("Microsoft", output)
        self.assertEqual(sorted(outputs), ["db_built", "grype", "syft"])

    def test_grype_failed_and_microsoft_worked_is_a_warning_with_both_facts(self):
        (status, output, errors, outputs), recorded = self.run_it(error=self.failing(self.MSRC_OK))
        self.assertEqual(status, "WARNING")
        self.assertIn("The Microsoft security data was downloaded (2026-11-10).", output)
        self.assertIn("Could not reach github.com.", output)
        self.assertEqual(errors, "Could not reach github.com.")
        self.assertEqual(recorded, ["Could not reach github.com."])
        self.assertEqual(outputs["grype_error"], "Could not reach github.com.")

    def test_an_unexpected_grype_exception_with_microsoft_worked_is_also_a_warning(self):
        exc = RuntimeError("boom")
        exc.msrc = self.MSRC_OK
        (status, output, errors, outputs), _ = self.run_it(error=exc)
        self.assertEqual(status, "WARNING")
        self.assertIn("The scanner setup failed: boom", output)

    def test_both_failed_is_a_failure_with_both_reasons(self):
        (status, output, errors, outputs), recorded = self.run_it(error=self.failing(self.MSRC_BAD))
        self.assertEqual((status, output), ("FAILURE", ""))
        self.assertIn("Could not reach github.com.", errors)
        self.assertIn("The Microsoft security data could not be downloaded: "
                      "The Microsoft security data could not be reached.", errors)
        self.assertEqual(recorded, ["Could not reach github.com."])

    def test_grype_failed_with_no_microsoft_result_is_the_old_failure(self):
        (status, output, errors, outputs), _ = self.run_it(error=tools.ToolError("Could not reach github.com."))
        self.assertEqual((status, output, errors, outputs), ("FAILURE", "", "Could not reach github.com.", {}))

    def test_a_setup_that_reports_the_grype_failure_in_its_result_is_read_the_same_way(self):
        (status, output, errors, outputs), _ = self.run_it(
            {"grype_error": "Could not reach github.com.", "msrc": self.MSRC_OK})
        self.assertEqual(status, "WARNING")

    def test_the_marker_is_always_cleared(self):
        with mock.patch.object(tools, "setup", side_effect=self.failing(None)), \
                mock.patch.object(tools, "record_setup_error"), \
                mock.patch.object(jobs, "_write_setup_marker") as marker:
            jobs.run_setup(types.SimpleNamespace(id=1))
        marker.assert_called_once_with(None)


# ---------------------------------------------------------------- the nightly job and the Microsoft download
# ---------------------------------------------------------------- display of the Windows data date
VIEW_STUBS = {}


def _import_views():
    """views.py against stubs of Django and CloudBolt's extension decorators, so its helpers can be called."""
    def stub(name, **attrs):
        module = types.ModuleType(name)
        module.__dict__.update(attrs)
        VIEW_STUBS[name] = module
        return module

    class Q:
        def __init__(self, **kw):
            pass

        def __or__(self, other):
            return self

    def identity(*a, **k):
        if len(a) == 1 and callable(a[0]) and not k:
            return a[0]
        return lambda view: view

    class Delegate:
        pass

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
    stub("extensions.views", admin_extension=identity, tab_extension=identity, TabExtensionDelegate=Delegate)
    stub("resources")
    stub("resources.models", Resource=object)
    stub("utilities.middleware", login_not_required=identity)
    stub("utilities.permissions", cbadmin_required=identity)
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
STAMP = "2026-11-10.26edfc27ff"
NOON = "2026-11-10T12:00:00Z"


class DisplayDateTests(unittest.TestCase):
    def test_a_microsoft_stamp_shows_as_its_date_and_a_grype_time_is_left_alone(self):
        self.assertEqual(views._display_built(STAMP), NOON)
        self.assertEqual(views._display_built("2026-11-10T01:23:45Z"), "2026-11-10T01:23:45Z")
        self.assertEqual(views._display_built("2026-11-10T01:23:45.123Z"), "2026-11-10T01:23:45.123Z")
        self.assertEqual(views._display_built(""), "")
        self.assertIsNone(views._display_built(None))

    def test_the_stamp_is_converted_with_the_microsoft_modules_own_function_when_it_has_one(self):
        with mock.patch.object(views.vuln_msrc, "stamp_date", create=True, return_value="2026-01-02") as converter:
            self.assertEqual(views._display_built(STAMP), "2026-01-02T12:00:00Z")
        converter.assert_called_once_with(STAMP)

    def test_the_age_counts_from_the_date_of_a_stamp(self):
        today = datetime.now(timezone.utc).date()
        stamp = "{}.26edfc27ff".format(today - timedelta(days=3))
        self.assertEqual(views._db_age_days(stamp), 3)
        self.assertIsNone(views._db_age_days("not a date"))
        self.assertIsNone(views._db_age_days(""))
        self.assertGreater(views._db_age_days("2000-01-01T00:00:00Z"), 1000)

    def test_the_microsoft_tile_has_the_age_from_the_stamp(self):
        today = datetime.now(timezone.utc).date()
        tile = views._msrc_tile({"msrc": {"present": True, "built": "{}.abc".format(today - timedelta(days=5)),
                                          "checked": "", "cves": 9}})
        self.assertEqual(tile["age_days"], 5)
        self.assertEqual(tile["cves"], 9)
        self.assertIsNone(views._msrc_tile({}))

    def test_a_summary_and_its_rows_are_converted_and_nothing_else_changes(self):
        page = {"rows": [{"server_id": 1, "db_built": STAMP}, {"server_id": 2, "db_built": GRYPE_STAMP},
                         {"server_id": 3, "db_built": ""}, {"server_id": 4}],
                "db_builts": [STAMP, GRYPE_STAMP], "total": 4}
        shown = views._with_built_display(page)
        self.assertEqual([row.get("db_built") for row in shown["rows"]], [NOON, GRYPE_STAMP, "", None])
        self.assertEqual(shown["total"], 4)
        self.assertEqual(shown["db_builts"], [NOON])

    def test_one_build_of_each_kind_is_one_entry_so_no_older_database_note_shows(self):
        self.assertEqual(views._display_builts([STAMP, GRYPE_STAMP]), [NOON])
        self.assertEqual(views._display_builts([STAMP]), [NOON])
        self.assertEqual(views._display_builts([]), [])
        # Two builds of one kind are kept: some servers really are on older data.
        self.assertEqual(views._display_builts(["2026-11-10.aaa", "2026-11-03.bbb"]), [NOON, "2026-11-03T12:00:00Z"])
        self.assertEqual(len(views._display_builts([GRYPE_STAMP, "2026-11-01T00:00:00Z", STAMP])), 3)

    def test_the_server_panel_shows_the_date_for_a_windows_row_and_keeps_what_is_stored(self):
        summary = {"os_name": "Windows Server 2025 Datacenter, 24H2, build 26100.33650", "db_built": STAMP,
                   "packages": 0, "inventory_at": "2026-11-24T10:00:00Z", "matched_at": "2026-11-24T10:00:00Z"}
        profile = types.SimpleNamespace(is_cbadmin=False)
        with contextlib.ExitStack() as stack:
            for patcher in (
                mock.patch.object(views.vuln_db, "server_summary", return_value=summary),
                mock.patch.object(views.store, "read_state",
                                  return_value={"active": None, "last": None, "history": []}),
                mock.patch.object(views.vuln_jobs, "panel_active", return_value=None),
                mock.patch.object(views.vuln_jobs, "can_scan", return_value=True),
                mock.patch.object(views.vuln_jobs, "in_unassigned_group", return_value=False),
                mock.patch.object(views.vuln_tools, "status", return_value={"ready_windows": True}),
                mock.patch.object(views, "_is_windows", return_value=True),
            ):
                stack.enter_context(patcher)
            shown = views._panel_context(windows_server(), profile)["vuln"]["summary"]
        self.assertEqual(shown["db_built"], NOON)
        self.assertEqual(shown["windows_build"], "26100.33650")
        self.assertEqual(shown["packages"], 0)
        self.assertEqual(summary["db_built"], STAMP)       # what is stored is not changed


if __name__ == "__main__":
    unittest.main()
