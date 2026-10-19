# POC Hello World (Windows): proves that CloudBolt runs a script inside the
# guest over WinRM without an agent (test plan 1.1.3, 4.1.2, 4.3.2).
$ErrorActionPreference = "Stop"
Write-Output ("Hello from CloudBolt on {0} at {1}" -f $env:COMPUTERNAME, (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ"))
Write-Output ("Running as {0} on {1}" -f [Security.Principal.WindowsIdentity]::GetCurrent().Name, (Get-CimInstance Win32_OperatingSystem).Caption)
Write-Output "poc_hello_world=ok"
