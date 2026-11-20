"""Turns Prometheus alerts into the rows the monitoring tab's alerts card shows.

An alert here is one entry of GET /api/v1/alerts: labels, annotations, state ("firing" or
"pending") and activeAt. https://prometheus.io/docs/prometheus/latest/querying/api/#alerts
"""

import re

SEVERITY_ORDER = {"critical": 0, "warning": 1, "info": 2}
TEXT_LIMIT = 300

# Split CamelCase alert names but keep runs of capitals (VM, VMI, CPU) together.
_WORD_BREAK = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")


def readable_name(alertname):
    """'VMCannotBeEvicted' -> 'VM cannot be evicted'."""
    words = _WORD_BREAK.sub(" ", alertname or "").replace("Kube Virt", "KubeVirt").split()
    words = [word if word.isupper() or word == "KubeVirt" else word.lower() for word in words]
    text = " ".join(words)
    return text[:1].upper() + text[1:]


def alert_row(labels, annotations=None, state="firing", active_at=None):
    """One row for the card. active_at is an RFC 3339 string or seconds since the epoch."""
    annotations = annotations or {}
    severity = (labels.get("severity") or "none").lower()
    detail = annotations.get("description") or annotations.get("summary") or annotations.get("message") or ""
    runbook = annotations.get("runbook_url") or ""
    return {
        "name": labels.get("alertname", ""),
        "title": readable_name(labels.get("alertname", "")),
        "severity": severity if severity in SEVERITY_ORDER else "none",
        "state": state if state in ("firing", "pending") else "firing",
        "since": active_at,
        "detail": detail[:TEXT_LIMIT],
        # Only a web address becomes a link in the browser.
        "runbook_url": runbook if runbook.startswith(("https://", "http://")) else "",
    }


def ordered(rows):
    """Firing before pending, then most severe first, then by name."""
    return sorted(
        rows,
        key=lambda row: (row["state"] != "firing", SEVERITY_ORDER.get(row["severity"], 3), row["title"]),
    )
