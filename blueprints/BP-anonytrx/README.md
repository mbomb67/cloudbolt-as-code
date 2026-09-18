# NGINX Web Application

Deploys one Oracle Linux 8 VM, installs nginx 1.24 from the OL8 AppStream module stream, writes a single-site configuration on the requested port with a landing page and `/health` endpoint, and stores the resulting URL on the Web Application resource.

## Contents
| Role | ID | Name |
|---|---|---|
| Server tier | — | Oracle Enterprise Linux (OS build "Oracle Linux 8"; hostname `web-<grp>-o-00X`) |
| Build (Remote Script, seq 2) | OHK-2vdm93hu | Install NGINX OEL8 |
| Build (plugin, seq 3) | OHK-1rel6s64 | Set URL Parameter |
| Parameter options hook | HPA-qb0w86mi | Generate options for 'Expiration Date' |

Order-form parameters: Site Name, Site Port (80 or 8080), Expiration Date (optional).

## Prerequisites
- An OL8 OS build with `dnf` access to the AppStream `nginx:1.24` module stream. The script must run as root (hard check) and warns, but continues, on non-EL8 releases.
- On SELinux-enforcing hosts the script installs `policycoreutils-python-utils` to label a non-80/443 port as `http_port_t`.
- The `web_application` resource type and a `site_url` custom field on the instance; OHK-1rel6s64 writes `site_url` but does not create the field.

## Setup
1. After sync, re-point the server tier's OS build to your Oracle Linux 8 build (the exported href `OSB-pd2spm2e` is instance-specific) and enable the environments the tier may deploy into (`all_environments_enabled` is false).
2. Confirm the run-as credential on OHK-2vdm93hu is root; `run_with_sudo` is false in metadata and the exported `credentials` value is the placeholder `YOUR_CREDENTIALS`.
3. Optionally adjust defaults in `plugins/OHK-2vdm93hu/OHK-2vdm93hu_script.sh`: `NGINX_STREAM` (1.24), `OPEN_FIREWALL` (true), `SITE_ROOT`, `WORKER_CONNECTIONS`.

## Notes
- The script replaces `/etc/nginx/nginx.conf` with a minimal managed file (original kept as `nginx.conf.cb-orig`) and writes `/etc/nginx/conf.d/cloudbolt-site.conf`. Idempotent on re-run.
- OHK-1rel6s64 sets `site_url` to `http://<server IP>:<port>` using the resource field named in its Port Field Name input (`nginx_site_port` here).
- OHK-1rel6s64 and OHK-2vdm93hu are `shared: true`; OHK-1rel6s64 is also used by BP-122nbdt5 (IIS).
- No teardown items; deleting the resource deletes the server and nothing else.
- Expiration Date defaults to 7 days out (HPA-qb0w86mi); enforcement is done by the Expire Servers recurring job (RJB-nsx4v2s1).
