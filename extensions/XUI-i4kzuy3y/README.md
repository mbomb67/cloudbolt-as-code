# OpenSCAP (XUI)

Adds an **OpenSCAP** tab to Linux servers, and to any server labelled `OpenScap`. The tab runs an OpenSCAP scan on the server as a CloudBolt job, shows the latest score, the failed rules by severity and the scan history, and opens the stored reports. A **Check server** button reports the installed `oscap` version and the SCAP Security Guide datastreams and profiles the server offers, so a user can pick a profile.

Scans run `oscap xccdf eval` as root on the server through `Server.execute_script`, with the server's own credentials or its resource handler's agent channel. The server uploads its result files itself, so they never pass through the CloudBolt database.

Other content can run a scan too: `oscap_api.scan_and_wait(server, job.owner, profile_id, install=True)` starts the same job from a plug-in, waits for it, and returns the summary and a link to the report. The [Run OpenSCAP Scan](../../plugins/OHK-oqqmfoah/) blueprint step uses it to scan new servers before an order completes.

## How results are stored

Each scan produces `report.html`, `results.xml.gz` and `arf.xml.gz`. A small index per server (`/var/opt/cloudbolt/proserv/openscap/servers/<server id>.json`) keeps the summary: score, counts, failed rules, and where the files are.

- **S3 bucket**: the server uploads to presigned PUT URLs; viewing redirects to a presigned GET URL that expires after the link lifetime (default 15 minutes). Keys are `<prefix>/<hostname>-<server id>/<scan id>/<file>`. Nothing is deleted: use a bucket lifecycle rule.
- **CloudBolt appliance** (default): files are kept under `/var/opt/cloudbolt/proserv/openscap/reports/<server id>/<scan id>/` keeping the newest 10 scans per server. When the backend would pass its space limit (5 GB by default), the oldest scans of any server are removed first. The server uploads each file once with HTTP PUT to a URL on CloudBolt, with a signed, expiring token in an `X-Oscap-Token` header, not in the URL; viewing needs a CloudBolt login and the view permission on the server. The report is served in a sandbox so its script cannot reach the CloudBolt session.

## Prerequisites
- CloudBolt 8.6 or later.
- A Linux server in a resource handler whose script execution works (the same channel Remote Scripts use), powered on, with `curl` and `gzip`.
- OpenShift Virtualization VMs are reached through the `kubevirt_api_tunnel` shared module (the pod network as the VM's first NIC, and an SSH key or password on the server record); the module comes with `server_actions/SVA-5vsp0tv6`. With appliance storage these VMs do not upload: the job reads the result files back over the same tunnel, so the VM needs no route to CloudBolt.
- `openscap-scanner` and the SCAP Security Guide on the server (`scap-security-guide`; `ssg-debderived` on Debian and Ubuntu), or turn on **Allow install** in settings and tick Install when running.
- To scan: the `server.all_actions` permission on the server (the permission to run any server action), or CB Admin. To view: the view permission on the server.
- For S3: an AWS resource handler whose credentials allow `s3:PutObject` and `s3:GetObject` on the bucket's prefix, and `s3:ListBucket` if you leave the region empty. The server needs outbound HTTPS to S3.
- For appliance storage, except on OpenShift Virtualization VMs: the default portal must have a domain (Admin > Portals): the upload URL is built from it and never from a request's host. Without it a scan still runs but its files are not stored. The server must reach CloudBolt at that domain over HTTPS. If the certificate is not trusted by the server, turn on **Skip certificate check** in settings.

## Setup
1. Sync the repository, then restart the web service (`httpd`) so the new routes load, and confirm the extension is enabled under Admin > Extensions.
2. Open Admin > Custom Extensions > OpenSCAP Settings and choose where results are stored. For S3, pick the AWS handler and enter the bucket (and region, optionally).
3. Open a Linux server's OpenSCAP tab and click **Check server**.

## Notes
- Settings: default profiles tried in order when none is chosen (`cis_server_l1,cis,stig,standard`), scan timeout (30 minutes), link lifetime, scans kept per server (1-100) and the total space the appliance backend may use in GB (`local_max_gb`, 1-100), whether Install is allowed, and whether the tab shows on Linux servers or only on servers labelled `OpenScap`.
- The tab does not contact the server when it opens; it shows the last check and scan. One scan or check runs per server at a time.
- The upload links are bearer secrets held in the job's context and blanked when the job ends. A job cannot be rerun after that; start a new scan.
- A scan started by content with `install=True` installs OpenSCAP when it is missing, whatever the **Allow install** setting says. That setting governs the tab's Install box.
- Limitations: Linux only; datastreams come from the SCAP Security Guide on the server; remote content is never fetched; `oscap` output per rule is not kept in the job log.
- The previous version stored reports in `MEDIA_ROOT/openscap_reports` on the appliance, readable by anyone who could reach that URL. This version does not use that folder; delete it by hand.

## Tested
On CloudBolt 2026.3.131:
- **AWS, Amazon Linux 2023, SSH, appliance storage:** Check server, install through `dnf`, scans with the `standard` profile (the only one its guide ships; about a minute), upload of the three files, and opening the report and downloads from the tab.
- **OpenShift Virtualization, RHEL 9.9, KubeVirt API tunnel:** check with install and a `cis_server_l1` scan (44 seconds). The guide offers 20 profiles there (CIS, STIG, PCI-DSS, HIPAA, ANSSI and others). The VM could not reach the appliance, so the files were not stored: use S3 for servers that cannot reach CloudBolt.
- **vCenter, Oracle Linux 8.10, SSH:** check and `stig` scan (52 seconds); the three files were stored on the appliance. With tech-specific script execution on, the lab appliance could not reach its ESXi host and the check timed out before the script ran.
- **Azure, Oracle Linux 8.10, Azure Run Command:** check with install (about three and a half minutes) and a `stig` scan (about two minutes). Run Command returns only part of the script output, so the tab listed the first 61 of 233 failed rules; the score and counts were complete. The VM could not resolve the appliance, so the files were not stored.
- **AWS, Ubuntu 22.04:** `oscap` installs, but Ubuntu's repositories carry no guide content, so there is nothing to scan against. Canonical ships its profiles with Ubuntu Pro (`usg`); that route is not tested.

Not yet tested: the S3 backend and its presigned URLs; the SSM and VMware Tools script channels; installs through `zypper`; RHEL (registered), Rocky, Alma, SLES and Debian; and scanning as a non-admin user.
