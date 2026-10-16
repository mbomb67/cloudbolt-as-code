"""
Join Windows Server to AD Domain

Post-Provision orchestration action. It joins each Windows server in the job
to Active Directory in the OU its domain_ou parameter names, a distinguished
name. The domain is the OU's DC components, and the join account is the
service account of the LDAP Utility that serves that domain (ad_domain.resolve).
domain_to_join is optional: without an OU it names the domain to join (into
the default Computers container), and it chooses between several LDAP
Utilities for one domain. A server with neither parameter is skipped.

Steps per server:
  1. Run JOIN_SCRIPT in the guest through Server.execute_script, the call a
     Remote Script action makes: the handler's own channel when the server's
     tech_specific_script_execution is on (VMware Tools, Azure Run Command, AWS
     SSM), else WinRM with the server's credentials. The script exits early
     when the computer is already in the domain (CloudBolt's vCenter guest
     customization joins it when domain_to_join is set), checks that the
     domain's DC locator SRV record resolves, runs Add-Computer, and schedules
     a restart 15 seconds out so the call returns first.
  2. Wait for the restart and run VERIFY_SCRIPT until the guest has booted
     again, is in the domain, and passes Test-ComputerSecureChannel.
  3. Look the computer object up over LDAP and record its objectGUID and DN on
     the server (ad_computer_object_guid, ad_computer_object_dn), so the
     cleanup action at Pre-Delete deletes exactly that object.

The action's OS family filter (Windows) keeps Linux servers out; the check in
_join_server keeps them out if the filter is edited.

Security: the join password reaches the guest inside the script body, base64
encoded for quoting only, and is decoded straight into a SecureString. It is
scrubbed from everything this plug-in logs or returns. On AWS the script body
stays in Systems Manager command history; see the README.

The scripts write only to stdout and report through exit codes, because
VMware Tools fails a Windows script on any stderr output and drops Write-Host.
CloudBolt renders this file as a template before running it, so it must never
contain template tags; values reach the scripts through the __CB_*__ tokens.

References:
  Add-Computer:               https://learn.microsoft.com/en-us/powershell/module/microsoft.powershell.management/add-computer?view=powershell-5.1
  Test-ComputerSecureChannel: https://learn.microsoft.com/en-us/powershell/module/microsoft.powershell.management/test-computersecurechannel?view=powershell-5.1
  Resolve-DnsName:            https://learn.microsoft.com/en-us/powershell/module/dnsclient/resolve-dnsname
  shutdown.exe:               https://learn.microsoft.com/en-us/windows-server/administration/windows-commands/shutdown
  Domain join hardening:      https://support.microsoft.com/topic/kb5020276-netjoin-domain-join-hardening-changes-2b65a0f3-1f4c-42ef-ac0f-1caaf421baf8
"""
import base64
import re
import time

from common.methods import set_progress
from infrastructure.models import CustomField
from utilities.logger import ThreadLogger

from shared_modules import ad_domain

logger = ThreadLogger(__name__)

GUID_FIELD = "ad_computer_object_guid"
DN_FIELD = "ad_computer_object_dn"

JOIN_TIMEOUT = 600
VERIFY_TIMEOUT = 120
FIRST_CHECK_DELAY = 60
CHECK_INTERVAL = 30
RESTART_WAIT = 1200

JOIN_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$WarningPreference = 'SilentlyContinue'
function Decode([string]$Value) { [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($Value)) }
try {
    $Domain = Decode '__CB_DOMAIN__'
    $User = Decode '__CB_USER__'
    $OUPath = Decode '__CB_OU__'
    $cs = Get-CimInstance -ClassName Win32_ComputerSystem
    $boot = (Get-CimInstance -ClassName Win32_OperatingSystem).LastBootUpTime.ToUniversalTime().Ticks
    Write-Output "CB_AD_COMPUTER_NAME=$env:COMPUTERNAME"
    Write-Output "CB_AD_BOOT_TICKS=$boot"
    if ($cs.PartOfDomain) {
        if ($cs.Domain -eq $Domain) {
            Write-Output "CB_AD_RESULT=ALREADY_JOINED"
            exit 0
        }
        Write-Output "ERROR: the computer is already in domain $($cs.Domain), not $Domain."
        exit 7
    }
    try {
        Resolve-DnsName -Name "_ldap._tcp.dc._msdcs.$Domain" -Type SRV -DnsOnly | Out-Null
    } catch {
        Write-Output "ERROR: _ldap._tcp.dc._msdcs.$Domain does not resolve: $($_.Exception.Message)"
        exit 3
    }
    $secret = ConvertTo-SecureString -String (Decode '__CB_PASSWORD__') -AsPlainText -Force
    $credential = New-Object System.Management.Automation.PSCredential -ArgumentList $User, $secret
    $join = @{ DomainName = $Domain; Credential = $credential; Force = $true; WarningAction = 'SilentlyContinue' }
    if ($OUPath) { $join.OUPath = $OUPath }
    try {
        Add-Computer @join
    } catch {
        Write-Output "ERROR: Add-Computer failed: $($_.Exception.Message)"
        exit 4
    }
    shutdown.exe /r /t 15 /d p:4:2 /c "CloudBolt joined this computer to $Domain." | Out-Null
    if ($LASTEXITCODE -ne 0) {
        Write-Output "ERROR: joined $Domain, but shutdown.exe could not schedule the restart (exit $LASTEXITCODE)."
        exit 5
    }
    Write-Output "CB_AD_RESULT=JOINED"
    exit 0
} catch {
    Write-Output "ERROR: $($_.Exception.Message)"
    exit 1
}
"""

VERIFY_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
try {
    $Domain = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('__CB_DOMAIN__'))
    $boot = (Get-CimInstance -ClassName Win32_OperatingSystem).LastBootUpTime.ToUniversalTime().Ticks
    if ($boot -le __CB_BOOT_TICKS__) {
        Write-Output "CB_AD_RESULT=NOT_RESTARTED"
        exit 8
    }
    $cs = Get-CimInstance -ClassName Win32_ComputerSystem
    if (-not $cs.PartOfDomain -or $cs.Domain -ne $Domain) {
        Write-Output "ERROR: after the restart the computer is in $($cs.Domain), not $Domain."
        exit 4
    }
    if (-not (Test-ComputerSecureChannel)) {
        Write-Output "ERROR: the computer's secure channel to $Domain does not work yet."
        exit 6
    }
    Write-Output "CB_AD_RESULT=VERIFIED"
    exit 0
} catch {
    Write-Output "ERROR: $($_.Exception.Message)"
    exit 1
}
"""

EXIT_CODE_DIAGNOSTICS = {
    3: "DNS: the server cannot find a domain controller. Its DNS servers must be the domain's DNS "
       "(or forward to it).",
    4: "Add-Computer failed. Check the join account's rights on the OU, the password, connectivity to "
       "the domain controllers, and whether a computer object with this name already exists: since "
       "KB5020276 Windows will not reuse one the join account did not create.",
    5: "The computer joined the domain but the restart could not be scheduled; restart it to finish.",
    7: "The computer is already in a different domain; this action does not move computers between domains.",
}

MARKER = re.compile(r"^(CB_AD_[A-Z_]+)=(.*?)\s*$", re.MULTILINE)


def _b64(text):
    return base64.b64encode(str(text).encode("utf-8")).decode("ascii")


def _scrub(text, password):
    text = str(text or "")
    if password:
        text = text.replace(password, "***").replace(_b64(password), "***")
    return text


def _markers(output):
    return {name: value for name, value in MARKER.findall(output or "")}


def _errors(output):
    """The script's ERROR: lines, or the end of its output when it printed none."""
    lines = [line.strip() for line in (output or "").splitlines() if line.strip().startswith("ERROR:")]
    return " ".join(lines) or (output or "").strip()[-500:]


def _execute(server, script, timeout, show_output=True):
    """(exit code, output) of a PowerShell script in the guest; -1 when it could not run at all."""
    try:
        output = server.execute_script(
            script_contents=script,
            file_extension="ps1",
            timeout=timeout,
            show_streaming_output=show_output,
        )
        return 0, str(output or "")
    except Exception as err:
        rv = getattr(err, "rv", None)
        try:
            rv = int(rv)
        except (TypeError, ValueError):
            rv = -1
        return rv, str(getattr(err, "output", "") or err)


def _ensure_custom_fields():
    for name, label, description in (
        (GUID_FIELD, "AD Computer Object GUID",
         "objectGUID of the Active Directory computer object this server joined as."),
        (DN_FIELD, "AD Computer Object DN",
         "Distinguished name of the Active Directory computer object this server joined as."),
    ):
        CustomField.objects.get_or_create(
            name=name,
            defaults=dict(label=label, description=description, type="STR", show_on_servers=True),
        )


def _wait_for_restart(server, domain, boot_ticks):
    """Run VERIFY_SCRIPT until it passes or RESTART_WAIT runs out. Returns (ok, message)."""
    script = VERIFY_SCRIPT.replace("__CB_DOMAIN__", _b64(domain)).replace("__CB_BOOT_TICKS__", boot_ticks)
    deadline = time.time() + RESTART_WAIT
    time.sleep(FIRST_CHECK_DELAY)
    last = "the guest did not answer"
    while True:
        rv, output = _execute(server, script, VERIFY_TIMEOUT, show_output=False)
        if rv == 0:
            return True, ""
        if rv == 4:
            return False, _errors(output)
        last = _errors(output) if rv > 0 else "the guest did not answer: " + output.strip()[-300:]
        if time.time() + CHECK_INTERVAL > deadline:
            return False, "after %d minutes: %s" % (RESTART_WAIT // 60, last)
        time.sleep(CHECK_INTERVAL)


def _record_computer_object(server, ldap_utility, name):
    """Store the computer object's GUID and DN on the server. Returns a warning, or '' when recorded."""
    try:
        conn = ad_domain.connect(ldap_utility)
        try:
            matches = ad_domain.find_computers(conn, ad_domain.search_base(ldap_utility, conn), name=name)
        finally:
            conn.unbind_s()
    except Exception as err:
        return "could not look up computer object %s over LDAP (%s)" % (name, err)
    if len(matches) != 1:
        return "found %d computer objects named %s over LDAP" % (len(matches), name)
    server.set_value_for_custom_field(GUID_FIELD, matches[0]["guid"])
    server.set_value_for_custom_field(DN_FIELD, matches[0]["dn"])
    return ""


def _join_server(server):
    """Join one server. Returns (status, message), status SUCCESS, FAILURE, or SKIPPED."""
    hostname = server.hostname
    if not server.is_windows():
        return "SKIPPED", "%s: not Windows, skipped." % hostname
    if server.status == "HISTORICAL" or not server.resource_handler_svr_id:
        return "SKIPPED", "%s: the VM was not created, skipped." % hostname

    try:
        ldap_utility, domain, ou = ad_domain.resolve(server)
    except ValueError as err:
        return "FAILURE", "%s: cannot tell which domain to join: %s." % (hostname, err)
    if ldap_utility is None:
        return "SKIPPED", "%s: no domain_ou or domain_to_join, skipped." % hostname

    user = (ldap_utility.serviceaccount or "").strip()
    password = ldap_utility.servicepasswd or ""
    if not domain or not user or not password:
        return "FAILURE", ("%s: LDAP Utility '%s' is missing its domain, account, or password."
                           % (hostname, ldap_utility))

    set_progress("%s: joining Active Directory domain %s%s" % (
        hostname, domain, " in " + ad_domain.canonical_name(ou) if ou else ""))
    script = (JOIN_SCRIPT.replace("__CB_DOMAIN__", _b64(domain))
              .replace("__CB_USER__", _b64(user))
              .replace("__CB_OU__", _b64(ou))
              .replace("__CB_PASSWORD__", _b64(password)))
    rv, output = _execute(server, script, JOIN_TIMEOUT)
    output = _scrub(output, password)
    if rv != 0:
        diagnosis = EXIT_CODE_DIAGNOSTICS.get(rv, "The join script failed (exit %s)." % rv)
        message = "%s: AD domain join failed. %s %s" % (hostname, diagnosis, _errors(output))
        logger.error(message)
        return "FAILURE", message

    markers = _markers(output)
    name = markers.get("CB_AD_COMPUTER_NAME") or ad_domain.computer_name(hostname)
    if markers.get("CB_AD_RESULT") == "ALREADY_JOINED":
        set_progress("%s: already in domain %s." % (hostname, domain))
    else:
        set_progress("%s: joined %s, waiting for the restart." % (hostname, domain))
        boot_ticks = markers.get("CB_AD_BOOT_TICKS", "")
        ok, problem = _wait_for_restart(server, domain, boot_ticks if boot_ticks.isdigit() else "0")
        if not ok:
            return "FAILURE", "%s: joined %s, but the check after the restart failed: %s" % (
                hostname, domain, _scrub(problem, password))

    warning = _record_computer_object(server, ldap_utility, name)
    if warning:
        set_progress("%s: %s; the cleanup action will look it up by name instead." % (hostname, warning))
    return "SUCCESS", "%s: in Active Directory domain %s as %s." % (hostname, domain, name)


def run(job, *args, **kwargs):
    """Post-Provision entry point."""
    if job.status == "FAILURE":
        # CloudBolt still runs Post-Provision actions after a failed provision
        # (jobs/models.py, Job.run); there is no VM to join.
        return "SUCCESS", "Provisioning failed, so no server was joined to a domain.", ""
    _ensure_custom_fields()
    # With an OS family filter CloudBolt passes only the matching servers.
    servers = kwargs.get("servers") or list(job.server_set.all())
    results = []
    for server in servers:
        status, message = _join_server(server)
        set_progress(message)
        results.append((status, message))

    failures = [message for status, message in results if status == "FAILURE"]
    joined = [message for status, message in results if status == "SUCCESS"]
    if failures:
        return "FAILURE", "\n".join(joined), "\n".join(failures)
    if not joined:
        return "SUCCESS", "No Windows server in this job has a domain_ou or domain_to_join.", ""
    return "SUCCESS", "\n".join(joined), ""
