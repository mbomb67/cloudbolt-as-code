# Windows Server with Data Disks

Deploys one Windows Server 2025 VM with the data disks the requester lists on the order form (drive letter, size, volume name, as many rows as needed). CloudBolt adds each disk to the VM in row order and records how the guest can recognize it (serial number, SCSI target, LUN) through a provider for the server's resource handler; the in-guest script then finds each disk by that identity, not by guess, and initializes it GPT, assigns the drive letter, and formats it NTFS with the volume name as its label.

## Contents
| Role | ID | Name |
|---|---|---|
| Custom form | FRM-jqnr84d0 | Environment page plus a Data Disks table (`plugin-bdi-xwp2jz95.data_disks`) |
| Server tier (seq 1) | — | Windows Server (OS build "Windows 2025"; hostname `wdd-<grp>-w-00X`; all environments enabled) |
| Build (Plug-in, seq 2) | OHK-1m6n3gq9 | Attach Data Disks (Windows) |
| Shared module | SHM-iobsa4dm | Data Disk Mapping (per-handler providers) |
| Build (Remote Script, seq 3) | OHK-ea8opsps | Configure Data Disks (Windows) |

## How a disk is matched in the guest
OHK-ea8opsps matches each row to an uninitialized disk, trying in turn:
1. **Serial number**: the disk's serial number or unique ID contains the recorded serial.
2. **SCSI address**: the disk's SCSI target and LUN equal the recorded ones.
3. **Attach order**: the next RAW disk in SCSI address order.

Every match must also agree on size. If any row cannot be matched, or a requested letter is held by another volume, the script stops before touching a disk. A CD-ROM on a requested letter is moved to a free letter. The job log ends with a table of row → hypervisor address → guest disk → match method.

## Resource handlers
The guest scripts never know the hypervisor. The `data_disk_mapping` shared module (SHM-iobsa4dm) adds each disk through the server's resource handler and asks that handler's provider what the guest can use to find it, saved on the server's `data_disk_layout` parameter as neutral keys: `serial`, `scsi_target`, `scsi_lun`, and a display `address`.

| Handler | What the provider records | Status |
|---|---|---|
| vCenter (`vmware`) | VMDK UUID as serial (needs `disk.EnableUUID = TRUE`), unit number as SCSI target, LUN 0 | Mocked only |
| Azure (`azure_arm`) | Data disk LUN; refuses an order the VM size cannot hold before adding any disk | Tested on a lab appliance (Windows Server 2022, Oracle Linux 8) |
| Nutanix AHV (`acropolis`) | Disk UUID as serial (the AHV guest serial embeds the vmdisk UUID), device index as LUN on target 0 | Untested: verify on a cluster |
| Any other handler that can add disks | Nothing; the guest matches by attach order and size | — |

To support another handler, subclass `DiskProvider` in `shared_modules/SHM-iobsa4dm/SHM-iobsa4dm_script.py` and register it in `PROVIDERS` under the handler's `type_slug`.

## Prerequisites
- A resource handler that can add disks after provisioning (see the table above). Azure VM sizes with NVMe disks (v6 and later) report no SCSI address, so they fall back to attach order.
- A Windows Server 2025 OS build CloudBolt can run remote scripts on with an administrator account.

## Setup
1. After sync, re-point the server tier's OS build to your Windows 2025 build (the exported href `OSB-35y5mshd` is instance-specific) and restrict its environments as needed.
2. Enter run-as credentials on OHK-ea8opsps; the exported `credentials` value is the placeholder `YOUR_CREDENTIALS`. On Azure, turn on **Use Tech-Specific Script Execution** in the order form's environment panel instead: the script then runs through Azure Run Command and needs no credentials.
3. Optional: set `$AllocationUnitSize` (for example 65536 for SQL Server) at the top of `plugins/OHK-ea8opsps/OHK-ea8opsps_script.ps1`.

## Notes
- Drive letters D to Z are offered; letters must be unique. Azure VM sizes with a temporary disk already use D:. Sizes are whole GB from 1 to 16384. Labels are up to 32 characters.
- Re-running the build is safe: attached rows are not added again, and letters that already carry the requested label are reported as already configured.
- On vCenter, disks go on the VM's `vmware_datastore` (or the OS disk's datastore) with the server's `vmware_disk_type` (thin by default), on the first SCSI controller with a free unit.
- Each Azure VM size caps its data disks (Standard_B1s takes 2), so pick a size that holds the rows ordered.
- No teardown items; deleting the resource deletes the VM and its disks.
