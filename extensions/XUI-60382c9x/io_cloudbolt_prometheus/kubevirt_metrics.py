"""Monitoring data for OpenShift Virtualization servers, read from the cluster's own monitoring.

OpenShift's platform Prometheus already scrapes KubeVirt's per-VM series, labelled with the VM's
name and namespace, so nothing is installed in the guest and no target is registered. The queries
go to the cluster's Thanos Querier route with the bearer token the server's resource handler
already uses; that identity needs the cluster-monitoring-view cluster role.

Access: https://docs.redhat.com/en/documentation/openshift_container_platform/4.18/html/monitoring/accessing-metrics
Metric names: https://github.com/kubevirt/kubevirt/blob/release-1.6/docs/observability/metrics.md

Each fetch_* function returns the payload shape of the function with the same name in metrics.py,
so both sources share the charts.
"""

import re
import time

from django.core.cache import cache
from requests import RequestException

from utilities.exceptions import CloudBoltException
from utilities.logger import ThreadLogger
from xui.io_cloudbolt_prometheus.alert_rows import alert_row, ordered
from xui.io_cloudbolt_prometheus.resource_queries import quote as _quote

logger = ThreadLogger(__name__)

THANOS_ROUTE_PATH = "/apis/route.openshift.io/v1/namespaces/openshift-monitoring/routes/thanos-querier"
HOST_CACHE_SECONDS = 3600
QUERY_TIMEOUT_SECONDS = 10
DAY_SECONDS = 24 * 60 * 60
GIB = 2 ** 30

CPU_SERIES_NAMES = {"load1": "CPU 2 min avg", "load5": "CPU 5 min avg", "load15": "CPU 15 min avg"}


def is_kubevirt_server(server):
    """True when the server is an OpenShift Virtualization VM."""
    return getattr(server, "ovirtserverinfo", None) is not None


def _thanos_host(handler, client):
    """Return the host of the cluster's Thanos Querier route, cached per handler."""
    key = f"io_cloudbolt_prometheus:thanos_host:{handler.id}"
    host = cache.get(key)
    if not host:
        try:
            host = client.send_request("GET", THANOS_ROUTE_PATH)["spec"]["host"]
        except (CloudBoltException, KeyError):
            # The handler's identity may not be allowed to read the route; this is its default host.
            host = f"thanos-querier-openshift-monitoring.apps.{handler.ip}"
        cache.set(key, host, HOST_CACHE_SECONDS)
    return host


class ThanosClient:
    """Runs PromQL against a cluster's Thanos Querier. Every query returns [] when it cannot be asked."""

    def __init__(self, handler, description):
        """handler is the cast OpenShift Virtualization resource handler; description is for the log."""
        self.session = None
        self.base_url = ""
        # Set when the cluster cannot be reached or answers 5xx; every later call then returns at once,
        # so a dead cluster costs one wait, not one per query.
        self.failed = False
        try:
            # The wrapper's session carries the bearer token, CloudBolt's CA bundle and its proxies.
            client = handler.get_api_wrapper().client
            self.base_url = f"https://{_thanos_host(handler, client)}/api/v1"
            self.session = client.http_session
        except Exception as error:  # the wrapper raises bare Exception when the login fails
            self.failed = True
            logger.warning(f"Monitoring: could not connect to the cluster for {description}: {error}")

    def get_data(self, path, params=None):
        """Return the "data" member of the answer, or None when the cluster cannot be asked."""
        if self.session is None or self.failed:
            return None
        try:
            response = self.session.get(
                f"{self.base_url}{path}", params=params or {}, timeout=QUERY_TIMEOUT_SECONDS
            )
        except RequestException as error:
            self.failed = True
            logger.warning(f"Monitoring: Thanos Querier request failed: {error}")
            return None
        if response.status_code >= 500:
            self.failed = True
        if response.status_code != 200:
            # 403 means the handler's identity lacks the cluster-monitoring-view cluster role.
            logger.warning(
                f"Monitoring: Thanos Querier answered {response.status_code} for "
                f"{path} {(params or {}).get('query', '')}: {response.text[:200]}"
            )
            return None
        return response.json().get("data") or {}

    def _get(self, path, params):
        data = self.get_data(path, params)
        return data.get("result", []) if data else []

    def instant(self, query):
        return self._get("/query", {"query": query})

    def instant_value(self, query):
        result = self.instant(query)
        return result[0]["value"][1] if result else None

    def range(self, query, step):
        """Return the result list of a 24-hour range query."""
        end = time.time()
        return self.range_between(query, end - DAY_SECONDS, end, step)

    def range_between(self, query, start, end, step):
        """Return the result list of a range query over exactly this window."""
        return self._get(
            "/query_range",
            {"query": query, "start": start, "end": end, "step": step},
        )

    def values(self, query, step):
        """Return the [timestamp, value] pairs of the first series of a 24-hour range query."""
        result = self.range(query, step)
        return result[0]["values"] if result else []


class ThanosQuerier(ThanosClient):
    """Runs PromQL for one VM."""

    def __init__(self, server):
        self.selector = ""
        try:
            handler = server.resource_handler.cast()
            self.selector = (
                f'namespace="{_quote(server.ovirtserverinfo.namespace)}", '
                f'name="{_quote(server.hostname)}"'
            )
        except Exception as error:
            logger.warning(f"Monitoring: could not connect to the cluster for {server.hostname}: {error}")
            self.session = None
            self.base_url = ""
            self.failed = True
            return
        super().__init__(handler, server.hostname)


def _cpu_fraction(querier, server, window):
    """Share of the VM's vCPUs in use (0-1) over the window."""
    vcpus = max(server.cpu_cnt or 1, 1)
    return f"sum(rate(kubevirt_vmi_cpu_usage_seconds_total{{{querier.selector}}}[{window}])) / {vcpus}"


def _rate_sum(querier, metric, function="irate"):
    return f"sum({function}({metric}{{{querier.selector}}}[5m]))"


def fetch_cpu_load(server):
    querier = ThanosQuerier(server)
    payload = {"names": CPU_SERIES_NAMES}
    # The cluster scrapes every 30 seconds, so the shortest window that always holds two samples is 2m.
    for key, window in (("load1", "2m"), ("load5", "5m"), ("load15", "15m")):
        payload[key] = querier.values(_cpu_fraction(querier, server, window), 60)
    return payload


def fetch_disk_io(server):
    querier = ThanosQuerier(server)
    reads = _rate_sum(querier, "kubevirt_vmi_storage_read_traffic_bytes_total")
    writes = _rate_sum(querier, "kubevirt_vmi_storage_write_traffic_bytes_total")
    return {
        "reads": querier.values(reads, 60),
        "writes": querier.values(writes, 60),
        "total": querier.values(f"{reads} + {writes}", 60),
    }


def fetch_iops(server):
    querier = ThanosQuerier(server)
    reads = _rate_sum(querier, "kubevirt_vmi_storage_iops_read_total")
    writes = _rate_sum(querier, "kubevirt_vmi_storage_iops_write_total")
    io_time = (
        f"{_rate_sum(querier, 'kubevirt_vmi_storage_read_times_seconds_total')} + "
        f"{_rate_sum(querier, 'kubevirt_vmi_storage_write_times_seconds_total')}"
    )
    return {
        "reads": querier.values(reads, 60),
        "writes": querier.values(writes, 60),
        "iops": querier.values(f"{reads} + {writes}", 60),
        "io_time": querier.values(io_time, 60),
    }


def fetch_memory(server):
    """Guest memory as the balloon driver reports it: unused is free, cached is reclaimable."""
    querier = ThanosQuerier(server)
    available = f"kubevirt_vmi_memory_available_bytes{{{querier.selector}}}"
    unused = f"kubevirt_vmi_memory_unused_bytes{{{querier.selector}}}"
    cached = f"kubevirt_vmi_memory_cached_bytes{{{querier.selector}}}"
    return {
        "used": querier.values(f"clamp_min({available} - {unused} - {cached}, 0)", 60),
        "free": querier.values(unused, 60),
        "cached": querier.values(cached, 60),
        "buffers": querier.values(f"0 * {unused}", 60),
    }


def fetch_disk_usage(server):
    """Free bytes per guest filesystem; empty unless the QEMU guest agent runs in the VM."""
    querier = ThanosQuerier(server)
    free = (
        f"kubevirt_vmi_filesystem_capacity_bytes{{{querier.selector}}} - "
        f"kubevirt_vmi_filesystem_used_bytes{{{querier.selector}}}"
    )
    return querier.range(f'label_replace({free}, "mountpoint", "$1", "mount_point", "(.+)")', 300)


def fetch_net_io(server):
    querier = ThanosQuerier(server)
    # A five-minute average: a quiet interface moves in bursts, so the last two samples alone
    # swing between zero and the burst and draw as a solid block.
    return {
        "rx": querier.values(_rate_sum(querier, "kubevirt_vmi_network_receive_bytes_total", "rate"), 120),
        "tx": querier.values(_rate_sum(querier, "kubevirt_vmi_network_transmit_bytes_total", "rate"), 120),
    }


def fetch_health(server):
    querier = ThanosQuerier(server)
    namespace = _quote(server.ovirtserverinfo.namespace)
    # The VM runs in a pod named virt-launcher-<vm>-<5 characters>; its start time is the VM's.
    # https://github.com/kubernetes/kube-state-metrics/blob/main/docs/metrics/workload/pod-metrics.md
    pod = _quote(f"virt-launcher-{re.escape(server.hostname)}-[a-z0-9]{{5}}")
    uptime = querier.instant_value(
        f'time() - max(kube_pod_start_time{{namespace="{namespace}", pod=~"{pod}"}})'
    )
    memory = querier.instant_value(f"kubevirt_vmi_memory_domain_bytes{{{querier.selector}}}")
    load = querier.instant_value(_cpu_fraction(querier, server, "5m"))
    if memory is None and server.mem_size:
        memory = float(server.mem_size) * GIB
    # Space in use across the guest's filesystems, which only the QEMU guest agent reports.
    disk_used = querier.instant_value(f"sum(kubevirt_vmi_filesystem_used_bytes{{{querier.selector}}})")
    disk_total = server.disk_size * GIB if server.disk_size else querier.instant_value(
        f"sum(kubevirt_vmi_filesystem_capacity_bytes{{{querier.selector}}})"
    )
    return {
        "source": "kubevirt",
        "up": uptime is not None,
        "uptime": uptime if uptime is not None else -1,
        "core_count": server.cpu_cnt if server.cpu_cnt else -1,
        "disk_total": disk_total if disk_total is not None else -1,
        "disk_used": disk_used if disk_used is not None else -1,
        "mem_total": memory if memory is not None else -1,
        "load": load,
        "scraper_cpu_use": None,
    }


def matching_vm(labels, namespace, hostnames):
    """The hostname of the VM, out of hostnames, that an alert's labels point at; None when none.

    Rules name the VM differently: KubeVirt's own use name or vmi, a rule written on the
    kubevirt_vmi_* series inherits name, and a pod rule carries the virt-launcher pod.
    """
    if labels.get("namespace") != namespace:
        return None
    for key in ("name", "vmi", "vm"):
        if labels.get(key) in hostnames:
            return labels[key]
    pod = re.fullmatch(r"virt-launcher-(.+)-[a-z0-9]{5}", labels.get("pod", ""))
    if pod and pod.group(1) in hostnames:
        return pod.group(1)
    return None


def _about_vm(labels, namespace, hostname):
    """True when an alert's labels point at this VM."""
    return matching_vm(labels, namespace, {hostname}) is not None


def fetch_alert_rows(client, wanted):
    """[(namespace, hostname, alert row)] for alerts about the VMs in wanted ({namespace: {hostnames}}).

    None when the cluster cannot be asked. Same sources as fetch_alerts: the alerts API, else the
    ALERTS series. One call serves every namespace of the cluster.
    """
    data = client.get_data("/alerts")
    if data is not None:
        pairs = [
            (alert.get("labels", {}), alert_row(
                alert.get("labels", {}), alert.get("annotations"), alert.get("state"), alert.get("activeAt")))
            for alert in data.get("alerts", [])
        ]
    else:
        namespaces = "|".join(_quote(re.escape(namespace)) for namespace in sorted(wanted))
        data = client.get_data("/query", {"query": f'ALERTS{{namespace=~"{namespaces}"}}'})
        if data is None:
            return None
        pairs = [
            (series["metric"], alert_row(series["metric"], state=series["metric"].get("alertstate")))
            for series in data.get("result", [])
        ]
    rows = []
    for labels, row in pairs:
        namespace = labels.get("namespace")
        if namespace in wanted:
            hostname = matching_vm(labels, namespace, wanted[namespace])
            if hostname is not None:
                rows.append((namespace, hostname, row))
    return rows


def fetch_alerts(server):
    """Firing and pending alerts about this VM, from platform rules and the project's own.

    The cluster's alerts API takes no label filter, so every alert is fetched and matched here.
    https://github.com/openshift/cluster-monitoring-operator/blob/main/Documentation/resources.md
    """
    querier = ThanosQuerier(server)
    namespace = server.ovirtserverinfo.namespace
    data = querier.get_data("/alerts")
    if data is not None:
        rows = [
            alert_row(alert.get("labels", {}), alert.get("annotations"), alert.get("state"), alert.get("activeAt"))
            for alert in data.get("alerts", [])
            if _about_vm(alert.get("labels", {}), namespace, server.hostname)
        ]
        return {"available": True, "alerts": ordered(rows)}

    # Without the alerts API the ALERTS series still names what is active, with labels only.
    data = querier.get_data("/query", {"query": f'ALERTS{{namespace="{_quote(namespace)}"}}'})
    if data is None:
        return {"available": False, "alerts": []}
    rows = [
        alert_row(series["metric"], state=series["metric"].get("alertstate"))
        for series in data.get("result", [])
        if _about_vm(series["metric"], namespace, server.hostname)
    ]
    return {"available": True, "alerts": ordered(rows)}


FETCHERS = {
    "alerts": fetch_alerts,
    "cpu_load": fetch_cpu_load,
    "disk_io": fetch_disk_io,
    "iops": fetch_iops,
    "memory": fetch_memory,
    "disk_usage": fetch_disk_usage,
    "net_io": fetch_net_io,
    "health": fetch_health,
}
