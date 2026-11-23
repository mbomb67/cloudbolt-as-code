# Install OpenSCAP and Run First Scan (Linux)

Post-Provision orchestration action (run_seq 200) that installs OpenSCAP on each new Linux server of a provisioning job and runs a first scan, so a report is on the server's Compliance tab when provisioning completes. Ships enabled.

It never fails or warns provisioning. Every problem is written to the provisioning job's progress messages, and the scan itself is a separate job, "Compliance scan: <hostname>", that ends as Failure when it fails. Runs after the AD join actions (run_seq 100).

Prerequisites are in the [Compliance extension](../../extensions/XUI-i4kzuy3y/README.md).

## Contents
| Role | ID | Name |
|---|---|---|
| Plugin | OHK-ne7jd381 | Install OpenSCAP and Run First Scan (Linux), OS family Linux |
| Extension | XUI-i4kzuy3y | Compliance |

## Notes
- A server is scanned when the extension's "Show the tab on" setting gives it the Compliance tab. Historical servers, Windows servers and servers without a VM are skipped. Windows servers are handled by the [Install SCC and Run First Scan (Windows)](../HPA-ruxgbj67/) action.
- The scan picks the SCAP Security Guide datastream that matches the guest's `/etc/os-release`, then the first default profile from the settings page that the datastream offers (`cis_server_l1,cis,stig,standard` by default). The Profile input overrides this.
- The servers of one job are scanned one after another, and provisioning waits. Maximum wait (minutes) covers the whole action (30 by default): a scan still running then is cancelled and the servers left are skipped.
- A server that is not powered on is not scanned.

## Tested
On CloudBolt 2026.3.131, AWS, servers ordered from a blueprint:
- **Amazon Linux 2023:** OpenSCAP installed and scanned with the `standard` profile (46.4%), the three result files stored and the report link written to the provisioning job. The action added about 100 seconds. Run again after the extension was renamed to Compliance, with the same result in about 75 seconds.
- **Ubuntu 22.04:** the scan failed (Ubuntu's repositories carry no guide content). The scan job ended as Failure; the provisioning job, the deploy job and the order stayed Success with no error text. The action added about 15 seconds.

Not tested: other Linux distributions, a job with more than one server, the Maximum wait cut-off, and a Windows server being skipped.
