# Manage Windows Service
#
# Server task for Run Task (server_actions/SVA-4sin3s0q), built on the Server
# Task Template (Windows): shows, starts, stops or restarts one Windows service
# by its service name (not its display name) and prints the service's state
# afterwards. Starting a running service or stopping a stopped one changes
# nothing. A service other services depend on is not stopped or restarted.
#
# References:
#   Get-Service:     https://learn.microsoft.com/powershell/module/microsoft.powershell.management/get-service
#   Start-Service:   https://learn.microsoft.com/powershell/module/microsoft.powershell.management/start-service
#   Stop-Service:    https://learn.microsoft.com/powershell/module/microsoft.powershell.management/stop-service
#   Restart-Service: https://learn.microsoft.com/powershell/module/microsoft.powershell.management/restart-service

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$WarningPreference = 'SilentlyContinue'

function Log([string]$Text) {
    Write-Output ('[{0}] {1}' -f (Get-Date -Format 's'), $Text)
}

try {
    # ---- inputs ----
    $ServiceName = [uri]::UnescapeDataString('{{ service_name|urlencode:"" }}').Trim()
    $Operation = [uri]::UnescapeDataString('{{ operation|urlencode:"" }}').Trim().ToLowerInvariant()
    if (-not $ServiceName) { throw 'service_name is required.' }
    if ($ServiceName -match '[\*\?\[\]]') { throw "service_name '$ServiceName' must be one exact service name, without wildcards." }
    if ($Operation -notin @('status', 'start', 'stop', 'restart')) {
        throw "operation must be status, start, stop or restart, not '$Operation'."
    }

    # ---- task body ----
    $service = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
    if (-not $service) { throw "There is no service named '$ServiceName' on $env:COMPUTERNAME." }
    Log "$($service.Name) ($($service.DisplayName)) is $($service.Status)."

    switch ($Operation) {
        'start' {
            if ($service.Status -eq 'Running') { Log 'Already running; nothing to do.' }
            else { Log 'Starting...'; Start-Service -InputObject $service }
        }
        'stop' {
            if ($service.Status -eq 'Stopped') { Log 'Already stopped; nothing to do.' }
            else { Log 'Stopping...'; Stop-Service -InputObject $service }
        }
        'restart' { Log 'Restarting...'; Restart-Service -InputObject $service }
    }

    $service.Refresh()
    $startType = (Get-CimInstance -ClassName Win32_Service -Filter ("Name='{0}'" -f ($service.Name -replace "'", "\'"))).StartMode
    Log "$($service.Name) is $($service.Status) (start type: $startType)."
    Log 'Task finished.'
    exit 0
}
catch {
    $err = $_.Exception.Message
    Write-Output "ERROR: $err"
    [Console]::Error.WriteLine("ERROR: $err")
    exit 1
}
