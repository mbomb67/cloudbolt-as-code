# Oracle Linux Server with Data Disks

Deploys one Oracle Linux VM with the data disks the requester lists on the order form (mount point, size, volume name, as many rows as needed). CloudBolt adds each disk to the VM in row order and records where the hypervisor attached it (the SCSI controller, unit number, and VMDK UUID on vCenter, the LUN on Azure); the in-guest script then finds each disk by that identity, not by device name, and turns it into an LVM volume with an XFS file system mounted at the requested path.

## Contents
| Role | ID | Name |
|---|---|---|
| Custom form | FRM-1lqjo1lk | Environment page plus a Data Disks table (`plugin-bdi-dvzbczv3.data_disks`) |
| Server tier (seq 1) | — | Oracle Linux Server (OS build "Oracle Linux 8"; hostname `ldd-<grp>-l-00X`; all environments enabled) |
| Build (Plug-in, seq 2) | OHK-sjr9f2hf | Attach Data Disks (Linux) |
| Build (Remote Script, seq 3) | OHK-p962x9z6 | Configure Data Disks (Oracle Linux) |

## How a disk is matched in the guest
OHK-sjr9f2hf saves the rows on the server's `data_disk_layout` parameter (JSON). OHK-p962x9z6 matches each row to an unused disk (no partitions, no signature), trying in turn:
1. **Serial number or WWN** equal to the VMDK UUID (vCenter; needs `disk.EnableUUID = TRUE` on the VM or template).
2. **LUN**, the L in the disk's `H:C:T:L`, equal to the Azure data disk LUN.
3. **SCSI target**, the T in the disk's `H:C:T:L`, equal to the vCenter unit number.
4. **Attach order**, taking the next unused disk in `H:C:T:L` order.

Every match must also agree on size. If any row cannot be matched, or a mount point is in use or not empty, the script stops before touching a disk. Each disk becomes volume group `vg_<volume name>` with logical volume `lv_<volume name>`, formatted XFS with the volume name as its label, and mounted by UUID from `/etc/fstab` with `nofail` (a backup is kept at `/etc/fstab.cloudbolt-data-disks.bak`). The job log ends with a table of row → hypervisor address → guest device → match method.

## Prerequisites
- A resource handler that can add disks after provisioning. The attach address is captured on vCenter and Azure; elsewhere matching falls back to attach order and size. Azure VM sizes with NVMe disks (v6 and later) report no SCSI address, so they also fall back to attach order.
- An Oracle Linux 8 or 9 OS build CloudBolt can run remote scripts on as root, with `lvm2` and `xfsprogs` (both in the default install).

## Setup
1. After sync, re-point the server tier's OS build to your Oracle Linux build (the exported href `OSB-pd2spm2e` is instance-specific) and restrict its environments as needed.
2. Enter root run-as credentials on OHK-p962x9z6; the exported `credentials` value is the placeholder `YOUR_CREDENTIALS`.

## Notes
- Mount points must be unique, absolute, not nested in each other, and not operating system directories such as `/var` or `/usr` (subdirectories like `/var/lib/pgsql` are allowed). Volume names are up to 12 letters, digits, or underscores (the XFS label limit). Sizes are whole GB from 1 to 16384.
- Re-running the build is safe: attached rows are not added again, and volumes already mounted at their mount point are reported as already configured.
- On vCenter, disks go on the VM's `vmware_datastore` (or the OS disk's datastore) with the server's `vmware_disk_type` (thin by default), on the first SCSI controller with a free unit.
- No teardown items; deleting the resource deletes the VM and its disks.
