# Attach Data Disks from Disk Map

Post-Provision orchestration action (run_seq 50) that adds the data disks in each server's `disk_map` parameter through its resource handler and saves where each one landed (controller, SCSI target, LUN, serial) on `data_disk_layout`. Servers without a `disk_map` are skipped. Ships enabled.

The Configure Data Disks from Disk Map actions (HPA-42uy9dyo, HPA-4ptcwgg4) run next and prepare the disks in the guest. The `disk_map` format, the order form, and setup are in [blueprints/BP-g9zwqjoa](../../blueprints/BP-g9zwqjoa/README.md).

## Contents
| Role | ID | Name |
|---|---|---|
| Plugin | OHK-jzkblrv7 | Attach Data Disks from Disk Map |
| Shared module | SHM-8qcvbri7 | `disk_map` |
| Shared module | SHM-iobsa4dm | `data_disk_mapping` |

## Notes
- The map is validated for the server's OS (drive letters on Windows, mount points on Linux) before any disk is added. An invalid map fails the job.
- Disks are added through a provider per resource handler (vCenter, Azure, Nutanix AHV); other handlers that can add disks are matched in the guest by attach order. To control controller placement, override `add_disk` in that handler's provider in SHM-iobsa4dm.
- `continue_on_failure` is false, so a failure stops the provisioning job before the guest step.
