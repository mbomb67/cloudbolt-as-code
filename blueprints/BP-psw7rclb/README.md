# Windows File Server

Deploys one Windows Server 2025 VM, initializes any raw data disk attached at order time, installs the File Server role and FSRM, creates the share folder with NTFS and share permissions, and publishes an encrypted, access-based-enumeration SMB share with a 100 GB hard quota.

## Contents
| Role | ID | Name |
|---|---|---|
| Server tier | — | Windows (OS build "Windows 2025"; hostname `wfs-<grp>-w-00X`; all environments enabled) |
| Build (Remote Script, seq 2) | OHK-2evtyysr | Initialize Windows Disk |
| Build (Remote Script, seq 3) | OHK-up7829pv | Install Windows File Server |
| Parameter options hook | HPA-qb0w86mi | Generate options for 'Expiration Date' |

Order-form parameters: File Share Name, File Share Path (e.g. `D:\Shares\ProjectData`), File Share Description, Expiration Date (optional).

## Prerequisites
- A Windows Server 2025 OS build (the script warns below build 26100 but continues) reachable by CloudBolt remote execution with an elevated run-as account.
- The drive letter in File Share Path must exist when OHK-up7829pv runs. OHK-2evtyysr formats every RAW disk as GPT/NTFS (label "Data") and lets Windows assign the next free letter, so add a data disk to the server tier on the order form for a `D:` path; with no extra disk, use a `C:` path.
- Any domain principals added to the permission arrays must resolve, so a domain join must run before OHK-up7829pv.
- The `file_server` resource type on the instance.

## Setup
1. After sync, re-point the server tier's OS build to your Windows 2025 build (the exported href `OSB-35y5mshd` is instance-specific).
2. Confirm the run-as credentials on OHK-2evtyysr and OHK-up7829pv; exported `credentials` values are the placeholder `YOUR_CREDENTIALS`.
3. Edit the policy block in `plugins/OHK-up7829pv/OHK-up7829pv_script.ps1` for your environment: `$FullAccessPrincipals` / `$ChangeAccessPrincipals` / `$ReadAccessPrincipals` (default grants only `BUILTIN\Administrators`), `$QuotaLimitGB` (100; 0 skips FSRM), `$RequireShareEncryption`, `$AccessBasedEnumeration`, `$RemoveEveryoneFromShare`, `$InstallDeduplication`.

## Notes
- Role install can take 10+ minutes; OHK-up7829pv has a 1200 s execution timeout, OHK-2evtyysr 180 s.
- Both scripts are idempotent. OHK-2evtyysr touches only disks with `PartitionStyle = RAW`; OHK-up7829pv updates an existing share of the same name but fails if it points at a different path.
- NTFS inheritance is broken on the share folder (SYSTEM and Administrators get Full Control) and the default `Everyone` share ACE is removed.
- SMB encryption is required on the share; Windows Server 2025 also enforces SMB signing, so legacy clients may need attention.
- No teardown items; deleting the resource deletes the server. Nothing is removed from AD or DNS.
