# Oracle Linux Server with Data Disks

Deploys one Oracle Linux VM with the data disks the requester lists on the order form (mount point, size, volume name, as many rows as needed). CloudBolt adds each disk to the VM in row order and records how the guest can recognize it (serial number, SCSI target, LUN) through a provider for the server's resource handler; the in-guest script then finds each disk by that identity, not by device name, and turns it into an LVM volume with an XFS file system mounted at the requested path.

## Contents
| Role | ID | Name |
|---|---|---|
| Custom form | FRM-1lqjo1lk | Environment page plus a Data Disks table (`plugin-bdi-dvzbczv3.data_disks`) |
| Server tier (seq 1) | — | Oracle Linux Server (OS build "Oracle Linux 8"; hostname `ldd-<grp>-l-00X`; all environments enabled) |
| Build (Plug-in, seq 2) | OHK-sjr9f2hf | Attach Data Disks (Linux) |
| Shared module | SHM-iobsa4dm | Data Disk Mapping (per-handler providers) |
| Build (Remote Script, seq 3) | OHK-p962x9z6 | Configure Data Disks (Oracle Linux) |

## How a disk is matched in the guest
OHK-p962x9z6 matches each row to an unused disk (no partitions, no signature), trying in turn:
1. **Serial number**: the disk's SERIAL or WWN contains the recorded serial.
2. **SCSI address**: the T and L of the disk's `H:C:T:L` equal the recorded SCSI target and LUN.
3. **Attach order**: the next unused disk in `H:C:T:L` order.

Every match must also agree on size. If any row cannot be matched, or a mount point is in use or not empty, the script stops before touching a disk. Each disk becomes volume group `vg_<volume name>` with logical volume `lv_<volume name>`, formatted XFS with the volume name as its label, and mounted by UUID from `/etc/fstab` with `nofail` (a backup is kept at `/etc/fstab.cloudbolt-data-disks.bak`). The job log ends with a table of row → hypervisor address → guest device → match method.

## Resource handlers
The guest scripts never know the hypervisor. The `data_disk_mapping` shared module (SHM-iobsa4dm) adds each disk through the server's resource handler and asks that handler's provider what the guest can use to find it, saved on the server's `data_disk_layout` parameter as neutral keys: `serial`, `scsi_target`, `scsi_lun`, and a display `address`.

| Handler | What the provider records | Status |
|---|---|---|
| vCenter (`vmware`) | VMDK UUID as serial (needs `disk.EnableUUID = TRUE`), unit number as SCSI target, LUN 0 | Mocked only |
| Azure (`azure_arm`) | Data disk LUN | Live test in progress |
| Nutanix AHV (`acropolis`) | Disk UUID as serial (the AHV guest serial embeds the vmdisk UUID), device index as LUN on target 0 | Untested: verify on a cluster |
| Any other handler that can add disks | Nothing; the guest matches by attach order and size | — |

To support another handler, subclass `DiskProvider` in `shared_modules/SHM-iobsa4dm/SHM-iobsa4dm_script.py` and register it in `PROVIDERS` under the handler's `type_slug`.

## Prerequisites
- A resource handler that can add disks after provisioning (see the table above). Azure VM sizes with NVMe disks (v6 and later) report no SCSI address, so they fall back to attach order.
- An Oracle Linux 8 or 9 OS build CloudBolt can run remote scripts on as root, with `lvm2` and `xfsprogs` (both in the default install).

## Setup
1. After sync, re-point the server tier's OS build to your Oracle Linux build (the exported href `OSB-pd2spm2e` is instance-specific) and restrict its environments as needed.
2. Enter root run-as credentials on OHK-p962x9z6; the exported `credentials` value is the placeholder `YOUR_CREDENTIALS`. On Azure, turn on **Use Tech-Specific Script Execution** in the order form's environment panel instead: the script then runs through Azure Run Command and needs no credentials.

## Notes
- Mount points must be unique, absolute, not nested in each other, and not operating system directories such as `/var` or `/usr` (subdirectories like `/var/lib/pgsql` are allowed). Volume names are up to 12 letters, digits, or underscores (the XFS label limit). Sizes are whole GB from 1 to 16384.
- Re-running the build is safe: attached rows are not added again, and volumes already mounted at their mount point are reported as already configured.
- On vCenter, disks go on the VM's `vmware_datastore` (or the OS disk's datastore) with the server's `vmware_disk_type` (thin by default), on the first SCSI controller with a free unit.
- No teardown items; deleting the resource deletes the VM and its disks.
