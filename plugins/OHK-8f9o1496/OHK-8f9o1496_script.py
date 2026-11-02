"""
Install SCVMM

Build item 4 of the SCVMM Lab on AWS blueprint (blueprints/BP-9zp8udce). It
runs in parallel with Configure Hyper-V Hosts (item 5) and only touches the
SCVMM server. Every step is skipped when it is already done, so the item can
be re-run.

  1. Find the lab volume (labeled CBLab by Prepare SCVMM Lab on AWS), check C:
     has room, and restart the server first if Windows has a restart pending
     (SQL Server setup refuses to run then).
  2. SQL Server 2022 Developer, default instance, Database Engine only, as a
     SYSTEM scheduled task. The installer bootstrapper (SSEI) downloads the
     media from Microsoft. Its /Action=Download, /MediaPath, /MediaType and
     /Quiet switches are not documented; they are the ones the bootstrapper
     itself lists. The domain service account and BUILTIN\\Administrators
     become sysadmins, because SCVMM setup runs as that account and needs
     sysadmin. The services keep setup's default virtual accounts. SCVMM
     2025 lists Standard, Enterprise and Datacenter editions; Developer is the
     Enterprise engine, licensed for test labs only.
  3. Windows ADK Deployment Tools and the WinPE add-on, which SCVMM setup
     requires, from Microsoft's download links.
  4. The SCVMM 2025 media from File Archives: scvmm_media names the zip
     (SCVMM_2025.zip from the Evaluation Center, uploaded unchanged). The
     file_archives shared module downloads and checks it on the server; this
     moves the extracted folder to the lab volume.
  5. SCVMM setup from the command prompt, as a scheduled task signed in as the
     domain service account: new database VirtualManagerDB on the local
     instance, the VMM service as Local System (no distributed key
     management), the library share on the lab volume, the default ports,
     no product key (180-day evaluation).

The server downloads SQL Server and the ADK from the internet; with no
internet access, steps 2 and 3 fail with the download error.

CloudBolt renders this file as a template before running it, so it must never
contain template tags; values reach the scripts through the __CB_*__ tokens.

References:
  SQL Server from the command prompt:  https://learn.microsoft.com/en-us/sql/database-engine/install-windows/install-sql-server-from-the-command-prompt?view=sql-server-ver16
  SQL Server service accounts:         https://learn.microsoft.com/en-us/sql/database-engine/configure-windows/configure-windows-service-accounts-and-permissions?view=sql-server-ver16
  SQL Server 2022 downloads:           https://www.microsoft.com/en-us/sql-server/sql-server-downloads
  Windows ADK downloads:               https://learn.microsoft.com/en-us/windows-hardware/get-started/adk-install
  ADK silent install:                  https://learn.microsoft.com/en-us/windows-hardware/get-started/adk-offline-install
  ADK command-line options:            https://learn.microsoft.com/en-us/previous-versions/windows/it-pro/windows-8.1-and-8/dn621910(v=win.10)
  Install VMM (command prompt, ini):   https://learn.microsoft.com/en-us/system-center/vmm/install?view=sc-vmm-2025
  Plan the VMM installation:           https://learn.microsoft.com/en-us/system-center/vmm/plan-install?view=sc-vmm-2025
  VMM 2025 system requirements:        https://learn.microsoft.com/en-us/system-center/vmm/system-requirements?view=sc-vmm-2025
  System Center 2025 evaluation:       https://www.microsoft.com/en-us/evalcenter/download-system-center-2025
"""
from common.methods import set_progress

from shared_modules import scvmm_lab
from shared_modules.scvmm_lab import LabError

SQL_SSEI_URL = "https://go.microsoft.com/fwlink/p/?linkid=2215158"
ADK_URL = "https://go.microsoft.com/fwlink/?linkid=2289980"
WINPE_URL = "https://go.microsoft.com/fwlink/?linkid=2289981"
MIN_FREE_C_GB = 25

STATE = r"""
    $volume = Get-Volume -FileSystemLabel (Decode '__CB_LABEL__') -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $volume) { throw 'The lab volume is missing; run Prepare SCVMM Lab on AWS again.' }
    Mark 'ROOT' ('{0}:\CBLab' -f $volume.DriveLetter)
    Mark 'C_FREE_GB' ([math]::Floor((Get-Volume -DriveLetter C).SizeRemaining / 1GB))
    $pending = (Test-Path 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Component Based Servicing\RebootPending') -or
        (Test-Path 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\WindowsUpdate\Auto Update\RebootRequired') -or
        [bool](Get-ItemProperty 'HKLM:\SYSTEM\CurrentControlSet\Control\Session Manager' -Name PendingFileRenameOperations -ErrorAction SilentlyContinue)
    Mark 'RESTART_PENDING' ([int]$pending)
    Mark 'SQL' ([int][bool](Get-Service -Name MSSQLSERVER -ErrorAction SilentlyContinue))
    $kit = Join-Path ${env:ProgramFiles(x86)} 'Windows Kits\10\Assessment and Deployment Kit'
    Mark 'ADK' ([int](Test-Path (Join-Path $kit 'Deployment Tools')))
    Mark 'WINPE' ([int](Test-Path (Join-Path $kit 'Windows Preinstallation Environment')))
    Mark 'VMM' ([int][bool](Get-Service -Name SCVMMService -ErrorAction SilentlyContinue))
    Mark 'MEDIA' ([int](Test-Path ('{0}:\CBLab\Media\VMM\setup.exe' -f $volume.DriveLetter)))
"""

INSTALL_SQL = r"""
    [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor 3072
    $root = Decode '__CB_ROOT__'
    # SQL Server setup takes DOMAIN\user; resolve a UPN through its SID.
    $account = (New-Object Security.Principal.NTAccount (Decode '__CB_SYSADMIN__')).Translate([Security.Principal.SecurityIdentifier]).Translate([Security.Principal.NTAccount]).Value
    $media = Join-Path $root 'Media\SQL'
    New-Item -ItemType Directory -Force -Path $media | Out-Null
    $ssei = Join-Path $media 'SQL2022-SSEI-Dev.exe'
    if (-not (Test-Path -LiteralPath $ssei)) {
        Log 'Downloading the SQL Server 2022 Developer installer'
        Invoke-WebRequest -Uri (Decode '__CB_SSEI_URL__') -OutFile $ssei -UseBasicParsing
    }
    $box = Get-ChildItem -LiteralPath $media -Filter 'SQLServer2022-DEV-x64-*.exe' | Select-Object -First 1
    if (-not $box) {
        Log 'Downloading the SQL Server 2022 Developer media (CAB)'
        $p = Start-Process -FilePath $ssei -ArgumentList '/Action=Download', "/MediaPath=`"$media`"", '/MediaType=CAB', '/Quiet' -Wait -PassThru
        $box = Get-ChildItem -LiteralPath $media -Filter 'SQLServer2022-DEV-x64-*.exe' | Select-Object -First 1
        if (-not $box) { throw "The SQL Server installer did not download the media (exit code $($p.ExitCode)); the server needs internet access to download.microsoft.com." }
    }
    $setupDir = Join-Path $media 'setup'
    if (-not (Test-Path -LiteralPath (Join-Path $setupDir 'setup.exe'))) {
        Log 'Extracting the SQL Server media'
        $p = Start-Process -FilePath $box.FullName -ArgumentList '/q', "/x:`"$setupDir`"" -Wait -PassThru
        if (-not (Test-Path -LiteralPath (Join-Path $setupDir 'setup.exe'))) { throw "Extracting $($box.Name) failed (exit code $($p.ExitCode))." }
    }
    Log "Installing SQL Server 2022 Developer (MSSQLSERVER) with sysadmins $account and BUILTIN\Administrators"
    $arguments = @('/Q', '/ACTION=Install', '/FEATURES=SQLENGINE', '/INSTANCENAME=MSSQLSERVER',
        "/SQLSYSADMINACCOUNTS=`"$account`" `"BUILTIN\Administrators`"",
        '/IACCEPTSQLSERVERLICENSETERMS', '/SUPPRESSPRIVACYSTATEMENTNOTICE', '/UpdateEnabled=0')
    $p = Start-Process -FilePath (Join-Path $setupDir 'setup.exe') -ArgumentList $arguments -Wait -PassThru
    if ($p.ExitCode -notin 0, 3010) {
        throw "SQL Server setup exited with code $($p.ExitCode); see Summary.txt under C:\Program Files\Microsoft SQL Server\160\Setup Bootstrap\Log."
    }
    if ((Get-Service -Name MSSQLSERVER).Status -ne 'Running') { Start-Service -Name MSSQLSERVER }
    Log 'SQL Server is running'
"""

INSTALL_ADK = r"""
    [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor 3072
    $media = Join-Path (Decode '__CB_ROOT__') 'Media\ADK'
    New-Item -ItemType Directory -Force -Path $media | Out-Null
    $kit = Join-Path ${env:ProgramFiles(x86)} 'Windows Kits\10\Assessment and Deployment Kit'
    $parts = @(
        @{ Url = (Decode '__CB_ADK_URL__'); File = 'adksetup.exe'; Feature = 'OptionId.DeploymentTools'; Folder = 'Deployment Tools' },
        @{ Url = (Decode '__CB_WINPE_URL__'); File = 'adkwinpesetup.exe'; Feature = 'OptionId.WindowsPreinstallationEnvironment'; Folder = 'Windows Preinstallation Environment' }
    )
    foreach ($part in $parts) {
        if (Test-Path -LiteralPath (Join-Path $kit $part.Folder)) { Log "$($part.Folder) is already installed"; continue }
        $exe = Join-Path $media $part.File
        if (-not (Test-Path -LiteralPath $exe)) { Invoke-WebRequest -Uri $part.Url -OutFile $exe -UseBasicParsing }
        Log "Installing $($part.Folder) (it downloads from Microsoft)"
        $p = Start-Process -FilePath $exe -ArgumentList '/quiet', '/norestart', '/ceip', 'off', '/features', $part.Feature -Wait -PassThru
        if ($p.ExitCode -notin 0, 3010 -or -not (Test-Path -LiteralPath (Join-Path $kit $part.Folder))) {
            throw "$($part.File) exited with code $($p.ExitCode); see the logs under $env:TEMP\adk."
        }
    }
"""

STAGE_MEDIA = r"""
    $target = Join-Path (Decode '__CB_ROOT__') 'Media\VMM'
    $setup = Get-ChildItem -LiteralPath $env:CB_ARCHIVE_DIR -Filter setup.exe -Recurse -Depth 2 |
        Sort-Object { $_.FullName.Length } | Select-Object -First 1
    if (-not $setup) { throw 'The archive has no setup.exe. Upload SCVMM_2025.zip from the Evaluation Center unchanged.' }
    Get-ChildItem -LiteralPath $env:CB_ARCHIVE_DIR -Filter *.zip | Remove-Item -Force
    Set-Location -LiteralPath $env:SystemDrive\
    if (Test-Path -LiteralPath $target) { Remove-Item -LiteralPath $target -Recurse -Force }
    Move-Item -LiteralPath $setup.Directory.FullName -Destination $target
    Log "SCVMM media is in $target"
"""

INSTALL_VMM = r"""
    $root = Decode '__CB_ROOT__'
    $setup = Join-Path $root 'Media\VMM\setup.exe'
    $library = Join-Path $root 'Library'
    $ini = Join-Path $root 'VMServer.ini'
    New-Item -ItemType Directory -Force -Path $library | Out-Null
    $settings = @(
        '[OPTIONS]',
        'CreateNewSqlDatabase=1',
        'SqlInstanceName=MSSQLSERVER',
        'SqlDatabaseName=VirtualManagerDB',
        'RemoteDatabaseImpersonation=0',
        "SqlMachineName=$env:COMPUTERNAME",
        'IndigoTcpPort=8100',
        'IndigoHTTPSPort=8101',
        'IndigoNETTCPPort=8102',
        'IndigoHTTPPort=8103',
        'WSManTcpPort=5985',
        'BitsTcpPort=443',
        'CreateNewLibraryShare=1',
        'LibraryShareName=MSSCVMMLibrary',
        "LibrarySharePath=$library",
        'LibraryShareDescription=Virtual Machine Manager Library Files',
        'SQMOptIn=0',
        'MUOptIn=0',
        'VmmServiceLocalAccount=1',
        'HighlyAvailable=0'
    )
    # The sample VMServer.ini that ships with the media is UTF-16LE.
    Set-Content -LiteralPath $ini -Value $settings -Encoding Unicode
    Log "Running SCVMM setup as $(whoami)"
    $p = Start-Process -FilePath $setup -ArgumentList '/server', '/i', '/f', "`"$ini`"", '/IACCEPTSCEULA' -Wait -PassThru
    # In case the bootstrapper hands off to the setup engine and exits first.
    $deadline = (Get-Date).AddMinutes(60)
    while ((Get-Process -Name SetupVM -ErrorAction SilentlyContinue) -and (Get-Date) -lt $deadline) { Start-Sleep -Seconds 15 }
    $service = Get-Service -Name SCVMMService -ErrorAction SilentlyContinue
    if ($p.ExitCode -ne 0 -or -not $service) {
        throw "SCVMM setup exited with code $($p.ExitCode); see the logs in $env:SystemDrive\ProgramData\VMMLogs."
    }
    if ($service.Status -ne 'Running') { Start-Service -Name SCVMMService }
    Log 'The SCVMM service is running'
"""


def run(job, resource=None, **kwargs):
    resource = resource or job.resource_set.first()
    try:
        lab = scvmm_lab.Lab(resource)
        vmm = lab.vmm
        media_ref = str(resource.get_value_for_custom_field("scvmm_media") or "").strip()
        if not media_ref:
            raise LabError("scvmm_media is empty. Pin the File Archives reference of SCVMM_2025.zip "
                           "(for example scvmm/SCVMM_2025.zip) as the blueprint parameter's only option.")
        state, _ = scvmm_lab.run_ps(vmm, STATE, tokens={"LABEL": scvmm_lab.STORAGE_LABEL})
        if state["RESTART_PENDING"] == "1" and state["VMM"] == "0":
            scvmm_lab.restart([vmm], "clear a pending restart before SQL Server setup")
        if state["VMM"] == "1":
            return "SUCCESS", f"SCVMM is already installed on {vmm.hostname}.", ""
        if state["SQL"] == "0" and int(state["C_FREE_GB"]) < MIN_FREE_C_GB:
            raise LabError(f"{vmm.hostname}: C: has {state['C_FREE_GB']} GB free; SQL Server, the ADK and SCVMM "
                           f"need {MIN_FREE_C_GB} GB. Grow the root volume and extend C:.")
        root = state["ROOT"]
        temp = f"{root}\\Temp"
        tokens = {"ROOT": root}

        if state["SQL"] == "1":
            set_progress(f"{vmm.hostname}: SQL Server is already installed")
        else:
            scvmm_lab.run_task(vmm, INSTALL_SQL, "install SQL Server 2022 Developer", tokens=dict(
                tokens, SYSADMIN=lab.username, SSEI_URL=SQL_SSEI_URL), timeout=3600, temp_dir=temp)
        if state["ADK"] == "1" and state["WINPE"] == "1":
            set_progress(f"{vmm.hostname}: the Windows ADK and WinPE add-on are already installed")
        else:
            scvmm_lab.run_task(vmm, INSTALL_ADK, "install the Windows ADK and WinPE add-on", tokens=dict(
                tokens, ADK_URL=ADK_URL, WINPE_URL=WINPE_URL), timeout=3600, temp_dir=temp)
        if state["MEDIA"] == "0":
            scvmm_lab.run_task(vmm, STAGE_MEDIA, f"download {media_ref} from File Archives", tokens=tokens,
                               timeout=3600, temp_dir=temp, archive_refs=[f"{media_ref} extract"], job=job)
        scvmm_lab.run_task(vmm, INSTALL_VMM, "install SCVMM", tokens=tokens, account=lab.account,
                           timeout=3600, temp_dir=temp)
    except LabError as err:
        return "FAILURE", "", str(err)
    return "SUCCESS", f"SCVMM 2025 (evaluation) is running on {lab.fqdn(vmm)} with SQL Server 2022 Developer.", ""
