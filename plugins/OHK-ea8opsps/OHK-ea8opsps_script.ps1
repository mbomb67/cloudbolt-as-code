<#
.SYNOPSIS
    Configure Data Disks (Windows): finds each disk CloudBolt attached inside the
    guest, then initializes, letters, labels, and formats it.

.DESCRIPTION
    Build item of the "Windows Server with Data Disks" blueprint. It runs in the
    guest after the Attach Data Disks (Windows) plug-in, which saved the ordered
    disks on the server's data_disk_layout parameter: one entry per disk with its
    drive letter, size, volume name and where the hypervisor attached it (on
    vCenter the SCSI controller bus, unit number, and VMDK UUID; on Azure the
    data disk LUN).

    Each entry is matched to one uninitialized (RAW) guest disk, trying in turn:
      1. Serial number. With disk.EnableUUID = TRUE on the VM, Windows reports
         the VMDK UUID as the disk's serial number.
      2. LUN. An Azure data disk's LUN is the SCSI LUN Windows reports for it
         (Win32_DiskDrive.SCSILogicalUnit).
      3. SCSI target ID. The vCenter unit number is the SCSI target ID Windows
         reports for the disk (Win32_DiskDrive.SCSITargetId).
      4. Attach order. CloudBolt adds the disks in table order, so the next
         RAW disk in SCSI address order is taken.
    Every match must also agree on size. If any entry cannot be matched, or a
    requested drive letter is taken by another volume, the script stops before
    it changes any disk.

    Each matched disk is brought online, initialized GPT, given one partition
    with the requested drive letter, and formatted NTFS with the volume name as
    its label. A CD-ROM drive holding a requested letter is moved to a free
    letter. An entry whose drive letter already carries the requested label is
    reported as already configured, so the script can run again.

    CloudBolt fills in the server's data_disk_layout parameter below before it
    sends the script to the guest.

    References:
      Get-Disk / Set-Disk / Initialize-Disk / New-Partition / Format-Volume /
      Update-HostStorageCache: https://learn.microsoft.com/en-us/powershell/module/storage/
      New-Partition:   https://learn.microsoft.com/en-us/powershell/module/storage/new-partition
      Win32_DiskDrive: https://learn.microsoft.com/en-us/windows/win32/cimwin32prov/win32-diskdrive
      Win32_Volume (DriveLetter is writable, DriveType 5 = CD-ROM):
        https://learn.microsoft.com/en-us/previous-versions/windows/desktop/legacy/aa394515(v=vs.85)
      Windows disk serial number = VMDK UUID with disk.EnableUUID:
        https://knowledge.broadcom.com/external/article/432156/mapping-disk-in-windows-to-its-correspon.html
      Azure data disk LUN in the guest:
        https://learn.microsoft.com/en-us/azure/virtual-machines/windows/azure-to-guest-disk-mapping
#>

# Policy: change these for your environment.
$FileSystem          = 'NTFS'
$AllocationUnitSize  = 0      # bytes; 0 keeps the Windows default (4 KB). SQL Server data volumes often use 65536.
$WaitForDisksSeconds = 180    # how long to wait for hot-added disks to appear in the guest
$SizeToleranceGB     = 1

$ErrorActionPreference = 'Stop'

function Fail([string]$Message) {
    Write-Output "ERROR: $Message"
    exit 1
}

function ConvertTo-HexId([string]$Value) {
    if (-not $Value) { return '' }
    return ($Value -replace '[^0-9A-Fa-f]', '').ToLowerInvariant()
}

function Test-SizeMatch($GuestDisk, [int]$SizeGB) {
    return ([math]::Abs(($GuestDisk.SizeBytes / 1GB) - $SizeGB) -le $SizeToleranceGB)
}

# One object per guest disk with its SCSI address (Get-Disk Number = Win32_DiskDrive Index).
function Get-GuestDisk {
    $drives = @{}
    foreach ($drive in Get-CimInstance -ClassName Win32_DiskDrive) { $drives[[int]$drive.Index] = $drive }
    foreach ($disk in Get-Disk) {
        $drive = $drives[[int]$disk.Number]
        $port = -1; $bus = -1; $target = -1; $lun = -1
        if ($drive) {
            $port = [int]$drive.SCSIPort; $bus = [int]$drive.SCSIBus
            $target = [int]$drive.SCSITargetId; $lun = [int]$drive.SCSILogicalUnit
        }
        [pscustomobject]@{
            Number         = [int]$disk.Number
            SizeBytes      = [double]$disk.Size
            PartitionStyle = [string]$disk.PartitionStyle
            IsSystemDisk   = [bool]($disk.IsBoot -or $disk.IsSystem)
            Serial         = ConvertTo-HexId $disk.SerialNumber
            Port           = $port
            Bus            = $bus
            Target         = $target
            Lun            = $lun
        }
    }
}

# Win32_Volume DriveType: 3 = local disk, 5 = CD-ROM.
function Get-LetterVolume([string]$Letter) {
    return Get-CimInstance -ClassName Win32_Volume -Filter "DriveLetter = '$($Letter):'"
}

function Test-AlreadyConfigured($Entry) {
    $volume = Get-LetterVolume $Entry.drive_letter
    return [bool]($volume -and [int]$volume.DriveType -eq 3 -and [string]$volume.Label -eq [string]$Entry.volume_name)
}

function Move-CdRomOffLetter([string]$Letter, [string[]]$Reserved) {
    $cd = Get-LetterVolume $Letter
    if (-not $cd -or [int]$cd.DriveType -ne 5) { return }
    $used = @(Get-CimInstance -ClassName Win32_Volume | Where-Object { $_.DriveLetter } | ForEach-Object { $_.DriveLetter.Substring(0, 1) })
    $used += @(Get-PSDrive -PSProvider FileSystem | ForEach-Object { $_.Name })
    $free = [char[]](90..69) | ForEach-Object { [string]$_ } |
        Where-Object { ($used -notcontains $_) -and ($Reserved -notcontains $_) } | Select-Object -First 1
    if (-not $free) { Fail "No free drive letter to move the CD-ROM drive off $($Letter):." }
    Set-CimInstance -InputObject $cd -Property @{ DriveLetter = "$($free):" }
    Write-Output "Moved the CD-ROM drive from $($Letter): to $($free):"
}

try {
    $principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        Fail "This script must run as an administrator. Check the Remote Script's run-as credentials."
    }

    # Single-quoted here-string: PowerShell expands nothing inside it.
    $layoutJson = @'
{{ server.data_disk_layout }}
'@
    $layoutJson = $layoutJson.Trim()
    if (-not $layoutJson) { Fail "The server has no data_disk_layout parameter. The Attach Data Disks (Windows) build item must run first." }
    $parsed = ConvertFrom-Json -InputObject $layoutJson
    $requested = @($parsed | ForEach-Object { $_ } | Sort-Object -Property { [int]$_.disk })
    if ($requested.Count -eq 0) { Fail "data_disk_layout lists no disks." }
    $requestedLetters = @($requested | ForEach-Object { [string]$_.drive_letter })

    Write-Output ("Requested data disks: {0}" -f (($requested | ForEach-Object { "$($_.drive_letter): $($_.size_gb) GB '$($_.volume_name)'" }) -join ', '))

    $done = @($requested | Where-Object { Test-AlreadyConfigured $_ })
    $pending = @($requested | Where-Object { -not (Test-AlreadyConfigured $_) })

    # Pre-flight: every pending letter must be free or held by a CD-ROM drive.
    foreach ($entry in $pending) {
        $volume = Get-LetterVolume $entry.drive_letter
        if ($volume -and [int]$volume.DriveType -ne 5) {
            Fail "Disk $($entry.disk): drive letter $($entry.drive_letter): is already used by volume '$($volume.Label)'."
        }
    }

    # Wait for the hot-added disks to show up as RAW disks.
    $candidates = @()
    $deadline = (Get-Date).AddSeconds($WaitForDisksSeconds)
    while ($true) {
        Update-HostStorageCache -ErrorAction SilentlyContinue
        $candidates = @(Get-GuestDisk | Where-Object { $_.PartitionStyle -eq 'RAW' -and -not $_.IsSystemDisk })
        if ($candidates.Count -ge $pending.Count) { break }
        if ((Get-Date) -ge $deadline) {
            Fail ("Found {0} uninitialized disks but {1} are still to configure after waiting {2} seconds." -f $candidates.Count, $pending.Count, $WaitForDisksSeconds)
        }
        Write-Output ("Waiting for disks: {0} of {1} uninitialized disks visible" -f $candidates.Count, $pending.Count)
        Start-Sleep -Seconds 10
    }

    # Match every pending entry before changing anything.
    $claimed = @{}
    $plan = @()
    foreach ($entry in $pending) {
        $free = @($candidates | Where-Object { -not $claimed.ContainsKey($_.Number) })
        $match = $null
        $how = ''

        $uuid = ConvertTo-HexId ([string]$entry.disk_uuid)
        if ($uuid.Length -ge 16) {
            $bySerial = @($free | Where-Object { $_.Serial -eq $uuid })
            if ($bySerial.Count -eq 1) { $match = $bySerial[0]; $how = 'serial number = VMDK UUID' }
        }
        if (-not $match -and $null -ne $entry.lun) {
            $byLun = @($free | Where-Object { $_.Lun -eq [int]$entry.lun -and (Test-SizeMatch $_ $entry.size_gb) })
            if ($byLun.Count -eq 1) { $match = $byLun[0]; $how = "LUN $($entry.lun) = Azure LUN" }
        }
        if (-not $match -and $null -ne $entry.scsi_unit) {
            $byTarget = @($free | Where-Object { $_.Target -eq [int]$entry.scsi_unit -and (Test-SizeMatch $_ $entry.size_gb) })
            if ($byTarget.Count -eq 1) { $match = $byTarget[0]; $how = "SCSI target $($entry.scsi_unit) = vCenter unit" }
        }
        if (-not $match) {
            $byOrder = @($free | Sort-Object -Property Port, Bus, Target, Lun, Number | Where-Object { Test-SizeMatch $_ $entry.size_gb })
            if ($byOrder.Count -ge 1) { $match = $byOrder[0]; $how = 'attach order and size' }
        }
        if (-not $match) {
            Fail "Disk $($entry.disk) ($($entry.drive_letter): $($entry.size_gb) GB): no uninitialized guest disk of that size was found."
        }
        if (-not (Test-SizeMatch $match $entry.size_gb)) {
            Fail ("Disk {0}: guest disk {1} matched by {2} is {3:N1} GB, but {4} GB was ordered." -f $entry.disk, $match.Number, $how, ($match.SizeBytes / 1GB), $entry.size_gb)
        }
        $claimed[$match.Number] = $true
        $plan += [pscustomobject]@{ Entry = $entry; Disk = $match; MatchedBy = $how }
    }

    foreach ($item in $plan) {
        $entry = $item.Entry
        $letter = [string]$entry.drive_letter
        $number = $item.Disk.Number
        Write-Output ("Disk {0}: guest disk {1} (SCSI port {2} bus {3} target {4} LUN {5}, matched by {6}) -> {7}: '{8}'" -f `
            $entry.disk, $number, $item.Disk.Port, $item.Disk.Bus, $item.Disk.Target, $item.Disk.Lun, $item.MatchedBy, $letter, $entry.volume_name)

        Move-CdRomOffLetter $letter $requestedLetters

        $disk = Get-Disk -Number $number
        if ($disk.IsOffline)  { Set-Disk -Number $number -IsOffline $false }
        if ($disk.IsReadOnly) { Set-Disk -Number $number -IsReadOnly $false }
        Initialize-Disk -Number $number -PartitionStyle GPT -Confirm:$false
        $partition = New-Partition -DiskNumber $number -UseMaximumSize -DriveLetter $letter
        $format = @{
            Partition          = $partition
            FileSystem         = $FileSystem
            NewFileSystemLabel = [string]$entry.volume_name
            Confirm            = $false
        }
        if ($AllocationUnitSize -gt 0) { $format.AllocationUnitSize = $AllocationUnitSize }
        Format-Volume @format | Out-Null
    }

    $report = @()
    foreach ($item in $plan) {
        $e = $item.Entry
        $hypervisor = 'n/a'
        if ($null -ne $e.scsi_unit) { $hypervisor = "SCSI($($e.scsi_bus):$($e.scsi_unit))" }
        elseif ($null -ne $e.lun) { $hypervisor = "LUN $($e.lun)" }
        $report += [pscustomobject]@{
            Disk          = $e.disk
            Drive         = "$($e.drive_letter):"
            Label         = $e.volume_name
            'Size GB'     = $e.size_gb
            'Hypervisor'  = $hypervisor
            'Guest disk'  = $item.Disk.Number
            'Guest SCSI'  = "port $($item.Disk.Port) bus $($item.Disk.Bus) target $($item.Disk.Target) LUN $($item.Disk.Lun)"
            'Matched by'  = $item.MatchedBy
            Result        = 'formatted'
        }
    }
    foreach ($e in $done) {
        $report += [pscustomobject]@{
            Disk = $e.disk; Drive = "$($e.drive_letter):"; Label = $e.volume_name; 'Size GB' = $e.size_gb
            'Hypervisor' = ''; 'Guest disk' = ''; 'Guest SCSI' = ''; 'Matched by' = ''; Result = 'already configured'
        }
    }
    Write-Output ''
    Write-Output ($report | Sort-Object -Property Disk | Format-Table -AutoSize | Out-String -Width 220)

    Write-Output 'Volumes:'
    Write-Output (Get-Volume | Where-Object { $requestedLetters -contains [string]$_.DriveLetter } | Sort-Object -Property DriveLetter |
        Format-Table DriveLetter, FileSystemLabel, FileSystem,
            @{ Name = 'Size GB'; Expression = { [math]::Round($_.Size / 1GB, 1) } },
            @{ Name = 'Free GB'; Expression = { [math]::Round($_.SizeRemaining / 1GB, 1) } } -AutoSize | Out-String -Width 220)
}
catch {
    Write-Output ("ERROR: " + $_.Exception.Message)
    if ($_.InvocationInfo -and $_.InvocationInfo.PositionMessage) { Write-Output $_.InvocationInfo.PositionMessage }
    exit 1
}
