"""Turns per-server numbers into the per-tier numbers of the resource Monitoring tab.

No Django imports, so it can be tested on its own. A server here is a plain dict:
    id, hostname, url, tier, state, monitored, running (bool),
    cpu, mem (percent 0-100 or None), disk_used, disk_total, net_rx, net_tx (bytes or None)
A series is a list of [timestamp_ms, value] points.
"""

import math

OTHER_TIER = "Other"
OTHER_TIERS = "Other tiers"
KEEP_TIERS = 5
MAX_TIERS = 6


# --- values ---------------------------------------------------------------------------------

def number(value):
    """A finite float, or None for anything else (missing, NaN, infinite, not a number)."""
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def percent(value):
    """A percentage clamped to 0-100 and rounded to one decimal, or None."""
    value = number(value)
    return None if value is None else round(min(max(value, 0.0), 100.0), 1)


def _round(value):
    return None if value is None else round(value, 2)


def _mean(values):
    return sum(values) / len(values) if values else None


def _sum(values):
    return sum(values) if values else None


# --- Prometheus answers ---------------------------------------------------------------------

def matrix_to_series(result, label):
    """{label value: [[ms, value]]} from the result list of a range query; NaN points dropped."""
    out = {}
    for entry in result or []:
        key = (entry.get("metric") or {}).get(label)
        if key is None:
            continue
        points = []
        for pair in entry.get("values") or []:
            value = number(pair[1])
            if value is not None:
                points.append([int(float(pair[0]) * 1000), value])
        out[key] = points
    return out


def vector_to_values(result, label):
    """{label value: float} from the result list of an instant query."""
    out = {}
    for entry in result or []:
        key = (entry.get("metric") or {}).get(label)
        value = number((entry.get("value") or [None, None])[1])
        if key is not None and value is not None:
            out[key] = value
    return out


def drop_before(points, cutoff_ms):
    """The points at or after cutoff_ms. A server recreated under an old name must not show its predecessor.

    cutoff_ms None keeps everything (the server's add date is unknown).
    """
    if cutoff_ms is None:
        return points
    return [point for point in points if point[0] >= cutoff_ms]


def break_gaps(points, step):
    """Insert one null point a step after any point followed by more than 1.5 steps of nothing.

    A line chart then breaks there instead of drawing a slope across the gap. Points are
    [ms, value] or, for a band, [ms, low, high]; the null point has the same length. A series never
    starts or ends with a null, because nulls only go between two points. step is in seconds.
    """
    step_ms = step * 1000
    out = []
    for point in points:
        if out and point[0] - out[-1][0] > 1.5 * step_ms:
            out.append([out[-1][0] + step_ms] + [None] * (len(point) - 1))
        out.append(point)
    return out


def scale_series(points, factor):
    return [[ts, value * factor] for ts, value in points]


def clamp_series(points):
    return [[ts, min(max(value, 0.0), 100.0)] for ts, value in points]


# --- tiers ----------------------------------------------------------------------------------

def tier_of(name):
    return (name or "").strip() or OTHER_TIER


def tier_names(servers):
    """Tier names in the order the servers first mention them, with "Other" last."""
    names = []
    for server in servers:
        if server["tier"] not in names:
            names.append(server["tier"])
    if OTHER_TIER in names:
        names.remove(OTHER_TIER)
        names.append(OTHER_TIER)
    return names


def group_by_tier(servers):
    """{tier: [servers]}, tiers in tier_names order."""
    groups = {name: [] for name in tier_names(servers)}
    for server in servers:
        groups[server["tier"]].append(server)
    return groups


def merge_map(groups):
    """{tier: name shown in the charts}.

    Up to MAX_TIERS tiers show as they are. With more, the KEEP_TIERS with the most servers
    (ties keep their order) stay and the rest become one "Other tiers" line.
    """
    names = list(groups)
    if len(names) <= MAX_TIERS:
        return {name: name for name in names}
    ranked = sorted(range(len(names)), key=lambda i: (-len(groups[names[i]]), i))
    keep = {names[i] for i in ranked[:KEEP_TIERS]}
    return {name: (name if name in keep else OTHER_TIERS) for name in names}


# --- headroom -------------------------------------------------------------------------------

def headroom(cpu, mem):
    """The higher of CPU and memory use, the figure servers are ranked by; None when both unknown."""
    values = [v for v in (cpu, mem) if v is not None]
    return max(values) if values else None


def rank_by_headroom(servers):
    """Highest headroom use first; servers without metrics last, then by hostname."""
    return sorted(
        servers,
        key=lambda s: (s.get("headroom") is None, -(s.get("headroom") or 0), s["hostname"].lower()),
    )


def least_headroom(servers):
    """{server, metric, value} for the server closest to a limit, or None when nothing is known."""
    for server in rank_by_headroom(servers):
        if server.get("headroom") is None:
            return None
        cpu, mem = server.get("cpu"), server.get("mem")
        metric = "memory" if mem is not None and (cpu is None or mem >= cpu) else "cpu"
        return {"server": server["hostname"], "url": server.get("url"), "tier": server["tier"],
                "metric": metric, "value": server["headroom"]}
    return None


# --- summary --------------------------------------------------------------------------------

def _worst(servers, key):
    """(maximum, the server it is on) for a metric; (None, None) when no server has it."""
    known = [s for s in servers if s.get(key) is not None]
    if not known:
        return None, None
    top = max(known, key=lambda s: s[key])
    return top[key], top


def tier_row(name, servers):
    cpus = [s["cpu"] for s in servers if s.get("cpu") is not None]
    mems = [s["mem"] for s in servers if s.get("mem") is not None]
    cpu_max, cpu_server = _worst(servers, "cpu")
    mem_max, mem_server = _worst(servers, "mem")
    disks = [s for s in servers if s.get("disk_used") is not None and s.get("disk_total")]
    rx = [s["net_rx"] for s in servers if s.get("net_rx") is not None]
    tx = [s["net_tx"] for s in servers if s.get("net_tx") is not None]
    return {
        "name": name,
        "server_count": len(servers),
        "running_count": sum(1 for s in servers if s.get("running")),
        "cpu_avg": _round(_mean(cpus)),
        "cpu_max": _round(cpu_max),
        "cpu_max_server": cpu_server["hostname"] if cpu_server else None,
        "cpu_max_server_url": cpu_server.get("url") if cpu_server else None,
        "mem_avg": _round(_mean(mems)),
        "mem_max": _round(mem_max),
        "mem_max_server": mem_server["hostname"] if mem_server else None,
        "mem_max_server_url": mem_server.get("url") if mem_server else None,
        "disk_used": _sum([s["disk_used"] for s in disks]),
        "disk_total": _sum([s["disk_total"] for s in disks]),
        "net_rx": _sum(rx),
        "net_tx": _sum(tx),
    }


def server_row(server):
    keys = ("id", "hostname", "url", "tier", "state", "monitored", "cpu", "mem",
            "disk_used", "disk_total", "net_rx", "net_tx", "headroom")
    return {key: server.get(key) for key in keys}


def build_summary(servers, tier=None):
    """The "summary" payload.

    tiers always lists every tier, so the tab can offer them; servers and tiles cover the tier
    asked for, or everything when tier is None. A tier no server has gives an empty scope.
    """
    for server in servers:
        server["headroom"] = headroom(server.get("cpu"), server.get("mem"))
    groups = group_by_tier(servers)
    scope = servers if tier is None else [s for s in servers if s["tier"] == tier]
    cpus = [s["cpu"] for s in scope if s.get("cpu") is not None]
    mems = [s["mem"] for s in scope if s.get("mem") is not None]
    return {
        "tiers": [tier_row(name, members) for name, members in groups.items()],
        "servers": [server_row(s) for s in rank_by_headroom(scope)],
        "tiles": {
            "servers_total": len(scope),
            "servers_running": sum(1 for s in scope if s.get("running")),
            "cpu_avg": _round(_mean(cpus)),
            "mem_avg": _round(_mean(mems)),
            "least_headroom": least_headroom(scope),
        },
    }


# --- chart series ---------------------------------------------------------------------------

def aggregate(series_list, how, step=None):
    """Combine several servers' series per timestamp.

    how "avg": data is the mean and band is [ts, lowest, highest]; how "sum": data is the total
    and band is None. A timestamp counts the servers that have a point there. With step (seconds),
    gaps longer than 1.5 steps are broken by a null point.
    """
    by_time = {}
    for points in series_list:
        for ts, value in points:
            by_time.setdefault(ts, []).append(value)
    data, band = [], []
    for ts in sorted(by_time):
        values = by_time[ts]
        if how == "avg":
            data.append([ts, _round(_mean(values))])
            band.append([ts, _round(min(values)), _round(max(values))])
        else:
            data.append([ts, _round(sum(values))])
    if step:
        data = break_gaps(data, step)
        band = break_gaps(band, step)
    return data, (band if how == "avg" and band else None)


def choose_mode(tier, tier_count):
    """"servers" when a tier is selected or the resource has just one tier, else "tiers"."""
    return "servers" if tier is not None or tier_count <= 1 else "tiers"


def tier_series(servers, per_server, how, step=None):
    """The "tiers" payload series: one per (merged) tier, in tier order.

    servers is every server in scope, so counts and the merge match the summary's tiers; a server
    without data adds to the count and nothing to the line.
    """
    groups = group_by_tier(servers)
    shown = merge_map(groups)
    merged = {}
    for name, members in groups.items():
        merged.setdefault(shown[name], []).extend(members)
    out = []
    for name, members in merged.items():
        data, band = aggregate([per_server[s["id"]] for s in members if per_server.get(s["id"])], how, step)
        out.append({"name": name, "server_count": len(members), "data": data, "band": band})
    out.sort(key=lambda s: s["name"] == OTHER_TIERS)
    return [s for s in out if s["data"]]


def server_series(servers, per_server, step=None):
    """The "servers" payload series: one per server that has data."""
    return [
        {"id": s["id"], "hostname": s["hostname"],
         "data": break_gaps([[ts, _round(v)] for ts, v in per_server[s["id"]]], step) if step
         else [[ts, _round(v)] for ts, v in per_server[s["id"]]]}
        for s in servers
        if per_server.get(s["id"])
    ]


def series_payload(servers, per_server, how, unit, tier=None, step=None):
    """Either payload of the chart endpoints. servers are the ones in scope, monitored or not."""
    monitored = [s for s in servers if s.get("monitored")]
    mode = choose_mode(tier, len(group_by_tier(servers)))
    if mode == "servers":
        return {"mode": "servers", "unit": unit, "series": server_series(monitored, per_server, step)}
    return {"mode": "tiers", "unit": unit, "series": tier_series(servers, per_server, how, step)}
