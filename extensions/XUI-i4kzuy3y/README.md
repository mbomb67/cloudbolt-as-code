# OpenSCAP (XUI)

Adds an **OpenSCAP** tab to Linux servers, and to any server labelled `OpenScap`. The tab runs an OpenSCAP scan on the server as a CloudBolt job, shows the latest score, the failed rules by severity and the scan history, and opens the stored reports. A **Check server** button reports the installed `oscap` version and the SCAP Security Guide datastreams and profiles the server offers, so a user can pick a profile.

Scans run `oscap xccdf eval` as root on the server through `Server.execute_script`, with the server's own credentials or its resource handler's agent channel. The server uploads its result files itself, so they never pass through the CloudBolt database.

## How results are stored

Each scan produces `report.html`, `results.xml.gz` and `arf.xml.gz`. A small index per server (`/var/opt/cloudbolt/proserv/openscap/servers/<server id>.json`) keeps the summary: score, counts, failed rules, and where the files are.

- **S3 bucket**: the server uploads to presigned PUT URLs; viewing redirects to a presigned GET URL that expires after the link lifetime (default 15 minutes). Keys are `<prefix>/<hostname>-<server id>/<scan id>/<file>`. Nothing is deleted: use a bucket lifecycle rule.
- **CloudBolt appliance** (default): files are kept under `/var/opt/cloudbolt/proserv/openscap/reports/<server id>/<scan id>/` keeping the newest 10 scans per server. When the backend would pass its space limit (5 GB by default), the oldest scans of any server are removed first. The server uploads each file once with HTTP PUT to a URL on CloudBolt, with a signed, expiring token in an `X-Oscap-Token` header, not in the URL; viewing needs a CloudBolt login and the view permission on the server. The report is served in a sandbox so its script cannot reach the CloudBolt session.

## Prerequisites
- CloudBolt 8.6 or later.
- A Linux server in a resource handler whose script execution works (the same channel Remote Scripts use), powered on, with `curl` and `gzip`.
- `openscap-scanner` and the SCAP Security Guide on the server (`scap-security-guide`; `ssg-debderived` on Debian and Ubuntu), or turn on **Allow install** in settings and tick Install when running.
- To scan: the `server.all_actions` permission on the server (the permission to run any server action), or CB Admin. To view: the view permission on the server.
- For S3: an AWS resource handler whose credentials allow `s3:PutObject` and `s3:GetObject` on the bucket's prefix, and `s3:ListBucket` if you leave the region empty. The server needs outbound HTTPS to S3.
- For appliance storage: the default portal must have a domain (Admin > Portals): the upload URL is built from it and never from a request's host. Without it a scan still runs but its files are not stored. The server must reach CloudBolt at that domain over HTTPS. If the certificate is not trusted by the server, turn on **Skip certificate check** in settings.

## Setup
1. Sync the repository, then restart the web service (`httpd`) so the new routes load, and confirm the extension is enabled under Admin > Extensions.
2. Open Admin > Custom Extensions > OpenSCAP Settings and choose where results are stored. For S3, pick the AWS handler and enter the bucket (and region, optionally).
3. Open a Linux server's OpenSCAP tab and click **Check server**.

## Notes
- Settings: default profiles tried in order when none is chosen (`cis_server_l1,cis,stig,standard`), scan timeout (30 minutes), link lifetime, scans kept per server (1-100) and the total space the appliance backend may use in GB (`local_max_gb`, 1-100), whether Install is allowed, and whether the tab shows on Linux servers or only on servers labelled `OpenScap`.
- The tab does not contact the server when it opens; it shows the last check and scan. One scan or check runs per server at a time.
- The upload links are bearer secrets held in the job's context and blanked when the job ends. A job cannot be rerun after that; start a new scan.
- Limitations: Linux only; datastreams come from the SCAP Security Guide on the server; remote content is never fetched; `oscap` output per rule is not kept in the job log.
- The previous version stored reports in `MEDIA_ROOT/openscap_reports` on the appliance, readable by anyone who could reach that URL. This version does not use that folder; delete it by hand.

## Not yet tested
Nothing here has run on a live appliance. Untested: the S3 backend and its presigned URLs (including the private botocore accessor used to sign with Signature Version 4); the local upload route behind `httpd`; running as a job on each handler type (SSH, SSM, Azure Run Command, vCenter tools); installs on dnf, yum, zypper and apt; datastream ranking on Rocky, Alma, Oracle, SLES, Ubuntu and Debian; and the permission check for non-admin roles. The summary parser is covered by local tests with sample XCCDF results only.
