import math
import copy
from datetime import datetime, timedelta

import json
from django.utils.timezone import now
from requests import HTTPError, RequestException

from accounts.models import UserProfile
from infrastructure.models import Server
from utilities.decorators import json_view
from utilities.logger import ThreadLogger
from utilities.middleware import login_not_required
from utilities.models import ConnectionInfo
from xui.io_cloudbolt_prometheus.prom_api_wrapper import PromConnection

logger = ThreadLogger(__name__)

CONNECTION_INFO_NAME = "MONITORING"
MONITORING_LABEL = "monitor"
REQUEST_TIMEOUT = 3.0
PROME_JOB = "cloudbolt"


def _server_is_windows(server):
    """True when this server should use windows_exporter PromQL."""
    if getattr(server, "os_family_name", None) == "Windows":
        return True
    os_family = getattr(server, "os_family", None)
    return os_family is not None and getattr(os_family, "name", None) == "Windows"


def _prom_server_labels(server):
    return f'job="{PROME_JOB}", server="{server.id}"'


def _windows_cpu_busy_fraction_promql(server, rate_window):
    """Average non-idle CPU share (0–1) from windows_cpu_time_total."""
    lb = _prom_server_labels(server)
    return (
        "1 - ("
        f'sum(rate(windows_cpu_time_total{{{lb},mode="idle"}}[{rate_window}])) '
        f"/ clamp_min(sum(rate(windows_cpu_time_total{{{lb}}}[{rate_window}])), 1e-9))"
    )



def _fill_zero_values_like(template_values):
    if not template_values:
        return []
    return [[ts, "0"] for ts, _ in template_values]


@json_view
def server_metrics_dispatch(request, server_id, metric_type):
    """
    Supported metrics include:
        cpu_load, memory, disk_usage, disk_io, network_io
    :param request:
    :param server_id:
    :param metric_type:
    :return:
    """
    fetch_function = globals().get(f'fetch_{metric_type}', None)
    if fetch_function:
        server = Server.objects.get(id=server_id)
        if MONITORING_LABEL not in server.labels:
            return {}

        ci = ConnectionInfo.objects.get(name=CONNECTION_INFO_NAME)
        return fetch_function(request, server, ci)
    else:
        # TODO: Invalid metric requested
        logger.error(f"Invalid metric type requested: '{metric_type}'")
        pass

    return {}


def fetch_cpu_load(request, server, ci):
    if _server_is_windows(server):
        return _fetch_cpu_load_windows(request, server, ci)
    return _fetch_cpu_load_linux(request, server, ci)


def _fetch_cpu_load_linux(request, server, ci):
    cpu_load_data = {
        "load1": [],
        "load5": [],
        "load15": []
    }
    labels = _prom_server_labels(server)
    delta = timedelta(hours=24)
    start = (datetime.now() - delta).timestamp()
    end = datetime.now().timestamp()
    step = 60

    with PromConnection(ci) as conn:
        params = {
            "query": f'node_load1{{{labels}}}',
            "start": start,
            "end": end,
            "step": step,
        }
        response = conn.get("/query_range", params=params)
        data = validate_prometheus_response(response)

        # 1 minuate load average
        result = data.get('result')
        if result and len(result) > 0:
            cpu_load_data["load1"] = result[0]['values']

        # 5 minute load average
        params["query"] = f'node_load5{{{labels}}}'
        response = conn.get("/query_range", params=params)
        data = validate_prometheus_response(response)

        result = data.get('result')
        if result and len(result) > 0:
            cpu_load_data["load5"] = result[0]['values']

        # 15 minute load average
        params["query"] = f'node_load15{{{labels}}}'
        response = conn.get("/query_range", params=params)
        data = validate_prometheus_response(response)

        result = data.get('result')
        if result and len(result) > 0:
            cpu_load_data["load15"] = result[0]['values']

    return cpu_load_data


def _fetch_cpu_load_windows(request, server, ci):
    """
    Map load1/5/15 to CPU utilization (0–1) using 1m/5m/15m rate windows.
    Frontend multiplies by 100 (same as Linux path) for the chart scale.
    """
    cpu_load_data = {
        "load1": [],
        "load5": [],
        "load15": []
    }
    delta = timedelta(hours=24)
    start = (datetime.now() - delta).timestamp()
    end = datetime.now().timestamp()
    step = 60

    with PromConnection(ci) as conn:
        for key, window in (("load1", "1m"), ("load5", "5m"), ("load15", "15m")):
            params = {
                "query": _windows_cpu_busy_fraction_promql(server, window),
                "start": start,
                "end": end,
                "step": step,
            }
            response = conn.get("/query_range", params=params)
            data = validate_prometheus_response(response)
            result = data.get("result") if data else None
            if result and len(result) > 0:
                cpu_load_data[key] = result[0]["values"]

    return cpu_load_data


def fetch_disk_io(request, server, ci):
    if _server_is_windows(server):
        return _fetch_disk_io_windows(request, server, ci)
    return _fetch_disk_io_linux(request, server, ci)


def _fetch_disk_io_linux(request, server, ci):
    disk_io_data = {
        "reads": [],
        "writes": [],
        "total": []
    }
    delta = timedelta(days=1)
    start = (datetime.now() - delta).timestamp()
    end = datetime.now().timestamp()
    step = 60

    sid = server.id
    with PromConnection(ci) as conn:
        # disk writes
        params = {
            "query": f'sum by (server) (irate(node_disk_written_bytes_total{{server="{sid}"}}[5m]))',
            "start": start,
            "end": end,
            "step": step,
        }
        response = conn.get("/query_range", params=params)
        data = validate_prometheus_response(response)
        result = data.get('result')
        if result and len(result) > 0:
            disk_io_data["writes"] = result[0]['values']

        # disk reads
        params = {
            "query": f'sum by (server) (irate(node_disk_read_bytes_total{{server="{sid}"}}[5m]))',
            "start": start,
            "end": end,
            "step": step,
        }
        response = conn.get("/query_range", params=params)
        data = validate_prometheus_response(response)
        result = data.get('result')
        if result and len(result) > 0:
            disk_io_data["reads"] = result[0]['values']

        # disk total
        params = {
            "query": (
                f'sum by (server) (irate(node_disk_read_bytes_total{{server="{sid}"}}[5m])) + '
                f'sum by (server) (irate(node_disk_written_bytes_total{{server="{sid}"}}[5m]))'
            ),
            "start": start,
            "end": end,
            "step": step,
        }
        response = conn.get("/query_range", params=params)
        data = validate_prometheus_response(response)
        result = data.get('result')
        if result and len(result) > 0:
            disk_io_data["total"] = result[0]['values']

    return disk_io_data


def _fetch_disk_io_windows(request, server, ci):
    disk_io_data = {
        "reads": [],
        "writes": [],
        "total": []
    }
    delta = timedelta(days=1)
    start = (datetime.now() - delta).timestamp()
    end = datetime.now().timestamp()
    step = 60
    lb = _prom_server_labels(server)

    with PromConnection(ci) as conn:
        params = {
            "query": (
                f'sum by (server) (irate('
                f'windows_logical_disk_read_bytes_total{{{lb}, volume=~"[A-Z]:"}}[5m]))'
            ),
            "start": start,
            "end": end,
            "step": step,
        }
        response = conn.get("/query_range", params=params)
        data = validate_prometheus_response(response)
        result = data.get("result")
        if result and len(result) > 0:
            disk_io_data["reads"] = result[0]["values"]

        params["query"] = (
            f'sum by (server) (irate('
            f'windows_logical_disk_write_bytes_total{{{lb}, volume=~"[A-Z]:"}}[5m]))'
        )
        response = conn.get("/query_range", params=params)
        data = validate_prometheus_response(response)
        result = data.get("result")
        if result and len(result) > 0:
            disk_io_data["writes"] = result[0]["values"]

        params["query"] = (
            f'sum by (server) (irate('
            f'windows_logical_disk_read_bytes_total{{{lb}, volume=~"[A-Z]:"}}[5m])) + '
            f'sum by (server) (irate('
            f'windows_logical_disk_write_bytes_total{{{lb}, volume=~"[A-Z]:"}}[5m]))'
        )
        response = conn.get("/query_range", params=params)
        data = validate_prometheus_response(response)
        result = data.get("result")
        if result and len(result) > 0:
            disk_io_data["total"] = result[0]["values"]

    return disk_io_data


def fetch_iops(request, server, ci):
    if _server_is_windows(server):
        return _fetch_iops_windows(request, server, ci)
    return _fetch_iops_linux(request, server, ci)


def _fetch_iops_linux(request, server, ci):
    iops_data = {
        "reads": [],
        "writes": [],
        "iops": [],
        "io_time": []
    }
    delta = timedelta(days=1)
    start = (datetime.now() - delta).timestamp()
    end = datetime.now().timestamp()
    step = 60

    with PromConnection(ci) as conn:
        # disk writes
        params = {
            "query": f'sum by (instance) (irate('
                     f'node_disk_writes_completed_total{{'
                     f'server="{server.id}"}}[5m]))',
            "start": start,
            "end": end,
            "step": step,
        }
        response = conn.get("/query_range", params=params)
        data = validate_prometheus_response(response)
        result = data.get('result')
        if result and len(result) > 0:
            iops_data["writes"] = result[0]['values']

        # disk reads
        params = {
            "query": f'sum by (instance) (irate('
                     f'node_disk_reads_completed_total{{'
                     f'server="{server.id}"}}[5m]))',
            "start": start,
            "end": end,
            "step": step,
        }
        response = conn.get("/query_range", params=params)
        data = validate_prometheus_response(response)
        result = data.get('result')
        if result and len(result) > 0:
            iops_data["reads"] = result[0]['values']

        # iops
        params = {
            "query": f'sum by (instance) (irate('
                     f'node_disk_reads_completed_total{{'
                     f'server="{server.id}"}}[5m])) + sum by (instance) '
                     f'(irate(node_disk_writes_completed_total{{'
                     f'server="{server.id}"}}[5m]))',
            "start": start,
            "end": end,
            "step": step,
        }
        response = conn.get("/query_range", params=params)
        data = validate_prometheus_response(response)
        result = data.get('result')
        if result and len(result) > 0:
            iops_data["iops"] = result[0]['values']

        # io time
        params = {
            "query": f'sum by (instance) (irate('
                     f'node_disk_io_time_seconds_total{{'
                     f'server="{server.id}"}}[5m]))',
            "start": start,
            "end": end,
            "step": step,
        }
        response = conn.get("/query_range", params=params)
        data = validate_prometheus_response(response)
        result = data.get('result')
        if result and len(result) > 0:
            iops_data["io_time"] = result[0]['values']

    return iops_data


def _fetch_iops_windows(request, server, ci):
    iops_data = {
        "reads": [],
        "writes": [],
        "iops": [],
        "io_time": []
    }
    delta = timedelta(days=1)
    start = (datetime.now() - delta).timestamp()
    end = datetime.now().timestamp()
    step = 60
    lb = _prom_server_labels(server)
    vol = f'{lb}, volume=~"[A-Z]:"'

    with PromConnection(ci) as conn:
        params = {
            "query": (
                f'sum by (server) (irate(windows_logical_disk_writes_total{{{vol}}}[5m]))'
            ),
            "start": start,
            "end": end,
            "step": step,
        }
        response = conn.get("/query_range", params=params)
        data = validate_prometheus_response(response)
        result = data.get("result")
        if result and len(result) > 0:
            iops_data["writes"] = result[0]["values"]

        params["query"] = (
            f'sum by (server) (irate(windows_logical_disk_reads_total{{{vol}}}[5m]))'
        )
        response = conn.get("/query_range", params=params)
        data = validate_prometheus_response(response)
        result = data.get("result")
        if result and len(result) > 0:
            iops_data["reads"] = result[0]["values"]

        params["query"] = (
            f'sum by (server) (irate(windows_logical_disk_reads_total{{{vol}}}[5m])) + '
            f'sum by (server) (irate(windows_logical_disk_writes_total{{{vol}}}[5m]))'
        )
        response = conn.get("/query_range", params=params)
        data = validate_prometheus_response(response)
        result = data.get("result")
        if result and len(result) > 0:
            iops_data["iops"] = result[0]["values"]

        params["query"] = (
            f'sum by (server) (irate(windows_logical_disk_read_seconds_total{{{vol}}}[5m])) + '
            f'sum by (server) (irate(windows_logical_disk_write_seconds_total{{{vol}}}[5m]))'
        )
        response = conn.get("/query_range", params=params)
        data = validate_prometheus_response(response)
        result = data.get("result")
        if result and len(result) > 0:
            iops_data["io_time"] = result[0]["values"]

    return iops_data


def fetch_memory(request, server, ci):
    if _server_is_windows(server):
        return _fetch_memory_windows(request, server, ci)
    return _fetch_memory_linux(request, server, ci)


def _fetch_memory_linux(request, server, ci):
    memory_data = {
        "used": [],
        "buffers": [],
        "cached": [],
        "free": []
    }
    delta = timedelta(days=1)
    start = (datetime.now() - delta).timestamp()
    end = datetime.now().timestamp()
    step = 30

    with PromConnection(ci) as conn:
        # USED Memory
        params = {
            "query": f'node_memory_MemTotal_bytes{{server="{server.id}"}} - '
                     f'node_memory_MemFree_bytes{{server="{server.id}"}} - '
                     f'node_memory_Cached_bytes{{server="{server.id}"}} - '
                     f'node_memory_Buffers_bytes{{server="{server.id}"}} - '
                     f'node_memory_Slab_bytes{{server="{server.id}"}}',
            "start": start,
            "end": end,
            "step": step,
        }
        response = conn.get("/query_range", params=params)
        data = validate_prometheus_response(response)
        result = data.get('result')
        if result and len(result) > 0:
            memory_data["used"] = result[0]['values']

        # FREE
        params = {
            "query": f'node_memory_MemFree_bytes{{server="{server.id}"}}',
            "start": start,
            "end": end,
            "step": step,
        }
        response = conn.get("/query_range", params=params)
        data = validate_prometheus_response(response)
        result = data.get('result')
        if result and len(result) > 0:
            memory_data["free"] = result[0]['values']

        # BUFFER
        params = {
            "query": f'node_memory_Buffers_bytes{{server="{server.id}"}}',
            "start": start,
            "end": end,
            "step": step,
        }
        response = conn.get("/query_range", params=params)
        data = validate_prometheus_response(response)
        result = data.get('result')
        if result and len(result) > 0:
            memory_data["buffers"] = result[0]['values']

        # CACHED
        params = {
            "query": f'node_memory_Cached_bytes{{server="{server.id}"}} + '
                     f'node_memory_Slab_bytes{{server="{server.id}"}}',
            "start": start,
            "end": end,
            "step": step,
        }
        response = conn.get("/query_range", params=params)
        data = validate_prometheus_response(response)
        result = data.get('result')
        if result and len(result) > 0:
            memory_data["cached"] = result[0]['values']

    return memory_data


def _fetch_memory_windows(request, server, ci):
    """
    Align with Linux stacked chart: used, cached, free, buffers.
    Uses windows_memory_* with fallbacks for older exporters.
    """
    memory_data = {
        "used": [],
        "buffers": [],
        "cached": [],
        "free": []
    }
    delta = timedelta(days=1)
    start = (datetime.now() - delta).timestamp()
    end = datetime.now().timestamp()
    step = 30
    lb = _prom_server_labels(server)
    free_or = (
        f"(windows_memory_physical_free_bytes{{{lb}}} or "
        f"windows_os_physical_memory_free_bytes{{{lb}}})"
    )
    total_or = (
        f"(windows_memory_physical_total_bytes{{{lb}}} or "
        f"windows_cs_physical_memory_bytes{{{lb}}})"
    )

    with PromConnection(ci) as conn:
        params = {
            "query": (
                f"clamp_min({total_or} - "
                f"(windows_memory_available_bytes{{{lb}}} or {free_or}), 0)"
            ),
            "start": start,
            "end": end,
            "step": step,
        }
        response = conn.get("/query_range", params=params)
        data = validate_prometheus_response(response)
        result = data.get("result")
        if result and len(result) > 0:
            memory_data["used"] = result[0]["values"]

        params["query"] = free_or
        response = conn.get("/query_range", params=params)
        data = validate_prometheus_response(response)
        result = data.get("result")
        if result and len(result) > 0:
            memory_data["free"] = result[0]["values"]

        params["query"] = (
            f"clamp_min(windows_memory_available_bytes{{{lb}}} - {free_or}, 0)"
        )
        response = conn.get("/query_range", params=params)
        data = validate_prometheus_response(response)
        result = data.get("result")
        if result and len(result) > 0:
            memory_data["cached"] = result[0]["values"]

        params["query"] = f"0 * {free_or}"
        response = conn.get("/query_range", params=params)
        data = validate_prometheus_response(response)
        result = data.get("result")
        if result and len(result) > 0:
            memory_data["buffers"] = result[0]["values"]

    if memory_data["free"] and not memory_data["cached"]:
        memory_data["cached"] = _fill_zero_values_like(memory_data["free"])
    if memory_data["free"] and not memory_data["buffers"]:
        memory_data["buffers"] = _fill_zero_values_like(memory_data["free"])

    return memory_data


def fetch_disk_usage(request, server, ci):
    if _server_is_windows(server):
        return _fetch_disk_usage_windows(request, server, ci)
    return _fetch_disk_usage_linux(request, server, ci)


def _fetch_disk_usage_linux(request, server, ci):
    disk_usage_data = []

    delta = timedelta(days=1)
    start = (datetime.now() - delta).timestamp()
    end = datetime.now().timestamp()

    with PromConnection(ci) as conn:
        params = {
            "query": f'node_filesystem_avail_bytes{{server="{server.id}", '
                     f'mountpoint=~"/|/var|/opt|/usr/var|/home"}}',
            "start": start,
            "end": end,
            "step": "15s",
        }
        response = conn.get("/query_range", params=params)
        data = validate_prometheus_response(response)

        results = data.get('result')
        if results and len(results) > 0:
            disk_usage_data = results

    return disk_usage_data


def _fetch_disk_usage_windows(request, server, ci):
    """Expose drive letters as mountpoint for the existing disk chart JS."""
    disk_usage_data = []
    delta = timedelta(days=1)
    start = (datetime.now() - delta).timestamp()
    end = datetime.now().timestamp()
    lb = _prom_server_labels(server)

    with PromConnection(ci) as conn:
        params = {
            "query": (
                f'label_replace('
                f'windows_logical_disk_free_bytes{{{lb}, volume=~"[A-Z]:"}}, '
                f'"mountpoint", "$1", "volume", "(.+)")'
            ),
            "start": start,
            "end": end,
            "step": "15s",
        }
        response = conn.get("/query_range", params=params)
        data = validate_prometheus_response(response)
        results = data.get("result")
        if results and len(results) > 0:
            disk_usage_data = results

    return disk_usage_data


def fetch_net_io(request, server, ci):
    if _server_is_windows(server):
        return _fetch_net_io_windows(request, server, ci)
    return _fetch_net_io_linux(request, server, ci)


def _fetch_net_io_linux(request, server, ci):
    net_io_data = {
        "rx": [],
        "tx": [],
    }
    delta = timedelta(days=1)
    start = (datetime.now() - delta).timestamp()
    end = datetime.now().timestamp()
    step = 120

    with PromConnection(ci) as conn:
        # disk writes
        params = {
            "query": f'irate(node_network_transmit_bytes_total{{'
                     f'server="{server.id}", '
                     f'device!~"lo|bond[0-9]|cbr[0-9]|veth.*"}}[5m]) > 0',
            "start": start,
            "end": end,
            "step": step,
        }
        response = conn.get("/query_range", params=params)
        data = validate_prometheus_response(response)
        result = data.get('result')
        if result and len(result) > 0:
            net_io_data["tx"] = result[0]['values']

        # disk reads
        params = {
            "query": f'irate(node_network_receive_bytes_total{{'
                     f'server="{server.id}", '
                     f'device!~"lo|bond[0-9]|cbr[0-9]|veth.*"}}[5m]) > 0',
            "start": start,
            "end": end,
            "step": step,
        }
        response = conn.get("/query_range", params=params)
        data = validate_prometheus_response(response)
        result = data.get('result')
        if result and len(result) > 0:
            net_io_data["rx"] = result[0]['values']

    return net_io_data


def _fetch_net_io_windows(request, server, ci):
    net_io_data = {
        "rx": [],
        "tx": [],
    }
    delta = timedelta(days=1)
    start = (datetime.now() - delta).timestamp()
    end = datetime.now().timestamp()
    step = 120
    lb = _prom_server_labels(server)
    nic_filter = f'{lb}, nic!~".*[Ll]oopback.*|.*[Ii][Ss][Aa][Tt][Aa][Pp].*"'  # may not be supported in old prom

    with PromConnection(ci) as conn:
        queries_tx = [
            f'sum by (server) (irate(windows_net_bytes_sent_total{{{nic_filter}}}[5m]))',
            f'sum by (server) (irate(windows_net_bytes_sent_total{{{lb}}}[5m]))',
        ]
        queries_rx = [
            f'sum by (server) (irate(windows_net_bytes_received_total{{{nic_filter}}}[5m]))',
            f'sum by (server) (irate(windows_net_bytes_received_total{{{lb}}}[5m]))',
        ]

        for q in queries_tx:
            params = {
                "query": q,
                "start": start,
                "end": end,
                "step": step,
            }
            response = conn.get("/query_range", params=params)
            data = validate_prometheus_response(response)
            result = data.get("result")
            if result and len(result) > 0:
                net_io_data["tx"] = result[0]["values"]
                break

        for q in queries_rx:
            params = {
                "query": q,
                "start": start,
                "end": end,
                "step": step,
            }
            response = conn.get("/query_range", params=params)
            data = validate_prometheus_response(response)
            result = data.get("result")
            if result and len(result) > 0:
                net_io_data["rx"] = result[0]["values"]
                break

    return net_io_data


def fetch_health(request, server, ci):
    health_data = {
        'up': False,
        'uptime': -1,
        'disk_total': -1,
        'core_count': -1,
        'mem_total': -1,
        'load': 0,
        'scraper_cpu_use': -1
    }
    is_win = _server_is_windows(server)
    with PromConnection(ci) as conn:
        uptime = __query_uptime(server, conn, is_win)
        if uptime:
            health_data['uptime'] = math.ceil(float(uptime))

        core_count = __query_core_count(server, conn, is_win)
        if core_count is not None:
            health_data['core_count'] = core_count

        disk_total = __query_disk_total(server, conn, is_win)
        if disk_total:
            health_data['disk_total'] = disk_total

        mem_total = __query_mem_total(server, conn, is_win)
        if mem_total:
            health_data['mem_total'] = mem_total

        load = __query_load(server, conn, is_win)
        if load is not None:
            health_data['load'] = load

        scraper_cpu_use = __query_scraper_cpu_use(server, conn, is_win)
        if scraper_cpu_use:
            health_data['scraper_cpu_use'] = scraper_cpu_use

    return health_data


def __query_up_status(labels, conn):
    # Get up status
    params = {
        "query": f'up{{{labels}}}'
    }
    response = conn.get("/query", params=params, timeout=REQUEST_TIMEOUT)
    data = validate_prometheus_response(response)
    if data is None:
        return False

    results = data.get('result')
    if results and len(results) > 0 and results[0]['value'][1] == "1":
        return True
    else:
        return False


def __query_uptime(server, conn, is_windows=False):
    lb = _prom_server_labels(server)
    if is_windows:
        q = (
            f"(time() - (windows_system_boot_time_timestamp{{{lb}}} or "
            f"windows_os_boot_timestamp_seconds{{{lb}}}))"
        )
    else:
        q = f"(time() - node_boot_time_seconds{{{lb}}})"
    params = {"query": q}
    response = conn.get("/query", params=params, timeout=REQUEST_TIMEOUT)
    data = validate_prometheus_response(response)
    if data is None:
        return -1

    results = data.get('result')
    if results and len(results) > 0:
        return results[0]['value'][1]
    else:
        return -1


def __query_core_count(server, conn, is_windows=False):
    """Logical CPU count from node_exporter or windows_exporter."""
    lb = _prom_server_labels(server)
    if is_windows:
        q = f'count(windows_cpu_time_total{{{lb}, mode="idle"}})'
    else:
        q = f'count(node_cpu_seconds_total{{{lb}, mode="idle"}})'
    params = {"query": q}
    response = conn.get("/query", params=params, timeout=REQUEST_TIMEOUT)
    data = validate_prometheus_response(response)
    if data is None:
        return None

    results = data.get('result')
    if not results:
        return None
    try:
        n = int(float(results[0]['value'][1]))
    except (TypeError, ValueError):
        return None
    if n < 0:
        return None
    return n


def __query_disk_total(server, conn, is_windows=False):
    if is_windows:
        lb = _prom_server_labels(server)
        q = (
            f'sum(windows_logical_disk_size_bytes{{{lb}, volume=~"[A-Z]:"}})'
        )
    else:
        q = (
            f'sum(node_filesystem_size_bytes{{server="{server.id}", '
            f'device=~"/dev/.*"}})'
        )
    params = {"query": q}
    response = conn.get("/query", params=params, timeout=REQUEST_TIMEOUT)
    data = validate_prometheus_response(response)
    if data is None:
        return -1

    results = data.get('result')
    if results and len(results) > 0:
        return results[0]['value'][1]
    else:
        return -1


def __query_mem_total(server, conn, is_windows=False):
    if is_windows:
        lb = _prom_server_labels(server)
        q = (
            f"(windows_memory_physical_total_bytes{{{lb}}} or "
            f"windows_cs_physical_memory_bytes{{{lb}}})"
        )
    else:
        q = f'node_memory_MemTotal_bytes{{server="{server.id}"}}'
    params = {"query": q}
    response = conn.get("/query", params=params, timeout=REQUEST_TIMEOUT)
    data = validate_prometheus_response(response)
    if data is None:
        return -1

    results = data.get('result')
    if results and len(results) > 0:
        return results[0]['value'][1]
    else:
        return -1


def __query_load(server, conn, is_windows=False):
    """Load average / CPU pressure as a fraction of capacity (0–1+ on Linux)."""
    lb = _prom_server_labels(server)
    if is_windows:
        params = {
            "query": _windows_cpu_busy_fraction_promql(server, "5m"),
        }
    else:
        params = {
            "query": (
                f'node_load1{{{lb}}} / scalar(clamp_min('
                f'count(node_cpu_seconds_total{{{lb}, mode="idle"}}), 1))'
            ),
        }
    response = conn.get("/query", params=params, timeout=REQUEST_TIMEOUT)
    data = validate_prometheus_response(response)
    if data is None:
        return None

    results = data.get('result')
    if not results:
        return None
    try:
        v = float(results[0]['value'][1])
    except (TypeError, ValueError):
        return None
    if math.isnan(v) or math.isinf(v):
        return None
    return v


def __query_scraper_cpu_use(server, conn, is_windows=False):
    if is_windows:
        lb = _prom_server_labels(server)
        q = (
            f'sum(rate(windows_exporter_perflib_snapshot_duration_seconds{{{lb}}}[5m]))'
        )
    else:
        q = (
            f'sum(irate(node_scrape_collector_duration_seconds'
            f'{{server="{server.id}"}}[5m]))'
        )
    params = {"query": q}
    response = conn.get("/query", params=params, timeout=REQUEST_TIMEOUT)
    data = validate_prometheus_response(response)
    if data is None:
        return -1

    results = data.get('result')
    if results and len(results) > 0:
        return results[0]['value'][1]
    else:
        return 0


def validate_prometheus_response(response):
    data = None
    try:
        payload = response.json()
    except ValueError as value_error:
        logger.error(value_error)
        return None

    # Prometheus is returning valid JSON, but there is no data which
    # indicates there was an error processing the query.
    if "data" not in payload:
        import inspect
        caller = inspect.currentframe().f_back.f_code.co_name
        logger.error(f"{caller}: {payload.get('error')}")
        return None

    return payload["data"]
