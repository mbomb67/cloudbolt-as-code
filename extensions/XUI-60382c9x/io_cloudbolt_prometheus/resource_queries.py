"""PromQL for the resource Monitoring tab: one query per metric per source, covering many servers.

No Django imports, so the strings can be tested on their own. Each builder takes a selector that
already names every member server of one source group and returns {metric: [query, ...]}; the
caller runs the queries of a metric in order and keeps the first that answers.

Every query groups its answer by one label ("name" for OpenShift Virtualization, "server" for the
lab Prometheus), so one answer holds one series per server.

Metric names:
    https://github.com/kubevirt/kubevirt/blob/release-1.6/docs/observability/metrics.md
    https://github.com/prometheus/node_exporter (node_cpu_seconds_total, node_memory_*, node_disk_*,
    node_network_*, node_filesystem_*)
    https://github.com/prometheus-community/windows_exporter (windows_cpu_time_total, windows_memory_*,
    windows_logical_disk_*, windows_net_*)
"""

import re

RATE_WINDOW = "5m"
NETWORK_DEVICES_EXCLUDED = "lo|bond[0-9]|cbr[0-9]|veth.*"
WINDOWS_NIC_EXCLUDED = ".*[Ll]oopback.*|.*[Ii][Ss][Aa][Tt][Aa][Pp].*"

# The label each source's answer is keyed by.
KUBEVIRT_KEY = "name"
LAB_KEY = "server"


def quote(value):
    """Escape a value for use inside a double-quoted PromQL string."""
    return str(value).replace("\\", "\\\\").replace('"', '\\"')


def regex_alternation(names):
    """a|b|c with every name escaped for a PromQL regex (RE2) and then for a PromQL string.

    The regex escape comes first and the string escape second, so a "." in a hostname reaches
    Prometheus as the regex \\. rather than as an invalid string escape.
    """
    return "|".join(quote(re.escape(str(name))) for name in sorted(set(names)))


def kubevirt_selector(namespace, hostnames):
    return f'namespace="{quote(namespace)}", name=~"{regex_alternation(hostnames)}"'


def lab_selector(server_ids, job):
    """job plus the CloudBolt server ids; ids are integers, so nothing needs escaping."""
    ids = "|".join(str(int(server_id)) for server_id in sorted(set(server_ids)))
    return f'job="{quote(job)}", server=~"{ids}"'


def kubevirt_queries(selector):
    """OpenShift Virtualization. "cpu" is cores in use; the caller divides by each VM's vCPUs."""
    available = f"kubevirt_vmi_memory_available_bytes{{{selector}}}"
    unused = f"kubevirt_vmi_memory_unused_bytes{{{selector}}}"
    cached = f"kubevirt_vmi_memory_cached_bytes{{{selector}}}"
    w = RATE_WINDOW

    def total(metric, function="rate"):
        return f"sum by (name)({function}({metric}{{{selector}}}[{w}]))"

    return {
        "cpu": [total("kubevirt_vmi_cpu_usage_seconds_total")],
        # The per-VM tab's "used" formula, as a share of what the balloon driver reports.
        "mem": [f"100 * max by (name)(clamp_min({available} - {unused} - {cached}, 0) / {available})"],
        "net_rx": [total("kubevirt_vmi_network_receive_bytes_total")],
        "net_tx": [total("kubevirt_vmi_network_transmit_bytes_total")],
        "disk_read": [total("kubevirt_vmi_storage_read_traffic_bytes_total", "irate")],
        "disk_write": [total("kubevirt_vmi_storage_write_traffic_bytes_total", "irate")],
        "fs_used": [f"sum by (name)(kubevirt_vmi_filesystem_used_bytes{{{selector}}})"],
        "fs_total": [f"sum by (name)(kubevirt_vmi_filesystem_capacity_bytes{{{selector}}})"],
    }


def linux_queries(selector):
    """node_exporter on a server the lab Prometheus scrapes."""
    w = RATE_WINDOW
    devices = f'device!~"{NETWORK_DEVICES_EXCLUDED}"'
    fs = f'{selector}, device=~"/dev/.*"'

    def total(metric, extra=""):
        return f"sum by (server)(irate({metric}{{{selector}{extra}}}[{w}]))"

    return {
        "cpu": [
            f'100 * (1 - avg by (server)(rate(node_cpu_seconds_total{{{selector}, mode="idle"}}[{w}])))'
        ],
        "mem": [
            "100 * (1 - max by (server)(node_memory_MemAvailable_bytes{%s} / node_memory_MemTotal_bytes{%s}))"
            % (selector, selector)
        ],
        "net_rx": [total("node_network_receive_bytes_total", f", {devices}")],
        "net_tx": [total("node_network_transmit_bytes_total", f", {devices}")],
        "disk_read": [total("node_disk_read_bytes_total")],
        "disk_write": [total("node_disk_written_bytes_total")],
        "fs_used": [
            f"sum by (server)(node_filesystem_size_bytes{{{fs}}} - node_filesystem_free_bytes{{{fs}}})"
        ],
        "fs_total": [f"sum by (server)(node_filesystem_size_bytes{{{fs}}})"],
    }


def windows_queries(selector):
    """windows_exporter, with the same fallbacks the per-server tab uses for older exporters."""
    w = RATE_WINDOW
    volumes = f'{selector}, volume=~"[A-Z]:"'
    idle = f'sum by (server)(rate(windows_cpu_time_total{{{selector}, mode="idle"}}[{w}]))'
    busy_total = f"clamp_min(sum by (server)(rate(windows_cpu_time_total{{{selector}}}[{w}])), 1e-9)"
    free = (
        f"(windows_memory_physical_free_bytes{{{selector}}} or "
        f"windows_os_physical_memory_free_bytes{{{selector}}})"
    )
    total_memory = (
        f"(windows_memory_physical_total_bytes{{{selector}}} or "
        f"windows_cs_physical_memory_bytes{{{selector}}})"
    )
    available = f"(windows_memory_available_bytes{{{selector}}} or {free})"
    nic = f'{selector}, nic!~"{WINDOWS_NIC_EXCLUDED}"'

    def net(metric):
        # The filtered query first; an older Prometheus may not support it, so the plain one follows.
        return [
            f"sum by (server)(irate({metric}{{{nic}}}[{w}]))",
            f"sum by (server)(irate({metric}{{{selector}}}[{w}]))",
        ]

    return {
        "cpu": [f"100 * (1 - {idle} / {busy_total})"],
        "mem": [f"100 * max by (server)(clamp_min({total_memory} - {available}, 0) / {total_memory})"],
        "net_rx": net("windows_net_bytes_received_total"),
        "net_tx": net("windows_net_bytes_sent_total"),
        "disk_read": [f"sum by (server)(irate(windows_logical_disk_read_bytes_total{{{volumes}}}[{w}]))"],
        "disk_write": [f"sum by (server)(irate(windows_logical_disk_write_bytes_total{{{volumes}}}[{w}]))"],
        "fs_used": [
            f"sum by (server)(windows_logical_disk_size_bytes{{{volumes}}} - "
            f"windows_logical_disk_free_bytes{{{volumes}}})"
        ],
        "fs_total": [f"sum by (server)(windows_logical_disk_size_bytes{{{volumes}}})"],
    }
