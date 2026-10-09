"""
Fetch Job Log (MCP Tool Action plugin).

Returns what the built-in fetch_job tool leaves out: the job's progress
messages, its child jobs, every failed descendant with its errors, the tail
of the job's log file (tracebacks), and, for a sourcecodereposync job, the
per-object results flattened into rows with plain keys. Read-only.

Every declared input is read from its rendered template token in
_read_inputs(); keep each one referenced that way or CloudBolt's token scan
deletes the input when the script is saved. Output keys are camelCase so a
synchronous run and an asynchronous fetch_job read identically.
"""
import html
import os
import re

from jobs.models import Job, ProgressMessage

DEFAULT_TAIL = 100
MAX_TAIL = 1000
MAX_FILE_TAIL = 2000
MAX_DESCENDANTS = 50
MAX_DEPTH = 6
TAG_RE = re.compile(r"<[^>]+>")


def _text(value):
    return html.unescape(value or "").strip()


def _bool(value, default):
    text = _text(value).lower()
    if not text:
        return default
    return text in ("true", "1", "yes", "y", "on")


def _int(value, default, low, high):
    text = _text(value)
    if not text:
        return default
    try:
        number = int(float(text))
    except ValueError:
        return default
    return max(low, min(high, number))


def _failure(message):
    return {
        "status": "FAILURE",
        "output_message": "",
        "error_message": message,
        "outputs": {"error": message},
    }


def _read_inputs():
    raw = {
        "log_job_id": """{{ log_job_id }}""",
        "log_tail": """{{ log_tail }}""",
        "log_include_children": """{{ log_include_children }}""",
        "log_file_tail": """{{ log_file_tail }}""",
    }
    return {
        "job_id": _text(raw["log_job_id"]),
        "tail": _int(raw["log_tail"], DEFAULT_TAIL, 1, MAX_TAIL),
        "include_children": _bool(raw["log_include_children"], True),
        "file_tail": _int(raw["log_file_tail"], 0, 0, MAX_FILE_TAIL),
    }


def _iso(value):
    return value.isoformat() if value else None


def _find_job(ref):
    job = Job.objects.filter(global_id=ref).first()
    if job is None and ref.isdigit():
        job = Job.objects.filter(id=int(ref)).first()
    return job


def _progress(job, limit):
    rows = list(ProgressMessage.objects.filter(job=job).order_by("-id")[:limit])
    rows.reverse()
    return [
        {
            "time": _iso(row.date),
            "message": row.message,
            "detail": (row.detailed_message or "")[:2000],
        }
        for row in rows
    ]


def _summary(job, errors_cap=4000):
    return {
        "id": job.global_id,
        "pk": job.id,
        "type": job.type,
        "label": job.label or "",
        "status": job.status,
        "startDate": _iso(job.start_date),
        "endDate": _iso(job.end_date),
        "output": (job.output or "")[:4000],
        "errors": (job.errors or "")[:errors_cap],
        "outputs": job.outputs,
    }


def _children(job):
    rows = []
    for child in job.children_jobs.all().order_by("id"):
        row = _summary(child, 1000)
        row["lastProgress"] = _progress(child, 3)
        rows.append(row)
    return rows


def _failed_descendants(job):
    found = []
    stack = [(job, 0)]
    while stack and len(found) < MAX_DESCENDANTS:
        parent, depth = stack.pop()
        if depth >= MAX_DEPTH:
            continue
        for child in parent.children_jobs.all().order_by("id"):
            if child.status == "FAILURE":
                row = _summary(child, 4000)
                row["parentId"] = parent.global_id
                row["lastProgress"] = _progress(child, 15)
                found.append(row)
            stack.append((child, depth + 1))
    return found


def _sync_results(job):
    """Flatten a sourcecodereposync job's outputs into rows with plain keys."""
    if job.type != "sourcecodereposync" or not isinstance(job.outputs, dict):
        return []
    rows = []
    for type_key, per_path in job.outputs.items():
        if not isinstance(per_path, dict):
            continue
        for path, result in per_path.items():
            result = result if isinstance(result, dict) else {}
            rows.append(
                {
                    "objectType": type_key,
                    "path": path,
                    "status": result.get("status"),
                    "message": html.unescape(TAG_RE.sub("", result.get("message") or "")),
                }
            )
    return rows


def _file_tail(job, lines):
    if lines <= 0:
        return None
    try:
        path = job.get_log_file_path()
    except Exception as exc:  # defensive: never fail the whole call over the file
        return {"path": None, "lines": [], "note": str(exc)}
    if not os.path.isfile(path):
        return {"path": path, "lines": [], "note": "Log file not found on this host."}
    try:
        with open(path, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            chunk = min(size, 512 * 1024)
            handle.seek(size - chunk)
            text = handle.read().decode("utf-8", "replace")
    except OSError as exc:
        return {"path": path, "lines": [], "note": str(exc)}
    return {"path": path, "lines": text.splitlines()[-lines:]}


def run(job, *args, **kwargs):
    profile = kwargs.get("profile")
    if profile is None and job is not None:
        profile = job.owner
    if profile is None:
        return _failure(
            "No calling user profile was supplied. Run this tool through the MCP "
            "server or the API as an authenticated user."
        )

    inputs = _read_inputs()
    if not inputs["job_id"]:
        return _failure("log_job_id is required, e.g. JOB-abc12345.")
    target = _find_job(inputs["job_id"])
    if target is None:
        return _failure("Job '{}' not found.".format(inputs["job_id"]))
    if not (profile.is_cbadmin or target.owner_id == profile.id):
        return _failure("You may only read jobs you own.")

    outputs = {
        "job": _summary(target),
        "progress": _progress(target, inputs["tail"]),
        "syncResults": _sync_results(target),
    }
    if inputs["include_children"]:
        outputs["children"] = _children(target)
        outputs["failedDescendants"] = _failed_descendants(target)
    file_tail = _file_tail(target, inputs["file_tail"])
    if file_tail is not None:
        outputs["logFileTail"] = file_tail

    message = "Job {} is {} with {} progress message(s) returned.".format(
        target.global_id, target.status, len(outputs["progress"])
    )
    return {
        "status": "SUCCESS",
        "output_message": message,
        "error_message": "",
        "outputs": outputs,
    }
