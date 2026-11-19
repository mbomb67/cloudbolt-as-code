# Io Cloudbolt Prometheus (XUI)

Adds a **Monitoring** server tab that graphs CPU, memory, disk usage, disk I/O, network I/O and node health, plus a **Monitoring Admin** page under Admin. The data comes from one of two sources:

- **OpenShift Virtualization VMs** are read from their cluster's built-in monitoring. Nothing is installed in the VM and the tab appears on every such server.
- **Every other server** is read from a Prometheus server that scrapes an exporter on it. The extension publishes an HTTP service-discovery endpoint (`/xui/io_cloudbolt_prometheus/api/targets/`) that lists every active, powered-on server tagged `monitor` so Prometheus can find them.

## Prerequisites
- CloudBolt 8.6 or later.

OpenShift Virtualization VMs:
- CloudBolt can reach the cluster's Thanos Querier route (`thanos-querier` in `openshift-monitoring`) on port 443. It is served by the same ingress as the OpenShift console.
- The credential on the OpenShift Virtualization resource handler has the `cluster-monitoring-view` cluster role. A cluster administrator has it already.

Other servers:
- A Prometheus server with network access to the monitored servers and to the CloudBolt targets endpoint.
- Prometheus `node_exporter` (Linux, port 9100) or `windows_exporter` (Windows, port 9182) on each monitored server.

## Setup
OpenShift Virtualization VMs need no setup beyond the prerequisites above.

Other servers:
1. Create a Connection Info named `MONITORING` (protocol, IP/host, port) pointing at the Prometheus server.
2. Add an `http_sd_configs` job to `prometheus.yml` with the CloudBolt targets URL above (example in the package readme).
3. Install an exporter on target servers. Use `scripts/configure_monitoring.py` as an orchestration or server action (RHEL-family, Ubuntu, Windows; also applies the `monitor` tag), or `scripts/install_node_exporter.sh` as a post-provision remote script (yum-based Linux only).

Then run `collectstatic` (or "Collect Static Assets" on Admin > Extensions) and restart httpd so the `static/` JavaScript is served.

## Notes
- The tab appears on OpenShift Virtualization VMs and on servers with the `monitor` tag.
- For an OpenShift Virtualization VM, the CPU chart shows utilization instead of load average, memory needs the virtio balloon driver in the guest, and the disk usage chart stays empty unless the QEMU guest agent runs in the guest.
- OpenShift Virtualization VMs are left out of the targets endpoint, because their address is a pod IP that an outside Prometheus cannot reach.
- A user sees metrics only for servers they are allowed to view.
- The targets endpoint is unauthenticated (`@login_not_required`) and returns hostnames and private IPs; restrict it at the network level.
- `scripts/generate_targets.py` prints the same targets JSON from the CloudBolt shell for troubleshooting.

See [package readme](io_cloudbolt_prometheus/README.md) for details.
