# Install SCC and Run First Scan (Windows)

Post-Provision orchestration action (run_seq 200) that installs DISA SCC on each new Windows server of a provisioning job and runs a first scan, so a report is on the server's Compliance tab when provisioning completes. Ships enabled.

It never fails or warns provisioning. Every problem is written to the provisioning job's progress messages, and the scan itself is a separate job, "Compliance scan: <hostname>", that ends as Failure when it fails. Runs after the AD join actions (run_seq 100).

Prerequisites are in the [Compliance extension](../../extensions/XUI-i4kzuy3y/README.md).

## Contents
| Role | ID | Name |
|---|---|---|
| Plugin | OHK-kwx1fvho | Install SCC and Run First Scan (Windows), OS family Windows |
| Extension | XUI-i4kzuy3y | Compliance |

## Notes
- A server is scanned when the extension's "Show the tab on" setting gives it the Compliance tab. Historical servers, Linux servers and servers without a VM are skipped. Linux servers are handled by the [Install OpenSCAP and Run First Scan (Linux)](../HPA-b14wu721/) action.
- SCC is downloaded from DISA once per server (about 108 MB) and later scans reuse it. The profile is the Profile input, or the Windows default profile of the settings page (`MAC-1_Classified` by default) when it is empty.
- A scan of Windows Server 2025 took about 13 minutes in the extension's own test, so this action adds roughly that long to every Windows provisioning job.
- The servers of one job are scanned one after another, and provisioning waits. Maximum wait (minutes) covers the whole action (45 by default): a scan still running then is cancelled and the servers left are skipped. One scan is also limited by the extension's scan timeout setting (30 minutes by default), which a longer Maximum wait does not extend.
- A server that is not powered on is not scanned.

## Tested
Not tested on an appliance yet.
