# Configure Data Disks from Disk Map (Windows)

Post-Provision orchestration action (run_seq 51) that, for each Windows server with a `disk_map`, runs PowerShell in the guest to match every disk recorded on `data_disk_layout` and initialize it GPT with its drive letter and an NTFS label. Servers without a `disk_map` are skipped without contacting the guest. Ships enabled.

Runs after Attach Data Disks from Disk Map (HPA-n67skws4). The `disk_map` format and setup are in [blueprints/BP-g9zwqjoa](../../blueprints/BP-g9zwqjoa/README.md).

## Contents
| Role | ID | Name |
|---|---|---|
| Plugin | OHK-pdwb3xso | Configure Data Disks from Disk Map (Windows), OS family Windows |
| Shared module | SHM-8qcvbri7 | `disk_map` |

## Notes
- The PowerShell is the `SCRIPT` constant in OHK-pdwb3xso; set `$AllocationUnitSize` there (for example 65536 for SQL Server). It must contain no template tags, and Azure Run Command caps it at about 8,500 characters.
- Runs with the server's credentials (an administrator) over WinRM, or through VMware Tools or Azure Run Command with Tech-Specific Script Execution.
- A disk is matched by serial number, then SCSI address, then attach order, and must agree on size. A CD-ROM on a requested letter is moved to a free one.
