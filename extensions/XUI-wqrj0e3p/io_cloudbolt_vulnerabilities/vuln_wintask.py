"""
A generic runner for a long Windows job: stages a PowerShell worker on the guest, runs it as a detached SYSTEM
scheduled task, and reads the marker lines it appends to status.log. The patch engine (vuln_patch.py) uses it to run
Windows Update; nothing here knows what the worker does.

Transport limits this module is built around (CloudBolt's Server.execute_script on Windows always runs PowerShell):
  - WinRM sends the script as "powershell -encodedcommand <base64 of UTF-16LE>" on one command line, so base64
    length + 27 must stay within 8000 (about 2,900 characters) and a call gives up after about 120 s. On a non-zero
    exit only stderr comes back.
  - AWS SSM returns about 24,000 characters of stdout, Azure Run Command about 4 KB, and VMware Tools treats any
    stderr output as failure.
So every script sent here is short, finishes in seconds, always exits 0, writes nothing to stderr and reports a failure
as a CBWT|terr|... line. The real work runs detached; its lines are CBVULN|key|value in status.log, which poll reads.

Flow: start (prepare the run directory, stage the gzipped base64 worker in chunks, launch the task) -> poll -> cleanup.
Everything lives under Program Files (writable by administrators only), never ProgramData, which any user can write to:
SYSTEM runs the worker from there. The run directory is readable by SYSTEM and Administrators only.

Only a validated run id and integers are ever placed in a script; the worker's own settings travel inside the staged
payload (build_worker), single-quoted.
"""
import base64
import gzip
import re

# Resolved on the guest in every script (ProgramW6432 keeps a 32-bit PowerShell on the 64-bit Program Files).
BASEDEF = "$base=Join-Path $(if($env:ProgramW6432){$env:ProgramW6432}else{$env:ProgramFiles}) 'CloudBolt\\Patching'\n"
RUN_ID_RE = re.compile(r"[A-Za-z0-9-]{8,64}")
KEY_RE = re.compile(r"[A-Za-z][A-Za-z0-9]{0,15}")
# C0 and C1 controls plus the Unicode line and paragraph separators (built from code points: this file is ASCII).
CONTROL_RE = re.compile("[\\x00-\\x1f\\x7f-\\x9f" + chr(0x2028) + chr(0x2029) + "]")
URL_RE = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://\S+")
# PowerShell reads these as single quotes too (U+2018, U+2019, U+201A, U+201B); inside a single-quoted string any of
# them is escaped by doubling.
PS_QUOTES = "'" + "".join(chr(code) for code in (0x2018, 0x2019, 0x201A, 0x201B))
# The UTF-8 byte order mark that makes PowerShell 5.1 read a script file as UTF-8.
BOM = chr(0xFEFF)

TRANSPORT_PREFIX = "CBWT|"
WORKER_PREFIX = "CBVULN|"
TRANSPORT_KEYS = ("prepared", "chunk", "launched", "next", "more", "alive", "cleaned", "boot", "build", "ccm",
                  "reboot", "terr")

# WinRM: len(b64(utf-16-le(script))) + 27 <= 8000. 2,900 ASCII characters gives 7,763.
MAX_SCRIPT_CHARS = 2900
# The stripped worker (comments, blank lines and indentation removed) and the stage calls it may need.
MAX_WORKER_CHARS = 12000
MAX_CHUNKS = 8
EXEC_TIMEOUT = 110
LINE_CAP = 400
BUDGET_AZURE = 3000
BUDGET_DEFAULT = 20000
GRACE_MINUTES = 10


class WinTaskError(RuntimeError):
    """A script could not be delivered, or a call failed. .output is any text that came back."""

    def __init__(self, message, output=""):
        super().__init__(message)
        self.output = output


# ---------------------------------------------------------------- quoting and building
def ps_quote(value):
    """A PowerShell single-quoted string literal for value: quotes doubled, control characters refused."""
    text = "" if value is None else str(value)
    if CONTROL_RE.search(text):
        raise WinTaskError("A setting contains a control character.")
    for quote in PS_QUOTES:
        text = text.replace(quote, quote * 2)
    return "'" + text + "'"


def build_worker(script_path, values):
    """The worker text: a $Cfg hashtable header from values (keys match KEY_RE, values single-quoted), then the file
    without comment lines, blank lines and indentation. Raises WinTaskError for a bad key, a control character,
    non-ASCII text, a double brace (CloudBolt's template engine) or a worker longer than MAX_WORKER_CHARS."""
    rows = []
    for key, value in (values or {}).items():
        if not KEY_RE.fullmatch(str(key)):
            raise WinTaskError("A setting name is not valid.")
        rows.append("{}={}\n".format(key, ps_quote(value)))
    header = "$Cfg=@{\n" + "".join(rows) + "}\n"
    with open(script_path, encoding="utf-8", newline="") as handle:
        body = handle.read().replace("\r\n", "\n")
    lines = [line.strip() for line in body.split("\n")]
    text = header + "\n".join(line for line in lines if line and not line.startswith("#")) + "\n"
    if not text.isascii():
        raise WinTaskError("The worker has characters that are not allowed.")
    if "{" + "{" in text:
        raise WinTaskError("The worker has a template marker in it.")
    if len(text) > MAX_WORKER_CHARS:
        raise WinTaskError("The worker is too long to send ({} of {} characters allowed).".format(len(text), MAX_WORKER_CHARS))
    return text


def encode_worker(text):
    """gzip then base64. The UTF-8 BOM makes PowerShell 5.1 read the file as UTF-8."""
    return base64.b64encode(gzip.compress((BOM + text).encode("utf-8"), 9, mtime=0)).decode("ascii")


def decode_worker(b64):
    """The inverse of encode_worker (used by tests)."""
    return gzip.decompress(base64.b64decode(b64)).decode("utf-8-sig")


# ---------------------------------------------------------------- scripts
PRELUDE = "$ErrorActionPreference='Stop';$ProgressPreference='SilentlyContinue'\n"

# Always run first, every run: creates the run directory, refuses a reparse point, (re)applies the ACL (SYSTEM and
# Administrators only), prunes run directories older than two days and unregisters tasks whose run directory is gone.
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
    + "Get-ChildItem $r -Directory|?{$_.LastWriteTime -lt (Get-Date).AddDays(-2)}|Remove-Item -Recurse -Force -EA 0\n"
    + "Get-ScheduledTask -TaskName 'CloudBolt-Patch-*' -EA 0|?{$i=$_.TaskName.Substring(16);$i -ne '@@ID@@' -and !(Test-Path \"$r\\$i\")}|Unregister-ScheduledTask -Confirm:$false -EA 0\n"
    + "'CBWT|prepared|1'\n"
    + "}catch{'CBWT|terr|prepare: '+$_.Exception.Message}\n"
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
    + "'CBWT|chunk|'+$c\n"
    + "}catch{'CBWT|terr|stage: '+$_.Exception.Message}\n"
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
    + "$n='CloudBolt-Patch-@@ID@@'\n"
    + "$a=New-ScheduledTaskAction -Execute 'powershell.exe' -Argument \"-NoProfile -NonInteractive -ExecutionPolicy Bypass -File `\"$d\\worker.ps1`\"\"\n"
    + "$p=New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest\n"
    + "$s=New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Minutes @@MIN@@) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries\n"
    + "$null=Register-ScheduledTask -TaskName $n -Action $a -Principal $p -Settings $s -Force\n"
    + "Start-ScheduledTask -TaskName $n\n"
    + "'CBWT|launched|1'\n"
    + "}catch{'CBWT|terr|launch: '+$_.Exception.Message}\n"
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
    + "'CBWT|next|'+($n+$k)\n"
    + "'CBWT|more|'+$m\n"
    + "$s=(Get-ScheduledTask -TaskName 'CloudBolt-Patch-@@ID@@' -EA 0).State\n"
    + "'CBWT|alive|'+$(if($s -eq 'Running' -or $s -eq 'Queued'){1}else{0})\n"
    + "}catch{'CBWT|terr|poll: '+$_.Exception.Message}\n"
    + "exit 0\n"
)

CLEANUP = (
    PRELUDE
    + BASEDEF
    + "$n='CloudBolt-Patch-@@ID@@'\n"
    + "try{Stop-ScheduledTask -TaskName $n -EA 0}catch{}\n"
    + "try{Unregister-ScheduledTask -TaskName $n -Confirm:$false -EA 0}catch{}\n"
    + "try{Remove-Item -LiteralPath \"$base\\run\\@@ID@@\" -Recurse -Force -EA 0}catch{}\n"
    + "'CBWT|cleaned|1'\n"
    + "exit 0\n"
)


def _fill(template, **values):
    for key, value in values.items():
        template = template.replace("@@" + key.upper() + "@@", str(value))
    return template


def _check_id(run_id):
    text = str(run_id or "")
    if not RUN_ID_RE.fullmatch(text):
        raise WinTaskError("The run id has characters that are not allowed.")
    return text


def script_fits(script):
    """The WinRM command-line limit (CloudBolt: len(b64(utf-16-le)) + 27 <= 8000)."""
    return len(base64.b64encode(script.encode("utf-16-le"))) + 27 <= 8000


def chunk_size():
    """Characters of base64 per stage call: what the worst-case stage script (64-character id, 8-digit offsets)
    leaves under MAX_SCRIPT_CHARS."""
    overhead = len(_fill(STAGE, id="x" * 64, start="99999999", chunk="", len="9999"))
    return MAX_SCRIPT_CHARS - overhead


def prep_script(run_id):
    return _fill(PREP, id=_check_id(run_id))


def stage_scripts(run_id, b64):
    """The scripts that append b64 to p.b64, one chunk each. Returns [(script, expected acknowledgement)]; the
    acknowledgement is the file length after the chunk."""
    run_id = _check_id(run_id)
    size = chunk_size()
    chunks = [b64[i:i + size] for i in range(0, len(b64), size)] or [""]
    return [
        (_fill(STAGE, id=run_id, start=index * size, chunk=chunk, len=len(chunk)), index * size + len(chunk))
        for index, chunk in enumerate(chunks)
    ]


def launch_script(run_id, limit_s):
    minutes = int(limit_s) // 60 + GRACE_MINUTES
    return _fill(LAUNCH, id=_check_id(run_id), min=minutes)


def poll_script(run_id, offset, budget):
    return _fill(POLL, id=_check_id(run_id), offset=int(offset), cap=LINE_CAP, budget=int(budget))


def cleanup_script(run_id):
    return _fill(CLEANUP, id=_check_id(run_id))


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


def _short(exc):
    return URL_RE.sub("<url>", CONTROL_RE.sub(" ", str(exc))).strip()[:200] or type(exc).__name__


def exec_script(server, script):
    """Run one PowerShell script on the server and return its output as text."""
    return _text(server.execute_script(script_contents=script, timeout=EXEC_TIMEOUT, show_streaming_output=False))


def _split(text):
    """(worker lines, transport values) of one call's output."""
    lines, transport = [], {}
    for raw in _text(text).split("\n"):
        line = raw.strip()
        if line.startswith(TRANSPORT_PREFIX):
            parts = line.split("|", 2)
            if len(parts) == 3 and parts[1] in TRANSPORT_KEYS:
                transport[parts[1]] = parts[2]
        elif line.startswith(WORKER_PREFIX):
            lines.append(line)
    return lines, transport


def _call(server, script):
    try:
        return _split(exec_script(server, script))
    except Exception as exc:
        raise WinTaskError(_short(exc))


def inline(server, script):
    """Run a script and return its CBWT transport values as a dict. Raises WinTaskError when the call raises."""
    return _call(server, script)[1]


def _step(server, script, key, expected, what):
    """One script, tried twice: its reply must carry transport key == expected."""
    reason = ""
    for _attempt in range(2):
        try:
            transport = inline(server, script)
        except WinTaskError as exc:
            reason = str(exc)
            continue
        if transport.get(key) == str(expected):
            return
        reason = (transport.get("terr") or "no reply")[:200]
    raise WinTaskError("{}: {}".format(what, reason))


def start(server, run_id, worker_text, limit_s):
    """Prepare the run directory, stage worker_text and launch it as a SYSTEM task that may run limit_s seconds
    (plus a grace period). Each call is tried twice. Raises WinTaskError; the caller runs cleanup."""
    run_id = _check_id(run_id)
    scripts = stage_scripts(run_id, encode_worker(worker_text))
    if len(scripts) > MAX_CHUNKS:
        raise WinTaskError("The worker is too long to send.")
    _step(server, prep_script(run_id), "prepared", 1, "Could not prepare the server")
    for script, expected in scripts:
        _step(server, script, "chunk", expected, "Could not send the update script to the server")
    _step(server, launch_script(run_id, limit_s), "launched", 1, "Could not start the update on the server")


def poll(server, run_id, offset, budget=None):
    """One read of status.log from line offset: {"lines": [CBVULN lines], "next": int, "more": bool, "alive": bool}.
    One try; raises WinTaskError when the call fails or its reply is not a valid answer."""
    script = poll_script(run_id, offset, _budget(server) if budget is None else budget)
    lines, transport = _call(server, script)
    if "terr" in transport or not str(transport.get("next", "")).isdigit():
        raise WinTaskError((transport.get("terr") or "no reply")[:200])
    return {
        "lines": lines,
        "next": int(transport["next"]),
        "more": transport.get("more") == "1",
        "alive": transport.get("alive") != "0",
    }


def cleanup(server, run_id):
    """Stop and unregister the task and delete the run directory. Never raises."""
    try:
        exec_script(server, cleanup_script(run_id))
    except Exception:
        pass
