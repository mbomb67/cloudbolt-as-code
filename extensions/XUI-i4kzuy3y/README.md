# Openscap (XUI)

Adds an **OpenScap** server tab for Enterprise Linux servers. The tab shows the installed `oscap` version, lets a user pick an SCAP Security Guide profile and run `oscap xccdf eval` against the server, then copies the resulting HTML report back to CloudBolt and lists all reports for download.

## Prerequisites
- CloudBolt 8.6 or later.
- Target server: `openscap-scanner` and `scap-security-guide` installed (`yum install openscap-scanner scap-security-guide`). Confirmed on CentOS 7 and RHEL 7.
- Server record in CloudBolt must have working SSH credentials (username/password or key) with sudo rights; the extension runs scripts through `server.execute_script`.
- CloudBolt appliance needs `scp` and, for password-based SSH, `sshpass`, plus SSH access to the server.

## Setup
1. Sync the repository and confirm the extension is enabled under Admin > Extensions.
2. Add the tag `OpenScap` to each server that should show the tab.

## Notes
- Reports are stored under `MEDIA_ROOT/openscap_reports/` and served from `MEDIA_URL`; they are visible to anyone who can reach that URL.
- The evaluation runs synchronously inside the dialog request with a 120-second timeout per remote command; long profiles may time out.
- Profiles are discovered from `/usr/share/xml/scap/ssg/content/ssg-*-ds.xml` on the target server.
