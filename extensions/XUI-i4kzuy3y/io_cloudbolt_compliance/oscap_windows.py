"""
The Windows engine of the Compliance extension: scans a Windows guest with DISA SCC
(cscc.exe) and returns the same OSCAP|key|value text oscap_scan.sh prints, so
oscap_jobs.parse_output reads it unchanged.

Transport limits this module is built around (CloudBolt's Server.execute_script on
Windows always runs PowerShell):
  - WinRM sends the script as "powershell -encodedcommand <base64 of UTF-16LE>" on
    one command line, so base64 length + 27 must stay under 8000 (about 2,900
    characters) and a call gives up after about 120 s. On a non-zero exit only
    stderr comes back.
  - AWS SSM returns about 24,000 characters of stdout, Azure Run Command about 4 KB,
    and VMware Tools treats any stderr output as failure.
So every script sent here is short, finishes in seconds, always exits 0, writes
nothing to stderr, and the real work (scc_worker.ps1) runs detached as a SYSTEM
scheduled task that appends marker lines to status.log, which run() polls.

Flow: stage the gzipped, base64 worker in chunks -> launch the scheduled task ->
poll status.log -> clean up. The upload URLs and token travel inside the staged
payload (gzip + base64 chunks, as the Linux script's header does), so they are in the work directory's p.b64 until the
launch script deletes it and in worker.ps1 until cleanup; the work directory is readable by SYSTEM and Administrators
only, and they never go into a log line, an error message or status.log.
Everything lives under Program Files (writable by administrators only), never ProgramData, which any user can write to:
SYSTEM runs cscc.exe from there.

SCC command line reference (SCC 5.15 user manual, chapter 5): -o FILE and -u DIR
are [Combinable] with a scan.
"""
import base64
import gzip
import os
import re
import time

SCRIPT_FILE = "scc_worker.ps1"
# Resolved on the guest in every script (ProgramW6432 keeps a 32-bit PowerShell on the 64-bit Program Files).
BASEDEF = "$base=Join-Path $(if($env:ProgramW6432){$env:ProgramW6432}else{$env:ProgramFiles}) 'CloudBolt\\Compliance'\n"
MODES = ("check", "scan")
SCAN_ID_RE = re.compile(r"[A-Za-z0-9-]{8,64}")
SHA_RE = re.compile(r"[0-9A-Fa-f]{64}")
VERSION_RE = re.compile(r"latest|[0-9][0-9.]{0,15}")
CONTROL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f\u2028\u2029]")
URL_RE = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://\S+")
# PowerShell reads these as single quotes too; inside a single-quoted string any of them is escaped by doubling.
PS_QUOTES = "'\u2018\u2019\u201a\u201b"

# WinRM: len(b64(utf-16-le(script))) + 27 <= 8000. 2,900 ASCII characters gives 7,763.
MAX_SCRIPT_CHARS = 2900
EXEC_TIMEOUT = 110
POLL_SECONDS = 10
LINE_CAP = 400
BUDGET_AZURE = 3000
BUDGET_DEFAULT = 20000
GRACE_MINUTES = 10
TRANSPORT_KEYS = ("prepared", "chunk", "launched", "next", "more", "alive", "terr", "cleaned")

# Replaceable in tests.
_now = time.monotonic
_sleep = time.sleep


class WindowsScanError(RuntimeError):
    """The scan could not be delivered or did not finish. .output is the marker text collected so far."""

    def __init__(self, message, output=""):
        super().__init__(message)
        self.output = output


# ---------------------------------------------------------------- SCC configuration (read this before changing the worker)
# scc_worker.ps1 function Rn is the only place that configures SCC for one scan. What it does:
#   1. copies <scc dir>\options.xml to <work dir>\options.xml (a per-scan file: the shared one is never touched),
#      enables and reviews only the one content block whose <benchmarkID> equals the requested benchmark, sets its
#      <selectedProfile> to the full XCCDF profile id, and sets keepXCCDFXML=1, keepARFXML=1,
#      allSettingsHTMLReport=1, nonComplianceHTMLReport=0, saveDISACKL=0, saveDISACKLB=0, enableSummaryViewer=0;
#   2. runs:  cscc.exe -o <work dir>\options.xml -u <work dir>\scc   (working directory = the SCC folder, because the
#      <filepath> values in options.xml are relative to it), stdout and stderr redirected to files.
# The manual (5.2) marks -o and -u [Combinable] with a scan and says nothing about whether config options combine with
# one. Alternatives if a live run shows -o is ignored or rejected, in the order to try them:
#   a. cscc.exe -dn -cd <private dir> -u <work dir>\scc   after seeding <private dir> with the edited options.xml
#      (-cd = private config dir, -dn = do not save changes to options.xml);
#   b. cscc.exe --setOpt keepARFXML 1 ... then --disableAll / --enableBenchmark <ID> / --setProfile <PROFILE> <ID>
#      as separate calls against the shared options.xml (changes it for every scan on the guest; serialise scans), then
#      cscc.exe -u <work dir>\scc;
#   c. cscc.exe -ear ... is NOT suitable: it enables and runs all content.
# Results are looked for under <work dir>\scc first, then under the SYSTEM profile's SCC folder (the default user data
# directory), keeping files written after the worker started.


# ---------------------------------------------------------------- quoting and building
def ps_quote(value):
    """A PowerShell single-quoted string literal for value: quotes doubled, control characters refused."""
    text = "" if value is None else str(value)
    if CONTROL_RE.search(text):
        raise WindowsScanError("A scan setting contains a control character.")
    for quote in PS_QUOTES:
        text = text.replace(quote, quote * 2)
    return "'" + text + "'"


def _flag(value):
    return "1" if value else "0"


def build_worker(params):
    """The worker script text: a $Cfg hashtable header built from params, then scc_worker.ps1 minus indentation."""
    mode = str(params.get("mode") or "")
    if mode not in MODES:
        raise WindowsScanError("Unknown mode.")
    scan_id = str(params.get("scan_id") or "")
    if not SCAN_ID_RE.fullmatch(scan_id):
        raise WindowsScanError("The scan id has characters that are not allowed.")
    version = str(params.get("scc_version") or "latest").strip()
    if not VERSION_RE.fullmatch(version):
        raise WindowsScanError("The SCC version is not valid.")
    sha = str(params.get("scc_sha256") or "").strip()
    if sha and not SHA_RE.fullmatch(sha):
        raise WindowsScanError("The SCC checksum must be 64 hexadecimal characters.")
    uploads = params.get("uploads") or {}
    values = [
        ("mode", mode),
        ("id", scan_id),
        ("bench", params.get("benchmark") or ""),
        ("prof", params.get("profile") or ""),
        ("defp", params.get("default_profile") or ""),
        ("inst", _flag(params.get("allow_install"))),
        ("ver", version),
        ("sha", sha),
        ("url", params.get("scc_url") or ""),
        ("upr", uploads.get("report.html", "")),
        ("upz", uploads.get("results.xml.gz", "")),
        ("upa", uploads.get("arf.xml.gz", "")),
        ("tok", params.get("upload_token") or ""),
        ("ins", _flag(params.get("insecure"))),
    ]
    header = "$Cfg=@{\n" + "".join("{}={}\n".format(key, ps_quote(value)) for key, value in values) + "}\n"
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), SCRIPT_FILE)
    with open(path, encoding="utf-8", newline="") as handle:
        body = handle.read().replace("\r\n", "\n")
    body = "\n".join(line.strip() for line in body.split("\n") if line.strip())
    return header + body + "\n"


def encode_worker(text):
    """gzip then base64. The UTF-8 BOM makes PowerShell 5.1 read the file as UTF-8."""
    return base64.b64encode(gzip.compress(("\ufeff" + text).encode("utf-8"), 9, mtime=0)).decode("ascii")


def decode_worker(b64):
    """The inverse of encode_worker (used by tests)."""
    return gzip.decompress(base64.b64decode(b64)).decode("utf-8-sig")


# ---------------------------------------------------------------- scripts
PRELUDE = "$ErrorActionPreference='Stop';$ProgressPreference='SilentlyContinue'\n"

# Always run first, every run: creates the work directory, refuses a reparse point, (re)applies the ACL (SYSTEM and
# Administrators only; the directory later holds the upload token), prunes run directories older than a day and
# unregisters scan tasks whose run directory is gone.
PREP = (
    PRELUDE
    + "try{\n"
    + BASEDEF
    + "$r=\"$base\\run\";$d=\"$r\\@@ID@@\"\n"
    + "New-Item -ItemType Directory -Force -Path $d|Out-Null\n"
    + "if((Get-Item $d -Force).Attributes -band [IO.FileAttributes]::ReparsePoint){throw 'the work directory is a reparse point'}\n"
    + "$null=icacls $d /inheritance:r /grant:r '*S-1-5-18:(OI)(CI)F' '*S-1-5-32-544:(OI)(CI)F'\n"
    + "if($LASTEXITCODE -ne 0){throw 'icacls failed'}\n"
    + "Remove-Item \"$d\\p.b64\" -Force -EA 0\n"
    + "Get-ChildItem $r -Directory|?{$_.LastWriteTime -lt (Get-Date).AddDays(-1)}|Remove-Item -Recurse -Force -EA 0\n"
    + "Get-ScheduledTask -TaskName 'CloudBolt-Compliance-*' -EA 0|?{$i=$_.TaskName.Substring(21);$i -ne '@@ID@@' -and !(Test-Path \"$r\\$i\")}|Unregister-ScheduledTask -Confirm:$false -EA 0\n"
    + "'OSCAP|prepared|1'\n"
    + "}catch{'OSCAP|terr|prepare: '+$_.Exception.Message}\n"
    + "exit 0\n"
)

# Idempotent: the chunk is appended only when the file holds exactly the characters before it, and the reply is the
# current length, so a repeated call after a lost reply changes nothing.
STAGE = (
    PRELUDE
    + "try{\n"
    + BASEDEF
    + "$f=\"$base\\run\\@@ID@@\\p.b64\"\n"
    + "$c=0;if(Test-Path $f){$c=(Get-Item $f).Length}\n"
    + "if($c -eq @@START@@){[IO.File]::AppendAllText($f,'@@CHUNK@@');$c+=@@LEN@@}\n"
    + "'OSCAP|chunk|'+$c\n"
    + "}catch{'OSCAP|terr|stage: '+$_.Exception.Message}\n"
    + "exit 0\n"
)

LAUNCH = (
    PRELUDE
    + "try{\n"
    + BASEDEF
    + "$d=\"$base\\run\\@@ID@@\"\n"
    + "if(Test-Path \"$d\\p.b64\"){\n"
    + "$b=[Convert]::FromBase64String([IO.File]::ReadAllText(\"$d\\p.b64\"))\n"
    + "$g=New-Object IO.Compression.GZipStream((New-Object IO.MemoryStream(,$b)),[IO.Compression.CompressionMode]::Decompress)\n"
    + "$o=New-Object IO.MemoryStream\n"
    + "$g.CopyTo($o)\n"
    + "[IO.File]::WriteAllBytes(\"$d\\worker.ps1\",$o.ToArray())\n"
    + "Remove-Item \"$d\\p.b64\" -Force}\n"
    + "$n='CloudBolt-Compliance-@@ID@@'\n"
    + "$a=New-ScheduledTaskAction -Execute 'powershell.exe' -Argument \"-NoProfile -NonInteractive -ExecutionPolicy Bypass -File `\"$d\\worker.ps1`\"\"\n"
    + "$p=New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest\n"
    + "$s=New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Minutes @@MIN@@) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries\n"
    + "$null=Register-ScheduledTask -TaskName $n -Action $a -Principal $p -Settings $s -Force\n"
    + "Start-ScheduledTask -TaskName $n\n"
    + "'OSCAP|launched|1'\n"
    + "}catch{'OSCAP|terr|launch: '+$_.Exception.Message}\n"
    + "exit 0\n"
)

POLL = (
    PRELUDE
    + "try{\n"
    + BASEDEF
    + "$f=\"$base\\run\\@@ID@@\\status.log\"\n"
    + "$n=@@OFFSET@@;$c=@@CAP@@;$b=@@BUDGET@@;$t=0;$k=0;$m=0\n"
    + "$a=@();if(Test-Path $f){$a=@(Get-Content -LiteralPath $f -Encoding UTF8)}\n"
    + "for($i=$n;$i -lt $a.Count;$i++){\n"
    + "$l=[string]$a[$i];if($l.Length -gt $b){$l=$l.Substring(0,$b)}\n"
    + "if($k -ge $c -or ($k -gt 0 -and $t+$l.Length+1 -gt $b)){$m=1;break}\n"
    + "$l;$t+=$l.Length+1;$k++}\n"
    + "'OSCAP|next|'+($n+$k)\n"
    + "'OSCAP|more|'+$m\n"
    + "$s=(Get-ScheduledTask -TaskName 'CloudBolt-Compliance-@@ID@@' -EA 0).State\n"
    + "'OSCAP|alive|'+$(if($s -eq 'Running' -or $s -eq 'Queued'){1}else{0})\n"
    + "}catch{'OSCAP|terr|poll: '+$_.Exception.Message}\n"
    + "exit 0\n"
)

CLEANUP = (
    PRELUDE
    + BASEDEF
    + "$n='CloudBolt-Compliance-@@ID@@'\n"
    + "try{Stop-ScheduledTask -TaskName $n -EA 0}catch{}\n"
    + "try{Unregister-ScheduledTask -TaskName $n -Confirm:$false -EA 0}catch{}\n"
    + "try{Get-Process cscc -EA 0|?{$_.Path -like \"$base\\*\"}|Stop-Process -Force -EA 0}catch{}\n"
    + "try{Remove-Item -LiteralPath \"$base\\run\\@@ID@@\" -Recurse -Force -EA 0}catch{}\n"
    + "'OSCAP|cleaned|1'\n"
    + "exit 0\n"
)


def _fill(template, **values):
    for key, value in values.items():
        template = template.replace("@@" + key.upper() + "@@", str(value))
    return template


def script_fits(script):
    """The WinRM command-line limit (CloudBolt: len(b64(utf-16-le)) + 27 <= 8000)."""
    return len(base64.b64encode(script.encode("utf-16-le"))) + 27 <= 8000


def chunk_size():
    """Characters of base64 per stage call: what the worst-case stage script (64-character id, 8-digit offsets)
    leaves under MAX_SCRIPT_CHARS."""
    overhead = len(_fill(STAGE, id="x" * 64, start="99999999", chunk="", len="9999"))
    return MAX_SCRIPT_CHARS - overhead


def prep_script(scan_id):
    return _fill(PREP, id=scan_id)


def stage_scripts(scan_id, b64):
    """The scripts that append b64 to p.b64, one chunk each. Returns [(script, expected acknowledgement)]; the
    acknowledgement is the file length after the chunk."""
    size = chunk_size()
    chunks = [b64[i:i + size] for i in range(0, len(b64), size)] or [""]
    return [
        (_fill(STAGE, id=scan_id, start=index * size, chunk=chunk, len=len(chunk)), index * size + len(chunk))
        for index, chunk in enumerate(chunks)
    ]


def launch_script(scan_id, timeout):
    minutes = int(timeout) // 60 + GRACE_MINUTES
    return _fill(LAUNCH, id=scan_id, min=minutes)


def poll_script(scan_id, offset, budget):
    return _fill(POLL, id=scan_id, offset=int(offset), cap=LINE_CAP, budget=int(budget))


def cleanup_script(scan_id):
    return _fill(CLEANUP, id=scan_id)


# ---------------------------------------------------------------- running
def _budget(server):
    """Characters of status lines per poll: Azure Run Command returns about 4 KB, the others far more."""
    try:
        handler = server.resource_handler.cast()
        if "azure" in (type(handler).__module__ + type(handler).__name__).lower():
            return BUDGET_AZURE
    except Exception:
        pass
    return BUDGET_DEFAULT


def _text(value):
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    return str(value or "").replace("\r\n", "\n").replace("\r", "\n")


def _exec(server, script):
    return _text(server.execute_script(script_contents=script, timeout=EXEC_TIMEOUT, show_streaming_output=False))


def _short(exc):
    return URL_RE.sub("<url>", CONTROL_RE.sub(" ", str(exc))).strip()[:200] or type(exc).__name__


def _split(text):
    """(marker lines, transport values) of one call's output."""
    lines, transport = [], {}
    for raw in _text(text).split("\n"):
        line = raw.strip()
        if not line.startswith("OSCAP|"):
            continue
        parts = line.split("|", 2)
        if len(parts) == 3 and parts[1] in TRANSPORT_KEYS:
            transport[parts[1]] = parts[2]
        else:
            lines.append(line)
    return lines, transport


def run(server, params, timeout, progress=None):
    """Run a check or a scan on a Windows server and return the OSCAP|key|value text.

    params: mode ("check"|"scan"), scan_id, benchmark, profile, default_profile, allow_install (bool),
    scc_version ("5.15" or "latest"), scc_sha256, scc_url (with {version}), uploads ({"report.html",
    "results.xml.gz", "arf.xml.gz": PUT URL}), upload_token, insecure (bool). timeout is the seconds the worker may
    run. Raises WindowsScanError (a RuntimeError with .output) when the script cannot be delivered, the connection
    fails twice in a row, or the worker runs past the timeout."""
    deadline = _now() + timeout
    worker = build_worker(params)
    scan_id = params["scan_id"]
    scripts = stage_scripts(scan_id, encode_worker(worker))
    budget = _budget(server)
    collected = []

    def fail(message):
        return WindowsScanError(message, "\n".join(collected))

    def take(lines):
        for line in lines:
            collected.append(line)
            parts = line.split("|", 2)
            if progress and len(parts) == 3 and parts[1] == "progress":
                try:
                    progress(URL_RE.sub("<url>", parts[2]).strip())
                except Exception:
                    pass

    def call(script, key, expected, what):
        """One script, tried twice: its reply must carry transport key == expected."""
        reason = ""
        for _attempt in range(2):
            try:
                _lines, transport = _split(_exec(server, script))
            except Exception as exc:
                reason = _short(exc)
                continue
            if transport.get(key) == str(expected):
                return
            reason = (transport.get("terr") or "no reply")[:200]
        raise fail("{}: {}".format(what, reason))

    try:
        call(prep_script(scan_id), "prepared", 1, "Could not prepare the server for the scan")
        for script, expected in scripts:
            call(script, "chunk", expected, "Could not send the scan to the server")
        call(launch_script(scan_id, timeout), "launched", 1, "Could not start the scan on the server")

        offset, dead, failures = 0, 0, 0
        while True:
            reason = ""
            try:
                lines, transport = _split(_exec(server, poll_script(scan_id, offset, budget)))
                if "terr" in transport or not str(transport.get("next", "")).isdigit():
                    reason = transport.get("terr") or "no reply"
            except Exception as exc:
                reason = _short(exc)
            if reason:
                failures += 1
                if failures > 1:
                    raise fail("Lost contact with the server while the scan ran: " + reason[:200])
                _sleep(POLL_SECONDS)
                continue
            failures = 0
            offset = int(transport["next"])
            more = transport.get("more") == "1"
            take(lines)
            if any(line == "OSCAP|done|1" for line in lines):
                break
            if transport.get("alive") == "0" and not lines:
                dead += 1
            else:
                dead = 0
            if dead >= 2:
                collected.append("OSCAP|problem|The scan process on the server ended without finishing.")
                break
            if _now() >= deadline:
                raise fail("The scan did not finish within {} minutes.".format(max(1, int(timeout) // 60)))
            if not more:
                _sleep(POLL_SECONDS)
    finally:
        try:
            _exec(server, cleanup_script(scan_id))
        except Exception:
            pass
    return "\n".join(collected)
