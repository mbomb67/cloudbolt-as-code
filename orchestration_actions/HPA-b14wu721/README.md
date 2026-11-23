# Install OpenSCAP and Run First Scan (Linux)

Post-Provision orchestration action (run_seq 200) that installs OpenSCAP on each new Linux server of a provisioning job and runs a first scan, so a report is on the server's OpenSCAP tab when provisioning completes. Ships enabled.

It never fails or warns provisioning. Every problem is written to the provisioning job's progress messages, and the scan itself is a separate job, "OpenSCAP scan: <hostname>", that ends as Failure when it fails. Runs after the AD join actions (run_seq 100).

Prerequisites are in the [OpenSCAP extension](../../extensions/XUI-i4kzuy3y/README.md).

## Contents
| Role | ID | Name |
|---|---|---|
| Plugin | OHK-ne7jd381 | Install OpenSCAP and Run First Scan (Linux), OS family Linux |
| Extension | XUI-i4kzuy3y | OpenSCAP |

## Notes
- A server is scanned when the extension's "Show the tab on" setting gives it the OpenSCAP tab. Historical servers, Windows servers and servers without a VM are skipped.
- The scan picks the SCAP Security Guide datastream that matches the guest's `/etc/os-release`, then the first default profile from the settings page that the datastream offers (`cis_server_l1,cis,stig,standard` by default). The Profile input overrides this.
- The servers of one job are scanned one after another, and provisioning waits. Maximum wait (minutes) covers the whole action (30 by default): a scan still running then is cancelled and the servers left are skipped.
- A server that is not powered on is not scanned.
