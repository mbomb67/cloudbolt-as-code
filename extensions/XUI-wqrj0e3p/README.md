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

## Vulnerability search page

Admin > Custom Extensions > Vulnerability Search (CB Admins) searches every scanned server at once. The **Vulnerabilities** view lists each known vulnerability with its severity, whether it is on the CISA exploited list, how many servers it affects and whether a fix exists; search by CVE or advisory id, filter by severity or exploited only, and choose a row to see the servers it affects (group, environment, OS, package, installed and fixed versions, last match). The **Servers** view lists each scanned server with its critical, high and exploited counts. The open view and vulnerability are in the page address, so a link opens the same place. Both views are paged on the server. A search for a CVE id that finds nothing means no server with a stored software list is affected: the server is not affected, has not been scanned yet, or the vulnerability is not in the database yet. The server tab has an **All servers** link to this page for CB Admins. New vulnerabilities appear here and on the server tabs without rescanning servers only when the nightly re-match job has run, so run it by hand or turn its schedule on (see Scheduled jobs).

## Scheduled jobs

Two recurring jobs are created the first time the Vulnerability Settings page opens. Both are created **off**; the extension never changes a job's schedule or on/off state afterwards. Open a job from the Schedules card on the settings page to run it now or to turn its schedule on.

- **Vulnerability Nightly Re-match** (03:00 every day). Updates the vulnerability database, then matches every stored software list that was matched against an older database. Servers are checked a few at a time (setting below). It also removes the results, stored software lists and state of servers that were deleted, are historical or are in an Unassigned group.
- **Vulnerability Weekly Inventory** (01:00 every Sunday). Collects a fresh software list from every server that can be scanned without anyone present: Linux, powered on, in a group that is not Unassigned, with a working script channel and credentials. Servers that cannot be scanned are skipped and counted in the job output; a server whose stored list is under 20 hours old is skipped. It needs the default portal's site URL (Admin > Portals), like a scan from the tab.

## Scan after provisioning

Turn on **Scan new servers after they are built** on the settings page. The extension then adds an action to the post-provisioning hook point. After a successful build, each new server that can be scanned is scanned as its own job: the build never waits for it and never fails because of it. Turning the setting off switches the action off; it is never created while the setting is off.

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
- Scan new servers after they are built (off by default).
- Servers re-matched at once by the nightly job (1 to 4, default 2).
- Servers scanned at once by the weekly inventory (1 to 10, default 4).

An Unassigned group is never scanned or re-matched by any job. The nightly job removes the stored results of deleted servers and of servers moved to an Unassigned group; with that job off they stay in the results until it is run by hand.

## Not in this version

- A roll-up tab on resources.
- Windows servers.
- Storing software lists in S3.
- A lookup of whether a CVE is in the vulnerability database itself (the Vulnerability search page only shows what affects scanned servers).

## Tested

Tested on CloudBolt 2026.3 with AWS servers over SSH: scanner setup, scans of Amazon Linux 2023 and Ubuntu 22.04, a repeat scan, the tab's search, filters and paging, the certificate failure message, and the refusal for a server in an Unassigned group.

Also tested there: the upgrade of an existing findings store, the Vulnerability search page (lists, one vulnerability's servers, search by CVE and by advisory), both scheduled jobs run by hand (cleanup of a deleted server, a weekly inventory limited to labelled servers that scanned one server and skipped two scanned recently), and the scan after provisioning starting as its own job while the build finished.

Not tested: Red Hat Enterprise Linux, Oracle Linux, arm64 servers, OpenShift virtual machines, a user who is not a CB Admin, a database update after the first one, the nightly job re-matching a server against a newer database, either job running on its schedule, and more than a few servers at once.

`extensions/XUI-wqrj0e3p/tests` holds unit tests that run without CloudBolt: `python -B -m unittest discover -s extensions/XUI-wqrj0e3p/tests -p "test_*.py"`.
