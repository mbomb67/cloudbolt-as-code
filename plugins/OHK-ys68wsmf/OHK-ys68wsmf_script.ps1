# Check Windows App Host
#
# Step 1 of the Prepare Windows app host example workflow
# (runbook_workflows/SWF-7s01lyyk). The workflow step attaches the config file
# runbook-examples/windows-app-host.json from File Archives; this task reads
# the checkHost section of that file, checks free disk space, memory and
# required services, fails when a check does not pass, and publishes what it
# found for the later steps.
#
# Built from the Runbook Task Template (Windows), plugins/OHK-lhm2916o, which
# explains the input, template, output and size conventions.

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

function Log([string]$Text) {
    Write-Output ('[{0}] {1}' -f (Get-Date -Format 's'), $Text)
}

function Write-TaskOutput([string]$Name, [string]$Value) {
    if ($Name -notmatch '^[A-Za-z0-9_]+$') { throw "Output name '$Name' must be letters, digits and underscores." }
    Write-Output ('CB_OUTPUT {0}={1}' -f $Name, ($Value -replace '[\r\n]+', ' '))
}

# The attached config file, parsed. File Archives downloads the step's files
# into CB_ARCHIVE_DIR before the script runs.
function Read-TaskConfig([string]$Name) {
    $path = if ($env:CB_ARCHIVE_DIR) { Join-Path $env:CB_ARCHIVE_DIR $Name } else { '' }
    if (-not $path -or -not (Test-Path -LiteralPath $path -PathType Leaf)) {
        throw "$Name is not attached to this step. Run the task from the Prepare Windows app host workflow, whose steps attach runbook-examples/$Name from File Archives."
    }
    try { Get-Content -Raw -LiteralPath $path | ConvertFrom-Json }
    catch { throw "$Name is not valid JSON: $($_.Exception.Message)" }
}

try {
    # ---- inputs ----
    $ConfigFile = [uri]::UnescapeDataString('{% autoescape off %}{% filter urlencode:"" %}{{ config_file }}{% endfilter %}{% endautoescape %}').Trim()
    if ($ConfigFile -notmatch '^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$') { throw 'config_file must be a file name, such as windows-app-host.json.' }

    # ---- config file ----
    $config = (Read-TaskConfig $ConfigFile).checkHost
    Log ('Reading {0} (SHA-256 {1})' -f $ConfigFile, (Get-FileHash -Algorithm SHA256 -LiteralPath (Join-Path $env:CB_ARCHIVE_DIR $ConfigFile)).Hash.Substring(0, 12).ToLower())
    if (-not $config) { throw "$ConfigFile has no checkHost section." }
    $drive = [string]$config.drive
    if ($drive -notmatch '^[A-Za-z]:$') { throw "checkHost.drive in $ConfigFile must be a drive letter and colon, such as C:." }
    $minFreeGB = [int]$config.minFreeGB
    $minMemoryMB = [int]$config.minMemoryMB
    $services = @($config.requiredServices | Where-Object { $_ })

    # ---- task body ----
    $os = Get-CimInstance -ClassName Win32_OperatingSystem
    $disk = Get-CimInstance -ClassName Win32_LogicalDisk -Filter "DeviceID='$drive'"
    if (-not $disk) { throw "Drive $drive does not exist on $env:COMPUTERNAME." }
    $freeGB = [math]::Floor($disk.FreeSpace / 1GB)
    $memoryMB = [math]::Floor($os.TotalVisibleMemorySize / 1KB)
    Log "OS: $($os.Caption) $($os.Version)"

    $problems = @()
    if ($freeGB -ge $minFreeGB) { Log "Free space on $drive $freeGB GB (at least $minFreeGB GB): ok" }
    else { $problems += "$drive has $freeGB GB free, below minFreeGB $minFreeGB." }
    if ($memoryMB -ge $minMemoryMB) { Log "Memory: $memoryMB MB (at least $minMemoryMB MB): ok" }
    else { $problems += "The VM has $memoryMB MB of memory, below minMemoryMB $minMemoryMB." }
    foreach ($name in $services) {
        $service = Get-Service -Name $name -ErrorAction SilentlyContinue
        if (-not $service) { $problems += "Service $name is not installed." }
        elseif ($service.Status -ne 'Running') { $problems += "Service $name is $($service.Status), not running." }
        else { Log "Service $name ($($service.DisplayName)): running" }
    }
    if ($problems) { throw "$env:COMPUTERNAME is not ready: $($problems -join ' ')" }

    Log 'All checks passed.'
    Write-TaskOutput 'os_caption' $os.Caption
    Write-TaskOutput 'free_gb' $freeGB
    Write-TaskOutput 'memory_mb' $memoryMB
    Write-TaskOutput 'powershell_version' $PSVersionTable.PSVersion
    exit 0
}
catch {
    $err = $_.Exception.Message
    Write-Output "ERROR: $err"
    [Console]::Error.WriteLine("ERROR: $err")
    exit 1
}
