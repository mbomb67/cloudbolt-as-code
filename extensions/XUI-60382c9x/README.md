# Io Cloudbolt Prometheus (XUI)

Adds a **Monitoring** server tab that graphs CPU load, memory, disk usage, disk I/O, network I/O and node health from a Prometheus server, plus a **Monitoring Admin** page under Admin. It also publishes an HTTP service-discovery endpoint (`/xui/io_cloudbolt_prometheus/api/targets/`) that lists every active, powered-on server tagged `monitor` so Prometheus can scrape them.

## Prerequisites
- CloudBolt 8.6 or later.
- A Prometheus server with network access to the monitored servers and to the CloudBolt targets endpoint.
- Prometheus `node_exporter` (Linux, port 9100) or `windows_exporter` (Windows, port 9182) on each monitored server.
- End-user browsers need Internet access to `unpkg.com` for the `lit-html` library unless the airgapped steps in the package readme are followed.

## Setup
1. Create a Connection Info named `MONITORING` (protocol, IP/host, port) pointing at the Prometheus server.
2. Add an `http_sd_configs` job to `prometheus.yml` with the CloudBolt targets URL above (example in the package readme).
3. Install an exporter on target servers. Use `scripts/configure_monitoring.py` as an orchestration or server action (RHEL-family, Ubuntu, Windows; also applies the `monitor` tag), or `scripts/install_node_exporter.sh` as a post-provision remote script (yum-based Linux only).
4. Run `collectstatic` (or "Collect Static Assets" on Admin > Extensions) and restart httpd so the `static/` JavaScript is served.

## Notes
- The tab appears only on servers with the `monitor` tag.
- The targets endpoint is unauthenticated (`@login_not_required`) and returns hostnames and private IPs; restrict it at the network level.
- `scripts/generate_targets.py` prints the same targets JSON from the CloudBolt shell for troubleshooting.

See [package readme](io_cloudbolt_prometheus/README.md) for details.
