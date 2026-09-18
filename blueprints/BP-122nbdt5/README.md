# IIS Web Application

Deploys one Windows Server 2025 VM, installs the IIS role, creates a dedicated application pool and site on the requested port, publishes a landing page with a `/health.txt` endpoint, and stores the resulting URL on the Web Application resource.

## Contents
| Role | ID | Name |
|---|---|---|
| Server tier | — | Windows (OS build "Windows 2025"; hostname `web-<grp>-w-00X`) |
| Build (Remote Script, seq 2) | OHK-0ql0870k | Install IIS Windows |
| Build (plugin, seq 3) | OHK-1rel6s64 | Set URL Parameter |
| Parameter options hook | HPA-qb0w86mi | Generate options for 'Expiration Date' |

Order-form parameters: App Pool Name, Site Name, Site Port (80 or 8080), Site Title, Expiration Date (optional).

## Prerequisites
- A Windows Server OS build reachable by CloudBolt remote execution with an elevated run-as account. The script refuses to run on a client OS.
- The `web_application` resource type and a `site_url` custom field on the instance. OHK-1rel6s64 writes `site_url` on the resource but does not create the field.
- The IIS role installs from the local component store; no outbound access is needed. It can take several minutes on a cold template (execution timeout 1200 s).

## Setup
1. After sync, re-point the server tier's OS build to your Windows 2025 build (the exported href `OSB-35y5mshd` is instance-specific) and enable the environments the tier may deploy into (`all_environments_enabled` is false).
2. Confirm the run-as credential on OHK-0ql0870k; the exported `credentials` value is the redacted placeholder `YOUR_CREDENTIALS`.
3. Optionally adjust defaults in `plugins/OHK-0ql0870k/OHK-0ql0870k_script.ps1`: `$InstallAspNet` (false), `$StopDefaultSite` (true), `$OpenFirewall` (true), `$SitePath`.

## Notes
- The install script is idempotent; a re-run updates the existing site and pool. It stops "Default Web Site" so it cannot shadow port 80 and opens the site port in Windows Firewall.
- OHK-1rel6s64 sets `site_url` to `http://<server IP>:<port>` using the resource field named in its Port Field Name input (`iis_site_port` here). The FQDN-based SITE_URL the script prints goes only to the job log.
- No teardown items; deleting the resource deletes the server and nothing else.
- Expiration Date defaults to 7 days out (HPA-qb0w86mi); enforcement is done by the Expire Servers recurring job (RJB-nsx4v2s1).
