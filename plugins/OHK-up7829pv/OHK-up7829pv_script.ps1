<#
    CloudBolt catalog item post-provision script
    "Windows File Server" - Windows Server 2025

    Installs the File Server role (plus FSRM when a quota is requested),
    creates the share folder, applies NTFS and share permissions, publishes
    the SMB share, and optionally applies a hard quota.

    Idempotent: safe to re-run on the same server.

    Notes for CloudBolt integration:
      - The VARIABLES block reads from the environment first, so values can be
        supplied either by parameter substitution or by exporting them
        from a CloudBolt plugin before the script runs.
      - Principal names must be resolvable. For domain groups the server must
        already be domain joined (chain this after your AD domain-join plugin).
      - Exits 0 on success, 1 on failure so CloudBolt marks the job correctly.

    Requires: elevated (SYSTEM or local admin) execution context.
#>

#Requires -RunAsAdministrator
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

# ---------------------------------------------------------------------------
# VARIABLES  (override via CloudBolt parameters / environment)
# ---------------------------------------------------------------------------
$ShareName            = '{{ resource.share_name }}' 
$SharePath            = '{{ resource.share_path }}' 
$ShareDescription     = '{{ resource.share_description }}'

# Permission principals - use DOMAIN\Group or BUILTIN\Group. Empty arrays are skipped.
$FullAccessPrincipals   = @('BUILTIN\Administrators')
$ChangeAccessPrincipals = @()          # e.g. @('CONTOSO\FS-ProjectData-RW')
$ReadAccessPrincipals   = @()          # e.g. @('CONTOSO\FS-ProjectData-RO')

$RemoveEveryoneFromShare  = $true      # drop the default Everyone/Full ACE
$BreakNtfsInheritance     = $true      # make this share folder self-contained
$AccessBasedEnumeration   = $true      # hide what the user cannot read
$RequireShareEncryption   = $true      # SMB3 encryption for this share

$QuotaLimitGB             = 100        # 0 = do not create a quota
$QuotaWarningPercents     = @(85, 95)  # warning thresholds (event log action)
$QuotaSoftLimit           = $false     # $true = report only, do not block writes

$InstallDeduplication     = $false     # $true = also install FS-Data-Deduplication
$OpenFirewall             = $true      # enable the File and Printer Sharing rules

$LogPath                  = 'C:\Windows\Temp\cb_fileserver_install.log'

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

function Resolve-Principal {
    param([Parameter(Mandatory)][string]$Name)
    try {
        [void](New-Object System.Security.Principal.NTAccount($Name)).Translate(
            [System.Security.Principal.SecurityIdentifier])
        return $true
    }
    catch {
        Write-Log "Principal '$Name' could not be resolved - skipping." 'WARN'
        return $false
    }
}

try {
    Write-Log "=== File Server configuration starting on $env:COMPUTERNAME ==="

    # -----------------------------------------------------------------------
    # 1. Preflight
    # -----------------------------------------------------------------------
    $os = Get-CimInstance Win32_OperatingSystem
    Write-Log ("OS: {0} (build {1})" -f $os.Caption, $os.BuildNumber)
    if ($os.ProductType -eq 1) { throw 'This script targets Windows Server, not a client OS.' }
    if ([int]$os.BuildNumber -lt 26100) {
        Write-Log 'Build is older than Windows Server 2025 (26100). Continuing.' 'WARN'
    }

    $driveLetter = (Split-Path -Path $SharePath -Qualifier).TrimEnd(':')
    if (-not (Get-PSDrive -Name $driveLetter -PSProvider FileSystem -ErrorAction SilentlyContinue)) {
        throw "Volume ${driveLetter}: does not exist. Check the data disk parameter on the blueprint."
    }

    # -----------------------------------------------------------------------
    # 2. Roles and features
    # -----------------------------------------------------------------------
    $features = @('FS-FileServer')
    if ($QuotaLimitGB -gt 0)   { $features += 'FS-Resource-Manager' }
    if ($InstallDeduplication) { $features += 'FS-Data-Deduplication' }

    foreach ($feature in $features) {
        $state = Get-WindowsFeature -Name $feature
        if ($state.Installed) {
            Write-Log "Feature '$feature' already installed"
        }
        else {
            Write-Log "Installing feature '$feature'"
            $result = Install-WindowsFeature -Name $feature -IncludeManagementTools
            if (-not $result.Success) { throw "Install-WindowsFeature failed for '$feature'." }
            if ($result.RestartNeeded -eq 'Yes') {
                Write-Log "Feature '$feature' reports a restart is needed." 'WARN'
            }
        }
    }

    if ($QuotaLimitGB -gt 0) {
        Import-Module FileServerResourceManager -ErrorAction Stop
        if ((Get-Service -Name SrmSvc).Status -ne 'Running') {
            Write-Log 'Starting the File Server Resource Manager service (SrmSvc)'
            Set-Service -Name SrmSvc -StartupType Automatic
            Start-Service -Name SrmSvc
        }
    }

    # -----------------------------------------------------------------------
    # 3. Folder
    # -----------------------------------------------------------------------
    if (Test-Path -LiteralPath $SharePath) {
        Write-Log "Folder '$SharePath' already exists"
    }
    else {
        Write-Log "Creating folder '$SharePath'"
        [void](New-Item -Path $SharePath -ItemType Directory -Force)
    }

    # -----------------------------------------------------------------------
    # 4. NTFS permissions
    # -----------------------------------------------------------------------
    Write-Log 'Applying NTFS permissions'
    $acl = Get-Acl -LiteralPath $SharePath

    if ($BreakNtfsInheritance) {
        $acl.SetAccessRuleProtection($true, $false)   # protect, do not copy inherited ACEs
        foreach ($builtin in @('NT AUTHORITY\SYSTEM', 'BUILTIN\Administrators')) {
            $acl.AddAccessRule((New-Object System.Security.AccessControl.FileSystemAccessRule(
                $builtin, 'FullControl', 'ContainerInherit,ObjectInherit', 'None', 'Allow')))
        }
    }

    $ntfsMap = @(
        @{ Rights = 'FullControl'; Principals = $FullAccessPrincipals }
        @{ Rights = 'Modify,Synchronize'; Principals = $ChangeAccessPrincipals }
        @{ Rights = 'ReadAndExecute,Synchronize'; Principals = $ReadAccessPrincipals }
    )

    foreach ($entry in $ntfsMap) {
        foreach ($principal in $entry.Principals) {
            if (-not (Resolve-Principal -Name $principal)) { continue }
            Write-Log ("NTFS: {0} -> {1}" -f $principal, $entry.Rights)
            $acl.AddAccessRule((New-Object System.Security.AccessControl.FileSystemAccessRule(
                $principal, $entry.Rights, 'ContainerInherit,ObjectInherit', 'None', 'Allow')))
        }
    }

    Set-Acl -LiteralPath $SharePath -AclObject $acl

    # -----------------------------------------------------------------------
    # 5. SMB share
    # -----------------------------------------------------------------------
    $enumMode = if ($AccessBasedEnumeration) { 'AccessBased' } else { 'Unrestricted' }
    $existingShare = Get-SmbShare -Name $ShareName -ErrorAction SilentlyContinue

    if ($existingShare) {
        if ($existingShare.Path -ne $SharePath) {
            throw "Share '$ShareName' already exists but points to '$($existingShare.Path)'."
        }
        Write-Log "Share '$ShareName' already exists - updating properties"
        Set-SmbShare -Name $ShareName -Description $ShareDescription `
            -FolderEnumerationMode $enumMode `
            -EncryptData $RequireShareEncryption -Force
    }
    else {
        Write-Log "Creating SMB share '$ShareName' on '$SharePath'"
        [void](New-SmbShare -Name $ShareName -Path $SharePath -Description $ShareDescription `
            -FolderEnumerationMode $enumMode `
            -EncryptData $RequireShareEncryption -FullAccess 'BUILTIN\Administrators')
    }

    Write-Log 'Applying share-level permissions'
    $shareMap = @(
        @{ Level = 'Full';   Principals = $FullAccessPrincipals }
        @{ Level = 'Change'; Principals = $ChangeAccessPrincipals }
        @{ Level = 'Read';   Principals = $ReadAccessPrincipals }
    )

    foreach ($entry in $shareMap) {
        foreach ($principal in $entry.Principals) {
            if (-not (Resolve-Principal -Name $principal)) { continue }
            Write-Log ("Share: {0} -> {1}" -f $principal, $entry.Level)
            Grant-SmbShareAccess -Name $ShareName -AccountName $principal `
                -AccessRight $entry.Level -Force | Out-Null
        }
    }

    if ($RemoveEveryoneFromShare) {
        $everyone = Get-SmbShareAccess -Name $ShareName |
            Where-Object { $_.AccountName -in @('Everyone', 'Tout le monde') }
        if ($everyone) {
            Write-Log 'Removing the default Everyone share ACE'
            Revoke-SmbShareAccess -Name $ShareName -AccountName 'Everyone' -Force | Out-Null
        }
    }

    # -----------------------------------------------------------------------
    # 6. FSRM quota
    # -----------------------------------------------------------------------
    if ($QuotaLimitGB -gt 0) {
        $quotaBytes = [int64]$QuotaLimitGB * 1GB
        $thresholds = foreach ($pct in $QuotaWarningPercents) {
            $action = New-FsrmAction -Type Event -EventType Warning -Body (
                "Share [Quota Path] has reached $pct% of its $QuotaLimitGB GB limit " +
                "(current usage: [Quota Used] of [Quota Limit]).")
            New-FsrmQuotaThreshold -Percentage $pct -Action $action
        }

        $quotaArgs = @{ Path = $SharePath; Size = $quotaBytes; SoftLimit = $QuotaSoftLimit }
        if ($thresholds) { $quotaArgs['Threshold'] = $thresholds }

        if (Get-FsrmQuota -Path $SharePath -ErrorAction SilentlyContinue) {
            Write-Log "Updating quota on '$SharePath' to $QuotaLimitGB GB"
            Set-FsrmQuota @quotaArgs | Out-Null
        }
        else {
            Write-Log "Creating $QuotaLimitGB GB quota on '$SharePath'"
            New-FsrmQuota @quotaArgs | Out-Null
        }
    }
    else {
        Write-Log 'Quota not requested - skipping FSRM configuration'
    }

    # -----------------------------------------------------------------------
    # 7. Firewall
    # -----------------------------------------------------------------------
    if ($OpenFirewall) {
        Write-Log 'Enabling the File and Printer Sharing firewall rules'
        Enable-NetFirewallRule -DisplayGroup 'File and Printer Sharing' -ErrorAction SilentlyContinue
    }

    # -----------------------------------------------------------------------
    # 8. Summary
    # -----------------------------------------------------------------------
    $share  = Get-SmbShare -Name $ShareName
    $access = Get-SmbShareAccess -Name $ShareName |
        ForEach-Object { "$($_.AccountName)=$($_.AccessRight)" }
    $fqdn   = [System.Net.Dns]::GetHostEntry($env:COMPUTERNAME).HostName

    Write-Log ''
    Write-Log '========================================================================='
    Write-Log ' Windows File Server deployment complete'
    Write-Log '========================================================================='
    Write-Log (" UNC path        : \\{0}\{1}" -f $fqdn, $ShareName)
    Write-Log (" Local path      : {0}" -f $share.Path)
    Write-Log (" ABE / encrypted : {0} / {1}" -f $share.FolderEnumerationMode, $share.EncryptData)
    Write-Log (" Share access    : {0}" -f ($access -join ', '))
    if ($QuotaLimitGB -gt 0) {
        $q = Get-FsrmQuota -Path $SharePath
        Write-Log (" Quota           : {0} GB ({1}), used {2:N2} GB" -f `
            $QuotaLimitGB, ($(if ($QuotaSoftLimit) { 'soft' } else { 'hard' })), ($q.Usage / 1GB))
    }
    Write-Log ' Note            : Windows Server 2025 requires SMB signing by default;'
    Write-Log '                   legacy clients and NAS targets may need attention.'
    Write-Log (" Install log     : {0}" -f $LogPath)
    Write-Log '========================================================================='
    Write-Log '=== Finished successfully ==='
    exit 0
}
catch {
    Write-Log ("Failed: {0}" -f $_.Exception.Message) 'ERROR'
    Write-Log ("At: {0}" -f $_.InvocationInfo.PositionMessage) 'ERROR'
    exit 1
}