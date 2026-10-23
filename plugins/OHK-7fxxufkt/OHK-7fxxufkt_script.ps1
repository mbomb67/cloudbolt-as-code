# Create Windows App Folders
#
# Step 2 of the Prepare Windows app host example workflow
# (runbook_workflows/SWF-7s01lyyk). Reads the appFolders section of the
# attached config file (runbook-examples/windows-app-host.json in File
# Archives), creates the local operators group and the application folders
# when they do not exist, gives the group Modify on the application root, and
# publishes the root and group for the next step. Running it again changes
# nothing that is already in place.
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
    $config = (Read-TaskConfig $ConfigFile).appFolders
    Log ('Reading {0} (SHA-256 {1})' -f $ConfigFile, (Get-FileHash -Algorithm SHA256 -LiteralPath (Join-Path $env:CB_ARCHIVE_DIR $ConfigFile)).Hash.Substring(0, 12).ToLower())
    if (-not $config) { throw "$ConfigFile has no appFolders section." }
    $appName = [string]$config.appName
    $root = [string]$config.root
    $group = [string]$config.operatorsGroup
    $subfolders = @($config.subfolders | Where-Object { $_ } | ForEach-Object { [string]$_ })
    if ($appName -notmatch '^[A-Za-z0-9._-]{1,64}$') { throw "appFolders.appName in $ConfigFile must be up to 64 letters, digits, dots, dashes and underscores." }
    # A folder below a drive root, never a system folder itself.
    if ($root -notmatch '^[A-Za-z]:\\[A-Za-z0-9 ._-]+(\\[A-Za-z0-9 ._-]+)*$' -or $root -match '\.\.' -or
        $root -match '^[A-Za-z]:\\(Windows|Program Files|Program Files \(x86\)|ProgramData|Users)(\\|$)') {
        throw "appFolders.root in $ConfigFile must be a folder such as C:\DemoApp, outside the Windows, Program Files, ProgramData and Users folders."
    }
    if ($group -notmatch '^[A-Za-z0-9 ._-]{1,64}$') { throw "appFolders.operatorsGroup in $ConfigFile must be up to 64 letters, digits, spaces, dots, dashes and underscores." }
    foreach ($sub in $subfolders) {
        if ($sub -notmatch '^[A-Za-z0-9._-]{1,64}$' -or $sub -match '^\.+$') { throw "appFolders.subfolders in $ConfigFile must be folder names; '$sub' is not." }
    }

    # ---- task body ----
    $created = 0
    if (Get-LocalGroup -Name $group -ErrorAction SilentlyContinue) {
        Log "Local group $group exists."
    } else {
        New-LocalGroup -Name $group -Description "Operators of $appName. Created by the CloudBolt Runbooks workflow Prepare Windows app host." | Out-Null
        Log "Created local group $group."
        $created++
    }

    foreach ($path in @($root) + @($subfolders | ForEach-Object { Join-Path $root $_ })) {
        if (Test-Path -LiteralPath $path -PathType Container) {
            Log "$path exists."
        } else {
            New-Item -ItemType Directory -Path $path | Out-Null
            Log "Created $path."
            $created++
        }
    }

    # Modify for the operators group on the root, inherited by every folder and file below it.
    $folder = Get-Item -LiteralPath $root
    $acl = $folder.GetAccessControl('Access')
    $rule = New-Object Security.AccessControl.FileSystemAccessRule(
        "$env:COMPUTERNAME\$group", 'Modify', 'ContainerInherit, ObjectInherit', 'None', 'Allow')
    $acl.AddAccessRule($rule)
    $folder.SetAccessControl($acl)
    Log "$group has Modify on $root and everything below it."

    Log "Folders ready; $created item(s) created."
    Write-TaskOutput 'app_root' $root
    Write-TaskOutput 'app_group' $group
    Write-TaskOutput 'items_created' $created
    exit 0
}
catch {
    $err = $_.Exception.Message
    Write-Output "ERROR: $err"
    [Console]::Error.WriteLine("ERROR: $err")
    exit 1
}
