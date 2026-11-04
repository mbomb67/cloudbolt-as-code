"""
SCVMM lab: the shared steps of the SCVMM Lab on AWS blueprint
(blueprints/BP-9zp8udce).

The blueprint builds two or more Hyper-V hosts (server tier HOST_TIER) and
one SCVMM server (server tier VMM_TIER) as EC2 instances that the Join
Windows Server to AD Domain action (orchestration_actions/HPA-jjp1vlfi)
joins to an existing domain. Its build plug-ins then call this module for:

  - the lab's servers, domain and service account (Lab);
  - PowerShell in the guest through Server.execute_script (run_ps). Results
    come back in CB_LAB_* marker lines and every script exits 0, because
    CloudBolt's AWS SSM transport reports only the exit code of a failed
    script, not its output. A script too long for WinRM's one command line
    is uploaded in pieces and run from the guest (_stage);
  - long steps, and steps that must run as the domain account, as a one-off
    scheduled task polled every POLL_INTERVAL seconds: run_task on one
    server, or start_task on each server then wait_tasks for all. No
    transport holds a session open for an hour, and a step that talks to AD
    or to other servers signs in with the account's own password, so there
    is no second hop to lose its credentials on;
  - restarts (restart, wait_for_restart);
  - checking that every lab server's name resolves to its own address
    (check_dns), before the steps that reach the servers by name;
  - EC2 calls through the handler's boto3 client: nested virtualization,
    the hosts' cluster IP addresses and the storage volume.

Every step is safe to run again, so a failed build item can be re-run.

The domain account comes from the domain_service_account parameter the join
action uses: the ID of a connection info labeled domain_service_account. Its
password reaches the guest inside a script body, base64 encoded for quoting
only; it is scrubbed from everything this module logs or returns, and the
task's script file is deleted when the task ends. On AWS the script body stays
in Systems Manager command history (see the blueprint README).

References:
  Server.execute_script, AWS SSM:  typings/infrastructure/models.pyi, resourcehandlers/aws run_script_on_guest
  Register-ScheduledTask:          https://learn.microsoft.com/en-us/powershell/module/scheduledtasks/register-scheduledtask
  New-ScheduledTaskPrincipal:      https://learn.microsoft.com/en-us/powershell/module/scheduledtasks/new-scheduledtaskprincipal
  Resolve-DnsName:                 https://learn.microsoft.com/en-us/powershell/module/dnsclient/resolve-dnsname
  Register-DnsClient:              https://learn.microsoft.com/en-us/powershell/module/dnsclient/register-dnsclient
  Clear-DnsClientCache:            https://learn.microsoft.com/en-us/powershell/module/dnsclient/clear-dnsclientcache
  Nested virtualization:           https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/amazon-ec2-nested-virtualization.html
  ModifyInstanceCpuOptions:        https://docs.aws.amazon.com/AWSEC2/latest/APIReference/API_ModifyInstanceCpuOptions.html
  AssignPrivateIpAddresses:        https://docs.aws.amazon.com/AWSEC2/latest/APIReference/API_AssignPrivateIpAddresses.html
  CreateVolume / AttachVolume:     https://docs.aws.amazon.com/AWSEC2/latest/APIReference/API_CreateVolume.html
                                   https://docs.aws.amazon.com/AWSEC2/latest/APIReference/API_AttachVolume.html
  ModifyInstanceAttribute:         https://docs.aws.amazon.com/AWSEC2/latest/APIReference/API_ModifyInstanceAttribute.html
  ModifyVolume:                    https://docs.aws.amazon.com/AWSEC2/latest/APIReference/API_ModifyVolume.html
  DescribeVolumesModifications:    https://docs.aws.amazon.com/AWSEC2/latest/APIReference/API_DescribeVolumesModifications.html
  DescribeInstanceTypes:           https://docs.aws.amazon.com/AWSEC2/latest/APIReference/API_DescribeInstanceTypes.html
  Windows device names (Nitro):    https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/device_naming.html
  boto3 EC2 client and waiters:    https://boto3.amazonaws.com/v1/documentation/api/latest/reference/services/ec2.html
"""
import base64
import gzip
import re
import secrets
import time
from contextlib import contextmanager

from botocore.exceptions import ClientError

from common.methods import set_progress
from infrastructure.models import CustomField
from utilities.logger import ThreadLogger
from utilities.models import ConnectionInfo

logger = ThreadLogger(__name__)

HOST_TIER = "Hyper-V Hosts"
VMM_TIER = "SCVMM Server"
# The parameter and the connection info label share this name (HPA-jjp1vlfi).
SERVICE_ACCOUNT = "domain_service_account"
# Tag on the storage volume, valued with the resource ID.
VOLUME_TAG = "cloudbolt-scvmm-lab"
# File system label of the storage volume on the SCVMM server.
STORAGE_LABEL = "CBLab"
TASK_PATH = "\\CloudBolt\\"

GUEST_READY_WAIT = 900
POLL_INTERVAL = 30
RESTART_WAIT = 1200

FIELDS = {
    "scvmm_lab_domain": ("AD Domain", "Active Directory domain of the SCVMM lab servers."),
    "scvmm_lab_cluster": ("Hyper-V Cluster", "Fully qualified name of the lab's Hyper-V failover cluster."),
    "scvmm_lab_cluster_ips": ("Cluster IP Addresses",
                              "Secondary private IP address of each host, one per cluster IP address resource."),
    "scvmm_lab_vmm_server": ("SCVMM Server", "Fully qualified name of the lab's SCVMM management server."),
}

MARKER = re.compile(r"^CB_LAB_([A-Z0-9_]+)=(.*?)\s*$", re.MULTILINE)
RESPONSE_CODE = re.compile(r"Response Code: (-?\d+)")
COMPUTER_NAME = re.compile(r"^(?![0-9]+$)[A-Za-z0-9-]{1,15}$")

PRELUDE = r"""
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$WarningPreference = 'SilentlyContinue'
function Decode([string]$Value) { [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($Value)) }
function Mark([string]$Name, $Value) { Write-Output ('CB_LAB_{0}={1}' -f $Name, ("$Value" -replace '[\r\n]+', ' ')) }
function Log([string]$Text) { Write-Output ('[{0}] {1}' -f (Get-Date -Format 's'), $Text) }
# A lookup that may throw a terminating "not found" error, which -ErrorAction does not stop: $null instead.
function Lookup([scriptblock]$Query) { try { & $Query } catch { $null } }
"""

# Wraps every step: errors become ERROR: lines and the script still exits 0.
WRAPPER = PRELUDE + r"""
try {
__CB_BODY__
    Mark 'STEP' 'OK'
} catch {
    Write-Output "ERROR: $($_.Exception.Message)"
}
exit 0
"""

START_TASK = r"""
$Tag = Decode '__CB_TAG__'
$Root = Join-Path $env:ProgramData 'CloudBolt\ScvmmLab'
if (-not (Test-Path -LiteralPath $Root)) {
    New-Item -ItemType Directory -Path $Root -Force | Out-Null
    # Step scripts can hold the domain account's password: SYSTEM and Administrators only.
    & icacls.exe $Root /inheritance:r /grant:r '*S-1-5-18:(OI)(CI)F' '*S-1-5-32-544:(OI)(CI)F' | Out-Null
}
$Step = Join-Path $Root "$Tag.ps1"
$Runner = Join-Path $Root "$Tag-run.ps1"
$Log = Join-Path $Root "$Tag.log"
$Exit = Join-Path $Root "$Tag.exit"
$TempDir = Decode '__CB_TEMP__'
$utf8 = New-Object Text.UTF8Encoding $true
[IO.File]::WriteAllText($Step, (Decode '__CB_SCRIPT__'), $utf8)
$runnerLines = @(
    '$code = 1',
    'try {',
    "    if ('$TempDir') { New-Item -ItemType Directory -Force -Path '$TempDir' | Out-Null; `$env:TMP = '$TempDir'; `$env:TEMP = '$TempDir' }",
    "    & '$Step' *> '$Log'",
    '    $code = 0 + $LASTEXITCODE',
    '} catch {',
    "    Add-Content -LiteralPath '$Log' -Value ('ERROR: ' + `$_.Exception.Message)",
    '} finally {',
    "    Remove-Item -LiteralPath '$Step' -Force -ErrorAction SilentlyContinue",
    "    Set-Content -LiteralPath '$Exit' -Value `$code",
    '}'
)
[IO.File]::WriteAllText($Runner, ($runnerLines -join "`r`n"), $utf8)
$action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument ('-NoProfile -NonInteractive -ExecutionPolicy Bypass -File "{0}"' -f $Runner)
$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Hours 4) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew
$User = Decode '__CB_USER__'
if ($User) {
    # Task Scheduler stores DOMAIN\user; resolve a UPN through its SID.
    $sid = (New-Object Security.Principal.NTAccount $User).Translate([Security.Principal.SecurityIdentifier])
    $User = $sid.Translate([Security.Principal.NTAccount]).Value
    Register-ScheduledTask -TaskName $Tag -TaskPath '__TASK_PATH__' -Action $action -Settings $settings -User $User -Password (Decode '__CB_PASSWORD__') -RunLevel Highest -Force | Out-Null
} else {
    $principal = New-ScheduledTaskPrincipal -UserId 'NT AUTHORITY\SYSTEM' -LogonType ServiceAccount -RunLevel Highest
    Register-ScheduledTask -TaskName $Tag -TaskPath '__TASK_PATH__' -Action $action -Settings $settings -Principal $principal -Force | Out-Null
}
Start-ScheduledTask -TaskName $Tag -TaskPath '__TASK_PATH__'
Mark 'TASK_USER' $(if ($User) { $User } else { 'SYSTEM' })
""".replace("__TASK_PATH__", TASK_PATH)

POLL_TASK = r"""
$Tag = Decode '__CB_TAG__'
$Root = Join-Path $env:ProgramData 'CloudBolt\ScvmmLab'
$Log = Join-Path $Root "$Tag.log"
$Exit = Join-Path $Root "$Tag.exit"
if (Test-Path -LiteralPath $Exit) {
    Mark 'TASK_STATE' 'DONE'
    Mark 'TASK_EXIT' ((Get-Content -LiteralPath $Exit -Raw).Trim())
    if (Test-Path -LiteralPath $Log) {
        # Prefixed so the step's own ERROR: lines do not read as this script's.
        Get-Content -LiteralPath $Log -Tail 120 | ForEach-Object { 'LOG| ' + ("$_" -replace '^(.{300}).+$', '$1...') }
    }
    Unregister-ScheduledTask -TaskName $Tag -TaskPath '__TASK_PATH__' -Confirm:$false -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $Log, $Exit, (Join-Path $Root "$Tag-run.ps1"), (Join-Path $Root "$Tag.ps1") -Force -ErrorAction SilentlyContinue
} else {
    $task = Get-ScheduledTask -TaskName $Tag -TaskPath '__TASK_PATH__' -ErrorAction SilentlyContinue
    if (-not $task) {
        Mark 'TASK_STATE' 'MISSING'
    } else {
        $info = Get-ScheduledTaskInfo -InputObject $task
        Mark 'TASK_STATE' $(if ($task.State -eq 'Running') { 'RUNNING' } else { 'NOT_RUNNING' })
        Mark 'TASK_RESULT' ('0x{0:X8}' -f $info.LastTaskResult)
        if (Test-Path -LiteralPath $Log) {
            $last = Get-Content -LiteralPath $Log -Tail 1
            if ($last) { Mark 'TASK_TAIL' ("$last" -replace '^(.{200}).+$', '$1...') }
        }
    }
}
""".replace("__TASK_PATH__", TASK_PATH)

# Task Scheduler results seen when the task cannot start as the account.
TASK_RESULT_HINTS = {
    "0x8007052E": "the domain account's user name or password is wrong",
    "0x80070569": "the domain account lacks the 'Log on as a batch job' right on this server "
                  "(members of the local Administrators group have it by default)",
    "0x80070532": "the domain account's password has expired",
    "0x80070533": "the domain account is disabled",
}

# CloudBolt's WinRM transport runs a script as powershell -encodedcommand
# (UTF-16 base64) on one command line, which Windows limits to 8191
# characters: about 3,000 characters of script. A longer script is gzipped
# and written in CHUNK-sized base64 pieces to the lab folder (SYSTEM and
# Administrators only, because a step can hold the domain account's
# password), then STAGE_RUN reads it back, deletes the file and runs it in
# the same session, so its output and exit code are the script's own.
INLINE_LIMIT = 2800
CHUNK = 2400
STAGE_CHUNK = r"""
$ErrorActionPreference = 'Stop'
$r = Join-Path $env:ProgramData 'CloudBolt\ScvmmLab'
if (-not (Test-Path -LiteralPath $r)) {
    New-Item -ItemType Directory -Path $r -Force | Out-Null
    & icacls.exe $r /inheritance:r /grant:r '*S-1-5-18:(OI)(CI)F' '*S-1-5-32-544:(OI)(CI)F' | Out-Null
}
[IO.File]::__METHOD__((Join-Path $r '__PATH__'), '__CHUNK__')
"""
STAGE_RUN = r"""
$ErrorActionPreference = 'Stop'
$f = Join-Path $env:ProgramData 'CloudBolt\ScvmmLab\__PATH__'
try { $b = [Convert]::FromBase64String([IO.File]::ReadAllText($f)) } finally { Remove-Item -LiteralPath $f -Force -ErrorAction SilentlyContinue }
$z = New-Object IO.Compression.GZipStream((New-Object IO.MemoryStream(,$b)), [IO.Compression.CompressionMode]::Decompress)
& ([scriptblock]::Create((New-Object IO.StreamReader($z, [Text.Encoding]::UTF8)).ReadToEnd()))
"""

RESTART = r"""
Mark 'BOOT' ((Get-CimInstance -ClassName Win32_OperatingSystem).LastBootUpTime.ToUniversalTime().Ticks)
& shutdown.exe /r /t 15 /d p:4:2 /c 'CloudBolt SCVMM lab: restart to finish installing Windows features.' | Out-Null
if ($LASTEXITCODE) { throw "shutdown.exe could not schedule the restart (exit $LASTEXITCODE)." }
"""

BOOT = r"""
Mark 'BOOT' ((Get-CimInstance -ClassName Win32_OperatingSystem).LastBootUpTime.ToUniversalTime().Ticks)
"""

DNS_RESOLVE = r"""
    $names = @((Decode '__CB_NAMES__') -split ',')
    Clear-DnsClientCache
    for ($i = 0; $i -lt $names.Count; $i++) {
        $found = @(Lookup { Resolve-DnsName -Name $names[$i] -Type A -DnsOnly -NoHostsFile -ErrorAction Stop } |
            Where-Object { $_.Type -eq 'A' } | ForEach-Object { $_.IPAddress })
        Mark "DNS_$i" ($found -join ' ')
    }
"""

DNS_REGISTER = r"""
    Register-DnsClient
"""

DNS_WAIT = 180


class LabError(Exception):
    """A problem to show the user as the build item's error."""


# ---------------------------------------------------------------- lab layout

def _tier(server):
    item = getattr(server, "service_item", None)
    return (getattr(item, "name", "") or "").strip()


def computer_name(server):
    """The Windows computer name: the CloudBolt hostname's first label, as the join action sets it."""
    name = server.hostname.split(".")[0]
    if not COMPUTER_NAME.match(name):
        raise LabError(f"{server.hostname}: '{name}' is not a valid Windows computer name (at most 15 "
                       "letters, digits and hyphens, not only digits).")
    return name


def service_account(server):
    """The connection info the server's domain_service_account names. Raises LabError when unusable."""
    value = str(server.get_value_for_custom_field(SERVICE_ACCOUNT) or "").strip()
    if not value:
        raise LabError(f"{server.hostname} has no {SERVICE_ACCOUNT}. Set it next to domain_ou on the "
                       "environment, group or blueprint: the lab runs its cluster and SCVMM steps as that "
                       "domain account.")
    account = None
    if value.isdigit():
        account = ConnectionInfo.objects.filter(labels__name=SERVICE_ACCOUNT, id=int(value)).first()
    if account is None:
        raise LabError(f"{SERVICE_ACCOUNT} {value} on {server.hostname} is not the ID of a connection "
                       f"info labeled {SERVICE_ACCOUNT}.")
    username = (account.username or "").strip()
    if "@" not in username and "\\" not in username:
        raise LabError(f"The username of connection info '{account.name}' must include its domain, as "
                       "user@corp.example.com.")
    if not account.password:
        raise LabError(f"Connection info '{account.name}' has no password.")
    return account


class Lab:
    """The resource's hosts, SCVMM server and the domain account they share."""

    def __init__(self, resource):
        self.resource = resource
        tiers = {}
        for server in resource.server_set.exclude(status="HISTORICAL"):
            tiers.setdefault(_tier(server), []).append(server)
        self.hosts = sorted(tiers.get(HOST_TIER, []), key=lambda s: s.hostname.lower())
        vmm = tiers.get(VMM_TIER, [])
        found = ", ".join(f"'{name}' ({len(servers)})" for name, servers in tiers.items()) or "none"
        if len(self.hosts) < 2:
            raise LabError(f"The lab needs at least two servers in the '{HOST_TIER}' tier for a failover "
                           f"cluster. Server tiers on this resource: {found}.")
        if len(vmm) != 1:
            raise LabError(f"The lab needs exactly one server in the '{VMM_TIER}' tier. "
                           f"Server tiers on this resource: {found}.")
        self.vmm = vmm[0]
        accounts = {server.hostname: service_account(server) for server in self.servers}
        ids = {account.id for account in accounts.values()}
        if len(ids) > 1:
            raise LabError("Every lab server must use the same domain_service_account; found "
                           + ", ".join(f"{h}: '{a.name}'" for h, a in accounts.items()) + ".")
        self.account = accounts[self.vmm.hostname]
        self.username = self.account.username.strip()
        self.password = self.account.password

    @property
    def servers(self):
        return self.hosts + [self.vmm]

    @property
    def domain(self):
        return str(self.resource.get_value_for_custom_field("scvmm_lab_domain") or "")

    def fqdn(self, server):
        return f"{computer_name(server)}.{self.domain}".lower()

    @property
    def cluster_name(self):
        """The cluster's NetBIOS name, at most 15 characters and unique per resource."""
        return f"hvc-{self.resource.id}"

    @property
    def cluster_fqdn(self):
        return f"{self.cluster_name}.{self.domain}".lower()


def ensure_fields():
    for name, (label, description) in FIELDS.items():
        CustomField.objects.get_or_create(
            name=name, defaults=dict(label=label, description=description, type="STR", show_as_attribute=True),
        )


# ------------------------------------------------------------ guest scripts

def b64(text):
    return base64.b64encode(str(text).encode("utf-8")).decode("ascii")


def scrub(text, hidden=()):
    text = str(text or "")
    for value in hidden:
        if value:
            text = text.replace(value, "***").replace(b64(value), "***")
    return text


def render(body, tokens=None):
    """The body wrapped in WRAPPER, with every __CB_<NAME>__ token replaced by its value, base64 encoded."""
    script = WRAPPER.replace("__CB_BODY__", body)
    for name, value in (tokens or {}).items():
        script = script.replace(f"__CB_{name}__", b64(value))
    missing = sorted(set(re.findall(r"__CB_[A-Z0-9_]+__", script)))
    if missing:
        raise LabError(f"Internal error: no value for {', '.join(missing)}.")
    return script


def _execute(server, script, timeout):
    """(exit code, output) of a PowerShell script; -1 when CloudBolt could not run it at all."""
    if len(script) > INLINE_LIMIT:
        rv, output, script = _stage(server, script)
        if rv != 0:
            return rv, output
    return _run(server, script, timeout)


def _stage(server, script):
    """
    Upload a script too long for one command line to the guest in pieces.
    Returns (exit code, output, bootstrap): bootstrap is a short script that
    reads the pieces back, deletes the file and runs the script.
    """
    payload = base64.b64encode(gzip.compress(script.encode("utf-8"))).decode("ascii")
    path = f"{secrets.token_hex(6)}.b64"
    pieces = [payload[i:i + CHUNK] for i in range(0, len(payload), CHUNK)]
    for index, piece in enumerate(pieces):
        method = "WriteAllText" if index == 0 else "AppendAllText"
        rv, output = _run(server, STAGE_CHUNK.replace("__PATH__", path).replace("__METHOD__", method)
                          .replace("__CHUNK__", piece), 120)
        if rv != 0:
            return rv, output, ""
    return 0, "", STAGE_RUN.replace("__PATH__", path)


def _run(server, script, timeout):
    try:
        output = server.execute_script(
            script_contents=script, file_extension="ps1", timeout=timeout, show_streaming_output=False,
        )
        return 0, str(output or "")
    except Exception as err:
        rv = getattr(err, "rv", None)
        if rv is None:
            # The AWS SSM transport raises "... Response Code: N." for a non-zero exit.
            match = RESPONSE_CODE.search(str(err))
            rv = match.group(1) if match else -1
        try:
            rv = int(rv)
        except (TypeError, ValueError):
            rv = -1
        return rv, str(getattr(err, "output", "") or err)


def errors(output):
    return [line.strip()[len("ERROR:"):].strip() for line in (output or "").splitlines()
            if line.strip().startswith("ERROR:")]


def markers(output):
    return {name: value for name, value in MARKER.findall(output or "")}


def run_ps(server, body, tokens=None, timeout=300, ready_wait=GUEST_READY_WAIT, hidden=()):
    """
    Run body on server and return (markers, output). Retries for up to
    ready_wait seconds while CloudBolt cannot run a script in the guest (the
    SSM agent or WinRM is not up yet). Raises LabError when the script
    reports an error or does not finish.
    """
    script = render(body, tokens)
    deadline = time.time() + ready_wait
    while True:
        rv, output = _execute(server, script, timeout)
        if rv != -1 or time.time() + POLL_INTERVAL > deadline:
            break
        time.sleep(POLL_INTERVAL)
    output = scrub(output, hidden)
    problems = errors(output)
    if problems:
        raise LabError(f"{server.hostname}: {' '.join(problems)}")
    found = markers(output)
    if rv != 0 or found.get("STEP") != "OK":
        what = "CloudBolt could not run a script in the guest" if rv == -1 else f"the script ended with exit {rv}"
        raise LabError(f"{server.hostname}: {what}: {output.strip()[-500:]}")
    return found, output


def run_task(server, body, label, tokens=None, account=None, timeout=3600, hidden=(), temp_dir="",
             archive_refs=None, job=None):
    """
    Run body on server as a scheduled task, as account (a ConnectionInfo) or
    as SYSTEM when account is None, and return its log. Waits up to timeout
    seconds. archive_refs are File Archives references ("archive/file
    [extract]") the task downloads before body runs; see the file_archives
    shared module (shared_modules/SHM-v8llc736). temp_dir becomes the task's
    TMP and TEMP, so large downloads land there.
    """
    task = start_task(server, body, label, tokens=tokens, account=account, hidden=hidden, temp_dir=temp_dir,
                      archive_refs=archive_refs, job=job)
    return wait_tasks([task], timeout)[0]


def start_task(server, body, label, tokens=None, account=None, hidden=(), temp_dir="", archive_refs=None, job=None):
    """
    Start body on server as a scheduled task (arguments as for run_task) and
    return a handle for wait_tasks. Start one per server, then wait for them
    together, so the servers do the step at the same time.
    """
    hidden = list(hidden)
    user, password = "", ""
    if account is not None:
        user, password = account.username.strip(), account.password
        hidden.append(password)
    step = render(body, tokens)
    if archive_refs:
        try:
            from shared_modules import file_archives
        except ImportError:
            raise LabError("The file_archives shared module (SHM-v8llc736) is not on this CloudBolt. "
                           "Sync the Runbooks package (packages/PKG-v20qv308).")
        try:
            step, _ = file_archives.prepare(step, server, job, extra_refs=archive_refs, minutes=30)
        except file_archives.ArchiveError as err:
            raise LabError(f"File Archives: {err}")
    tag = f"cblab-{secrets.token_hex(4)}"
    run_ps(server, START_TASK, tokens={
        "TAG": tag, "SCRIPT": step, "USER": user, "PASSWORD": password, "TEMP": temp_dir,
    }, hidden=hidden)
    set_progress(f"{server.hostname}: {label} (scheduled task {TASK_PATH}{tag} as {user or 'SYSTEM'})")
    return {"server": server, "tag": tag, "label": label, "hidden": hidden,
            "log": None, "error": None, "tail": "", "not_running": 0}


def _poll(task):
    """Check one started task once; sets its log when it finished, or its error when it failed."""
    server, tag, label, hidden = task["server"], task["tag"], task["label"], task["hidden"]
    try:
        found, output = run_ps(server, POLL_TASK, tokens={"TAG": tag}, hidden=hidden)
    except LabError as err:
        task["error"] = str(err)
        return
    state = found.get("TASK_STATE")
    if state == "DONE":
        log = "\n".join(line[len("LOG| "):] for line in output.splitlines() if line.startswith("LOG| "))
        log = scrub(log, hidden)
        problems = errors(log)
        if problems or markers(log).get("STEP") != "OK":
            detail = " ".join(problems) or log.strip()[-800:] or f"exit {found.get('TASK_EXIT')}"
            task["error"] = f"{server.hostname}: {label} failed: {detail}"
        else:
            task["log"] = log
    elif state == "MISSING":
        task["error"] = f"{server.hostname}: the scheduled task for '{label}' disappeared before it finished."
    elif state == "NOT_RUNNING":
        # Right after Start-ScheduledTask the state can still read Ready.
        task["not_running"] += 1
        if task["not_running"] >= 2:
            code = found.get("TASK_RESULT", "")
            hint = TASK_RESULT_HINTS.get(code, "see Task Scheduler's history for the task")
            task["error"] = (f"{server.hostname}: the scheduled task for '{label}' did not run "
                             f"(result {code}): {hint}.")
    else:
        task["not_running"] = 0
        tail = found.get("TASK_TAIL", "")
        if tail and tail != task["tail"]:
            set_progress(f"{server.hostname}: {tail}")
            task["tail"] = tail


def wait_tasks(tasks, timeout=3600):
    """
    Poll tasks from start_task together every POLL_INTERVAL seconds until each
    has finished, and return their logs in the same order. Waits for every
    task even after one fails, so none is left running unreported, then raises
    LabError naming every failure.
    """
    deadline = time.time() + timeout
    pending = list(tasks)
    while pending:
        time.sleep(POLL_INTERVAL)
        for task in pending:
            _poll(task)
        pending = [task for task in pending if task["log"] is None and task["error"] is None]
        if pending and time.time() > deadline:
            for task in pending:
                task["error"] = (f"{task['server'].hostname}: '{task['label']}' did not finish within "
                                 f"{timeout // 60} minutes. Its scheduled task {TASK_PATH}{task['tag']} "
                                 f"is still on the server.")
            break
    failures = [task["error"] for task in tasks if task["error"]]
    if failures:
        raise LabError(" ".join(failures))
    return [task["log"] for task in tasks]


def restart(servers, reason):
    """Restart servers together and wait until each has booted again."""
    boots = {}
    for server in servers:
        found, _ = run_ps(server, RESTART)
        boots[server.id] = int(found["BOOT"])
        set_progress(f"{server.hostname}: restarting to {reason}")
    time.sleep(60)
    for server in servers:
        wait_for_restart(server, boots[server.id])


def wait_for_restart(server, boot_ticks):
    deadline = time.time() + RESTART_WAIT
    while True:
        rv, output = _execute(server, render(BOOT), 120)
        ticks = markers(output).get("BOOT", "")
        if rv == 0 and ticks.isdigit() and int(ticks) > boot_ticks:
            set_progress(f"{server.hostname}: back up after the restart")
            return
        if time.time() + POLL_INTERVAL > deadline:
            raise LabError(f"{server.hostname} did not come back within {RESTART_WAIT // 60} minutes of its restart.")
        time.sleep(POLL_INTERVAL)


def check_dns(lab, wait=DNS_WAIT):
    """
    Check, from the first host, that every lab server's name resolves to its
    primary private IP and to no address outside its own instance: the cluster
    and SCVMM reach the servers by name. A server whose name resolves
    elsewhere, or not at all, is asked to register again (Register-DnsClient)
    and checked until wait seconds pass. Raises LabError naming each wrong name.
    """
    servers = lab.servers
    names = [lab.fqdn(server) for server in servers]
    expected = []
    for server in servers:
        instance = describe_instance(ec2_client(server), server)
        own = {address["PrivateIpAddress"] for interface in instance.get("NetworkInterfaces", [])
               for address in interface.get("PrivateIpAddresses", [])}
        expected.append((instance["PrivateIpAddress"], own))
    deadline = time.time() + wait
    asked = set()
    while True:
        found, _ = run_ps(lab.hosts[0], DNS_RESOLVE, tokens={"NAMES": ",".join(names)})
        answers = [set(found.get(f"DNS_{i}", "").split()) for i in range(len(servers))]
        wrong = [i for i, answer in enumerate(answers) if expected[i][0] not in answer or not answer <= expected[i][1]]
        if not wrong:
            set_progress("DNS: " + ", ".join(f"{name} is {ip}" for name, (ip, _) in zip(names, expected)))
            return
        if time.time() + POLL_INTERVAL > deadline:
            break
        for i in wrong:
            if i not in asked:
                asked.add(i)
                run_ps(servers[i], DNS_REGISTER)
                set_progress(f"DNS: {names[i]} resolves to {', '.join(sorted(answers[i])) or 'nothing'}, not "
                             f"{expected[i][0]}; asked {servers[i].hostname} to register its name again")
        time.sleep(POLL_INTERVAL)
    problems = [f"{names[i]} resolves to {', '.join(sorted(answers[i])) or 'nothing'}, not {expected[i][0]}"
                for i in wrong]
    raise LabError(
        "; ".join(problems) + ". The cluster and SCVMM reach the lab servers by name. The usual cause is a DNS "
        "record an earlier server with the same name left behind: the zone lets only a record's owner update "
        f"it, so the new server cannot replace it. Delete those records in the {lab.domain} zone, then run this "
        "build item again.")


# ------------------------------------------------------------------- EC2

def ec2_client(server):
    handler = server.resource_handler.cast()
    return handler.get_boto3_client(server.ec2serverinfo.ec2_region, "ec2")


def describe_instance(client, server):
    reply = client.describe_instances(InstanceIds=[server.resource_handler_svr_id])
    return reply["Reservations"][0]["Instances"][0]


def is_metal(instance_type):
    return instance_type.partition(".")[2].startswith("metal")


def supports_nested(client, instance_type):
    """
    Whether AWS lists nested-virtualization in the type's supported processor
    features, the check the AWS nested virtualization page gives. The old
    botocore model does not know the value, but its parser passes it through.
    """
    reply = client.describe_instance_types(InstanceTypes=[instance_type])
    features = reply["InstanceTypes"][0].get("ProcessorInfo", {}).get("SupportedFeatures", [])
    return "nested-virtualization" in features


@contextmanager
def _raw_reply(client, operation):
    """Collect the raw XML replies of operation, for fields the installed botocore model does not parse."""
    replies = []

    def keep(http_response, **kwargs):
        replies.append(http_response.text)

    event = f"after-call.ec2.{operation}"
    client.meta.events.register(event, keep)
    try:
        yield replies
    finally:
        client.meta.events.unregister(event, keep)


@contextmanager
def _nested_virtualization_field(client, keep_cpu_counts):
    """
    Add NestedVirtualization=enabled to ModifyInstanceCpuOptions requests.
    The botocore on the appliance predates the field and requires CoreCount
    and ThreadsPerCore, which the API itself does not; the field is added to
    the form body before SigV4 signs it, and the counts are dropped unless
    keep_cpu_counts.
    """
    def add(params, **kwargs):
        body = params["body"]
        body["NestedVirtualization"] = "enabled"
        if not keep_cpu_counts:
            body.pop("CoreCount", None)
            body.pop("ThreadsPerCore", None)

    event = "before-call.ec2.ModifyInstanceCpuOptions"
    client.meta.events.register(event, add)
    try:
        yield
    finally:
        client.meta.events.unregister(event, add)


def nested_virtualization_enabled(client, server):
    with _raw_reply(client, "DescribeInstances") as replies:
        client.describe_instances(InstanceIds=[server.resource_handler_svr_id])
    cpu = re.search(r"<cpuOptions>(.*?)</cpuOptions>", replies[-1] if replies else "", re.S)
    return bool(cpu and "<nestedVirtualization>enabled</nestedVirtualization>" in cpu.group(1))


def _modify_cpu_options(client, instance, keep_cpu_counts):
    cpu = instance.get("CpuOptions") or {}
    with _nested_virtualization_field(client, keep_cpu_counts), _raw_reply(client, "ModifyInstanceCpuOptions") as replies:
        client.modify_instance_cpu_options(
            InstanceId=instance["InstanceId"],
            CoreCount=cpu.get("CoreCount", 1),
            ThreadsPerCore=cpu.get("ThreadsPerCore", 1),
        )
    return replies[-1] if replies else ""


def enable_nested_virtualization(client, server):
    """Stop the instance, turn nested virtualization on and start it. Returns what was done."""
    instance = describe_instance(client, server)
    instance_type = instance["InstanceType"]
    if is_metal(instance_type):
        return f"{instance_type} is bare metal; Hyper-V runs without nested virtualization"
    if not supports_nested(client, instance_type):
        raise LabError(f"{server.hostname} is {instance_type}, which AWS does not list as supporting nested "
                       "virtualization. Use a size from a family the AWS nested virtualization page lists "
                       "(for example m8i, c8i, r8i or m7i), or a .metal size.")
    if nested_virtualization_enabled(client, server):
        return "nested virtualization was already on"
    instance_id = instance["InstanceId"]
    waiter_config = {"Delay": 15, "MaxAttempts": 80}
    set_progress(f"{server.hostname}: stopping {instance_id} to turn on nested virtualization")
    client.stop_instances(InstanceIds=[instance_id])
    client.get_waiter("instance_stopped").wait(InstanceIds=[instance_id], WaiterConfig=waiter_config)
    try:
        reply = _modify_cpu_options(client, instance, keep_cpu_counts=False)
    except ClientError as err:
        if err.response.get("Error", {}).get("Code") != "MissingParameter":
            raise
        reply = _modify_cpu_options(client, instance, keep_cpu_counts=True)
    if "<nestedVirtualization>enabled</nestedVirtualization>" not in reply:
        logger.warning("ModifyInstanceCpuOptions reply for %s did not echo nestedVirtualization: %s",
                       instance_id, reply[:500])
    set_progress(f"{server.hostname}: starting {instance_id}")
    client.start_instances(InstanceIds=[instance_id])
    client.get_waiter("instance_running").wait(InstanceIds=[instance_id], WaiterConfig=waiter_config)
    try:
        # A public IP changes across a stop and start.
        server.refresh_info(skip_tags=True)
    except Exception as err:
        logger.warning("refresh_info for %s failed: %s", server.hostname, err)
    return "stopped it, turned nested virtualization on and started it"


def _primary_interface(instance):
    for interface in instance.get("NetworkInterfaces", []):
        if interface.get("Attachment", {}).get("DeviceIndex") == 0:
            return interface
    raise LabError(f"Instance {instance['InstanceId']} has no primary network interface.")


def cluster_ip(client, server, assign=False):
    """The secondary private IP of the host's primary interface; assigns one when assign and there is none."""
    interface = _primary_interface(describe_instance(client, server))
    secondary = [a["PrivateIpAddress"] for a in interface.get("PrivateIpAddresses", []) if not a.get("Primary")]
    if secondary or not assign:
        return secondary[0] if secondary else ""
    reply = client.assign_private_ip_addresses(
        NetworkInterfaceId=interface["NetworkInterfaceId"], SecondaryPrivateIpAddressCount=1,
    )
    assigned = [a["PrivateIpAddress"] for a in reply.get("AssignedPrivateIpAddresses", [])]
    if not assigned:
        raise LabError(f"{server.hostname}: AWS assigned no secondary private IP to {interface['NetworkInterfaceId']}.")
    return assigned[0]


def ensure_root_size(client, server, size_gb):
    """Grow the instance's root EBS volume to size_gb when it is smaller. The guest must then extend C:."""
    instance = describe_instance(client, server)
    root = next((m for m in instance.get("BlockDeviceMappings", [])
                 if m.get("DeviceName") == instance.get("RootDeviceName")), None)
    if root is None:
        raise LabError(f"{server.hostname}: cannot find the root volume of {instance['InstanceId']}.")
    volume_id = root["Ebs"]["VolumeId"]
    current = client.describe_volumes(VolumeIds=[volume_id])["Volumes"][0]["Size"]
    if current >= size_gb:
        return f"root volume {volume_id} is {current} GB"
    client.modify_volume(VolumeId=volume_id, Size=int(size_gb))
    # The new size is usable once the modification is optimizing or completed.
    deadline = time.time() + 900
    while True:
        state = client.describe_volumes_modifications(VolumeIds=[volume_id])["VolumesModifications"][0]
        if state["ModificationState"] in ("optimizing", "completed"):
            return f"grew root volume {volume_id} from {current} to {size_gb} GB"
        if state["ModificationState"] == "failed":
            raise LabError(f"{server.hostname}: growing {volume_id} failed: {state.get('StatusMessage', '')}")
        if time.time() > deadline:
            raise LabError(f"{server.hostname}: growing {volume_id} did not finish within 15 minutes.")
        time.sleep(10)


def ensure_storage_volume(client, server, size_gb, resource_id):
    """Create and attach the lab's gp3 storage volume to server once. Returns (volume ID, what was done)."""
    instance = describe_instance(client, server)
    instance_id = instance["InstanceId"]
    tag_filter = {"Name": f"tag:{VOLUME_TAG}", "Values": [str(resource_id)]}
    attached = client.describe_volumes(
        Filters=[tag_filter, {"Name": "attachment.instance-id", "Values": [instance_id]}])["Volumes"]
    if attached:
        return attached[0]["VolumeId"], "already attached"
    zone = instance["Placement"]["AvailabilityZone"]
    # A volume a failed earlier run created but did not attach.
    loose = client.describe_volumes(
        Filters=[tag_filter, {"Name": "status", "Values": ["available"]},
                 {"Name": "availability-zone", "Values": [zone]}])["Volumes"]
    if loose:
        volume_id = loose[0]["VolumeId"]
    else:
        volume_id = client.create_volume(
            AvailabilityZone=zone, Size=int(size_gb), VolumeType="gp3",
            TagSpecifications=[{"ResourceType": "volume", "Tags": [
                {"Key": VOLUME_TAG, "Value": str(resource_id)},
                {"Key": "Name", "Value": f"{server.hostname} SCVMM lab storage"},
            ]}],
        )["VolumeId"]
        client.get_waiter("volume_available").wait(VolumeIds=[volume_id], WaiterConfig={"Delay": 5, "MaxAttempts": 60})
    used = {m.get("DeviceName", "").replace("/dev/", "") for m in instance.get("BlockDeviceMappings", [])}
    device = next((f"xvd{letter}" for letter in "fghijklmnopqrstuvwxyz" if f"xvd{letter}" not in used), None)
    if device is None:
        raise LabError(f"{server.hostname}: no free device name between xvdf and xvdz.")
    client.attach_volume(Device=device, InstanceId=instance_id, VolumeId=volume_id)
    client.get_waiter("volume_in_use").wait(VolumeIds=[volume_id], WaiterConfig={"Delay": 5, "MaxAttempts": 60})
    # Terminating the SCVMM server deletes the volume with it.
    client.modify_instance_attribute(
        InstanceId=instance_id,
        BlockDeviceMappings=[{"DeviceName": device, "Ebs": {"DeleteOnTermination": True}}],
    )
    return volume_id, f"created {size_gb} GB gp3 volume {volume_id} and attached it as {device}"
