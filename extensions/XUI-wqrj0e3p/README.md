# Vulnerabilities (XUI)

Adds a **Vulnerabilities** tab to Linux and Windows servers, and to any server labelled `Vulnerabilities`. On Linux the tab collects a list of the software installed on the server, matches it against a database of known vulnerabilities, and shows the findings: how many are critical, high, medium and low, how many are on the CISA known-exploited list, and for each package the installed version, the version that fixes it and the advisory. Users can switch between a Packages view (what to update) and a CVEs view (one row per CVE), search, filter by severity and show only exploited findings or only those with a fix. The Known vulnerabilities card also says how many vulnerabilities have a fix; a vulnerability has one when any package it affects has a fixed version, and a Windows finding always has one.

Scanning changes nothing on the server (installing updates is a separate action, see [Install updates](#install-updates)). The tab does not contact the server when it opens; it shows the last result. Windows servers are covered in [Windows servers](#windows-servers).

## How it works

1. **Collect.** A CloudBolt job runs `vuln_scan.sh` on the server as root, through the same script channel Remote Scripts use. The script fetches the Syft inventory tool (from the appliance, or from github.com when the appliance cannot be reached), writes a software list and sends it back to the appliance.
   On the server the inventory tool runs from `/var/tmp`, or from `/root` or `/opt` when `/var/tmp` does not allow programs to run, and is removed after each run. By default the software list skips network and automounted file systems, container storage (`/var/lib/docker`, `/var/lib/containers`, `/var/lib/containerd`, `/var/lib/kubelet`, `/var/lib/rancher`) and `/snap`; more paths can be added in the settings. A software list larger than 100 MB compressed or 512 MB uncompressed is refused.
2. **Match.** On the appliance, Grype compares the software list with the vulnerability database. The scanner runs as the CloudBolt service account, not as root.
3. **Store.** The findings go into one SQLite file, `/var/opt/cloudbolt/proserv/vulnerabilities/findings.db`. The CloudBolt database is not touched.
4. **Show.** The tab reads one page at a time from that file, filtered on the server side.

## Windows servers

A Windows Server 2022 or 2025 server gets the same tab with a **Check for missing updates** button. The check reads the Windows build and the edition from the registry and the installed update ids from Get-HotFix and the servicing packages (Get-HotFix alone is known to miss some; at most 400 are kept, the newest first; when none can be read the tab says so), with a short PowerShell script that runs through the same script channel (WinRM, or the technology's own channel such as AWS SSM). Nothing is installed on the server, downloaded onto it or uploaded from it; the facts are stored on the appliance and matched there against Microsoft's published security updates, which an admin downloads from the settings page (Microsoft's Security Update Guide, plus the CISA known-exploited list). Each missing security update is one row in the Packages view, named by its KB number, with the build that fixes it; a vulnerability counts as exploited when CISA lists it or Microsoft marks it exploited.

- A server on the hotpatch track (any installed hotpatch update) is compared with the earliest build that fixes the CVE on that track; every other server with the earliest standard fix.
- Only the operating system is covered. Applications on Windows are not, and neither are Windows Server 2019 and older or Windows client versions: the check fails with a message saying so, and the weekly inventory, the resource scan and the scan after provisioning then skip that server as "a Windows version this extension does not cover" (the tab's button still tries; a later successful check lifts the skip).
- Needs: a powered-on server whose script channel and credentials work, in a group that is not Unassigned, and Microsoft security data on the appliance (outbound HTTPS to api.msrc.microsoft.com and www.cisa.gov, set up with the scanner). It needs neither Grype nor the portal's site URL.
- The nightly re-match, the weekly inventory, the resource tab and the scan after provisioning handle Windows servers the same way as Linux ones. The nightly job downloads the Microsoft data only when it has been set up here or a Windows server has been checked, so a Linux-only or offline appliance does not try it every night.

## What it installs on the appliance

Under `/var/opt/cloudbolt/proserv/vulnerabilities/`, set up by an admin from the Vulnerability Settings page:

- the Grype scanner binary (v0.122.1) and the Syft tarballs the servers download (v1.57.2, amd64 and arm64), all checked against pinned checksums;
- the Grype vulnerability database, which unpacks to about 3.6 GB;
- the findings file and the stored software lists (about 6 MB per server).

Setup downloads about 300 MB, needs about 9 GB free while the database unpacks, and uses about 3.6 GB afterwards. It needs outbound access to github.com and grype.anchore.io.

## What the server needs

- A Linux server (for Windows, see above) whose script execution works (the same channel Remote Scripts use), powered on.
- `curl`, `tar`, `gzip` and `sha256sum`.
- Outbound HTTPS access to the appliance, or to github.com (when **Let servers download the inventory tool from GitHub** is on). The upload URL comes from the default portal's site URL (Admin > Portals) and never from a request's host. The server must trust the appliance certificate. An appliance that serves its certificate without the issuing CA's intermediate works in a browser and still fails here (curl error 60): add the intermediate to the appliance's web server, install it on the servers, or turn on **Allow servers to skip the certificate check**.
- A group that is not an Unassigned group. Servers in an Unassigned group are never scanned.
- To scan: the `server.all_actions` permission on the server, or CB Admin. To view: the view permission on the server.

## Vulnerability search page

Admin > Custom Extensions > Vulnerability Search (CB Admins) searches every scanned server at once. The **Vulnerabilities** view lists each known vulnerability with its severity, whether it is on the CISA exploited list, how many servers it affects and whether a fix exists; search by CVE or advisory id, filter by severity, exploited only or with a fix, and choose a row to see the servers it affects (group, environment, OS, package, installed and fixed versions, last match). The **Servers** view lists each scanned server with its critical, high and exploited counts. The open view and vulnerability are in the page address, so a link opens the same place. Both views are paged on the server. A search for a CVE id that finds nothing means no server with a stored software list is affected: the server is not affected, has not been scanned yet, or the vulnerability is not in the database yet. The server tab has an **All servers** link to this page for CB Admins. New vulnerabilities appear here and on the server tabs without rescanning servers only when the nightly re-match job has run, so run it by hand or turn its schedule on (see Scheduled jobs).

## Resource tab

A resource whose servers include a Linux or Windows server (or, when the tab is limited to labelled servers, a labelled one) gets its own **Vulnerabilities** tab. It rolls up the known vulnerabilities of the resource's own servers: a card with how many of the servers you can see have a software list (for example 4 of 6), the totals, and the same two views as the Vulnerability search page. **Vulnerabilities** lists each one with its severity, whether it is on the CISA exploited list and how many of the resource's servers it affects; choose one to see which servers. **Servers** lists each server with its critical, high and exploited counts, or why it has none ("Not scanned yet", "Powered off", "In an Unassigned group", "Scanning"). Both views are paged on the server.

- The tab covers the servers of the resource itself. Servers of sub-resources are not included; they have their own tab.
- You see only the servers you may view. If you may view none of them, the tab says so.
- **Scan all servers** starts one job that collects a software list from every server of the resource that you may scan and that can be scanned without anyone present (Linux or Windows, powered on, not in an Unassigned group, with a working script channel and credentials). Others are skipped and counted in the job output. It needs the server.all_actions permission on a server, or CB Admin, and it never scans a server you cannot scan. The button shows only when at least one server qualifies. Servers are scanned a few at a time (the same setting as the weekly inventory). While it runs the tab shows how many servers are done and a link to the job, and refreshes itself every 5 seconds; only one such scan runs per resource at a time.
- The roll-up covers the first 500 servers of a resource. A larger resource shows the totals of those 500 and says so; the Servers view still lists every server you can see. For a resource with many servers the roll-up takes longer to load (about a second at 200 servers in testing).
- The totals come from stored software lists, so a new vulnerability appears without a rescan once the nightly re-match has run.

## Scheduled jobs

Two recurring jobs are created the first time the Vulnerability Settings page opens. Both are created **off**; the extension never changes a job's schedule or on/off state afterwards. Open a job from the Schedules card on the settings page to run it now or to turn its schedule on.

- **Vulnerability Nightly Re-match** (03:00 every day). Updates the vulnerability database and the Microsoft security data, then matches every stored software list or Windows build that was matched against older data. Servers are checked a few at a time (setting below). It also removes the results, stored software lists and state of servers that were deleted, are historical or are in an Unassigned group.
- **Vulnerability Weekly Inventory** (01:00 every Sunday). Collects a fresh software list or Windows build from every server that can be scanned without anyone present: Linux or Windows, powered on, in a group that is not Unassigned, with a working script channel and credentials. Servers that cannot be scanned are skipped and counted in the job output; a server whose stored list is under 20 hours old is skipped. A Linux server needs the default portal's site URL (Admin > Portals), like a scan from the tab.

## Scan after provisioning

Turn on **Scan new servers after they are built** on the settings page. The extension then adds an action to the post-provisioning hook point. After a successful build, each new server that can be scanned is scanned as its own job: the build never waits for it and never fails because of it. Turning the setting off switches the action off; it is never created while the setting is off.

## Install updates

Turn on **Install updates** on the settings page and the extension creates a server action, **Install Updates**, which installs every available update on one Linux or Windows server from the repositories or update source it is already configured with. The button of the same name then shows on the server's Vulnerabilities tab next to the scan button, and the action also appears in the server's Actions menu. Turning the setting off disables the action; its role grants and its Requires approval choice are kept. The action is offered on Linux servers and on Windows Server 2022 and 2025. A server in an Unassigned group is never offered it.

- **Who can run it.** CB Admins, and users whose role has `server.all_actions` on the server. For any other role, an admin adds **Install Updates** to the role's server actions. A new action is granted to no role.
- **Requires approval.** The action is created without it. An admin can switch it on in the action's settings; the run then waits for an approval like any other server action, and the setting is never changed again by the extension.
- **What it runs.** Red Hat Enterprise Linux and Oracle Linux 8 to 10: `dnf -y --refresh upgrade`. Amazon Linux 2023: `dnf -y --refresh upgrade --releasever=latest`. Ubuntu 22.04 and 24.04: `apt-get update`, then a non-interactive `apt-get -y dist-upgrade` that keeps the existing configuration files. Any other distribution is refused with a message and nothing is installed. The server needs its repositories reachable.
- **Restart.** The dialog has a **Restart if needed** option, on by default; a CloudBolt global preference decides whether it shows as a checkbox or as a True/False list. When an update needs a restart and the option is on, the server is restarted and the job waits for it to come back (up to the restart wait in the settings, 5 to 120 minutes, default 60); a server that is not back by then fails the job. With the option off the updates are installed, the panel says a restart is pending, and the scan afterwards still runs. The update itself is limited by the update timeout in the settings (10 to 240 minutes, default 90).
- **Windows.** Windows Server 2022 and 2025 only. The updates come from the server's configured source: WSUS when group policy sets one, otherwise Windows Update, so on WSUS only approved updates install. The work runs as a scheduled task under SYSTEM, so the credentials CloudBolt uses must be able to create one; a non-administrator WinRM credential cannot. A server with the Configuration Manager client is refused with a message and nothing is installed. A cumulative update finishes during the restart, so the server can take a long time to answer again; raise the restart wait if it fails. If more updates are offered afterwards, the job says how many; run the action again to install them.
- **Old kernels.** Only after a restart that was confirmed, old kernels are removed: on the Red Hat family `dnf -y remove --oldinstallonly`, which keeps only the newest kernel and the one running; on Ubuntu, which keeps the previous kernel as a boot fallback that its own cleanup never removes, the action removes the older kernel's packages by name, only when the server is running its newest kernel and nothing else would be removed. The setting **Remove old kernels after a restart** (on by default) turns this off; the scan then keeps listing the old kernel's vulnerabilities for as long as it stays installed.
- **What updating cannot fix.** Findings in software the package manager does not own, such as libraries bundled inside agents and tools (Go binaries, Python packages installed outside the distribution's packages). They stay listed with a fix version after the run.
- **Nothing to install.** A server whose image is already current reports "No updates were available".
- **After the update** the server is scanned again and the panel shows the vulnerabilities before and after, and a link to the job. If the scan fails the updates stay installed and the line shows the missing number as a dash.
- **Cancelling.** Cancelling the job does not stop the update on the server: a package transaction is never stopped halfway, so it runs to its end there. Run the action again to see the result.
- **After the dialog** CloudBolt returns to the server's default tab, not to Vulnerabilities, because the browser drops the tab from the address. Open the tab to follow the run; it refreshes every 5 seconds.
- **Not covered.** Windows versions other than 2022 and 2025, and any Linux distribution other than those above.

**Install updates on all servers of a resource.** The same setting also adds a resource action, **Install Updates on All Servers**, to every resource that has a Linux or Windows server. It installs the updates on the resource's servers one server at a time, in hostname order, and stops at the first server whose update fails; the servers after it are listed as not attempted. A server that needs a restart it was not allowed to do (restart-pending) is not a failure, and the run goes on. A server that cannot be updated now (historical, in an Unassigned group, unsupported, or one the person may not update) is skipped with its reason, and the skipped servers are listed in the job output. To run it a person needs permission to run the resource action on the resource (CB Admins, a role with `resource.all_actions`, or a role that has the action added) and, for every server, the same permission as for the server action; a server that fails the second check is skipped. Requires approval is set separately on the resource action in its settings and is never changed by the extension. A failed server ends the job as a failure and the remaining servers are not attempted. Servers the requester may not act on, or that are not eligible, are skipped and the run continues. A resource runs one install at a time.

## Setup

1. Sync the repository, then restart the web service (`httpd`) so the new routes load, and confirm the extension is enabled under Admin > Extensions.
2. Open Admin > Custom Extensions > Vulnerability Settings and choose **Set up scanner**. The page shows the job's progress and the installed versions when it finishes.
3. Open a Linux server's Vulnerabilities tab and choose **Collect software list**, or a Windows server's and choose **Check for missing updates**.

## Settings

- Scan timeout (5 to 120 minutes, default 20).
- Whether servers may skip the certificate check when they talk to the appliance (off by default).
- Whether servers may download the inventory tool from GitHub when the appliance is unreachable (on by default).
- Extra folders the software list skips, one absolute path per line, at most 20. System, container and network folders are always skipped.
- Whether the tab shows on every Linux and Windows server or only on servers labelled `Vulnerabilities`.
- Scan new servers after they are built (off by default).
- Vulnerability matches at once on the appliance, for scans and the nightly re-match (1 to 4, default 1). Each can use more than a gigabyte of memory; further scans wait for a free slot.
- Servers scanned at once by the weekly inventory and by Scan all servers on a resource (1 to 10, default 4).

An Unassigned group is never scanned or re-matched by any job. The nightly job removes the stored results of deleted servers and of servers moved to an Unassigned group; with that job off they stay in the results until it is run by hand.

## Not in this version

- Applications on Windows servers, and Windows Server 2019 and older.
- Storing software lists in S3.
- A lookup of whether a CVE is in the vulnerability database itself (the Vulnerability search page and the resource tab only show what affects scanned servers).

## Tested

Tested on CloudBolt 2026.3 with AWS servers over SSH: scanner setup, scans of Amazon Linux 2023 and Ubuntu 22.04, a repeat scan, the tab's search, filters and paging, the certificate failure message, and the refusal for a server in an Unassigned group.

Also tested there: the upgrade of an existing findings store, the Vulnerability search page (lists, one vulnerability's servers, search by CVE and by advisory), both scheduled jobs run by hand (cleanup of a deleted server, a weekly inventory limited to labelled servers that scanned one server and skipped two scanned recently), and the scan after provisioning starting as its own job while the build finished.

Also tested: a database update after the first one and the nightly job re-matching a stored software list against it; the resource tab on a three-server resource on OpenShift Virtualization (Red Hat Enterprise Linux 9 virtual machines) before and after Scan all servers, with one server whose build failed shown as not active and skipped; and two large matches queued one after the other by the matches-at-once limit.

Windows tested on the same appliance: the Microsoft data download, a check of a Windows Server 2025 on AWS through Systems Manager (standard track), the Windows tab, the search page with Linux and Windows servers together, and the nightly job with both data sources.

Install updates tested on the same appliance, each with a restart, old kernels removed and the scan run again: Ubuntu 22.04, Amazon Linux 2023 and Red Hat Enterprise Linux 9 (a pay-as-you-go AWS image, 9.4 to 9.9) over SSH, and Windows Server 2025 over WinRM. Also a second run with nothing to install, and a resource-wide run that stopped at its first failed server. Not tested for Install updates: a resource-wide run that completes across several servers, Requires approval, a role other than CB Admin, WSUS, Configuration Manager, Ubuntu 24.04, Systems Manager, Azure Run Command, and cancelling an update.

Not tested: Windows Server 2022, a server on the hotpatch track, Oracle Linux, arm64 servers, a user who is not a CB Admin, either job running on its schedule, and more than a few servers at once.

`extensions/XUI-wqrj0e3p/tests` holds unit tests that run without CloudBolt: `python -B -m unittest discover -s extensions/XUI-wqrj0e3p/tests -p "test_*.py"`.
