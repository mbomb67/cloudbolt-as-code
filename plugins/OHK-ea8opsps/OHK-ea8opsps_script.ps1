# Configure Data Disks (Windows), build item of the "Windows Server with Data Disks"
# blueprint (see its README). Works on any hypervisor: the Attach Data Disks (Windows)
# plug-in saved each disk on the server's data_disk_layout parameter with neutral keys
# (serial, scsi_target, scsi_lun, address) filled in by a per-handler provider. Each disk
# is matched to a RAW guest disk by serial number or unique ID, then SCSI target and LUN,
# then attach order, always checking the size, and nothing changes unless every disk
# matches. Each disk is then initialized GPT, given its drive letter, and formatted NTFS
# with its label. Safe to run again. Keep this file under 9,000 characters: Azure Run
# Command passes it to powershell.exe -EncodedCommand, and Windows caps a command line
# at 32,767.
#
# Storage cmdlets: https://learn.microsoft.com/en-us/powershell/module/storage/
# Win32_DiskDrive: https://learn.microsoft.com/en-us/windows/win32/cimwin32prov/win32-diskdrive
# Win32_Volume: https://learn.microsoft.com/en-us/previous-versions/windows/desktop/legacy/aa394515(v=vs.85)

# Policy
$FileSystem = 'NTFS'
$AllocationUnitSize = 0   # bytes; 0 = Windows default (4 KB). SQL Server data volumes often use 65536.
$WaitSeconds = 180        # how long to wait for hot-added disks to appear
$ToleranceGB = 1

$ErrorActionPreference = 'Stop'

function Fail([string]$Message) { Write-Output "ERROR: $Message"; exit 1 }

function Get-HexId([string]$Value) { ($Value -replace '[^0-9A-Fa-f]', '').ToLowerInvariant() }

function Test-Size($Disk, $SizeGB) { [math]::Abs($Disk.Bytes / 1GB - [int]$SizeGB) -le $ToleranceGB }

# Win32_Volume DriveType: 3 = local disk, 5 = CD-ROM.
function Get-LetterVolume([string]$Letter) {
  Get-CimInstance -ClassName Win32_Volume -Filter "DriveLetter = '$($Letter):'"
}

function Test-Done($Entry) {
  $v = Get-LetterVolume $Entry.drive_letter
  [bool]($v -and [int]$v.DriveType -eq 3 -and [string]$v.Label -eq [string]$Entry.volume_name)
}

# RAW, non-system disks with their SCSI address (Get-Disk Number = Win32_DiskDrive Index).
function Get-RawDisk {
  $drives = @{}
  foreach ($d in Get-CimInstance -ClassName Win32_DiskDrive) { $drives[[int]$d.Index] = $d }
  foreach ($disk in Get-Disk) {
    if ($disk.PartitionStyle -ne 'RAW' -or $disk.IsBoot -or $disk.IsSystem) { continue }
    $d = $drives[[int]$disk.Number]
    $a = @(-1, -1, -1, -1)
    if ($d) { $a = @([int]$d.SCSIPort, [int]$d.SCSIBus, [int]$d.SCSITargetId, [int]$d.SCSILogicalUnit) }
    [pscustomobject]@{
      Number = [int]$disk.Number; Bytes = [double]$disk.Size
      Ids = @((Get-HexId $disk.SerialNumber), (Get-HexId $disk.UniqueId))
      Port = $a[0]; Bus = $a[1]; Target = $a[2]; Lun = $a[3]
    }
  }
}

function Move-CdRom([string]$Letter, [string[]]$Reserved) {
  $cd = Get-LetterVolume $Letter
  if (-not $cd -or [int]$cd.DriveType -ne 5) { return }
  $used = @(Get-CimInstance -ClassName Win32_Volume | Where-Object { $_.DriveLetter } | ForEach-Object { $_.DriveLetter.Substring(0, 1) })
  $used += @(Get-PSDrive -PSProvider FileSystem | ForEach-Object { $_.Name })
  $free = [char[]](90..69) | ForEach-Object { [string]$_ } | Where-Object { $used -notcontains $_ -and $Reserved -notcontains $_ } | Select-Object -First 1
  if (-not $free) { Fail "No free letter to move the CD-ROM drive off $($Letter):." }
  Set-CimInstance -InputObject $cd -Property @{ DriveLetter = "$($free):" }
  Write-Output "Moved the CD-ROM drive from $($Letter): to $($free):"
}

try {
  $me = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
  if (-not $me.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Fail "Run this script as an administrator (check the Remote Script's run-as credentials)."
  }

  # Single-quoted here-string: PowerShell expands nothing inside it.
  $layoutJson = @'
{{ server.data_disk_layout }}
'@
  if (-not $layoutJson.Trim()) { Fail "The server has no data_disk_layout. Attach Data Disks (Windows) must run first." }
  $parsed = ConvertFrom-Json -InputObject $layoutJson.Trim()
  $requested = @($parsed | ForEach-Object { $_ } | Sort-Object -Property { [int]$_.disk })
  $letters = @($requested | ForEach-Object { [string]$_.drive_letter })
  $done = @($requested | Where-Object { Test-Done $_ })
  $pending = @($requested | Where-Object { -not (Test-Done $_) })
  Write-Output ("Requested: " + (($requested | ForEach-Object { "$($_.drive_letter): $($_.size_gb) GB '$($_.volume_name)'" }) -join ', '))

  foreach ($e in $pending) {
    $v = Get-LetterVolume $e.drive_letter
    if ($v -and [int]$v.DriveType -ne 5) { Fail "Disk $($e.disk): $($e.drive_letter): is already used by volume '$($v.Label)'." }
  }

  # Wait for hot-added disks to show up.
  $deadline = (Get-Date).AddSeconds($WaitSeconds)
  while ($true) {
    Update-HostStorageCache -ErrorAction SilentlyContinue
    $raw = @(Get-RawDisk)
    if ($raw.Count -ge $pending.Count) { break }
    if ((Get-Date) -ge $deadline) { Fail "Only $($raw.Count) RAW disks for $($pending.Count) pending disks after $WaitSeconds seconds." }
    Write-Output "Waiting for disks: $($raw.Count) of $($pending.Count) RAW disks visible"
    Start-Sleep -Seconds 10
  }

  # Match every pending disk before changing anything.
  $claimed = @{}
  $plan = @()
  foreach ($e in $pending) {
    $free = @($raw | Where-Object { -not $claimed.ContainsKey($_.Number) })
    $m = $null; $how = ''
    $sid = Get-HexId ([string]$e.serial)
    if ($sid.Length -ge 16) {
      $hits = @($free | Where-Object { @($_.Ids | Where-Object { $_.Contains($sid) }).Count -gt 0 })
      if ($hits.Count -eq 1) { $m = $hits[0]; $how = 'serial number' }
    }
    if (-not $m -and ($null -ne $e.scsi_target -or $null -ne $e.scsi_lun)) {
      $hits = @($free | Where-Object {
        ($null -eq $e.scsi_target -or $_.Target -eq [int]$e.scsi_target) -and
        ($null -eq $e.scsi_lun -or $_.Lun -eq [int]$e.scsi_lun) -and (Test-Size $_ $e.size_gb) })
      if ($hits.Count -eq 1) { $m = $hits[0]; $how = 'SCSI target and LUN' }
    }
    if (-not $m) {
      $m = @($free | Sort-Object -Property Port, Bus, Target, Lun, Number | Where-Object { Test-Size $_ $e.size_gb }) | Select-Object -First 1
      $how = 'attach order and size'
    }
    if (-not $m) { Fail "Disk $($e.disk) ($($e.drive_letter): $($e.size_gb) GB): no RAW guest disk of that size." }
    if (-not (Test-Size $m $e.size_gb)) { Fail "Disk $($e.disk): guest disk $($m.Number) matched by $how is $([math]::Round($m.Bytes / 1GB, 1)) GB, not $($e.size_gb) GB." }
    $claimed[$m.Number] = $true
    $plan += [pscustomobject]@{ Entry = $e; Disk = $m; How = $how }
  }

  $report = @()
  foreach ($p in $plan) {
    $e = $p.Entry; $n = $p.Disk.Number; $letter = [string]$e.drive_letter
    Move-CdRom $letter $letters
    $disk = Get-Disk -Number $n
    if ($disk.IsOffline) { Set-Disk -Number $n -IsOffline $false }
    if ($disk.IsReadOnly) { Set-Disk -Number $n -IsReadOnly $false }
    Initialize-Disk -Number $n -PartitionStyle GPT -Confirm:$false
    $part = New-Partition -DiskNumber $n -UseMaximumSize -DriveLetter $letter
    $fmt = @{ Partition = $part; FileSystem = $FileSystem; NewFileSystemLabel = [string]$e.volume_name; Confirm = $false }
    if ($AllocationUnitSize -gt 0) { $fmt.AllocationUnitSize = $AllocationUnitSize }
    Format-Volume @fmt | Out-Null

    $hv = 'n/a'
    if ($e.address) { $hv = [string]$e.address }
    $d = $p.Disk
    $report += [pscustomobject]@{
      Disk = $e.disk; Drive = "$($letter):"; Label = $e.volume_name; GB = $e.size_gb; Hypervisor = $hv
      'Guest disk' = $n; 'Port:Bus:Target:LUN' = "$($d.Port):$($d.Bus):$($d.Target):$($d.Lun)"; 'Matched by' = $p.How; Result = 'formatted'
    }
  }
  foreach ($e in $done) {
    $report += [pscustomobject]@{
      Disk = $e.disk; Drive = "$($e.drive_letter):"; Label = $e.volume_name; GB = $e.size_gb; Hypervisor = ''
      'Guest disk' = ''; 'Port:Bus:Target:LUN' = ''; 'Matched by' = ''; Result = 'already configured'
    }
  }
  Write-Output ($report | Sort-Object -Property Disk | Format-Table -AutoSize | Out-String -Width 200)
}
catch {
  Write-Output ("ERROR: " + $_.Exception.Message)
  if ($_.InvocationInfo) { Write-Output $_.InvocationInfo.PositionMessage }
  exit 1
}
