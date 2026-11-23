# Install SCC and Run First Scan (Windows)

Post-Provision orchestration action (run_seq 900) that installs DISA SCC on each new Windows server of a provisioning job and starts a first scan. Provisioning does not wait: the scan runs as a sub-job of the Provision Server job, and the report is on the server's Compliance tab when that sub-job ends. Ships enabled.

It never fails or warns provisioning. Every problem is written to the provisioning job's progress messages, and the scan sub-job, "Compliance scan: <hostname>", ends as Failure when the scan fails; that does not change the provisioning job or the order. It runs after the other Post-Provision actions in this repository (the highest other run_seq is 100, the AD join).

Prerequisites are in the [Compliance extension](../../extensions/XUI-i4kzuy3y/README.md).

## Contents
| Role | ID | Name |
|---|---|---|
| Plugin | OHK-kwx1fvho | Install SCC and Run First Scan (Windows), OS family Windows |
| Extension | XUI-i4kzuy3y | Compliance |

## Notes
- A server is scanned when the extension's "Show the tab on" setting gives it the Compliance tab. Historical servers, Linux servers and servers without a VM are skipped. Linux servers are handled by the [Install OpenSCAP and Run First Scan (Linux)](../HPA-b14wu721/) action.
- SCC is downloaded from DISA once per server (about 108 MB) and later scans reuse it. The profile is the Profile input, or the Windows default profile of the settings page (`MAC-1_Classified` by default) when it is empty.
- A scan of Windows Server 2025 (t3.large) took about 17 minutes, during which the server is already handed over.
- The scan job is a sub-job, so the default Jobs list (top-level jobs) does not show it; open it from the Provision Server job or the order. Cancelling the provisioning job also cancels a scan that is still running.
- Anything that reboots or powers off the server while the scan runs ends the scan as failed, which is why the action runs last. A blueprint step that runs after the server is built can still do that.
- While the scan runs, the Compliance tab refuses a second scan of the same server.
- The length of a scan is limited by the extension's scan timeout setting (30 minutes by default).
- A server that is not powered on is not scanned.

## Tested
The scan itself was tested with an earlier version of this action that waited for it: on CloudBolt 2026.3.131, AWS, one Windows Server 2025 server (t3.large) had SCC 5.15 downloaded and extracted in about a minute, and the `MAC-1_Classified` scan took about 17 minutes (42.5%) with the result files stored. Starting the scan as a sub-job without waiting has not been tested on an appliance yet.

Not tested: other Windows Server versions, a job with more than one server, a server joined to a domain by the AD join action, and a Linux server being skipped.
