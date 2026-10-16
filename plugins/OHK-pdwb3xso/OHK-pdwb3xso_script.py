"""
Configure Data Disks from Disk Map (Windows)

Post-Provision orchestration action, after Attach Data Disks from Disk Map. For
every Windows server of the job with a disk_map parameter, it runs the PowerShell
below in the guest with the server's data_disk_layout filled in. The script
matches each entry to a RAW disk by serial number, SCSI address, or attach order,
checks its size, then initializes it GPT, assigns the drive letter, and formats it
NTFS with the volume name as its label. Servers without a disk_map are skipped
without contacting the guest.

The action's OS family filter (Windows) keeps it off other servers; the check in
run() keeps it off them if the filter is edited.
"""
from shared_modules import disk_map as dm

# The in-guest script. CloudBolt renders this plug-in file as a template before
# running it, so the script must never contain template tags; the layout goes in
# through dm.LAYOUT_TOKEN instead. Keep it under about 8,500 characters: Azure Run
# Command sends it as powershell -EncodedCommand, capped at 32,767 characters.
# Docs: learn.microsoft.com/powershell/module/storage, .../cimwin32prov/win32-diskdrive,
# .../previous-versions/windows/desktop/legacy/aa394515(v=vs.85) (Win32_Volume)
SCRIPT = r'''
# Policy
$FileSystem = 'NTFS'
$AllocationUnitSize = 0   # bytes; 0 = Windows default (4 KB). SQL Server data volumes often use 65536.
$WaitSeconds = 180        # how long to wait for hot-added disks to appear
$ToleranceGB = 1

$ErrorActionPreference = 'Stop'

function Fail([string]$Message) { Write-Output "ERROR: $Message"; exit 1 }

function Get-HexId([string]$Value) { ($Value -replace '[^0-9A-Fa-f]', '').ToLowerInvariant() }

function Test-Size($Disk, $SizeGB) { [math]::Abs($Disk.Bytes / 1GB - [int]$SizeGB) -le $ToleranceGB }

# True when one of the disk's IDs contains an expected serial or the reverse (16+ hex digits).
function Test-Id($Disk, $Serials) {
  foreach ($i in $Serials) { foreach ($g in $Disk.Ids) { if ($g.Length -ge 16 -and ($g.Contains($i) -or $i.Contains($g))) { return $true } } }
  $false
}

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
    Fail "Run this script as an administrator (check the server's credentials in CloudBolt)."
  }

  # Single-quoted here-string: PowerShell expands nothing inside it.
  $layoutJson = @'
__CB_DATA_DISK_LAYOUT__
'@
  if (-not $layoutJson.Trim()) { Fail "The server has no data_disk_layout. Attach Data Disks from Disk Map must run first." }
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
    $sids = @($e.serials | ForEach-Object { Get-HexId ([string]$_) } | Where-Object { $_.Length -ge 16 })
    if ($sids.Count -gt 0) {
      $hits = @($free | Where-Object { Test-Id $_ $sids })
      if ($hits.Count -eq 1) { $m = $hits[0]; $how = 'serial number' }
    }
    if (-not $m -and ($null -ne $e.scsi_target -or $null -ne $e.scsi_lun)) {
      $hits = @($free | Where-Object {
        ($null -eq $e.scsi_target -or $_.Target -eq [int]$e.scsi_target) -and
        ($null -eq $e.scsi_lun -or $_.Lun -eq [int]$e.scsi_lun) -and (Test-Size $_ $e.size_gb) })
      if ($hits.Count -eq 1) {
        $m = $hits[0]
        $how = 'SCSI ' + ((@(@('target', $e.scsi_target), @('LUN', $e.scsi_lun)) | Where-Object { $null -ne $_[1] } | ForEach-Object { $_[0] }) -join ' and ')
      }
    }
    if (-not $m -and $null -ne $e.scsi_index) {
      $x = [int]$e.scsi_index
      $hits = @($free | Where-Object { (($_.Target -eq $x -and $_.Lun -eq 0) -or ($_.Target -eq 0 -and $_.Lun -eq $x)) -and (Test-Size $_ $e.size_gb) })
      if ($hits.Count -eq 1) { $m = $hits[0]; $how = 'SCSI index' }
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
'''


def run(job, *args, **kwargs):
    servers = kwargs.get("servers") or list(job.server_set.all())
    servers = [s for s in servers if s.is_windows()]
    return dm.configure_in_guest(job, servers, SCRIPT, "ps1", run_with_sudo=False)
