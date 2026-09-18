<#
.SYNOPSIS
    Initializes, partitions, and formats any newly added raw disks in a Windows guest.

.DESCRIPTION
    Rescans the storage bus, finds every disk with a RAW partition style (the state a
    freshly attached virtual disk arrives in), brings it online, initializes it as GPT,
    creates a single partition using all available space, and formats it NTFS.

    Windows assigns the next available drive letter automatically via
    New-Partition -AssignDriveLetter.

.PARAMETER FileSystem
    File system to format with. NTFS (default) or ReFS.

.PARAMETER PartitionStyle
    GPT (default) or MBR. Use MBR only for legacy/BIOS guests or disks under 2 TB
    that must remain MBR-compatible.

.PARAMETER Label
    Volume label applied to the new volume(s). Defaults to "Data".

.PARAMETER DiskNumber
    Optional. Restrict the operation to one specific disk number instead of
    processing every raw disk found.

.EXAMPLE
    .\Initialize-NewDisk.ps1

.EXAMPLE
    .\Initialize-NewDisk.ps1 -Label "SQLData" -DiskNumber 2

.NOTES
    Must run elevated. Only touches disks reporting PartitionStyle = RAW, so it will
    not overwrite an existing initialized disk.
#>

#Requires -RunAsAdministrator
#Requires -Version 5.1

[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [ValidateSet('NTFS', 'ReFS')]
    [string]$FileSystem = 'NTFS',

    [ValidateSet('GPT', 'MBR')]
    [string]$PartitionStyle = 'GPT',

    [string]$Label = 'Data',

    [int]$DiskNumber
)

$ErrorActionPreference = 'Stop'

# Force a storage rescan so a disk hot-added to the VM shows up without a reboot.
Write-Host "Rescanning storage bus..." -ForegroundColor Cyan
Update-HostStorageCache

# RAW = uninitialized. This is the safety gate: initialized disks are ignored.
$rawDisks = Get-Disk | Where-Object { $_.PartitionStyle -eq 'RAW' }

if ($PSBoundParameters.ContainsKey('DiskNumber')) {
    $rawDisks = $rawDisks | Where-Object { $_.Number -eq $DiskNumber }
    if (-not $rawDisks) {
        Write-Warning "Disk $DiskNumber was not found, or is already initialized. Nothing to do."
        return
    }
}

if (-not $rawDisks) {
    Write-Host "No uninitialized (RAW) disks found. Nothing to do." -ForegroundColor Yellow
    return
}

foreach ($disk in $rawDisks) {

    $sizeGB = [math]::Round($disk.Size / 1GB, 2)
    Write-Host "`nProcessing disk $($disk.Number) - $sizeGB GB - $($disk.FriendlyName)" -ForegroundColor Cyan

    if (-not $PSCmdlet.ShouldProcess("Disk $($disk.Number) ($sizeGB GB)", "Initialize and format $FileSystem")) {
        continue
    }

    try {
        # A newly attached disk can come up offline or read-only.
        if ($disk.IsOffline)  { Set-Disk -Number $disk.Number -IsOffline $false }
        if ($disk.IsReadOnly) { Set-Disk -Number $disk.Number -IsReadOnly $false }

        Initialize-Disk -Number $disk.Number -PartitionStyle $PartitionStyle -Confirm:$false

        # -AssignDriveLetter lets Windows pick the next free letter.
        $partition = New-Partition -DiskNumber $disk.Number -UseMaximumSize -AssignDriveLetter

        $volume = Format-Volume -Partition $partition `
                                -FileSystem $FileSystem `
                                -NewFileSystemLabel $Label `
                                -Confirm:$false

        Write-Host ("  Success: {0}: [{1}] {2} - {3} GB usable" -f `
                    $volume.DriveLetter, `
                    $volume.FileSystemLabel, `
                    $volume.FileSystem, `
                    [math]::Round($volume.Size / 1GB, 2)) -ForegroundColor Green
    }
    catch {
        Write-Error "  Failed on disk $($disk.Number): $($_.Exception.Message)"
    }
}

Write-Host "`nCurrent volumes:" -ForegroundColor Cyan
Get-Volume | Where-Object { $_.DriveLetter } |
    Sort-Object DriveLetter |
    Format-Table DriveLetter, FileSystemLabel, FileSystem,
        @{ N = 'Size(GB)';  E = { [math]::Round($_.Size / 1GB, 2) } },
        @{ N = 'Free(GB)';  E = { [math]::Round($_.SizeRemaining / 1GB, 2) } } -AutoSize