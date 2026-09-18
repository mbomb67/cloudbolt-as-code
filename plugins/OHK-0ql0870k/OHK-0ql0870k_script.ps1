<#
    CloudBolt catalog item post-provision script
    "IIS Web Server" - Windows Server 2025

    Installs the Web Server (IIS) role, creates a dedicated application pool
    and site, publishes a landing page that identifies the server and the
    order that built it, and verifies the site responds before reporting
    success.

    Idempotent: safe to re-run on the same server.

    Notes for CloudBolt integration:
      - The VARIABLES block reads from the environment first, so values can
        come from parameter substitution or be exported by a plugin.
      - The summary prints SITE_URL - map that to a server/resource attribute
        so the resource detail page can render it as a clickable link.
      - The role install is a CBS operation (TiWorker.exe) and can take several
        minutes on a cold template. Pre-installing Web-Server in the image makes
        this run in seconds.

    Requires: elevated (SYSTEM or local admin) execution context.
#>

#Requires -RunAsAdministrator
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

# ---------------------------------------------------------------------------
# VARIABLES  (override via CloudBolt parameters / environment)
# ---------------------------------------------------------------------------
$SiteName        = '{{server.iis_site_name}}'
$SiteTitle       = '{{server.iis_site_title}}'
$SitePort        = '{{server.iis_site_port}}'
try   { $SitePort = [int]$SitePort }
catch { throw "SitePort '$SitePort' is not a number." }
$SitePath        = 'C:\inetpub\cloudbolt-site'
$HostHeader      = ''   # '' = all hostnames
$AppPoolName     = '{{server.iis_app_pool_name}}'

# Shown on the landing page - wire these to order metadata
$OrderId         = 'n/a'
$RequestedBy     = '{{server.owner}}'
$EnvironmentName = '{{server.environment.name}}'
$OwnerGroup      = '{{server.group.name}}'

$StopDefaultSite = $true      # stop 'Default Web Site' so it cannot shadow port 80
$InstallAspNet   = $false     # $true = also install ASP.NET 4.8 features
$OpenFirewall    = $true      # enable World Wide Web Services firewall rule
$LogPath         = 'C:\Windows\Temp\cb_iis_install.log'

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
function Write-Log {
    param(
        [Parameter(Mandatory = $false)][AllowEmptyString()][string]$Message = '',
        [ValidateSet('INFO', 'WARN', 'ERROR')][string]$Level = 'INFO'
    )
    $line = '{0} [{1,-5}] {2}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $Level, $Message
    Write-Output $line
    try { Add-Content -Path $LogPath -Value $line -Encoding UTF8 } catch { }
}
 
try {
    Write-Log "=== IIS configuration starting on $env:COMPUTERNAME ==="
 
    # -----------------------------------------------------------------------
    # 1. Preflight
    # -----------------------------------------------------------------------
    $os = Get-CimInstance Win32_OperatingSystem
    Write-Log ("OS: {0} (build {1})" -f $os.Caption, $os.BuildNumber)
    if ($os.ProductType -eq 1) { throw 'This script targets Windows Server, not a client OS.' }
    # Coerce to int - a string value makes -lt/-gt compare lexicographically,
    # which wrongly rejects valid ports such as 80.
    try   { $SitePort = [int]$SitePort }
    catch { throw "SitePort '$SitePort' is not a number." }
    if ($SitePort -lt 1 -or $SitePort -gt 65535) { throw "Invalid SitePort '$SitePort'." }
    if ($SitePort -ge 49152) {
        Write-Log "Port $SitePort is in the Windows ephemeral range and may be in use." 'WARN'
    }
 
    $inUse = Get-NetTCPConnection -State Listen -LocalPort $SitePort -ErrorAction SilentlyContinue
    if ($inUse) {
        $owners = ($inUse | ForEach-Object {
            (Get-Process -Id $_.OwningProcess -ErrorAction SilentlyContinue).ProcessName
        } | Sort-Object -Unique) -join ', '
        Write-Log "TCP/$SitePort is already listening (process: $owners)." 'WARN'
    }
 
    # -----------------------------------------------------------------------
    # 2. Roles and features
    # -----------------------------------------------------------------------
    $features = @('Web-Server', 'Web-Mgmt-Console')
    if ($InstallAspNet) { $features += @('Web-Asp-Net45', 'Web-Net-Ext45') }
 
    foreach ($feature in $features) {
        if ((Get-WindowsFeature -Name $feature).Installed) {
            Write-Log "Feature '$feature' already installed"
        }
        else {
            Write-Log "Installing feature '$feature' (this can take several minutes)"
            $result = Install-WindowsFeature -Name $feature -IncludeManagementTools
            if (-not $result.Success) { throw "Install-WindowsFeature failed for '$feature'." }
            if ($result.RestartNeeded -eq 'Yes') {
                Write-Log "Feature '$feature' reports a restart is needed." 'WARN'
            }
        }
    }
 
    Import-Module WebAdministration -ErrorAction Stop
    if ((Get-Service W3SVC).Status -ne 'Running') {
        Write-Log 'Starting the World Wide Web Publishing service (W3SVC)'
        Set-Service -Name W3SVC -StartupType Automatic
        Start-Service -Name W3SVC
    }
 
    # -----------------------------------------------------------------------
    # 3. Content directory
    # -----------------------------------------------------------------------
    if (Test-Path -LiteralPath $SitePath) {
        Write-Log "Content directory '$SitePath' already exists"
    }
    else {
        Write-Log "Creating content directory '$SitePath'"
        [void](New-Item -Path $SitePath -ItemType Directory -Force)
    }
 
    # -----------------------------------------------------------------------
    # 4. Application pool
    # -----------------------------------------------------------------------
    if (Test-Path "IIS:\AppPools\$AppPoolName") {
        Write-Log "Application pool '$AppPoolName' already exists"
    }
    else {
        Write-Log "Creating application pool '$AppPoolName'"
        [void](New-WebAppPool -Name $AppPoolName)
    }
    Set-ItemProperty "IIS:\AppPools\$AppPoolName" -Name managedRuntimeVersion -Value ''
    Set-ItemProperty "IIS:\AppPools\$AppPoolName" -Name processModel.identityType -Value 4  # ApplicationPoolIdentity
 
    # The pool identity needs read access to the content directory
    $poolIdentity = "IIS AppPool\$AppPoolName"
    Write-Log "Granting read access to '$poolIdentity'"
    $acl = Get-Acl -LiteralPath $SitePath
    $acl.AddAccessRule((New-Object System.Security.AccessControl.FileSystemAccessRule(
        $poolIdentity, 'ReadAndExecute,Synchronize', 'ContainerInherit,ObjectInherit', 'None', 'Allow')))
    Set-Acl -LiteralPath $SitePath -AclObject $acl
 
    # -----------------------------------------------------------------------
    # 5. Landing page
    # -----------------------------------------------------------------------
    $fqdn      = [System.Net.Dns]::GetHostEntry($env:COMPUTERNAME).HostName
    $primaryIp = (Get-NetIPAddress -AddressFamily IPv4 |
                  Where-Object { $_.IPAddress -notlike '127.*' -and $_.PrefixOrigin -ne 'WellKnown' } |
                  Select-Object -First 1 -ExpandProperty IPAddress)
    $iisVer    = (Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\InetStp' -Name VersionString).VersionString
    $buildTime = Get-Date -Format 'yyyy-MM-dd HH:mm:ss zzz'
 
    Write-Log "Writing landing page to $SitePath\index.html"
    $html = @"
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>$SiteTitle</title>
  <style>
    :root { color-scheme: light dark; }
    body { margin: 0; min-height: 100vh; display: grid; place-items: center;
           font-family: ui-sans-serif, system-ui, "Segoe UI", Helvetica, Arial, sans-serif;
           background: #0f1b2d; color: #e8edf5; }
    .card { width: min(680px, 90vw); background: #16243b; border: 1px solid #24374f;
            border-radius: 14px; padding: 2.25rem 2.5rem;
            box-shadow: 0 18px 50px rgba(0,0,0,.35); }
    .status { display: inline-flex; align-items: center; gap: .5rem;
              font-size: .8rem; letter-spacing: .08em; text-transform: uppercase;
              color: #7ee2a8; margin-bottom: 1rem; }
    .dot { width: .6rem; height: .6rem; border-radius: 50%; background: #37d67a; }
    h1 { margin: 0 0 .35rem; font-size: 1.7rem; }
    p.lede { margin: 0 0 1.75rem; color: #9fb0c8; font-size: .95rem; }
    table { width: 100%; border-collapse: collapse; font-size: .92rem; }
    th, td { text-align: left; padding: .55rem .25rem; border-bottom: 1px solid #24374f; }
    th { color: #9fb0c8; font-weight: 500; width: 42%; }
    td { font-family: ui-monospace, Consolas, "Courier New", monospace; }
    tr:last-child th, tr:last-child td { border-bottom: none; }
    footer { margin-top: 1.75rem; font-size: .8rem; color: #7c8ea8; }
  </style>
</head>
<body>
  <main class="card">
    <div class="status"><span class="dot"></span> Service online</div>
    <h1>$SiteTitle</h1>
    <p class="lede">This IIS instance was provisioned and configured by CloudBolt.</p>
    <table>
      <tr><th>Hostname</th><td>$fqdn</td></tr>
      <tr><th>IP address</th><td>$primaryIp</td></tr>
      <tr><th>Operating system</th><td>$($os.Caption)</td></tr>
      <tr><th>Web server</th><td>IIS $iisVer</td></tr>
      <tr><th>Site / app pool</th><td>$SiteName / $AppPoolName</td></tr>
      <tr><th>Listening port</th><td>$SitePort</td></tr>
      <tr><th>Order ID</th><td>$OrderId</td></tr>
      <tr><th>Requested by</th><td>$RequestedBy</td></tr>
      <tr><th>Owner group</th><td>$OwnerGroup</td></tr>
      <tr><th>Environment</th><td>$EnvironmentName</td></tr>
      <tr><th>Configured at</th><td>$buildTime</td></tr>
    </table>
    <footer>Health endpoint: <code>/health.txt</code></footer>
  </main>
</body>
</html>
"@
    Set-Content -Path (Join-Path $SitePath 'index.html') -Value $html -Encoding UTF8
    Set-Content -Path (Join-Path $SitePath 'health.txt') -Value 'ok' -Encoding ASCII
 
    # -----------------------------------------------------------------------
    # 6. Site and binding
    # -----------------------------------------------------------------------
    if ($StopDefaultSite) {
        $defaultSite = Get-Website -Name 'Default Web Site' -ErrorAction SilentlyContinue
        if ($defaultSite -and $defaultSite.State -eq 'Started') {
            Write-Log "Stopping 'Default Web Site' so it cannot shadow port $SitePort"
            Stop-Website -Name 'Default Web Site'
        }
    }
 
    $site = Get-Website -Name $SiteName -ErrorAction SilentlyContinue
    if ($site) {
        Write-Log "Site '$SiteName' already exists - updating path and app pool"
        Set-ItemProperty "IIS:\Sites\$SiteName" -Name physicalPath    -Value $SitePath
        Set-ItemProperty "IIS:\Sites\$SiteName" -Name applicationPool -Value $AppPoolName
    }
    else {
        Write-Log "Creating site '$SiteName' on port $SitePort"
        $siteArgs = @{
            Name         = $SiteName
            PhysicalPath = $SitePath
            Port         = $SitePort
            ApplicationPool = $AppPoolName
        }
        if ($HostHeader) { $siteArgs['HostHeader'] = $HostHeader }
        [void](New-Website @siteArgs)
    }
 
    # Ensure the requested binding exists (covers a re-run with a changed port)
    $bindingInfo = "*:${SitePort}:$HostHeader"
    $bindings = (Get-Website -Name $SiteName).bindings.Collection |
                Where-Object { $_.bindingInformation -eq $bindingInfo }
    if (-not $bindings) {
        Write-Log "Adding binding '$bindingInfo'"
        New-WebBinding -Name $SiteName -Protocol http -Port $SitePort `
            -HostHeader $HostHeader -ErrorAction SilentlyContinue
    }
 
    # Ensure index.html is a default document. IIS inherits a machine-level list
    # that already contains index.html, and the collection is keyed on 'value',
    # so adding it again raises a duplicate-entry exception that -ErrorAction
    # cannot suppress (it comes from the config API, not the cmdlet).
    try {
        $docFilter  = 'system.webServer/defaultDocument/files'
        $currentDocs = (Get-WebConfiguration -Filter $docFilter -PSPath "IIS:\Sites\$SiteName").Collection |
                       Select-Object -ExpandProperty value
        if ($currentDocs -contains 'index.html') {
            Write-Log 'index.html is already a default document'
        }
        else {
            Write-Log 'Adding index.html to the default document list'
            Add-WebConfigurationProperty -PSPath 'MACHINE/WEBROOT/APPHOST' `
                -Location $SiteName -Filter $docFilter `
                -Name '.' -Value @{ value = 'index.html' } -AtIndex 0
        }
    }
    catch {
        Write-Log ("Could not adjust the default document list: {0}" -f $_.Exception.Message) 'WARN'
    }
 
    Write-Log "Starting site '$SiteName'"
    if ((Get-Website -Name $SiteName).State -ne 'Started') { Start-Website -Name $SiteName }
 
    # -----------------------------------------------------------------------
    # 7. Firewall
    # -----------------------------------------------------------------------
    if ($OpenFirewall) {
        if ($SitePort -eq 80) {
            Write-Log 'Enabling the World Wide Web Services firewall rule'
            Enable-NetFirewallRule -Name 'IIS-WebServerRole-HTTP-In-TCP' -ErrorAction SilentlyContinue
        }
        else {
            $ruleName = "CloudBolt-HTTP-$SitePort"
            if (-not (Get-NetFirewallRule -DisplayName $ruleName -ErrorAction SilentlyContinue)) {
                Write-Log "Creating firewall rule '$ruleName' for TCP/$SitePort"
                [void](New-NetFirewallRule -DisplayName $ruleName -Direction Inbound `
                    -Action Allow -Protocol TCP -LocalPort $SitePort)
            }
        }
    }
 
    # -----------------------------------------------------------------------
    # 8. Verify
    # -----------------------------------------------------------------------
    Write-Log 'Verifying the site responds locally'
    $statusCode = 0
    for ($i = 1; $i -le 15; $i++) {
        try {
            $response = Invoke-WebRequest -Uri "http://127.0.0.1:$SitePort/" `
                -UseBasicParsing -TimeoutSec 10
            $statusCode = [int]$response.StatusCode
            if ($statusCode -eq 200) { break }
        }
        catch { Start-Sleep -Seconds 2 }
    }
    if ($statusCode -ne 200) {
        throw "Site did not return HTTP 200 on port $SitePort (last status: $statusCode)."
    }
 
    $portSuffix = if ($SitePort -eq 80) { '' } else { ":$SitePort" }
    $siteUrl    = "http://$fqdn$portSuffix/"
 
    Write-Log ''
    Write-Log '========================================================================='
    Write-Log ' IIS web server deployment complete'
    Write-Log '========================================================================='
    Write-Log (" SITE_URL       : {0}" -f $siteUrl)
    Write-Log (" By IP          : http://{0}{1}/" -f $primaryIp, $portSuffix)
    Write-Log (" Health check   : {0}health.txt" -f $siteUrl)
    Write-Log (" IIS version    : {0}" -f $iisVer)
    Write-Log (" Site / pool    : {0} / {1}" -f $SiteName, $AppPoolName)
    Write-Log (" Content path   : {0}" -f $SitePath)
    Write-Log (" Local HTTP test: {0}" -f $statusCode)
    Write-Log (" Install log    : {0}" -f $LogPath)
    Write-Log '========================================================================='
    Write-Log '=== Finished successfully ==='
    exit 0
}
catch {
    Write-Log ("Failed: {0}" -f $_.Exception.Message) 'ERROR'
    Write-Log ("At: {0}" -f $_.InvocationInfo.PositionMessage) 'ERROR'
    exit 1
}