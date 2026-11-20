"""Monitoring data for a resource, combined over the servers it deployed.

The resource's non-historical servers are sorted into source groups: one per (resource handler,
namespace) for OpenShift Virtualization VMs, read from the cluster's Thanos Querier, and one per
OS family for servers tagged monitor, read from the MONITORING Prometheus. Each metric is one
PromQL per group that names every server of the group; the answer is split per server here.
The tier of a server is the name of the blueprint build item that created it.

A group that cannot be asked is logged and skipped, so the others still answer.
"""

import time

from infrastructure.models import Server
from resources.models import Resource
from utilities.decorators import json_view
from utilities.logger import ThreadLogger
from utilities.models import ConnectionInfo
from xui.io_cloudbolt_prometheus import kubevirt_metrics
from xui.io_cloudbolt_prometheus import resource_queries as queries
from xui.io_cloudbolt_prometheus import resource_rollup as rollup
from xui.io_cloudbolt_prometheus.alert_rows import alert_row, ordered
from xui.io_cloudbolt_prometheus.metrics import (
    CONNECTION_INFO_NAME,
    MONITORING_LABEL,
    PROME_JOB,
    _server_is_windows,
    validate_prometheus_response,
)
from xui.io_cloudbolt_prometheus.prom_api_wrapper import PromConnection

logger = ThreadLogger(__name__)

STEP_SECONDS = 300
DAY_SECONDS = 24 * 60 * 60
QUERY_TIMEOUT_SECONDS = 10
GIB = 2 ** 30
POWER_STATES = {"POWERON": "Running", "POWEROFF": "Stopped", "SUSPENDED": "Suspended"}

# metric type -> (queries to run, how a tier combines its servers, unit)
SERIES_METRICS = {
    "cpu": (["cpu"], "avg", "%"),
    "memory": (["mem"], "avg", "%"),
    "net_io": (["net_rx", "net_tx"], "sum", "bytes/s"),
    "disk_io": (["disk_read", "disk_write"], "sum", "bytes/s"),
}

class SourceDown(Exception):
    """A source answered with a connection error, a timeout or a 5xx; its other queries are skipped."""


KUBEVIRT = "kubevirt"
LINUX = "linux"
WINDOWS = "windows"


# --- members --------------------------------------------------------------------------------

class Member:
    """One server of the resource and where its metrics come from."""

    def __init__(self, server):
        self.server = server
        self.id = server.id
        self.hostname = server.hostname
        item = server.service_item
        self.tier = rollup.tier_of(item.name if item else "")
        labels = [tag.name for tag in server.tags.all()]
        if kubevirt_metrics.is_kubevirt_server(server):
            self.kind = KUBEVIRT
            self.namespace = server.ovirtserverinfo.namespace
        elif MONITORING_LABEL in labels:
            self.kind = WINDOWS if _server_is_windows(server) else LINUX
            self.namespace = ""
        else:
            self.kind = None
            self.namespace = ""
        if server.status != "ACTIVE":
            self.state = str(server.get_status_display())
        else:
            self.state = POWER_STATES.get(server.power_status, "Unknown")

    def info(self):
        """The server as the rollup sees it; metrics are filled in from the sources."""
        return {
            "id": self.id,
            "hostname": self.hostname,
            "url": self.server.get_absolute_url(),
            "tier": self.tier,
            "state": self.state,
            "monitored": self.kind is not None,
            "running": self.state == "Running",
            "cpu": None, "mem": None, "disk_used": None, "disk_total": None,
            "net_rx": None, "net_tx": None,
        }


def load_members(resource, profile):
    """The resource's live servers the profile may see, in blueprint deploy order."""
    servers = (
        Server.objects_for_profile(profile)
        .filter(resource=resource)
        .exclude(status="HISTORICAL")
        .select_related("service_item", "resource_handler", "os_family", "ovirtserverinfo")
        .prefetch_related("tags")
        .order_by("service_item__deploy_seq", "id")
    )
    members, seen = [], set()
    for server in servers:
        if server.id not in seen:
            seen.add(server.id)
            members.append(Member(server))
    return members


# --- source groups --------------------------------------------------------------------------

def _window():
    """The last 24 hours with both ends on a step boundary, so every source lines up."""
    end = int(time.time() // STEP_SECONDS * STEP_SECONDS)
    return end - DAY_SECONDS, end


class SourceGroup:
    """Members that one set of PromQL can ask about. Subclasses say how to run a query."""

    label = ""

    def __init__(self, members):
        self.members = members
        # key -> members: two servers can share a hostname inside one namespace; both get the answer.
        self.by_key = {}
        for member in members:
            self.by_key.setdefault(self.key_of(member), []).append(member)
        for key, same in self.by_key.items():
            if len(same) > 1:
                logger.warning(f"Monitoring: {len(same)} servers share the name {key}; they show the same numbers")
        self.promql = self.build_queries()

    def key_of(self, member):
        raise NotImplementedError

    def build_queries(self):
        raise NotImplementedError

    def run_range(self, query, start, end, step):
        """The result list of a range query."""
        raise NotImplementedError

    def run_instant(self, query):
        """The result list of an instant query."""
        raise NotImplementedError

    def first_answer(self, metric, run):
        """The answer of the metric's first query, plus what later queries return for the servers it missed."""
        answer, seen = [], set()
        for query in self.promql[metric]:
            for entry in run(query):
                key = (entry.get("metric") or {}).get(self.label)
                if key not in seen:
                    seen.add(key)
                    answer.append(entry)
            if len(seen & set(self.by_key)) >= len(self.by_key):
                break
        return answer

    def range_series(self, metric, start, end, step):
        """{server id: [[ms, value]]} for one metric."""
        result = self.first_answer(metric, lambda query: self.run_range(query, start, end, step))
        out = {}
        for key, points in rollup.matrix_to_series(result, self.label).items():
            for member in self.by_key.get(key, []):
                out[member.id] = self.convert_series(metric, member, points)
        return out

    def instant_values(self, metric):
        """{server id: value} for one metric."""
        result = self.first_answer(metric, self.run_instant)
        return {
            member.id: value
            for key, value in rollup.vector_to_values(result, self.label).items()
            for member in self.by_key.get(key, [])
        }

    def convert_series(self, metric, member, points):
        if metric in ("cpu", "mem"):
            return rollup.clamp_series(points)
        return points

    def instants(self):
        """{server id: {cpu, mem, disk_used, disk_total, net_rx, net_tx}}."""
        names = {"cpu": "cpu", "mem": "mem", "disk_used": "fs_used", "disk_total": "fs_total",
                 "net_rx": "net_rx", "net_tx": "net_tx"}
        out = {m.id: {} for m in self.members}
        for field, metric in names.items():
            for server_id, value in self.instant_values(metric).items():
                out[server_id][field] = self.convert_instant(field, self.member(server_id), value)
        for member in self.members:
            self.fill_disk_total(member, out[member.id])
        return out

    def member(self, server_id):
        return next(m for m in self.members if m.id == server_id)

    def convert_instant(self, field, member, value):
        return rollup.percent(value) if field in ("cpu", "mem") else value

    def fill_disk_total(self, member, values):
        pass


class KubeVirtGroup(SourceGroup):
    """VMs of one namespace of one cluster, read from its Thanos Querier."""

    label = queries.KUBEVIRT_KEY

    def __init__(self, members, clients):
        self.namespace = members[0].namespace
        self.handler_id = members[0].server.resource_handler_id
        self.clients = clients  # one ThanosClient per resource handler, shared by its namespace groups
        super().__init__(members)

    def key_of(self, member):
        return member.hostname

    def build_queries(self):
        selector = queries.kubevirt_selector(self.namespace, [m.hostname for m in self.members])
        return queries.kubevirt_queries(selector)

    @property
    def client(self):
        if self.handler_id not in self.clients:
            handler = self.members[0].server.resource_handler.cast()
            self.clients[self.handler_id] = kubevirt_metrics.ThanosClient(
                handler, f"resource handler {self.handler_id}"
            )
        return self.clients[self.handler_id]

    def _checked(self, result):
        if self.client.failed:
            raise SourceDown(f"the cluster of resource handler {self.handler_id} is not answering")
        return result

    def run_range(self, query, start, end, step):
        return self._checked(self.client.range_between(query, start, end, step))

    def run_instant(self, query):
        return self._checked(self.client.instant(query))

    def convert_series(self, metric, member, points):
        if metric == "cpu":
            # The query counts cores in use; the tab shows the share of the VM's vCPUs.
            points = rollup.scale_series(points, 100.0 / max(member.server.cpu_cnt or 1, 1))
        return super().convert_series(metric, member, points)

    def convert_instant(self, field, member, value):
        if field == "cpu":
            value = value * 100.0 / max(member.server.cpu_cnt or 1, 1)
        return super().convert_instant(field, member, value)

    def fill_disk_total(self, member, values):
        # The size CloudBolt knows wins over what the guest agent reports, as on the server tab.
        if member.server.disk_size:
            values["disk_total"] = float(member.server.disk_size) * GIB


class LabGroup(SourceGroup):
    """Servers of one OS family that the MONITORING Prometheus scrapes."""

    label = queries.LAB_KEY

    def __init__(self, members, kind, connection_info):
        self.kind = kind
        self.connection_info = connection_info
        super().__init__(members)

    def key_of(self, member):
        return str(member.id)

    def build_queries(self):
        selector = queries.lab_selector([m.id for m in self.members], PROME_JOB)
        build = queries.windows_queries if self.kind == WINDOWS else queries.linux_queries
        return build(selector)

    def _get(self, path, params):
        with PromConnection(self.connection_info) as conn:
            response = conn.get(path, params=params, timeout=QUERY_TIMEOUT_SECONDS)
        data = validate_prometheus_response(response)
        return (data or {}).get("result") or []

    def run_range(self, query, start, end, step):
        return self._get("/query_range", {"query": query, "start": start, "end": end, "step": step})

    def run_instant(self, query):
        return self._get("/query", {"query": query})


def lab_alert_rows(members, connection_info):
    """[(member, row)] from Prometheus's own alerts for these servers, or None when it cannot be asked."""
    by_key = {str(m.id): m for m in members}
    with PromConnection(connection_info) as conn:
        response = conn.get("/alerts", timeout=QUERY_TIMEOUT_SECONDS)
    data = validate_prometheus_response(response)
    if data is None:
        return None
    return [
        (by_key[alert.get("labels", {}).get("server")],
         alert_row(alert.get("labels", {}), alert.get("annotations"), alert.get("state"), alert.get("activeAt")))
        for alert in data.get("alerts", [])
        if alert.get("labels", {}).get("server") in by_key
    ]


def _connection_info():
    try:
        return ConnectionInfo.objects.get(name=CONNECTION_INFO_NAME)
    except ConnectionInfo.DoesNotExist:
        logger.warning(f"Monitoring: no Connection Info named {CONNECTION_INFO_NAME}")
        return None


def source_groups(members):
    """One group per (handler, namespace) of VMs and one per OS family of tagged servers."""
    vms, lab = {}, {LINUX: [], WINDOWS: []}
    for member in members:
        if member.kind == KUBEVIRT:
            vms.setdefault((member.server.resource_handler_id, member.namespace), []).append(member)
        elif member.kind in lab:
            lab[member.kind].append(member)
    clients = {}
    groups = [KubeVirtGroup(group_members, clients) for group_members in vms.values()]
    if lab[LINUX] or lab[WINDOWS]:
        connection_info = _connection_info()
        if connection_info is not None:
            groups += [LabGroup(lab[kind], kind, connection_info) for kind in (LINUX, WINDOWS) if lab[kind]]
    return groups


def kubevirt_alert_rows(groups):
    """[(member, row)] for the VM groups of one cluster, from one alerts call; None when it cannot be asked."""
    wanted, by_key = {}, {}
    for group in groups:
        wanted.setdefault(group.namespace, set()).update(m.hostname for m in group.members)
        for member in group.members:
            by_key.setdefault((group.namespace, member.hostname), []).append(member)
    found = kubevirt_metrics.fetch_alert_rows(groups[0].client, wanted)
    if found is None:
        return None
    return [(member, row) for namespace, hostname, row in found for member in by_key[(namespace, hostname)]]


def _describe(group):
    return f"{type(group).__name__} of {len(group.members)} servers (first: {group.members[0].hostname})"


# --- payloads -------------------------------------------------------------------------------

def summary_payload(members, tier):
    infos = {m.id: m.info() for m in members}
    for group in source_groups(members):
        try:
            for server_id, values in group.instants().items():
                infos[server_id].update(values)
        except Exception as error:  # one source down must not hide the rest
            logger.warning(f"Monitoring: summary failed for {_describe(group)}: {error}")
    for info in infos.values():
        if not info["running"]:
            # A server that just stopped still has samples in the last minutes; it is not in use now.
            for field in ("cpu", "mem", "net_rx", "net_tx"):
                info[field] = None
    return rollup.build_summary(list(infos.values()), tier)


def series_payload(members, tier, metric_type):
    metrics, how, unit = SERIES_METRICS[metric_type]
    scope = [m for m in members if tier is None or m.tier == tier]
    start, end = _window()
    per_server = {}
    for group in source_groups(scope):
        try:
            parts = [group.range_series(metric, start, end, STEP_SECONDS) for metric in metrics]
        except Exception as error:
            logger.warning(f"Monitoring: {metric_type} failed for {_describe(group)}: {error}")
            continue
        for server_id in {sid for part in parts for sid in part}:
            if len(parts) == 1:
                per_server[server_id] = parts[0].get(server_id, [])
            else:
                per_server[server_id] = rollup.aggregate([part.get(server_id, []) for part in parts], "sum")[0]
    return rollup.series_payload([m.info() for m in scope], per_server, how, unit, tier)


def alerts_payload(members, tier):
    scope = [m for m in members if tier is None or m.tier == tier]
    found, answered = [], False
    lab = [m for m in scope if m.kind in (LINUX, WINDOWS)]
    clusters = {}
    for group in source_groups([m for m in scope if m.kind == KUBEVIRT]):
        clusters.setdefault(group.handler_id, []).append(group)
    sources = [lambda groups=groups: kubevirt_alert_rows(groups) for groups in clusters.values()]
    if lab:
        connection_info = _connection_info()
        if connection_info is not None:
            sources.append(lambda: lab_alert_rows(lab, connection_info))
    for fetch in sources:
        try:
            rows = fetch()
        except Exception as error:
            logger.warning(f"Monitoring: alerts failed for a source of {len(scope)} servers: {error}")
            continue
        if rows is None:
            continue
        answered = True
        for member, row in rows:
            found.append(dict(
                row, server_id=member.id, hostname=member.hostname, tier=member.tier,
                url=member.server.get_absolute_url(),
            ))
    return {
        "available": answered,
        "monitored_count": sum(1 for m in scope if m.kind is not None),
        "alerts": ordered(found),
    }


def _summary(members, tier, request):
    return summary_payload(members, tier)


def _series(metric_type):
    return lambda members, tier, request: series_payload(members, tier, metric_type)


def _alerts(members, tier, request):
    return alerts_payload(members, tier)


PAYLOADS = {"summary": _summary, "alerts": _alerts, **{name: _series(name) for name in SERIES_METRICS}}


@json_view
def resource_metrics_dispatch(request, resource_id, metric_type):
    """Metrics of a resource's servers. {} when the resource does not exist or may not be viewed.

    metric_type: summary, cpu, memory, net_io, disk_io, alerts. ?tier= narrows the answer to the
    servers of one blueprint build item name; it only filters the server list here and is never
    part of a query.
    """
    profile = request.get_user_profile()
    # Resource.objects_for_profile keeps the resources where the profile has resource.view.
    resource = Resource.objects_for_profile(profile).filter(id=resource_id).first()
    if resource is None:
        return {}
    build = PAYLOADS.get(metric_type)
    if build is None:
        logger.error(f"Invalid resource metric type requested: '{metric_type}'")
        return {}
    tier = request.GET.get("tier") or None
    return build(load_members(resource, profile), tier, request)
