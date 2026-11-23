# Compliance (XUI)

Adds a **Compliance** tab to Linux and Windows servers, and to any server labelled `Compliance` (the older label `OpenScap` still works). The tab runs a compliance scan on the server as a CloudBolt job (OpenSCAP on Linux, DISA SCC on Windows), shows the latest score, the failed rules by severity and the scan history, and opens the stored reports. A **Check server** button reports the installed scanner version and the benchmarks and profiles the server offers, so a user can pick a profile.

On Linux, scans run `oscap xccdf eval` as root on the server through `Server.execute_script`, with the server's own credentials or its resource handler's agent channel. The server uploads its result files itself, so they never pass through the CloudBolt database.

## Windows servers

A Windows server is scanned with the DISA SCAP Compliance Checker (`cscc.exe`, SCC) against DISA STIG benchmarks, with the MAC profiles (default `MAC-1_Classified`). The tab shows SCC's benchmark where a Linux server shows its datastream.

- **Install:** with **Allow install** on and Install ticked, the server downloads the SCC bundle (about 108 MB) from DISA once, checks its SHA-256 and unpacks it to `C:\Program Files\CloudBolt\Compliance\scc\`. Later scans reuse it.
- **How a scan runs:** the script channel starts SCC as a scheduled task under SYSTEM and the job polls it until it ends, so a long scan does not hold one script call open.
- **Files:** `report.html`, `results.xml.gz` and `arf.xml.gz` are stored only when the server can reach the upload URL (the appliance, or a presigned S3 URL). There is no read-back, so an OpenShift Virtualization Windows VM that cannot reach the appliance keeps no files; the score and failed rules are still recorded.
- **Settings:** the Windows (SCC) group sets the default profile, the SCC version (default `5.15`), its SHA-256 and the download URL. Clear or replace the checksum when you change the version. DISA removes old SCC versions from its download site, so a pinned version can stop downloading. Version `latest` follows NIWC's release file, and that download is not checksum-verified.
- A resource with Linux and Windows servers scans each with its own engine. The profile list offers only profiles every server's last check found, so with both kinds it is usually each server's default.

## Resource tab

A resource that has at least one server that gets the server tab also gets a **Compliance** tab (the same servers and the same "Show the tab on" setting; historical servers are left out). It shows the latest completed result of every server in the resource: the average and lowest score, the failed rules (each rule counted once), the rules that fail on every server, a table of the servers with links to each server's report, and a rule-by-server grid. **Scan all servers** runs one scan job per server you choose, **Check servers** runs the readiness check on all powered-on servers. The profile list offers the profiles every powered-on server's last check found, or each server's default.

- **How a run works:** one parent job ("Compliance Resource Scan", created on first use) starts one ordinary Compliance Scan job per server, at most 5 at a time, and waits for them together. A server that is powered off, already busy, fails or runs past the scan timeout plus 5 minutes is recorded with its reason and does not stop the others. One run per resource at a time. The result is Success when every server completed, Warning when some did, Failure when none did.
- **No merged report:** each server scans itself and keeps its own report; the tab computes the roll-up from the servers' indexes. A resource run record (servers, scan ids, reasons, average score, failed rule count) is kept in `/var/opt/cloudbolt/proserv/openscap/resources/<resource id>.json`, newest 50, and feeds the History table and the "Up N points since" line (shown from the second run on).
- **Failed-rule lists are capped:** a server's index keeps up to 1000 failed rules, and only for its newest completed scan (older scans keep their counts). For a scan with more, or a script channel that returns partial output (Azure Run Command), the cross-server list and counts are a lower bound; the tab says so, and each report has the full list.
- **Permissions:** view on the resource, and the view permission on each server it lists. A server is offered for scanning only when you pass the same check as the server tab (`server.all_actions` or CB Admin); with none, the Scan all servers and Check servers buttons are hidden.

Other content can run a scan too: `from xui.io_cloudbolt_compliance import oscap_api`, then `oscap_api.scan_and_wait(server, job.owner, profile_id, install=True)` starts the same job from a plug-in, waits for it, and returns the summary and a link to the report. The [Run OpenSCAP Scan](../../plugins/OHK-oqqmfoah/) blueprint step uses it to scan new servers before an order completes.

## How results are stored

Each scan produces `report.html`, `results.xml.gz` and `arf.xml.gz`. A small index per server (`/var/opt/cloudbolt/proserv/openscap/servers/<server id>.json`) keeps the summary: score, counts, failed rules, and where the files are.

- **S3 bucket**: the server uploads to presigned PUT URLs; viewing redirects to a presigned GET URL that expires after the link lifetime (default 15 minutes). Keys are `<prefix>/<hostname>-<server id>/<scan id>/<file>`. Nothing is deleted: use a bucket lifecycle rule.
- **CloudBolt appliance** (default): files are kept under `/var/opt/cloudbolt/proserv/openscap/reports/<server id>/<scan id>/` keeping the newest 10 scans per server. When the backend would pass its space limit (5 GB by default), the oldest scans of any server are removed first. The server uploads each file once with HTTP PUT to a URL on CloudBolt, with a signed, expiring token in an `X-Oscap-Token` header, not in the URL; viewing needs a CloudBolt login and the view permission on the server. The report is served in a sandbox so its script cannot reach the CloudBolt session.

## Prerequisites
- CloudBolt 8.6 or later.
- A Linux server in a resource handler whose script execution works (the same channel Remote Scripts use), powered on, with `curl` and `gzip`.
- Or a Windows Server 2016 to 2025 server on the same kind of channel, with an administrator-capable script account, outbound HTTPS to DISA for the first scan (or to the upload URL), and about 400 MB free during install and 200 MB after.
- OpenShift Virtualization VMs are reached through the `kubevirt_api_tunnel` shared module (the pod network as the VM's first NIC, and an SSH key or password on the server record); the module comes with `server_actions/SVA-5vsp0tv6`. With appliance storage these VMs do not upload: the job reads the result files back over the same tunnel, so the VM needs no route to CloudBolt.
- `openscap-scanner` and the SCAP Security Guide on the server (`scap-security-guide`; `ssg-debderived` on Debian and Ubuntu), or turn on **Allow install** in settings and tick Install when running.
- To scan: the `server.all_actions` permission on the server (the permission to run any server action), or CB Admin. To view: the view permission on the server.
- For S3: an AWS resource handler whose credentials allow `s3:PutObject` and `s3:GetObject` on the bucket's prefix, and `s3:ListBucket` if you leave the region empty. The server needs outbound HTTPS to S3.
- For appliance storage, except on OpenShift Virtualization VMs: the default portal must have a domain (Admin > Portals): the upload URL is built from it and never from a request's host. Without it a scan still runs but its files are not stored. The server must reach CloudBolt at that domain over HTTPS. If the certificate is not trusted by the server, turn on **Skip certificate check** in settings.

## Setup
1. Sync the repository, then restart the web service (`httpd`) so the new routes load (the resource tab adds two), and confirm the extension is enabled under Admin > Extensions.
2. Open Admin > Custom Extensions > Compliance Settings and choose where results are stored. For S3, pick the AWS handler and enter the bucket (and region, optionally).
3. Open a server's Compliance tab and click **Check server**.

## Upgrading from the OpenSCAP extension

This extension was the package `openscap` before it was renamed `io_cloudbolt_compliance`. A sync does not rename an installed package: it writes the new folder next to the old one, and both would load. On an appliance that already has the old one:

1. Admin > Extensions: delete the OpenSCAP extension. This removes `/var/opt/cloudbolt/proserv/xui/openscap/`. Settings and scan history are kept; they live in `/var/opt/cloudbolt/proserv/openscap/`.
2. Sync the repository.
3. Restart `httpd`.

The jobs' actions are renamed on the first scan. The data folder keeps its `openscap` name so existing settings and scan history carry over.

## Notes
- Settings: default profiles tried in order when none is chosen (`cis_server_l1,cis,stig,standard`), scan timeout (30 minutes), link lifetime, scans kept per server (1-100) and the total space the appliance backend may use in GB (`local_max_gb`, 1-100), whether Install is allowed, and whether the tab shows on Linux and Windows servers or only on servers labelled `Compliance`. The Windows (SCC) group adds the Windows default profile, the SCC version, checksum and download URL.
- The tab does not contact the server when it opens; it shows the last check and scan. One scan or check runs per server at a time.
- The upload links are bearer secrets held in the job's context and blanked when the job ends. A job cannot be rerun after that; start a new scan.
- A scan started by content with `install=True` installs OpenSCAP when it is missing, whatever the **Allow install** setting says. That setting governs the tab's Install box.
- Limitations: on Linux, datastreams come from the SCAP Security Guide on the server and remote content is never fetched; Windows has DISA STIG content only, no CIS; DISA can remove a pinned SCC version from its site; scanner output per rule is not kept in the job log.
- The previous version stored reports in `MEDIA_ROOT/openscap_reports` on the appliance, readable by anyone who could reach that URL. This version does not use that folder; delete it by hand.

## Tested
On CloudBolt 2026.3.131:
- **AWS, Amazon Linux 2023, SSH, appliance storage:** Check server, install through `dnf`, scans with the `standard` profile (the only one its guide ships; about a minute), upload of the three files, and opening the report and downloads from the tab.
- **OpenShift Virtualization, RHEL 9.9, KubeVirt API tunnel:** check with install and a `cis_server_l1` scan (44 seconds). The guide offers 20 profiles there (CIS, STIG, PCI-DSS, HIPAA, ANSSI and others). The VM cannot reach the appliance, so the job read the three result files back over the tunnel and stored them on the appliance. Run from the 3-Tier Web App blueprint's scan steps (`oscap_api.scan_and_wait` with install): three VMs scanned in parallel, 48 seconds each including the install.
- **vCenter, Oracle Linux 8.10, SSH:** check and `stig` scan (52 seconds); the three files were stored on the appliance. With tech-specific script execution on, the lab appliance could not reach its ESXi host and the check timed out before the script ran.
- **Azure, Oracle Linux 8.10, Azure Run Command:** check with install (about three and a half minutes) and a `stig` scan (about two minutes). Run Command returns only part of the script output, so the tab listed the first 61 of 233 failed rules; the score and counts were complete. The VM could not resolve the appliance, so the files were not stored.
- **AWS, Ubuntu 22.04:** `oscap` installs, but Ubuntu's repositories carry no guide content, so there is nothing to scan against. Canonical ships its profiles with Ubuntu Pro (`usg`); that route is not tested.
- **AWS, Windows Server 2025, WinRM, appliance storage:** check with install of SCC 5.15 (87 seconds including the download), and a `MAC-1_Classified` scan of the Server 2025 STIG benchmark (about 13 minutes; 291 rules). The three files were stored on the appliance and the report opened from the tab.

The resource tab and its parent job are not yet tested on an appliance.

The rename to `io_cloudbolt_compliance` is not yet tested on an appliance.

Not yet tested on Windows: Server 2016, 2019 and 2022; the SSM, Azure Run Command and VMware Tools channels; version `latest`; and a Windows server in a resource scan.

Not yet tested: the S3 backend and its presigned URLs; the SSM and VMware Tools script channels; installs through `zypper`; RHEL (registered), Rocky, Alma, SLES and Debian; and scanning as a non-admin user.
