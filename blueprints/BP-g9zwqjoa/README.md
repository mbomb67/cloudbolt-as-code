# Server with Data Disks

Deploys one Windows Server or Oracle Linux VM with the data disks listed on the order form. The blueprint has no build items. The disk list is the server's `disk_map` parameter, an environment parameter that the order form shows as a table, and three Post-Provision orchestration actions add and prepare the disks for any server that has one, whichever blueprint or API order built it.

The single-OS blueprints BP-9ph7xcok and BP-rgdg9gho do the same work with build items and are unchanged. They do not set `disk_map`, so these actions skip their servers.

## Contents
| Role | ID | Name |
|---|---|---|
| Custom form | FRM-58d5rxg8 | Group, environment, and the environment's parameters |
| Form function | FJS-1h4v6wx2 | `dataDiskMapForm`: turns the environment's `disk_map` into a Data Disks table |
| Server tier (seq 1) | BDI-5501fspy | Windows or Oracle Linux, OS build picked on the form; hostname `dsk-<grp>-<w\|l>-00X` |
| Orchestration action (Post-Provision, run_seq 50) | HPA-n67skws4 | Attach Data Disks from Disk Map (plug-in OHK-jzkblrv7) |
| Orchestration action (Post-Provision, run_seq 51) | HPA-42uy9dyo | Configure Data Disks from Disk Map (Windows) (plug-in OHK-pdwb3xso, Windows only) |
| Orchestration action (Post-Provision, run_seq 51) | HPA-4ptcwgg4 | Configure Data Disks from Disk Map (Oracle Linux) (plug-in OHK-9h9vphf5, Oracle Enterprise Linux only) |
| Shared module | SHM-8qcvbri7 | `disk_map`: parses and validates the map, runs the guest scripts |
| Shared module | SHM-iobsa4dm | `data_disk_mapping`: adds disks through per-handler providers |
| Content package | PKG-zny1ynfs | All of the above |

## The disk_map parameter
A JSON list with one object per disk:

```json
[{"drive_letter": "E", "size_gb": 50, "volume_name": "Data"},
 {"mount_point": "/u01", "size_gb": 100, "volume_name": "u01"}]
```

| Key | Windows | Oracle Linux |
|---|---|---|
| `drive_letter` | Required, D to Z, unique | Ignored |
| `mount_point` | Ignored | Required, absolute path, not an OS directory, not nested in another disk's |
| `size_gb` | Whole GB, 1 to 16384 | Same |
| `volume_name` | NTFS label, up to 32 letters, digits, spaces, hyphens or underscores | Up to 12 letters, digits or underscores, starting with a letter; becomes `vg_<name>`, `lv_<name>` and the XFS label |

A row may carry both a drive letter and a mount point, so one map can serve either OS. The form writes `disk_map`, but an API order (`deploymentItems.server-bdi-5501fspy.parameters.disk_map`), a native order form, or another blueprint can set it too.

## How the order form builds the table
FRM-58d5rxg8 has no disk question of its own. When the requester picks an environment, CloudBolt fills the environment panel with that environment's parameters. If one is `disk_map`, `dataDiskMapForm` hides its text box, adds a Data Disks table under it, and writes the rows back to `disk_map` as JSON on every change.

- The columns follow the OS build picked in the same panel: Drive Letter for a build named like Windows, Mount Point for one named like Oracle Linux or another RHEL-family OS, both for any other name. End users cannot read OS families through the API, so the function goes by the build's name.
- The table checks its rows against the rules above before the order is submitted.
- A default value on the environment's `disk_map` seeds the table. A required `disk_map` makes the table require at least one disk.
- An environment without `disk_map` shows no table and orders no disks. A `disk_map` with options renders as a dropdown of preset maps, which the function leaves alone.

## Orchestration actions
1. **Attach Data Disks from Disk Map** validates each server's `disk_map` for its OS, adds one disk per entry in order through the resource handler, and saves the controller, SCSI target, LUN, and serial each disk got on `data_disk_layout`. An invalid map, or a VM size that cannot hold the disks, fails the job before the guest is touched.
2. **Configure Data Disks from Disk Map (Windows / Oracle Linux)** runs the in-guest script on servers of its OS family with the layout filled in. The script matches each entry to a disk by serial number, SCSI address, or attach order, checks its size, then formats it: GPT, drive letter and NTFS label on Windows; LVM, XFS and an fstab mount on Oracle Linux.

Servers without a `disk_map` are skipped without contacting the guest, so the actions ship enabled.

## Setup
1. Import PKG-zny1ynfs from the repository on the Content Packages admin page and Sync it. The package creates the `disk_map` parameter (`parameters/CF-q3bexgap`). Without the Content Package Manager, sync the blueprint and the three actions, then create the parameter yourself: Admin > Parameters > New Parameter, name `disk_map`, label `Data Disks`, type Multi-line Text, shown on servers.
2. Add `disk_map` to each environment that should offer data disks. Optionally give it a default map or make it required.
3. The server tier allows the Windows and Oracle Enterprise Linux families and pins no OS build, so the form offers the environment's builds of those families. Restrict environments as needed.
4. The guest scripts run with the server's credentials over WinRM or SSH (Linux with sudo), or through VMware Tools or Azure Run Command when the environment uses Tech-Specific Script Execution.

## Resource handlers
The same providers as BP-9ph7xcok: vCenter records the VMDK UUID and unit (needs `disk.EnableUUID = TRUE`), Azure the LUN and the VM size's disk limit, Nutanix AHV the vmdisk UUID and device index (untested). Other handlers that can add disks fall back to attach order and size. See `shared_modules/SHM-iobsa4dm`.

## Notes
- Tested 2026-10-16 on a lab appliance in Azure, with API orders and Tech-Specific Script Execution (Azure Run Command): Windows Server 2022 and Oracle Linux 8 each got two disks at LUN 0 and 1, matched by SCSI LUN, formatted (Windows) or mounted (Linux). Each order's map carried both a drive letter and a mount point on some rows, and each server used only its own. The order form's table and its OS-dependent columns were checked in the browser; a form submit was not exercised. vCenter and Nutanix were not run.
- Re-running the job is safe: attached disks are not added again and configured volumes are reported as already configured.
- Azure Run Command caps the Windows script's encoded command line, so keep the PowerShell in OHK-pdwb3xso under about 8,500 characters.
- No teardown items; deleting the resource deletes the VM and its disks.
