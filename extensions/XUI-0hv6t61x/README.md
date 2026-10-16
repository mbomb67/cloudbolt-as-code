# File Archives (XUI)

Adds **Admin > Custom Extensions > File Archives**, where CB Admins keep versioned files (installers, tarballs, config bundles) on CloudBolt for scripts that run on servers. A Remote Script names the files it needs in header lines; when Run Task runs it, each server downloads them through a short-lived signed link before the script body starts.

## Contents
| Role | ID | Name |
|---|---|---|
| UI extension | XUI-0hv6t61x | File Archives (admin page and download link) |
| Shared module | SHM-v8llc736 | file_archives (store, links, download step) |
| Remote Script | OHK-hgv6wp65 | Run Installer from File Archive (Linux) |
| Remote Script | OHK-ow68kerl | Run Installer from File Archive (Windows) |

Scripts run through Run Task (`server_actions/SVA-4sin3s0q`), whose `server_tasks` shared module calls `file_archives` for any script that declares files.

## Setup
1. Sync `extensions/XUI-0hv6t61x`, the Run Task server action (it brings `server_tasks` and `file_archives`), and the two Remote Scripts.
2. Restart the web server (`systemctl restart httpd`): the extension adds URL paths, and the web process keeps shared modules it already loaded.
3. Open the page. If it says the download base URL is not set, open **Settings** and enter the HTTPS address servers use to reach CloudBolt.

## Files and versions
- An archive is a named folder; a file in it is referenced as `archive/file`. Names use letters, digits, dots, dashes and underscores.
- Every upload is a new version. A reference without a version, or with `@latest`, gets the newest; `@3` pins version 3. **Restore** copies an old version to a new latest one.
- Settings keep the newest N versions per file (10 by default) and delete older ones on upload.
- Files are stored under `/var/opt/cloudbolt/proserv/file_archives`, outside the web root, so they are shared between the nodes of an HA install and are reachable only through a signed link or the admin page.

## Using a file in a script
Add a header line to a Remote Script for each file:
```sh
# cb-archive: installers/app-7.4.tar.gz extract
# cb-archive: configs/app.conf@4
```
Before the body runs, a small step at the top of the script downloads each file into a new directory, checks its SHA-256 against the stored value, and with `extract` unpacks a zip or tar file. The directory is the current directory and is in `CB_ARCHIVE_DIR` (`$env:CB_ARCHIVE_DIR` on Windows). A failed download or checksum prints an ERROR line and exits 1 before the body starts. Directories older than a day are removed on the next run.

- A header can use an input, e.g. `# cb-archive: {{ archive_file }} extract` (give the input a pattern; see the Run Installer scripts).
- `cb-archive-link:archive/file` anywhere in a script is replaced with a link, for a script that downloads the file itself.
- A PowerShell script that declares files cannot start with `param()`, `[CmdletBinding()]` or `using`.
- Run Task adds `CB_OUTPUT archive_<file>_version=<n>` to its output for each file (dots and dashes in the name become underscores), so the job's outputs record the version used.
- Headers only work through Run Task. A Remote Script run any other way treats them as comments.

## Links
- A link is `<base URL>/xui/file_archives/dl/<token>/<file>` and needs no CloudBolt login. The token is an HMAC-SHA256 signature over the file, version, server, job and expiry, made with a random key in the store (not CloudBolt's `SECRET_KEY`, which is the same on every install).
- Run Task issues links once per run. They live for the configured minutes (15 by default) plus, for each attempt, the script's timeout and five minutes for a busy Azure VM, at most an hour.
- A link serves only the version it was issued for, so a run never mixes versions. Expired, tampered, revoked or mismatched links return 404.
- **Revoke all links** replaces the signing key.
- Servers do not verify CloudBolt's TLS certificate unless **Settings** says so; the SHA-256 check still rejects a changed file. Turn verification on when servers trust CloudBolt's certificate.

## Activity
The page lists links issued, downloads, refusals, uploads, restores and deletions per file. Downloads also appear in the server's history. The log is stored in the parameter `file_archives_activity` (namespace `file_archives`), one value per file, newest 100 events each.
