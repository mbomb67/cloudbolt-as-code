# Configure Data Disks from Disk Map (Oracle Linux)

Post-Provision orchestration action (run_seq 51) that, for each Oracle Linux server with a `disk_map`, runs Bash in the guest with sudo to match every disk recorded on `data_disk_layout`, make it an LVM volume (`vg_<volume name>`, `lv_<volume name>`), format it XFS, and mount it by UUID from `/etc/fstab`. Servers without a `disk_map` are skipped without contacting the guest. Ships enabled.

Runs after Attach Data Disks from Disk Map (HPA-n67skws4). The `disk_map` format and setup are in [blueprints/BP-g9zwqjoa](../../blueprints/BP-g9zwqjoa/README.md).

## Contents
| Role | ID | Name |
|---|---|---|
| Plugin | OHK-9h9vphf5 | Configure Data Disks from Disk Map (Oracle Linux), OS family Oracle Enterprise Linux |
| Shared module | SHM-8qcvbri7 | `disk_map` |

## Notes
- The Bash is the `SCRIPT` constant in OHK-9h9vphf5; mount options and the wait for hot-added disks are set at its top. It must contain no template tags.
- It uses only LVM, XFS and util-linux tools, so other RHEL-family OS families can be added to the plug-in's OS filter.
- Runs with the server's credentials and sudo over SSH, or as root through VMware Tools or Azure Run Command with Tech-Specific Script Execution.
- A disk is matched by serial number, then SCSI address, then attach order, and must agree on size. `/etc/fstab` is backed up once to `/etc/fstab.cloudbolt-data-disks.bak`.
