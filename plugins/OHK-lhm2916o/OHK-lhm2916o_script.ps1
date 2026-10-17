# Server Task Template (Windows)
#
# Base script for Windows server tasks. Run Task (server_actions/SVA-4sin3s0q)
# lists every Remote Script that fits the server, so a copy of this script with
# the Windows OS family set shows up there without a new button. Copy it, rename
# it, declare your inputs, and replace the task body. This copy prints a short
# summary of the server and an optional message.
#
# Conventions:
#   Inputs   Each declared input is read once, under "inputs", inside single
#            quotes, wrapped in Django's autoescape-off and urlencode filter
#            tags, and decoded with UnescapeDataString. The encoded text is
#            only letters, digits and percent signs, so no value can end the
#            string. Keep the variable inside the tags bare (no filter after
#            its name): CloudBolt creates the script's inputs from the bare
#            template variables in the file and deletes the others. Validate
#            every input before using it.
#   Template CloudBolt renders the whole file as a Django template first, so
#            never write template braces anywhere else, even in a comment.
#   Output   Write-Output only (the Log function). VMware Tools captures only
#            the success stream, and on Windows it fails the script on any
#            stderr text.
#   Outputs  Write-TaskOutput name value prints a CB_OUTPUT name=value line.
#            Run Task turns those lines into the job's outputs, and a later
#            workflow step can read them. Print them last, just before exit 0:
#            WinRM drops stdout when a script fails, and Azure Run Command
#            keeps only the last 4 KB of output.
#   Errors   Throw. The catch block prints the error, writes one plain line to
#            stderr (the line a WinRM failure shows) and exits 1. Every
#            transport reads the exit code.
#   Size     Azure Run Command and WinRM pass the script on the powershell.exe
#            command line. Keep the rendered script under about 9,000
#            characters.
#   Account  Azure Run Command and AWS SSM run as SYSTEM. WinRM and VMware Tools
#            run as the Remote Script's credentials, else the server's, else the
#            username and password Run Task asks for.

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
    $Message = [uri]::UnescapeDataString('{% autoescape off %}{% filter urlencode:"" %}{{ message }}{% endfilter %}{% endautoescape %}').Trim()
    if ($Message.Length -gt 200) { throw 'message must be 200 characters or fewer.' }

    # ---- task body ----
    $os = Get-CimInstance -ClassName Win32_OperatingSystem
    Log "Host: $env:COMPUTERNAME"
    Log "OS: $($os.Caption) $($os.Version)"
    Log "Last boot: $($os.LastBootUpTime.ToString('s'))"
    Log "Running as: $([Security.Principal.WindowsIdentity]::GetCurrent().Name)"
    Log "PowerShell: $($PSVersionTable.PSVersion)"
    if ($Message) { Log "Message: $Message" }

    Log 'Task finished.'
    Write-TaskOutput 'os_version' $os.Version
    Write-TaskOutput 'host_name' $env:COMPUTERNAME
    exit 0
}
catch {
    $err = $_.Exception.Message
    Write-Output "ERROR: $err"
    [Console]::Error.WriteLine("ERROR: $err")
    exit 1
}
