# Run Installer from File Archive (Windows)
#
# Runs an installer that File Archives (extensions/XUI-0hv6t61x) keeps on
# CloudBolt. The archive header line at the end of this comment names the
# file. Run Task (server_actions/SVA-4sin3s0q) reads it and, before the task
# body starts, downloads the file to the server, checks its SHA-256 and
# unpacks a zip or tar file. The body runs in that directory, which is also in
# $env:CB_ARCHIVE_DIR.
#
# Installer is the installer's path inside the unpacked archive, for example
# app-7.4\setup.exe. Leave it empty to run the downloaded file itself. By type:
# .msi installs with msiexec /i /qn /norestart, .ps1 runs in a new
# powershell.exe, and .exe runs directly; each runs from its own directory.
# Installer arguments are split on spaces and passed after those. Exit codes 0
# and 3010 (restart needed) count as success.
#
# Built from Server Task Template (Windows), plugins/OHK-lhm2916o: read its
# conventions before changing this script.
#
# cb-archive: {{ archive_file }} extract

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

function Log([string]$Text) {
    Write-Output ('[{0}] {1}' -f (Get-Date -Format 's'), $Text)
}

function Write-TaskOutput([string]$Name, [string]$Value) {
    if ($Name -notmatch '^[A-Za-z0-9_]+$') { throw "Output name '$Name' must be letters, digits and underscores." }
    Write-Output ('CB_OUTPUT {0}={1}' -f $Name, ($Value -replace '[\r\n]+', ' '))
}

try {
    # ---- inputs ----
    $ArchiveFile = [uri]::UnescapeDataString('{% autoescape off %}{% filter urlencode:"" %}{{ archive_file }}{% endfilter %}{% endautoescape %}').Trim()
    $Installer = [uri]::UnescapeDataString('{% autoescape off %}{% filter urlencode:"" %}{{ installer }}{% endfilter %}{% endautoescape %}').Trim()
    $InstallerArgs = [uri]::UnescapeDataString('{% autoescape off %}{% filter urlencode:"" %}{{ installer_args }}{% endfilter %}{% endautoescape %}').Trim()

    if (-not $env:CB_ARCHIVE_DIR) { throw 'Run this task from Run Task: Run Task is what downloads the archive file.' }
    if ($Installer -match '^[\\/]|\.\.|^[A-Za-z]:') { throw 'Installer must be a path inside the archive, without .., a drive or a leading slash.' }
    $fileName = (($ArchiveFile -split '/', 2)[1]) -replace '@.*$', ''
    $relative = if ($Installer) { $Installer } else { $fileName }
    $target = Join-Path $env:CB_ARCHIVE_DIR $relative
    if (-not (Test-Path -LiteralPath $target -PathType Leaf)) {
        $found = (Get-ChildItem -LiteralPath $env:CB_ARCHIVE_DIR -Recurse -Depth 1 | Select-Object -First 20 -ExpandProperty Name) -join ', '
        throw "No file $relative in the archive. It holds: $found"
    }
    $argList = @($InstallerArgs -split '\s+' | Where-Object { $_ })

    # ---- task body ----
    Log "Running $relative from $ArchiveFile"
    Set-Location -LiteralPath (Split-Path -Parent $target)
    switch -Wildcard ($target.ToLower()) {
        '*.msi' {
            $msiArgs = @('/i', ('"{0}"' -f $target), '/qn', '/norestart') + $argList
            $code = (Start-Process -FilePath msiexec.exe -ArgumentList $msiArgs -Wait -PassThru).ExitCode
        }
        '*.ps1' {
            # A child powershell.exe, so the installer's own exit or errors cannot end this script.
            # stderr is merged into stdout: VMware Tools fails a Windows script on any stderr text.
            $ErrorActionPreference = 'Continue'
            & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $target @argList 2>&1 | ForEach-Object { Log "$_" }
            $code = $LASTEXITCODE
            $ErrorActionPreference = 'Stop'
        }
        '*.exe' {
            $exeArgs = @{ FilePath = $target; Wait = $true; PassThru = $true; NoNewWindow = $true }
            if ($argList) { $exeArgs.ArgumentList = $argList }
            $code = (Start-Process @exeArgs).ExitCode
        }
        default { throw "Cannot run $relative; use an .msi, .ps1 or .exe installer." }
    }
    if ($code -eq 3010) { Log 'The installer asks for a restart.' }
    elseif ($code -ne 0) { throw "The installer exited with code $code." }

    Log 'Task finished.'
    Write-TaskOutput 'installer' $relative
    Write-TaskOutput 'installer_exit_code' $code
    exit 0
}
catch {
    $err = $_.Exception.Message
    Write-Output "ERROR: $err"
    [Console]::Error.WriteLine("ERROR: $err")
    exit 1
}
