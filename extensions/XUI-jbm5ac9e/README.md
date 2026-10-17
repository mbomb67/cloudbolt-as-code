# Runbooks (XUI)

Adds **Admin > Custom Extensions > Runbooks**, a CB Admin page with three tabs: **Workflows** for the [Run Workflow](../../server_actions/SVA-j7rbsh39/) action, **Actions** for the Remote Scripts and plug-ins that [Run Task](../../server_actions/SVA-4sin3s0q/) and workflow steps run, and **File Archives** for versioned files that scripts download.

## Setup
1. Sync `packages/PKG-v20qv308` (Runbooks), or sync Run Task and Run Workflow first: they bring the `runbook_tasks`, `runbook_workflows` and `file_archives` shared modules the page needs.
2. Restart the web server (`systemctl restart httpd`): the extension adds URL paths, and the web process keeps shared modules it already loaded.
3. On the File Archives tab, if the page says the download base URL is not set, open **Settings** and enter the HTTPS address VMs use to reach CloudBolt. VMs must resolve and reach that address themselves; where CloudBolt's DNS name does not resolve (another cloud network, for example), use a name or IP address they can reach. An IP address works while certificate verification is off.

## Workflows tab
A workflow is a name, filters, and an ordered list of steps, each an existing Remote Script, CloudBolt plug-in, webhook, email or copy-file action with its inputs set to a fixed value, asked for when the workflow runs, or taken from an earlier step's output.

The step picker offers only actions that can run on every VM the workflow's filters allow, and lists the others with the reason. Copy makes a disabled copy for review. The same operations are available as JSON under `/xui/runbooks/api/` (CB Admin, session auth with CSRF).

## Actions tab
CloudBolt's UI creates a Remote Script or plug-in only inside a server, resource or orchestration action, and deleting that action deletes the script too. The Actions tab creates them on their own:

- **New Remote Script** (Windows or Linux) builds the script from Runbook Task Template (Windows) or (Linux) on the appliance: the template's helpers and error handling, one input line per declared input, and your task body. Run Task then offers it on every VM its OS families, groups and environments allow.
- **New plug-in** builds a CloudBolt Plug-in a workflow step can run on a VM.
- Inputs take a type, required, options and a regex pattern, saved as CloudBolt's regex constraint (matched from the start, on string and multi-line text inputs). Password inputs are not offered, because CloudBolt logs Run Task's and Run Workflow's inputs in plain text.

The list shows each Remote Script (CloudBolt's own included), plug-in, webhook, email and copy-file action with its filters, whether Run Task offers it, and what uses it (workflows, CloudBolt actions, blueprints and the rest of CloudBolt's dependents). **Check a VM** shows which actions can run on one VM. Edit and delete on CloudBolt's own action page, linked from each row. The same list and create calls are at `/xui/runbooks/api/base-actions/` (GET; POST with `"preview": true` returns the file without creating it).

## File Archives tab
CB Admins keep versioned files (installers, tarballs, config bundles) on CloudBolt. A Remote Script names the files it needs in header lines; when Run Task or a workflow step runs it, each VM downloads them through a short-lived signed link before the script body starts. The store, links and download step are in the `file_archives` shared module (`shared_modules/SHM-v8llc736`); [Run Installer from File Archive (Linux)](../../plugins/OHK-hgv6wp65/) and [(Windows)](../../plugins/OHK-ow68kerl/) are ready-made scripts.

### Files and versions
- An archive is a named folder; a file in it is referenced as `archive/file`. Names use letters, digits, dots, dashes and underscores.
- Every upload is a new version. A reference without a version, or with `@latest`, gets the newest; `@3` pins version 3. **Restore** copies an old version to a new latest one.
- Settings keep the newest N versions per file (10 by default) and delete older ones on upload.
- Uploads go through CloudBolt's own upload checks (antivirus scan and content-type allowlist, including any `CONTENT_TYPE_CHECK_CONF` setting). The upload dialog also allows archive and installer types that CloudBolt's default list leaves out (gzip, tar, bzip2, xz, 7z, rpm, deb, msi, exe, ELF binaries, jar, json, yaml, csv); other types are refused with their name. If CloudBolt's upload handler ever moves, the dialog falls back to CloudBolt's default checks rather than skipping them. Uploads over the limit in Settings (200 MB by default) are refused before they are stored.
- Files are stored under `/var/opt/cloudbolt/proserv/file_archives`, outside the web root, so they are shared between the nodes of an HA install and are reachable only through a signed link or the admin page.

### Using a file in a script
Add a header line to a Remote Script for each file:
```sh
# cb-archive: installers/app-7.4.tar.gz extract
# cb-archive: configs/app.conf@4
```
Before the body runs, a small step at the top of the script downloads each file into a new directory, checks its SHA-256 against the stored value, and with `extract` unpacks a zip or tar file. The directory is the current directory and is in `CB_ARCHIVE_DIR` (`$env:CB_ARCHIVE_DIR` on Windows). A failed download or checksum prints an ERROR line and exits 1 before the body starts. Directories older than a day are removed on the next run.

- A header can use an input, e.g. `# cb-archive: {{ archive_file }} extract` (give the input a regex constraint, `regex_constraint` in its metadata; see the Run Installer scripts).
- `cb-archive-link:archive/file` anywhere in a script is replaced with a link, for a script that downloads the file itself.
- A PowerShell script that declares files cannot start with `param()`, `[CmdletBinding()]` or `using`.
- Run Task adds `CB_OUTPUT archive_<file>_version=<n>` to its output for each file (dots and dashes in the name become underscores), so the job's outputs record the version used.
- Headers only work through Run Task and Run Workflow script steps. A Remote Script run any other way treats them as comments.

### Links
- A link is `<base URL>/xui/runbooks/file-archives/dl/<token>/<file>` and needs no CloudBolt login. The token is an HMAC-SHA256 signature over the file, version, VM, job and expiry, made with a random key in the store (not CloudBolt's `SECRET_KEY`, which is the same on every install).
- Links are issued once per run. They live for the configured minutes (15 by default) plus, for each attempt, the script's timeout and five minutes for a busy Azure VM, at most an hour.
- A link serves only the version it was issued for, so a run never mixes versions. Expired, tampered, revoked or mismatched links return 404.
- **Revoke all links** replaces the signing key.
- VMs do not verify CloudBolt's TLS certificate unless **Settings** says so; the SHA-256 check still rejects a changed file. Turn verification on when VMs trust CloudBolt's certificate.

### Activity
The tab lists links issued, downloads, refusals, uploads, restores and deletions per file. Downloads also appear in the VM's history. The log is stored in the parameter `file_archives_activity` (namespace `file_archives`), one value per file, newest 100 events each.
