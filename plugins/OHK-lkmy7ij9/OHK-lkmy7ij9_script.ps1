# Configure Windows App Host
#
# Step 3 of the Prepare Windows app host workflow (runbook_workflows/SWF-7s01lyyk):
# records settings in the registry, schedules a log clean-up and writes an event.
# Conventions: plugins/OHK-lhm2916o. Kept short because Azure Run Command and
# WinRM pass it, with the File Archives download step, on the command line.

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
function Log([string]$Text) { Write-Output ('[{0}] {1}' -f (Get-Date -Format 's'), $Text) }
function Write-TaskOutput([string]$Name, [string]$Value) {
    if ($Name -notmatch '^[A-Za-z0-9_]+$') { throw "Output name '$Name' must be letters, digits and underscores." }
    Write-Output ('CB_OUTPUT {0}={1}' -f $Name, ($Value -replace '[\r\n]+', ' '))
}
# Free text for the registry and the event: no control characters, at most 100 characters.
function Get-PlainText([string]$Text) {
    $clean = ($Text -replace '[^\w .,:()/+-]', '').Trim()
    if ($clean.Length -gt 100) { $clean = $clean.Substring(0, 100) }
    $clean
}

try {
    # ---- inputs ----
    $ConfigFile = [uri]::UnescapeDataString('{% autoescape off %}{% filter urlencode:"" %}{{ config_file }}{% endfilter %}{% endautoescape %}').Trim()
    $AppRoot = [uri]::UnescapeDataString('{% autoescape off %}{% filter urlencode:"" %}{{ app_root }}{% endfilter %}{% endautoescape %}').Trim()
    $AppGroup = [uri]::UnescapeDataString('{% autoescape off %}{% filter urlencode:"" %}{{ app_group }}{% endfilter %}{% endautoescape %}').Trim()
    $OsCaption = Get-PlainText ([uri]::UnescapeDataString('{% autoescape off %}{% filter urlencode:"" %}{{ os_caption }}{% endfilter %}{% endautoescape %}'))
    $ChangeTicket = [uri]::UnescapeDataString('{% autoescape off %}{% filter urlencode:"" %}{{ change_ticket }}{% endfilter %}{% endautoescape %}').Trim()
    if ($ConfigFile -notmatch '^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$') { throw 'config_file must be a file name, such as windows-app-host.json.' }
    if ($AppRoot -notmatch '^[A-Za-z]:\\[A-Za-z0-9 ._\\-]+$' -or $AppRoot -match '\.\.') { throw 'app_root must be a folder such as C:\DemoApp.' }
    if (-not (Test-Path -LiteralPath "$AppRoot\logs" -PathType Container)) { throw "$AppRoot\logs is missing. Run Create Windows App Folders first." }
    if ($AppGroup -notmatch '^[A-Za-z0-9 ._-]{1,64}$' -or -not (Get-LocalGroup -Name $AppGroup -ErrorAction SilentlyContinue)) {
        throw "Local group '$AppGroup' does not exist. Run Create Windows App Folders first."
    }
    if ($ChangeTicket -notmatch '^[A-Za-z0-9._-]{0,40}$') { throw 'change_ticket must be up to 40 letters, digits, dots, dashes and underscores.' }

    # ---- config file (File Archives downloads it into CB_ARCHIVE_DIR) ----
    $path = if ($env:CB_ARCHIVE_DIR) { Join-Path $env:CB_ARCHIVE_DIR $ConfigFile } else { '' }
    if (-not $path -or -not (Test-Path -LiteralPath $path -PathType Leaf)) {
        throw "$ConfigFile is not attached to this step. Run the task from the Prepare Windows app host workflow."
    }
    $sha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath $path).Hash.ToLower()
    Log "Reading $ConfigFile (SHA-256 $($sha256.Substring(0, 12)))"
    $config = Get-Content -Raw -LiteralPath $path | ConvertFrom-Json
    $appName = [string]$config.appFolders.appName
    $source = [string]$config.appSettings.eventSource
    $days = [int]$config.appSettings.logRetentionDays
    $environment = Get-PlainText ([string]$config.appSettings.environment)
    $ownerTeam = Get-PlainText ([string]$config.appSettings.ownerTeam)
    if ($appName -notmatch '^[A-Za-z0-9._-]{1,64}$') { throw "appFolders.appName in $ConfigFile is not a valid name." }
    if ($source -notmatch '^[A-Za-z0-9 ._-]{1,64}$') { throw "appSettings.eventSource in $ConfigFile is not a valid name." }
    if ($days -lt 1 -or $days -gt 9999) { throw "appSettings.logRetentionDays in $ConfigFile must be from 1 to 9999." }

    # ---- task body ----
    # 1. Settings in the registry.
    $key = "HKLM:\SOFTWARE\$appName"
    if (-not (Test-Path -LiteralPath $key)) { New-Item -Path $key | Out-Null }
    $values = [ordered]@{
        Environment = $environment; OwnerTeam = $ownerTeam; InstallRoot = $AppRoot; OperatorsGroup = $AppGroup
        OSCaption = $OsCaption; ChangeTicket = $ChangeTicket; ConfigFile = $ConfigFile; ConfigSha256 = $sha256
        ConfiguredAt = (Get-Date).ToUniversalTime().ToString('s') + 'Z'
    }
    foreach ($name in $values.Keys) { Set-ItemProperty -LiteralPath $key -Name $name -Value ([string]$values[$name]) }
    Log "Wrote $($values.Count) values to HKLM\SOFTWARE\$appName."

    # 2. Daily log clean-up at 03:00, run by SYSTEM.
    $logs = "$AppRoot\logs"
    $task = "$appName log clean-up"
    $command = "Get-ChildItem -LiteralPath '$logs' -File -Recurse | Where-Object LastWriteTime -lt (Get-Date).AddDays(-$days) | Remove-Item -Force"
    Register-ScheduledTask -TaskName $task -TaskPath "\$appName\" -Force `
        -Action (New-ScheduledTaskAction -Execute 'powershell.exe' -Argument "-NoProfile -NonInteractive -Command `"$command`"") `
        -Trigger (New-ScheduledTaskTrigger -Daily -At '03:00') `
        -Principal (New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest) `
        -Description "Deletes files in $logs older than $days days. Registered by the CloudBolt Runbooks workflow Prepare Windows app host." | Out-Null
    Log "Scheduled task \$appName\$task deletes files in $logs older than $days days."

    # 3. Event source and an event that records the configuration.
    if (-not [Diagnostics.EventLog]::SourceExists($source)) {
        [Diagnostics.EventLog]::CreateEventSource($source, 'Application')
        Log "Registered event source $source in the Application log."
    }
    $message = "$appName host configured by the CloudBolt Runbooks workflow Prepare Windows app host: environment $environment, owner $ownerTeam, root $AppRoot, config $ConfigFile ($sha256)."
    if ($ChangeTicket) { $message += " Change $ChangeTicket." }
    [Diagnostics.EventLog]::WriteEntry($source, $message, 'Information', 1000)
    Log "Wrote event 1000 from $source to the Application log."

    Log 'Host configured.'
    Write-TaskOutput 'registry_key' "HKLM\SOFTWARE\$appName"
    Write-TaskOutput 'cleanup_task' "\$appName\$task"
    Write-TaskOutput 'event_source' $source
    exit 0
}
catch {
    $err = $_.Exception.Message
    Write-Output "ERROR: $err"
    [Console]::Error.WriteLine("ERROR: $err")
    exit 1
}
