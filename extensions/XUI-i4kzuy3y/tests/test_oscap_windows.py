"""Local tests for io_cloudbolt_compliance/oscap_windows.py. Run: python -m unittest discover -s extensions/XUI-i4kzuy3y/tests
The module is loaded by path: the io_cloudbolt_compliance package imports CloudBolt, which is not needed here."""
import importlib.util
import os
import re
import shutil
import subprocess
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "..", "io_cloudbolt_compliance", "oscap_windows.py")
spec = importlib.util.spec_from_file_location("oscap_windows", SRC)
ow = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ow)

ID = "a" * 64
PARAMS = {
    "mode": "scan", "scan_id": ID, "benchmark": "MS_Windows_Server_2025_STIG", "profile": "MAC-1_Classified",
    "default_profile": "MAC-1_Classified", "allow_install": True, "scc_version": "5.15", "scc_sha256": "ab" * 32,
    "scc_url": "https://example.com/scc-{version}_Windows_bundle.zip",
    "uploads": {n: "https://bucket.s3.amazonaws.com/" + "k" * 900 + "?X-Amz-Signature=" + "f" * 64
                for n in ("report.html", "results.xml.gz", "arf.xml.gz")},
    "upload_token": "t" * 60, "insecure": True,
}


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
    for name, marker in (("prep", "OSCAP|prepared"), ("stage", "AppendAllText"), ("launch", "Register-ScheduledTask"),
                         ("poll", "status.log"), ("cleanup", "OSCAP|cleaned")):
        if marker in script:
            return name


def stage_ack(script):
    start = int(re.search(r"\$c -eq (\d+)", script).group(1))
    return start + int(re.search(r"\$c\+=(\d+)", script).group(1))


def log_server(lines, alive="1", cap=None):
    """A handler serving `lines` as status.log with the real offset semantics."""
    def handler(script):
        k = kind(script)
        if k == "prep":
            return "OSCAP|prepared|1\n"
        if k == "stage":
            return "OSCAP|chunk|%d\n" % stage_ack(script)
        if k == "launch":
            return "OSCAP|launched|1\n"
        if k == "cleanup":
            return "OSCAP|cleaned|1\n"
        offset = int(script.split("$n=")[1].split(";")[0])
        chunk = lines[offset:offset + (cap or 10 ** 6)]
        more = 1 if offset + len(chunk) < len(lines) else 0
        return "\n".join(chunk + ["OSCAP|next|%d" % (offset + len(chunk)), "OSCAP|more|%d" % more,
                                  "OSCAP|alive|" + alive]) + "\n"
    return handler


class Base(unittest.TestCase):
    def setUp(self):
        self.sleeps = []
        self.clock = [0.0]
        ow._sleep = lambda s: (self.sleeps.append(s), self.clock.__setitem__(0, self.clock[0] + s))
        ow._now = lambda: self.clock[0]


class QuoteTests(unittest.TestCase):
    def test_ps_quote(self):
        self.assertEqual(ow.ps_quote("a'b"), "'a''b'")
        self.assertEqual(ow.ps_quote("’x"), "'’’x'")
        self.assertEqual(ow.ps_quote(""), "''")
        self.assertEqual(ow.ps_quote(None), "''")
        self.assertEqual(ow.ps_quote("$x `n {{y}}"), "'$x `n {{y}}'")
        for bad in ("a\nb", "a\x00b", "a\rb", "a\x1bb"):
            with self.assertRaises(ow.WindowsScanError):
                ow.ps_quote(bad)

    def test_header_validation(self):
        for key, value in (("mode", "x"), ("scan_id", "short"), ("scc_version", "1;rm"), ("scc_sha256", "zz")):
            with self.assertRaises(ow.WindowsScanError):
                ow.build_worker(dict(PARAMS, **{key: value}))
        text = ow.build_worker(dict(PARAMS, profile="it's"))
        self.assertIn("prof='it''s'", text)


class LengthTests(unittest.TestCase):
    def all_scripts(self, params=PARAMS):
        b64 = ow.encode_worker(ow.build_worker(params))
        scripts = [s for s, _ack in ow.stage_scripts(ID, b64)]
        scripts += [ow.prep_script(ID), ow.launch_script(ID, 10 ** 7), ow.poll_script(ID, 99999999, 20000),
                    ow.cleanup_script(ID), ow.poll_script(ID, 0, 3000)]
        return scripts

    def test_all_scripts_fit_worst_case(self):
        scripts = self.all_scripts()
        for script in scripts:
            self.assertTrue(ow.script_fits(script), len(script))
            self.assertTrue(script.isascii())
        longest = max(len(s) for s in scripts)
        print("longest script", longest)
        self.assertLessEqual(longest, ow.MAX_SCRIPT_CHARS)
        stage = [s for s in scripts if kind(s) == "stage"]
        self.assertGreater(len(stage[0]), ow.MAX_SCRIPT_CHARS - 600)

    def test_many_chunks_still_fit(self):
        scripts = ow.stage_scripts(ID, "A" * 5000000)
        self.assertGreater(len(scripts), 1000)
        self.assertTrue(all(ow.script_fits(s) and len(s) <= ow.MAX_SCRIPT_CHARS for s, _ in scripts))

    def test_program_files_only(self):
        for script in self.all_scripts() + [ow.build_worker(PARAMS)]:
            self.assertNotIn("ProgramData", script)
            self.assertNotIn("C:\\", script)
        for script in self.all_scripts():
            self.assertIn("ProgramW6432", script)

    def test_no_url_or_token_in_plain_scripts(self):
        for script in self.all_scripts():
            self.assertNotIn("amazonaws", script)
            self.assertNotIn("t" * 60, script)

    def test_round_trip(self):
        text = ow.build_worker(dict(PARAMS, profile="p\u00e9"))
        b64 = ow.encode_worker(text)
        scripts = ow.stage_scripts(ID, b64)
        joined = "".join(re.search(r"AppendAllText\(\$f,'([^']*)'\)", s).group(1) for s, _ in scripts)
        self.assertEqual(joined, b64)
        self.assertEqual(ow.decode_worker(joined), text)
        self.assertEqual(scripts[-1][1], len(b64))
        self.assertIn("$Cfg=@{", text)
        self.assertNotIn("\r", text)

    def test_prep_does_the_directory_work(self):
        prep = ow.prep_script(ID)
        for word in ("icacls", "AddDays(-1)", "ReparsePoint", "Unregister-ScheduledTask", "S-1-5-18", "S-1-5-32-544"):
            self.assertIn(word, prep)
        self.assertIn("Substring(21)", prep)
        self.assertEqual(len("CloudBolt-Compliance-"), 21)
        for script, _ in ow.stage_scripts(ID, "A" * 10000):
            self.assertNotIn("icacls", script)

    def test_launch_time_limit(self):
        self.assertIn("-Minutes 70", ow.launch_script(ID, 3600))

    def test_worker_size(self):
        b64 = ow.encode_worker(ow.build_worker(PARAMS))
        print("worker chars", len(ow.build_worker(PARAMS)), "b64", len(b64), "stage calls", len(ow.stage_scripts(ID, b64)))

    def test_worker_hardening_text(self):
        text = ow.build_worker(PARAMS)
        for word in ("--proto '=https'", "--proto-redir '=https'", "--max-redirs 5", "-MaximumRedirection 5",
                     ".partial", "Rename-Item", "ProgramW6432", "-replace '_rule$'"):
            self.assertIn(word, text)


@unittest.skipUnless(shutil.which("powershell.exe"), "needs Windows PowerShell")
class ParseFunctionTests(unittest.TestCase):
    """Dot-sources only the pure XCCDF parsing function of the worker against a synthetic results file."""

    def run_px(self, xml):
        worker = os.path.abspath(os.path.join(HERE, "..", "io_cloudbolt_compliance", "scc_worker.ps1"))
        with tempfile.TemporaryDirectory() as tmp:
            res = os.path.join(tmp, "r.xml")
            with open(res, "w", encoding="utf-8") as handle:
                handle.write(xml)
            ps = os.path.join(tmp, "t.ps1")
            with open(ps, "w") as handle:
                handle.write(
                    "param($res,$worker)\n$t=$null;$e=$null\n"
                    "$ast=[System.Management.Automation.Language.Parser]::ParseFile($worker,[ref]$t,[ref]$e)\n"
                    "foreach($n in 'Px','Bm'){$fn=$ast.FindAll({param($a)$a -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $a.Name -eq $n},$true)[0];Invoke-Expression $fn.Extent.Text}\n"
                    "$r=Px $res;'n='+$r.n;'score='+$r.score;'bm='+(Bm $res);$r.kv;$r.failed\n")
            out = subprocess.run(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", ps, res, worker],
                                 capture_output=True, text=True, timeout=120)
            return out.stdout.split()

    XML = (
        '<?xml version="1.0"?><Benchmark xmlns="http://checklists.nist.gov/xccdf/1.2" '
        'id="xccdf_mil.disa.stig_benchmark_MS_Windows_Server_2025_STIG"><TestResult id="t">'
        '<rule-result idref="xccdf_mil.disa.stig_rule_SV-277982r1181944_rule" severity="medium" version="WN25-00-000001"><result>fail</result></rule-result>'
        '<rule-result idref="xccdf_mil.disa.stig_rule_SV-277983r1180655_rule" severity="high"><result>fail</result></rule-result>'
        '<rule-result idref="a" severity="low" version="WN25-00-000003"><result>pass</result></rule-result>'
        '<rule-result idref="b" version="WN25-00-000005"><result>fail</result></rule-result>'
        '<score system="urn:xccdf:scoring:default">50.5</score></TestResult></Benchmark>')

    def test_ids_counts_score_and_benchmark(self):
        out = self.run_px(self.XML)
        self.assertEqual(out[:3], ["n=4", "score=50.5", "bm=MS_Windows_Server_2025_STIG"])
        self.assertIn("fail=3", out[3])
        self.assertIn("sev_high=1~sev_medium=1~sev_low=0~sev_unknown=1~rules=4", out[3])
        self.assertEqual(out[4:], ["high", "SV-277983r1180655", "medium", "WN25-00-000001", "unknown", "WN25-00-000005"])

    def test_score_fallback(self):
        out = self.run_px(re.sub(r"<score.*?</score>", "", self.XML))
        self.assertEqual(out[1], "score=25")


class RunTests(Base):
    LINES = ["OSCAP|engine|scc", "OSCAP|progress|Running the SCC scan", "OSCAP|ready|1", "OSCAP|exit_code|2",
             "OSCAP|summary|ready=1~final=1", "OSCAP|done|1"]

    def test_normal_finish_strips_transport(self):
        seen = []
        server = Fake(log_server(self.LINES))
        out = ow.run(server, PARAMS, 600, progress=seen.append)
        self.assertEqual(out.split("\n"), self.LINES)
        self.assertEqual(seen, ["Running the SCC scan"])
        for word in ("chunk", "launched", "next", "alive", "more", "cleaned"):
            self.assertNotIn("|%s|" % word, out)
        self.assertEqual(kind(server.scripts[-1]), "cleanup")
        self.assertEqual([kind(s) for s in server.scripts].count("launch"), 1)

    def test_full_poll_repeats_immediately(self):
        server = Fake(log_server(self.LINES, cap=2))
        out = ow.run(server, PARAMS, 600)
        self.assertEqual(out.split("\n"), self.LINES)
        self.assertEqual(self.sleeps, [])  # every poll but the last was full, the last found done

    def test_idle_poll_sleeps(self):
        calls = []

        def handler(script):
            if kind(script) == "poll":
                calls.append(1)
                if len(calls) == 1:
                    return "OSCAP|next|0\nOSCAP|more|0\nOSCAP|alive|1\n"
            return log_server(self.LINES)(script)
        ow.run(Fake(handler), PARAMS, 600)
        self.assertEqual(self.sleeps, [ow.POLL_SECONDS])

    def test_worker_death(self):
        lines = ["OSCAP|engine|scc", "OSCAP|progress|x"]
        server = Fake(log_server(lines, alive="0"))
        out = ow.run(server, PARAMS, 600)
        self.assertTrue(out.endswith("The scan process on the server ended without finishing."))
        self.assertIn("OSCAP|problem|", out.split("\n")[-1])
        self.assertEqual(kind(server.scripts[-1]), "cleanup")

    def test_timeout_carries_output(self):
        server = Fake(log_server(["OSCAP|engine|scc"], alive="1"))
        with self.assertRaises(RuntimeError) as cm:
            ow.run(server, PARAMS, 120)
        self.assertEqual(cm.exception.output, "OSCAP|engine|scc")
        self.assertIn("did not finish", str(cm.exception))
        self.assertEqual(kind(server.scripts[-1]), "cleanup")

    def test_one_transient_failure_is_retried(self):
        state = {"failed": False}
        base = log_server(self.LINES)

        def handler(script):
            if kind(script) == "poll" and not state["failed"]:
                state["failed"] = True
                return RuntimeError("winrm hiccup")
            return base(script)
        out = ow.run(Fake(handler), PARAMS, 600)
        self.assertEqual(out.split("\n"), self.LINES)

    def test_two_failures_give_up(self):
        base = log_server(["OSCAP|engine|scc", "OSCAP|progress|a"], alive="1")
        polls = []

        def handler(script):
            if kind(script) == "poll":
                polls.append(1)
                if len(polls) > 1:
                    return RuntimeError("down")
            return base(script)
        with self.assertRaises(RuntimeError) as cm:
            ow.run(Fake(handler), PARAMS, 600)
        self.assertIn("OSCAP|engine|scc", cm.exception.output)

    def test_stage_failure_is_retried_once_then_raised(self):
        server = Fake(lambda s: RuntimeError("boom") if kind(s) == "stage" else log_server(self.LINES)(s))
        with self.assertRaises(RuntimeError) as cm:
            ow.run(server, PARAMS, 600)
        self.assertEqual(cm.exception.output, "")
        self.assertEqual([kind(s) for s in server.scripts].count("stage"), 2)
        self.assertEqual(kind(server.scripts[-1]), "cleanup")

    def test_lost_stage_reply_is_idempotent(self):
        """The first call of the second chunk applies but its reply is lost; the retry must not append again."""
        disk = {"c": 0}
        state = {"lost": False}
        base = log_server(self.LINES)

        def handler(script):
            if kind(script) == "stage":
                start = int(re.search(r"\$c -eq (\d+)", script).group(1))
                length = int(re.search(r"\$c\+=(\d+)", script).group(1))
                if disk["c"] == start:
                    disk["c"] += length
                if start > 0 and not state["lost"]:
                    state["lost"] = True
                    return RuntimeError("reply lost")
                return "OSCAP|chunk|%d\n" % disk["c"]
            return base(script)
        b64 = ow.encode_worker(ow.build_worker(PARAMS))
        ow.run(Fake(handler), PARAMS, 600)
        self.assertEqual(disk["c"], len(b64))

    def test_launch_failure_is_retried_once(self):
        state = {"n": 0}
        base = log_server(self.LINES)

        def handler(script):
            if kind(script) == "launch":
                state["n"] += 1
                if state["n"] == 1:
                    return "OSCAP|terr|launch: busy\n"
            return base(script)
        out = ow.run(Fake(handler), PARAMS, 600)
        self.assertEqual(state["n"], 2)
        self.assertEqual(out.split("\n"), self.LINES)

    def test_prep_failure(self):
        server = Fake(lambda s: "OSCAP|terr|prepare: the work directory is a reparse point\n" if kind(s) == "prep" else "")
        with self.assertRaises(RuntimeError) as cm:
            ow.run(server, PARAMS, 600)
        self.assertIn("reparse point", str(cm.exception))
        self.assertEqual([kind(s) for s in server.scripts], ["prep", "prep", "cleanup"])

    def test_deadline_covers_staging(self):
        base = log_server(["OSCAP|engine|scc"])

        def handler(script):
            if kind(script) == "stage":
                self.clock[0] += 1000
            return base(script)
        with self.assertRaises(RuntimeError) as cm:
            ow.run(Fake(handler), PARAMS, 600)
        self.assertIn("did not finish", str(cm.exception))

    def test_stage_without_ack(self):
        server = Fake(lambda s: "OSCAP|terr|stage: access denied\n" if kind(s) == "stage" else "OSCAP|prepared|1\n")
        with self.assertRaises(RuntimeError) as cm:
            ow.run(server, PARAMS, 600)
        self.assertIn("access denied", str(cm.exception))

    def test_launch_without_ack(self):
        base = log_server(self.LINES)
        server = Fake(lambda s: "OSCAP|terr|launch: no task\n" if kind(s) == "launch" else base(s))
        with self.assertRaises(RuntimeError):
            ow.run(server, PARAMS, 600)

    def test_budget_azure(self):
        class AzureARMHandler:
            pass

        class RH:
            def cast(self):
                return AzureARMHandler()
        server = Fake(log_server(self.LINES))
        server.resource_handler = RH()
        ow.run(server, PARAMS, 600)
        polls = [s for s in server.scripts if kind(s) == "poll"]
        self.assertIn("$b=3000;", polls[0])
        other = Fake(log_server(self.LINES))
        ow.run(other, PARAMS, 600)
        self.assertIn("$b=20000;", [s for s in other.scripts if kind(s) == "poll"][0])


if __name__ == "__main__":
    unittest.main()
