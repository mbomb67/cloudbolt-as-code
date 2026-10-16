# Windows Server with Data Disks

Deploys one Windows Server 2025 VM with the data disks the requester lists on the order form (drive letter, size, volume name, as many rows as needed). CloudBolt adds each disk to the VM in row order and records where the hypervisor attached it (the SCSI controller, unit number, and VMDK UUID on vCenter, the LUN on Azure); the in-guest script then finds each disk by that identity, not by guess, and initializes it GPT, assigns the drive letter, and formats it NTFS with the volume name as its label.

## Contents
| Role | ID | Name |
|---|---|---|
| Custom form | FRM-jqnr84d0 | Environment page plus a Data Disks table (`plugin-bdi-xwp2jz95.data_disks`) |
| Server tier (seq 1) | — | Windows Server (OS build "Windows 2025"; hostname `wdd-<grp>-w-00X`; all environments enabled) |
| Build (Plug-in, seq 2) | OHK-1m6n3gq9 | Attach Data Disks (Windows) |
| Build (Remote Script, seq 3) | OHK-ea8opsps | Configure Data Disks (Windows) |

## How a disk is matched in the guest
OHK-1m6n3gq9 saves the rows on the server's `data_disk_layout` parameter (JSON). OHK-ea8opsps matches each row to an uninitialized disk, trying in turn:
1. **Serial number** equal to the VMDK UUID (vCenter; needs `disk.EnableUUID = TRUE` on the VM or template).
2. **LUN** equal to the Azure data disk LUN.
3. **SCSI target ID** equal to the vCenter unit number.
4. **Attach order**, taking the next RAW disk in SCSI address order.

Every match must also agree on size. If any row cannot be matched, or a requested letter is held by another volume, the script stops before touching a disk. A CD-ROM on a requested letter is moved to a free letter. The job log ends with a table of row → hypervisor address → guest disk → match method.

## Prerequisites
- A resource handler that can add disks after provisioning. The attach address is captured on vCenter and Azure; elsewhere matching falls back to attach order and size. Azure VM sizes with NVMe disks (v6 and later) report no SCSI address, so they also fall back to attach order.
- A Windows Server 2025 OS build CloudBolt can run remote scripts on with an administrator account.

## Setup
1. After sync, re-point the server tier's OS build to your Windows 2025 build (the exported href `OSB-35y5mshd` is instance-specific) and restrict its environments as needed.
2. Enter run-as credentials on OHK-ea8opsps; the exported `credentials` value is the placeholder `YOUR_CREDENTIALS`.
3. Optional: set `$AllocationUnitSize` (for example 65536 for SQL Server) at the top of `plugins/OHK-ea8opsps/OHK-ea8opsps_script.ps1`.

## Notes
- Drive letters D to Z are offered; letters must be unique. Azure VM sizes with a temporary disk already use D:. Sizes are whole GB from 1 to 16384. Labels are up to 32 characters.
- Re-running the build is safe: attached rows are not added again, and letters that already carry the requested label are reported as already configured.
- On vCenter, disks go on the VM's `vmware_datastore` (or the OS disk's datastore) with the server's `vmware_disk_type` (thin by default), on the first SCSI controller with a free unit.
- No teardown items; deleting the resource deletes the VM and its disks.
