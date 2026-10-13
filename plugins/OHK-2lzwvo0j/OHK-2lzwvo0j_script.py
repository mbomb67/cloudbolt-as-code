"""
Cancel Jobs (MCP Tool Action plugin).

Sets each named job and its unfinished descendants to TO_CANCEL, the same
layer-by-layer walk the v3 jobs cancel endpoint performs, so an agent can
clear a hung test job before retrying. Finished jobs are left alone.

Every declared input is read from its rendered template token in
_read_inputs(); keep each one referenced that way or CloudBolt's token scan
deletes the input when the script is saved. Output keys are camelCase so a
synchronous run and an asynchronous fetch_job read identically.
"""
import html
import re

from jobs.models import Job

ACTIVE_STATUSES = getattr(
    Job,
    "ACTIVE_STATUSES",
    ["INIT", "QUEUED", "PENDING", "RUNNING", "TO_CANCEL", "CANCELING", "PAUSED"],
)


def _text(value):
    return html.unescape(value or "").strip()


def _split(value):
    return [part for part in re.split(r"[\s,]+", _text(value)) if part]


def _failure(message):
    return {
        "status": "FAILURE",
        "output_message": "",
        "error_message": message,
        "outputs": {"error": message},
    }


def _read_inputs():
    raw = {"cancel_job_ids": """{{ cancel_job_ids }}"""}
    return {"job_ids": _split(raw["cancel_job_ids"])}


def _cancel_tree(target):
    """Mark the job and its unfinished descendants TO_CANCEL; return the count touched."""
    touched = 0
    layer = Job.objects.filter(id=target.id)
    while layer.exists():
        layer_ids = list(layer.values_list("id", flat=True))
        touched += layer.filter(status__in=ACTIVE_STATUSES).update(status="TO_CANCEL")
        layer = Job.objects.filter(parent_job_id__in=layer_ids)
    return touched


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
    if not inputs["job_ids"]:
        return _failure("cancel_job_ids is required: JOB-... global ids, one per line.")

    results = []
    for job_id in inputs["job_ids"]:
        target = Job.objects.filter(global_id=job_id).first()
        if target is None:
            results.append({"jobId": job_id, "result": "NOT_FOUND", "jobsTouched": 0})
            continue
        if not (profile.is_cbadmin or target.owner_id == profile.id):
            results.append({"jobId": job_id, "result": "NOT_PERMITTED", "jobsTouched": 0})
            continue
        touched = _cancel_tree(target)
        results.append(
            {
                "jobId": job_id,
                "result": "TO_CANCEL" if touched else "ALREADY_FINISHED",
                "jobsTouched": touched,
            }
        )

    cancelled = sum(1 for row in results if row["result"] == "TO_CANCEL")
    message = "Requested cancellation of {} of {} job(s).".format(cancelled, len(results))
    return {
        "status": "SUCCESS",
        "output_message": message,
        "error_message": "",
        "outputs": {"results": results},
    }
