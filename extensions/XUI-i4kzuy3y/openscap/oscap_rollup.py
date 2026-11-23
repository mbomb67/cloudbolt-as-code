"""
OpenSCAP XUI: the roll-up of several servers' latest scans.

Pure Python, no Django: the resource tab and the resource run both call it, and
it can be checked with plain Python. Everything it reads is a server's scan
summary from the index (oscap_jobs.py scan_summary), which the views and the
job reduce to the few fields named below.

A server is a dict:
    id, hostname     who it is
    score            the benchmark score, or None
    fail             the scan's count of failed rules
    failed_rules     [{"id": rule id, "severity": high|medium|low|unknown}]; the
                     index keeps at most the first 1000 of them, and only for a server's newest completed scan
    truncated        True when failed_rules is shorter than fail
"""
import re

SEVERITIES = ("high", "medium", "low", "unknown")
RANK = {name: number for number, name in enumerate(SEVERITIES)}
SEPARATORS = re.compile(r"[-_.]")


def _severity(value):
    return value if value in RANK else "unknown"


def average(scores):
    """The plain mean of the scores that exist, or None when there are none."""
    scores = [score for score in scores if score is not None]
    return sum(scores) / len(scores) if scores else None


def short_names(hostnames):
    """The names with the prefix they all share dropped, so a column header
    reads web-01 and not webshop-web-01. A prefix is dropped only up to a
    separator and only when every name keeps something after it; otherwise the
    names stay whole."""
    names = list(hostnames)
    if len(names) < 2:
        return names
    prefix = names[0]
    for name in names[1:]:
        while prefix and not name.startswith(prefix):
            prefix = prefix[:-1]
    cut = max((match.end() for match in SEPARATORS.finditer(prefix)), default=0)
    if cut == 0 or any(len(name) <= cut for name in names):
        return names
    return [name[cut:] for name in names]


def rollup(servers):
    """The numbers of the summary cards and the rule grid.

    average        mean of the servers' scores; servers without a score are left out
    lowest         {"id", "hostname", "score"} of the lowest score, or None
    scanned        how many servers were given
    unique_failed  how many different rules fail on at least one server
    findings       the sum of every server's count of failed rules
    by_severity    {"high", "medium", "low", "unknown"}: unique failed rules by
                   severity, a rule counting under the highest severity seen
    on_every       rules that fail on every server given
    truncated      True when any server's failed-rule list is cut short, which
                   makes unique_failed, by_severity, on_every and the grid
                   a lower bound
    columns        [{"id", "hostname"}] the grid's servers, in the order given
    grid           [{"severity", "id", "count", "fails": [bool per column]}],
                   sorted by severity, then by count descending, then by id
    """
    servers = list(servers)
    scored = [s for s in servers if s.get("score") is not None]
    lowest = min(scored, key=lambda s: s["score"]) if scored else None
    rules = {}
    for index, server in enumerate(servers):
        for rule in server.get("failed_rules") or []:
            rule_id = rule.get("id")
            if not rule_id:
                continue
            found = rules.setdefault(rule_id, {"severity": "unknown", "on": set()})
            severity = _severity(rule.get("severity"))
            if RANK[severity] < RANK[found["severity"]]:
                found["severity"] = severity
            found["on"].add(index)
    by_severity = {name: 0 for name in SEVERITIES}
    grid = []
    for rule_id, found in rules.items():
        by_severity[found["severity"]] += 1
        grid.append({
            "severity": found["severity"],
            "id": rule_id,
            "count": len(found["on"]),
            "fails": [index in found["on"] for index in range(len(servers))],
        })
    grid.sort(key=lambda row: (RANK[row["severity"]], -row["count"], row["id"]))
    return {
        "average": average(s.get("score") for s in servers),
        "lowest": {"id": lowest["id"], "hostname": lowest["hostname"], "score": lowest["score"]} if lowest else None,
        "scanned": len(servers),
        "unique_failed": len(rules),
        "findings": sum(int(s.get("fail") or 0) for s in servers),
        "by_severity": by_severity,
        "on_every": sum(1 for row in grid if servers and row["count"] == len(servers)),
        "truncated": any(s.get("truncated") for s in servers),
        "columns": [{"id": s["id"], "hostname": s["hostname"]} for s in servers],
        "grid": grid,
    }


def trend(current, previous):
    """How the average score moved: {"direction": up|down|same, "points": whole
    points}, or None when either average is missing. The comparison is made on
    the scores rounded half up, the figures the card shows."""
    if current is None or previous is None:
        return None
    change = int(current + 0.5) - int(previous + 0.5)
    if change == 0:
        return {"direction": "same", "points": 0}
    return {"direction": "up" if change > 0 else "down", "points": abs(change)}
