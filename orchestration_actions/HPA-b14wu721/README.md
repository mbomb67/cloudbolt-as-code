# Install OpenSCAP and Run First Scan (Linux)

Post-Provision orchestration action (run_seq 900) that installs OpenSCAP on each new Linux server of a provisioning job and starts a first scan. Provisioning does not wait: the scan runs as a sub-job of the Provision Server job, and the report is on the server's Compliance tab when that sub-job ends. Ships enabled.

It never fails or warns provisioning. The only progress message it writes says that a scan started; a skipped server and every problem go to the provisioning job's log only. The scan sub-job, "Compliance scan: <hostname>", ends as Failure when the scan fails; that does not change the provisioning job or the order. It runs after the other Post-Provision actions in this repository (the highest other run_seq is 100, the AD join).

Prerequisites are in the [Compliance extension](../../extensions/XUI-i4kzuy3y/README.md).

## Contents
| Role | ID | Name |
|---|---|---|
| Plugin | OHK-ne7jd381 | Install OpenSCAP and Run First Scan (Linux), OS family Linux |
| Extension | XUI-i4kzuy3y | Compliance |

## Notes
- A server is scanned when the extension's "Show the tab on" setting gives it the Compliance tab. Historical servers, Windows servers and servers without a VM are skipped. Windows servers are handled by the [Install SCC and Run First Scan (Windows)](../HPA-ruxgbj67/) action.
- The scan picks the SCAP Security Guide datastream that matches the guest's `/etc/os-release`, then the first default profile from the settings page that the datastream offers (`cis_server_l1,cis,stig,standard` by default). The Profile input overrides this.
- The scan job is a sub-job, so the default Jobs list (top-level jobs) does not show it; open it from the Provision Server job or the order. Cancelling the provisioning job also cancels a scan that is still running.
- Anything that reboots or powers off the server while the scan runs ends the scan as failed, which is why the action runs last. A blueprint step that runs after the server is built can still do that.
- While the scan runs, the Compliance tab refuses a second scan of the same server.
- The length of a scan is limited by the extension's scan timeout setting (30 minutes by default).
- A server that is not powered on is not scanned.

## Tested
On CloudBolt 2026.3.131, AWS, servers ordered from a blueprint:
- **Amazon Linux 2023:** the action started the scan and ended in under a second. The provisioning job and the order ended Success while the scan sub-job was still running. The sub-job installed OpenSCAP, scanned with the `cis_server_l1` profile (89.4%), stored the result files and ended Success about 30 seconds later. The action's only progress message was the line that says the scan started.
- **Ubuntu 22.04**, with an earlier version of this action that waited for the scan: the scan failed (Ubuntu's repositories carry no guide content) and provisioning stayed Success.

Not tested: other Linux distributions, a job with more than one server, a Windows server being skipped, and a scan that fails after provisioning has ended.
