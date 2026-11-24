"""
Local tests for io_cloudbolt_vulnerabilities/vuln_wintask.py, the generic Windows SYSTEM scheduled-task runner.
It imports nothing from CloudBolt, so it is loaded by path.

    python -B -m unittest discover -s extensions/XUI-wqrj0e3p/tests -p "test_vuln_wintask.py"

Every script it builds is also parsed with Windows PowerShell 5.1 when powershell.exe is present (nothing is run).
"""
import importlib.util
import os
import re
import shutil
import subprocess
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "..", "io_cloudbolt_vulnerabilities", "vuln_wintask.py")
spec = importlib.util.spec_from_file_location("vuln_wintask", SRC)
wt = importlib.util.module_from_spec(spec)
spec.loader.exec_module(wt)

ID = "a" * 64
DOUBLE_OPEN = "{" + "{"

WORKER_SOURCE = (
    "# a comment line that is dropped\r\n"
    "function Cb($k,$v){\r\n"
    "    Add-Content -LiteralPath $f -Value ('CBVULN|'+$k+'|'+$v)\r\n"
    "}\r\n"
    "\r\n"
    "Cb 'phase' $Cfg.id\r\n"
    "Cb 'done' '1'\r\n"
)


def write_worker(directory, text=WORKER_SOURCE):
    path = os.path.join(directory, "worker.ps1")
    with open(path, "w", encoding="utf-8", newline="") as handle:
        handle.write(text)
    return path


class Fake:
    """A server whose execute_script is driven by a function(script) -> text, raising when it returns an Exception."""

    def __init__(self, handler):
        self.handler = handler
        self.scripts = []

    def execute_script(self, script_contents=None, timeout=None, **kwargs):
        self.scripts.append(script_contents)
        out = self.handler(script_contents)
        if isinstance(out, Exception):
            raise out
        return out


def kind(script):
    for name, marker in (("prep", "CBWT|prepared"), ("stage", "AppendAllText"), ("launch", "Register-ScheduledTask"),
                         ("poll", "status.log"), ("cleanup", "CBWT|cleaned")):
        if marker in script:
            return name


def stage_numbers(script):
    start = int(re.search(r"\$c -eq (\d+)", script).group(1))
    return start, int(re.search(r"\$c\+=(\d+)", script).group(1))


def good_handler(script):
    k = kind(script)
    if k == "prep":
        return "CBWT|prepared|1\n"
    if k == "stage":
        start, length = stage_numbers(script)
        return "CBWT|chunk|%d\n" % (start + length)
    if k == "launch":
        return "CBWT|launched|1\n"
    if k == "cleanup":
        return "CBWT|cleaned|1\n"
    return "CBWT|next|0\nCBWT|more|0\nCBWT|alive|1\n"


def all_scripts():
    import random
    rng = random.Random(3)
    worker = "".join(rng.choice("abcdefghijklmnopqrstuvwxyz0123456789$|;()'-") for _ in range(wt.MAX_WORKER_CHARS))
    b64 = wt.encode_worker(worker)
    scripts = [s for s, _ack in wt.stage_scripts(ID, b64)]
    scripts += [wt.prep_script(ID), wt.launch_script(ID, 10 ** 7), wt.poll_script(ID, 99999999, 20000),
                wt.poll_script(ID, 0, 3000), wt.cleanup_script(ID)]
    return scripts


class QuoteTests(unittest.TestCase):
    def test_ps_quote(self):
        self.assertEqual(wt.ps_quote("a'b"), "'a''b'")
        curly = chr(0x2019)
        self.assertEqual(wt.ps_quote(curly + "x"), "'" + curly * 2 + "x'")
        self.assertEqual(wt.ps_quote(""), "''")
        self.assertEqual(wt.ps_quote(None), "''")
        self.assertEqual(wt.ps_quote(7), "'7'")
        self.assertEqual(wt.ps_quote("$x `n y"), "'$x `n y'")
        for bad in ("a\nb", "a\x00b", "a\rb", "a\x1bb", "a" + chr(0x2028) + "b"):
            with self.assertRaises(wt.WinTaskError):
                wt.ps_quote(bad)


class BuildTests(unittest.TestCase):
    def test_header_and_stripping(self):
        with tempfile.TemporaryDirectory() as tmp:
            text = wt.build_worker(write_worker(tmp), {"id": "run-123", "note": "it's"})
        self.assertTrue(text.startswith("$Cfg=@{\nid='run-123'\nnote='it''s'\n}\n"))
        self.assertNotIn("comment", text)
        self.assertNotIn("\r", text)
        self.assertNotIn("\n\n", text)
        self.assertIn("\nAdd-Content -LiteralPath $f", text)
        self.assertTrue(text.endswith("Cb 'done' '1'\n"))

    def test_header_validation(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_worker(tmp)
            for key in ("1x", "a b", "a;b", "", "x" * 17, "a=b"):
                with self.assertRaises(wt.WinTaskError):
                    wt.build_worker(path, {key: "v"})
            with self.assertRaises(wt.WinTaskError):
                wt.build_worker(path, {"a": "line\nbreak"})
            self.assertIn("a=''", wt.build_worker(path, {"a": None}))
            self.assertIn("$Cfg=@{\n}\n", wt.build_worker(path, {}))

    def test_refuses_non_ascii_double_brace_and_size(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(wt.WinTaskError):
                wt.build_worker(write_worker(tmp, "Write-Output 'caf" + chr(0xE9) + "'\n"), {})
            with self.assertRaises(wt.WinTaskError):
                wt.build_worker(write_worker(tmp, "$x=1 " + DOUBLE_OPEN + " y\n"), {})
            with self.assertRaises(wt.WinTaskError):
                wt.build_worker(write_worker(tmp, "# c " + DOUBLE_OPEN + "\n" + "Cb 'x' 'y'\n" * 2000), {})
            with self.assertRaises(wt.WinTaskError):
                wt.build_worker(write_worker(tmp, "Cb 'x' 'y'\n"), {"a": "v" + DOUBLE_OPEN})
            # nested closing braces and a comment holding a brace are fine
            ok = write_worker(tmp, "if($a){if($b){1}}\n# ok\n")
            self.assertIn("if($a){if($b){1}}", wt.build_worker(ok, {}))

    def test_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            text = wt.build_worker(write_worker(tmp), {"id": "p" + "q" * 10})
        b64 = wt.encode_worker(text)
        scripts = wt.stage_scripts(ID, b64)
        joined = "".join(re.search(r"AppendAllText\(\$f,'([^']*)'\)", s).group(1) for s, _ in scripts)
        self.assertEqual(joined, b64)
        self.assertEqual(wt.decode_worker(joined), text)
        self.assertEqual(scripts[-1][1], len(b64))
        raw = __import__("base64").b64decode(b64)
        import gzip
        self.assertTrue(gzip.decompress(raw).startswith(b"\xef\xbb\xbf"))

    def test_run_id_validation(self):
        for bad in ("short", "a'b" * 5, "a" * 65, "", None, "abc def gh", "x;rm -rf /1"):
            for builder in (wt.prep_script, wt.cleanup_script, lambda i: wt.stage_scripts(i, "AAAA"),
                            lambda i: wt.launch_script(i, 60), lambda i: wt.poll_script(i, 0, 100)):
                with self.assertRaises(wt.WinTaskError):
                    builder(bad)


class LengthTests(unittest.TestCase):
    def test_all_scripts_fit_worst_case(self):
        scripts = all_scripts()
        for script in scripts:
            self.assertTrue(wt.script_fits(script), len(script))
            self.assertTrue(script.isascii())
            self.assertNotIn("#", script)  # no PowerShell comments in fragments
        self.assertLessEqual(max(len(s) for s in scripts), wt.MAX_SCRIPT_CHARS)
        stage = [s for s in scripts if kind(s) == "stage"]
        self.assertGreater(len(stage[0]), wt.MAX_SCRIPT_CHARS - 600)

    def test_worst_case_worker_fits_the_chunk_limit(self):
        # random-looking text compresses badly: the worst case for the number of stage calls
        import random
        rng = random.Random(7)
        alphabet = "abcdefghijklmnopqrstuvwxyz0123456789$|;()'-"
        worker = "".join(rng.choice(alphabet) for _ in range(wt.MAX_WORKER_CHARS))
        scripts = wt.stage_scripts(ID, wt.encode_worker(worker))
        print("worst-case worker chars", len(worker), "stage calls", len(scripts))
        self.assertLessEqual(len(scripts), wt.MAX_CHUNKS)

    def test_many_chunks_still_fit(self):
        scripts = wt.stage_scripts(ID, "A" * 5000000)
        self.assertGreater(len(scripts), 1000)
        self.assertTrue(all(wt.script_fits(s) and len(s) <= wt.MAX_SCRIPT_CHARS for s, _ in scripts))

    def test_program_files_only_and_no_template_markers(self):
        for script in all_scripts():
            self.assertNotIn("ProgramData", script)
            self.assertNotIn("C:\\", script)
            self.assertIn("ProgramW6432", script)
            self.assertNotIn(DOUBLE_OPEN, script)

    def test_prep_does_the_directory_work(self):
        prep = wt.prep_script(ID)
        for word in ("icacls", "AddDays(-2)", "ReparsePoint", "Unregister-ScheduledTask", "S-1-5-18", "S-1-5-32-544"):
            self.assertIn(word, prep)
        self.assertIn("Substring(16)", prep)
        self.assertEqual(len("CloudBolt-Patch-"), 16)
        self.assertIn("CloudBolt\\Patching", prep)
        for script, _ in wt.stage_scripts(ID, "A" * 10000):
            self.assertNotIn("icacls", script)

    def test_launch_time_limit(self):
        self.assertIn("-Minutes 70", wt.launch_script(ID, 3600))

    def test_nothing_writes_to_stderr_or_exits_non_zero(self):
        for script in all_scripts():
            self.assertTrue(script.rstrip().endswith("exit 0"))
            self.assertNotIn("Write-Error", script)
            self.assertNotIn("[Console]::Error", script)
            self.assertNotIn("2>", script)

    def test_source_is_neutral(self):
        with open(SRC, encoding="utf-8") as handle:
            source = handle.read().lower()
        self.assertNotIn("compliance", source)
        self.assertNotIn("scc", source)
        self.assertNotIn("oscap", source)
        self.assertNotIn(DOUBLE_OPEN, source)
        self.assertTrue(source.isascii())


@unittest.skipUnless(shutil.which("powershell.exe"), "needs Windows PowerShell")
class ParserTests(unittest.TestCase):
    def test_every_script_parses_with_windows_powershell(self):
        with tempfile.TemporaryDirectory() as tmp:
            worker = wt.build_worker(write_worker(tmp), {"id": "run-1"})
            scripts = all_scripts() + [worker]
            for index, script in enumerate(scripts):
                with open(os.path.join(tmp, "s%02d.ps1" % index), "w", encoding="utf-8-sig", newline="") as handle:
                    handle.write(script)
            driver = os.path.join(tmp, "parse_all.ps1")
            with open(driver, "w") as handle:
                handle.write(
                    "param($dir)\n$bad=0\n"
                    "foreach($f in Get-ChildItem $dir -Filter 's*.ps1'){\n"
                    "$t=$null;$e=$null\n"
                    "$null=[System.Management.Automation.Language.Parser]::ParseFile($f.FullName,[ref]$t,[ref]$e)\n"
                    "if($e.Count -gt 0){$bad++;$f.Name+': '+$e[0].Message}}\n"
                    "'bad='+$bad\n")
            out = subprocess.run(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", driver, tmp],
                                 capture_output=True, text=True, timeout=180)
            self.assertIn("bad=0", out.stdout, out.stdout + out.stderr)
            self.assertEqual(len([s for s in scripts]), len(scripts))


class StartTests(unittest.TestCase):
    def worker(self):
        return "Cb 'phase' 'x'\n" * 40

    def test_normal_start_order(self):
        server = Fake(good_handler)
        wt.start(server, ID, self.worker(), 1800)
        kinds = [kind(s) for s in server.scripts]
        self.assertEqual(kinds[0], "prep")
        self.assertEqual(kinds[-1], "launch")
        self.assertTrue(all(k == "stage" for k in kinds[1:-1]))
        self.assertEqual(kinds.count("launch"), 1)
        self.assertIn("-Minutes 40", server.scripts[-1])

    def test_lost_stage_reply_is_idempotent(self):
        """The second chunk applies but its first reply is lost; the retry must not append again."""
        disk = {"c": 0}
        state = {"lost": False}
        worker = "".join("Cb 'k%d' '%s'\n" % (i, os.urandom(12).hex()) for i in range(600))
        b64 = wt.encode_worker(worker)
        self.assertGreater(len(wt.stage_scripts(ID, b64)), 1)

        def handler(script):
            if kind(script) == "stage":
                start, length = stage_numbers(script)
                if disk["c"] == start:
                    disk["c"] += length
                if start > 0 and not state["lost"]:
                    state["lost"] = True
                    return RuntimeError("reply lost")
                return "CBWT|chunk|%d\n" % disk["c"]
            return good_handler(script)
        wt.start(Fake(handler), ID, worker, 1800)
        self.assertTrue(state["lost"])
        self.assertEqual(disk["c"], len(b64))

    def test_a_chunk_that_does_not_acknowledge_the_length_fails(self):
        server = Fake(lambda s: "CBWT|chunk|1\n" if kind(s) == "stage" else good_handler(s))
        with self.assertRaises(wt.WinTaskError):
            wt.start(server, ID, self.worker(), 1800)
        self.assertEqual([kind(s) for s in server.scripts].count("stage"), 2)

    def test_launch_failure_is_retried_once(self):
        state = {"n": 0}

        def handler(script):
            if kind(script) == "launch":
                state["n"] += 1
                if state["n"] == 1:
                    return "CBWT|terr|launch: busy\n"
            return good_handler(script)
        wt.start(Fake(handler), ID, self.worker(), 1800)
        self.assertEqual(state["n"], 2)

    def test_prep_failure_is_tried_twice_then_raised_with_the_reason(self):
        server = Fake(lambda s: "CBWT|terr|prepare: the work directory is a reparse point\n" if kind(s) == "prep" else "")
        with self.assertRaises(wt.WinTaskError) as cm:
            wt.start(server, ID, self.worker(), 1800)
        self.assertIn("reparse point", str(cm.exception))
        self.assertEqual([kind(s) for s in server.scripts], ["prep", "prep"])

    def test_raising_calls_are_retried_once(self):
        state = {"n": 0}

        def handler(script):
            if kind(script) == "prep":
                state["n"] += 1
                if state["n"] == 1:
                    return RuntimeError("winrm hiccup")
            return good_handler(script)
        wt.start(Fake(handler), ID, self.worker(), 1800)
        self.assertEqual(state["n"], 2)
        server = Fake(lambda s: RuntimeError("down http://10.0.0.1:5985/wsman"))
        with self.assertRaises(wt.WinTaskError) as cm:
            wt.start(server, ID, self.worker(), 1800)
        self.assertNotIn("10.0.0.1", str(cm.exception))
        self.assertEqual(len(server.scripts), 2)

    def test_worker_needing_too_many_chunks_is_refused_before_any_call(self):
        import random
        rng = random.Random(1)
        worker = "".join(rng.choice("abcdefghijklmnopqrstuvwxyz0123456789$|;()") for _ in range(40000))
        server = Fake(good_handler)
        with self.assertRaises(wt.WinTaskError):
            wt.start(server, ID, worker, 1800)
        self.assertEqual(server.scripts, [])

    def test_bad_run_id_sends_nothing(self):
        server = Fake(good_handler)
        with self.assertRaises(wt.WinTaskError):
            wt.start(server, "x;y", self.worker(), 1800)
        self.assertEqual(server.scripts, [])


class PollTests(unittest.TestCase):
    def test_parse(self):
        reply = ("CBVULN|phase|Searching\r\nnoise line\r\nCBVULN|found|3\r\n"
                 "CBWT|next|7\r\nCBWT|more|1\r\nCBWT|alive|0\r\n")
        server = Fake(lambda s: reply)
        out = wt.poll(server, ID, 4, budget=500)
        self.assertEqual(out, {"lines": ["CBVULN|phase|Searching", "CBVULN|found|3"], "next": 7, "more": True,
                               "alive": False})
        self.assertIn("$n=4;", server.scripts[0])
        self.assertIn("$b=500;", server.scripts[0])
        self.assertEqual(len(server.scripts), 1)

    def test_transport_keys_never_leak_into_lines_and_unknown_keys_are_ignored(self):
        out = wt.poll(Fake(lambda s: "CBWT|bogus|1\nCBWT|next|2\nCBWT|alive|1\nCBVULN|done|1\n"), ID, 0)
        self.assertEqual(out["lines"], ["CBVULN|done|1"])
        self.assertFalse(out["more"])
        self.assertTrue(out["alive"])
        self.assertEqual(out["next"], 2)

    def test_failures_raise_once_with_no_retry(self):
        for reply in ("CBWT|terr|poll: access denied\n", "", "CBWT|next|abc\n", RuntimeError("boom")):
            server = Fake(lambda s, r=reply: r)
            with self.assertRaises(wt.WinTaskError):
                wt.poll(server, ID, 0)
            self.assertEqual(len(server.scripts), 1)

    def test_budget_azure_and_default(self):
        class AzureARMHandler:
            pass

        class RH:
            def cast(self):
                return AzureARMHandler()
        server = Fake(good_handler)
        server.resource_handler = RH()
        wt.poll(server, ID, 0)
        self.assertIn("$b=3000;", server.scripts[0])
        other = Fake(good_handler)
        wt.poll(other, ID, 0)
        self.assertIn("$b=20000;", other.scripts[0])

    def test_inline_returns_transport_and_wraps_exceptions(self):
        self.assertEqual(wt.inline(Fake(lambda s: "CBWT|boot|20260101000000\nCBWT|ccm|0\nCBVULN|x|1\n"), "x"),
                         {"boot": "20260101000000", "ccm": "0"})
        with self.assertRaises(wt.WinTaskError):
            wt.inline(Fake(lambda s: RuntimeError("down")), "x")

    def test_exec_script_passes_the_limits_and_decodes_bytes(self):
        seen = {}

        class S:
            def execute_script(self, script_contents=None, timeout=None, show_streaming_output=None):
                seen.update(script=script_contents, timeout=timeout, stream=show_streaming_output)
                return b"CBWT|boot|1\r\n"
        self.assertEqual(wt.exec_script(S(), "go"), "CBWT|boot|1\n")
        self.assertEqual(seen, {"script": "go", "timeout": 110, "stream": False})


class CleanupTests(unittest.TestCase):
    def test_cleanup_swallows_everything(self):
        for reply in (RuntimeError("down"), "garbage", ""):
            server = Fake(lambda s, r=reply: r)
            self.assertIsNone(wt.cleanup(server, ID))
            self.assertEqual(kind(server.scripts[0]), "cleanup")
        self.assertIsNone(wt.cleanup(Fake(good_handler), ID))

    def test_cleanup_with_a_bad_id_sends_nothing_and_does_not_raise(self):
        server = Fake(good_handler)
        self.assertIsNone(wt.cleanup(server, "x;y"))
        self.assertEqual(server.scripts, [])


if __name__ == "__main__":
    unittest.main()
