# Vulnerabilities (XUI)

Adds a **Vulnerabilities** tab to Linux servers, and to any server labelled `Vulnerabilities`. The tab collects a list of the software installed on the server, matches it against a database of known vulnerabilities, and shows the findings: how many are critical, high, medium and low, how many are on the CISA known-exploited list, and for each package the installed version, the version that fixes it and the advisory. Users can switch between a Packages view (what to update) and a CVEs view (one row per CVE), search, filter by severity and show only exploited findings.

Nothing is changed on the server. The tab does not contact the server when it opens; it shows the last result.

## How it works

1. **Collect.** A CloudBolt job runs `vuln_scan.sh` on the server as root, through the same script channel Remote Scripts use. The script fetches the Syft inventory tool (from the appliance, or from github.com when the appliance cannot be reached), writes a software list and sends it back to the appliance.
   On the server the inventory tool runs from `/var/tmp`, or from `/root` or `/opt` when `/var/tmp` does not allow programs to run, and is removed after each run. By default the software list skips network and automounted file systems, container storage (`/var/lib/docker`, `/var/lib/containers`, `/var/lib/containerd`, `/var/lib/kubelet`, `/var/lib/rancher`) and `/snap`; more paths can be added in the settings. A software list larger than 100 MB compressed or 512 MB uncompressed is refused.
2. **Match.** On the appliance, Grype compares the software list with the vulnerability database. The scanner runs as the CloudBolt service account, not as root.
3. **Store.** The findings go into one SQLite file, `/var/opt/cloudbolt/proserv/vulnerabilities/findings.db`. The CloudBolt database is not touched.
4. **Show.** The tab reads one page at a time from that file, filtered on the server side.

## What it installs on the appliance

Under `/var/opt/cloudbolt/proserv/vulnerabilities/`, set up by an admin from the Vulnerability Settings page:

- the Grype scanner binary (v0.122.1) and the Syft tarballs the servers download (v1.57.2, amd64 and arm64), all checked against pinned checksums;
- the Grype vulnerability database, which unpacks to about 3.6 GB;
- the findings file and the stored software lists (about 6 MB per server).

Setup downloads about 300 MB, needs about 9 GB free while the database unpacks, and uses about 3.6 GB afterwards. It needs outbound access to github.com and grype.anchore.io.

## What the server needs

- A Linux server whose script execution works (the same channel Remote Scripts use), powered on.
- `curl`, `tar`, `gzip` and `sha256sum`.
- Outbound HTTPS access to the appliance, or to github.com (when **Let servers download the inventory tool from GitHub** is on). The upload URL comes from the default portal's site URL (Admin > Portals) and never from a request's host. The server must trust the appliance certificate. An appliance that serves its certificate without the issuing CA's intermediate works in a browser and still fails here (curl error 60): add the intermediate to the appliance's web server, install it on the servers, or turn on **Allow servers to skip the certificate check**.
- A group that is not an Unassigned group. Servers in an Unassigned group are never scanned.
- To scan: the `server.all_actions` permission on the server, or CB Admin. To view: the view permission on the server.

## Setup

1. Sync the repository, then restart the web service (`httpd`) so the new routes load, and confirm the extension is enabled under Admin > Extensions.
2. Open Admin > Custom Extensions > Vulnerability Settings and choose **Set up scanner**. The page shows the job's progress and the installed versions when it finishes.
3. Open a Linux server's Vulnerabilities tab and choose **Collect software list**.

## Settings

- Scan timeout (5 to 120 minutes, default 20).
- Whether servers may skip the certificate check when they talk to the appliance (off by default).
- Whether servers may download the inventory tool from GitHub when the appliance is unreachable (on by default).
- Extra folders the software list skips, one absolute path per line, at most 20. System, container and network folders are always skipped.
- Whether the tab shows on every Linux server or only on servers labelled `Vulnerabilities`.

## Not in this version

- Nightly re-matching of stored software lists against a newer database.
- An admin search page ("which servers have CVE-X").
- A roll-up tab on resources.
- Windows servers.
- Storing software lists in S3.

## Tested

Tested on CloudBolt 2026.3 with AWS servers over SSH: scanner setup, scans of Amazon Linux 2023 and Ubuntu 22.04, a repeat scan, the tab's search, filters and paging, and the certificate failure message.

Not tested: Red Hat Enterprise Linux, Oracle Linux, arm64 servers, OpenShift virtual machines, a user who is not a CB Admin, a database update after the first one, and the Unassigned group rule on a live server.
