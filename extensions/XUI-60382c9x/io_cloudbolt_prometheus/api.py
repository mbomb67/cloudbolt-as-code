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
DEFAULT_EXPORTER_PORT = 9100
DEFAULT_WINDOWS_EXPORTER_PORT = 9182
MONITORING_LABEL = "monitor"
REQUEST_TIMEOUT = 3.0
PROME_JOB = "cloudbolt"


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
        instance = f"{server.ip}:{DEFAULT_EXPORTER_PORT}"
        return fetch_function(request, server, ci)
    else:
        # TODO: Invalid metric requested
        logger.error(f"Invalid metric type requested: '{metric_type}'")
        pass

    return {}


def fetch_cpu_load(request, server, ci, labels):
    cpu_load_data = {
        "load1": [],
        "load5": [],
        "load15": []
    }
    labels = f'job="{PROME_JOB}", server="{server.id}"'
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


def fetch_disk_io(request, server, ci, labels):
    disk_io_data = {
        "reads": [],
        "writes": [],
        "total": []
    }
    delta = timedelta(days=1)
    start = (datetime.now() - delta).timestamp()
    end = datetime.now().timestamp()
    step = 60

    with PromConnection(ci) as conn:
        # disk writes
        params = {
            "query": f'sum by (server) (irate(node_disk_written_bytes_total{{server="{server.id}"}}[5m]))',
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
            "query": f'sum by (server) (irate(node_disk_read_bytes_total{{server="{server.id}"}}[5m]))',
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
            "query": f'sum by (instance) (irate(node_disk_read_bytes_total{{server="{server}"}}[5m])) + sum by (instance) (irate(node_disk_written_bytes_total{{server="{server}"}}[5m]))',
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


def fetch_iops(request, server, ci):
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

    return iops_data;


def fetch_memory(request, server, ci):
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


def fetch_disk_usage(request, server, ci):
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


def fetch_net_io(request, server, ci):
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

    return net_io_data;


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
    with PromConnection(ci) as conn:
        uptime = __query_uptime(server, conn)
        if uptime:
            health_data['uptime'] = math.ceil(float(uptime))

        core_count = __query_core_count(server, conn)
        if core_count is not None:
            health_data['core_count'] = core_count

        disk_total = __query_disk_total(server, conn)
        if disk_total:
            health_data['disk_total'] = disk_total

        mem_total = __query_mem_total(server, conn)
        if mem_total:
            health_data['mem_total'] = mem_total

        load = __query_load(server, conn)
        if load is not None:
            health_data['load'] = load

        scraper_cpu_use = __query_scraper_cpu_use(server, conn)
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


def __query_uptime(server, conn):
    labels = f'job="{PROME_JOB}", server="{server.id}"'
    params = {
        "query": f'(time() - node_boot_time_seconds{{{labels}}})'
    }
    response = conn.get("/query", params=params, timeout=REQUEST_TIMEOUT)
    data = validate_prometheus_response(response)
    if data is None:
        return -1

    results = data.get('result')
    if results and len(results) > 0:
        return results[0]['value'][1]
    else:
        return -1


def __query_core_count(server, conn):
    """CPU core count from node_exporter via node_cpu_seconds_total (idle per core)."""
    labels = f'job="{PROME_JOB}", server="{server.id}"'
    params = {
        "query": f'count(node_cpu_seconds_total{{{labels}, mode="idle"}})'
    }
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


def __query_disk_total(server, conn):
    params = {
        "query": f'sum(node_filesystem_size_bytes{{server="{server.id}", '
                 f'device=~"/dev/.*"}})'
    }
    response = conn.get("/query", params=params, timeout=REQUEST_TIMEOUT)
    data = validate_prometheus_response(response)
    if data is None:
        return -1

    results = data.get('result')
    if results and len(results) > 0:
        return results[0]['value'][1]
    else:
        return -1


def __query_mem_total(server, conn):
    params = {
        "query": f'node_memory_MemTotal_bytes{{server="{server.id}"}}'
    }
    response = conn.get("/query", params=params, timeout=REQUEST_TIMEOUT)
    data = validate_prometheus_response(response)
    if data is None:
        return -1

    results = data.get('result')
    if results and len(results) > 0:
        return results[0]['value'][1]
    else:
        return -1


def __query_load(server, conn):
    """Load average as a fraction of total CPU (0–1+); same core count as __query_core_count."""
    labels = f'job="{PROME_JOB}", server="{server.id}"'
    params = {
        "query": (
            f'node_load1{{{labels}}} / scalar(clamp_min('
            f'count(node_cpu_seconds_total{{{labels}, mode="idle"}}), 1))'
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


def __query_scraper_cpu_use(server, conn):
    params = {
        "query": f'sum(irate(node_scrape_collector_duration_seconds'
                 f'{{server="{server.id}"}}[5m]))'
    }
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


@json_view
@login_not_required
def prometheus_targets(request, **kwargs):
    config = []
    active_servers = Server.objects.filter(
        status='ACTIVE', power_status="POWERON")

    group_targets = dict()
    server: Server
    for server in active_servers:
        if "monitor" in server.labels and server.ip:
            if server.os_family.name == "Windows":
                port = DEFAULT_WINDOWS_EXPORTER_PORT
            else:
                port = DEFAULT_EXPORTER_PORT
            group = server.group.global_id
            environment = server.environment.global_id
            rh = server.environment.resource_handler.global_id
            config.append(
                {
                    "labels": {
                        "group": group,
                        "environment": environment,
                        "server": f"{server.id}",
                        "name": f"{server.hostname}",
                        "rh": rh,
                        "job": "cloudbolt"
                    },
                    "targets": [
                        f"{server.ip}:{port}"
                    ]
                }
            )

    return config
