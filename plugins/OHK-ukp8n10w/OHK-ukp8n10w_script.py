"""
Rerun Job (MCP Tool Action plugin).

Does what the Rerun button on a job's page does (jobs.views.clone) and what
POST /api/v3/cmp/jobs/<id>/clone/ does (JobViewSet.clone): the same checks in
the same order, then Job.clone(new_owner=caller). The clone reuses the old
job's job parameters, so a rerun build item runs the current plug-in code
against the same resource, servers and arguments. A failed provision job that
left a server behind also gets a decom job that runs first.

The rule for which jobs may be rerun is Job.rerun_allowed(profile), the check
behind both the button and the endpoint. The v3 job field canBeRequeued is not
part of it; CloudBolt never reads that field. On top of CloudBolt's checks
this tool refuses a job that is still active, because rerun_allowed lets an
admin rerun a plug-in or provision job whatever its status.

Every declared input is read from its rendered template token in
_read_inputs(); keep each one referenced that way or CloudBolt's token scan
deletes the input when the script is saved. Output keys are camelCase so a
synchronous run and an asynchronous fetch_job read identically.
"""
import html

from jobs.models import Job


def _text(value):
    return html.unescape(value or "").strip()


def _failure(message):
    return {
        "status": "FAILURE",
        "output_message": "",
        "error_message": message,
        "outputs": {"error": message},
    }


def _read_inputs():
    raw = {"rerun_job_id": """{{ rerun_job_id }}"""}
    return {"job_id": _text(raw["rerun_job_id"])}


def _find_job(ref):
    job = Job.objects.filter(global_id=ref).first()
    if job is None and ref.isdigit():
        job = Job.objects.filter(id=int(ref)).first()
    return job


def _describe(job):
    return "{} ({}, {}, {})".format(
        job.global_id, job.label or str(job), job.type, job.status
    )


def _latest_rerun(job):
    latest = job
    while latest.rerun_job_id:
        latest = latest.rerun_job
    return latest


def _refusal(job, profile):
    """Say why Job.rerun_allowed refused, in the terms that method uses."""
    if not profile.is_cbadmin:
        return (
            "CloudBolt only lets a non-admin rerun a failed job that is not a "
            "blueprint deployment or a parameter-change action; {} is not one."
        ).format(_describe(job))
    return (
        "CloudBolt does not rerun {}. Admins can rerun finished plug-in, "
        "trigger, server provision, blueprint deployment and decommission "
        "jobs, and any other job type only after it failed."
    ).format(_describe(job))


def _global_ids(manager):
    return [obj.global_id for obj in manager.all()] if manager is not None else []


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
        return _failure("rerun_job_id is required: a JOB-... global id or the numeric job id.")
    target = _find_job(inputs["job_id"])
    if target is None:
        return _failure("No job matches '{}'.".format(inputs["job_id"]))

    if not (profile.is_cbadmin or profile.has_permission("job.rerun", target)):
        return _failure("You do not have permission to rerun {}.".format(target.global_id))
    if target.is_active():
        return _failure(
            "{} is still active. Wait for it to finish, or cancel it with "
            "cancel_jobs, before rerunning it.".format(_describe(target))
        )
    if target.rerun_job_id:
        latest = _latest_rerun(target)
        return _failure(
            "{} was already rerun; the latest rerun is {}. Rerun that job "
            "instead if it failed.".format(target.global_id, _describe(latest))
        )
    if not target.rerun_allowed(profile=profile):
        return _failure(_refusal(target, profile))

    params = target.job_parameters.cast()
    if hasattr(params, "order"):
        can_duplicate, _ = params.order.can_duplicate(profile)
        if not can_duplicate:
            return _failure(
                "Constraints on order {} prevent {} from being rerun.".format(
                    params.order.id, target.global_id
                )
            )

    try:
        new_jobs = target.clone(new_owner=profile)
    except Exception as exc:
        return _failure("CloudBolt could not rerun {}: {}".format(target.global_id, exc))
    new_job = new_jobs[-1]
    cleanup_jobs = new_jobs[:-1]

    outputs = {
        "newJobId": new_job.global_id,
        "newJobPk": new_job.id,
        "rerunOfJobId": target.global_id,
        "rerunOfJobPk": target.id,
        "type": target.type,
        "label": target.label or str(target),
        "previousStatus": target.status,
        "parentJobId": target.parent_job.global_id if target.parent_job_id else None,
        "resourceIds": _global_ids(getattr(params, "resources", None)),
        "serverIds": _global_ids(new_job.server_set),
        "cleanupJobIds": [cleanup.global_id for cleanup in cleanup_jobs],
        "next": "Poll fetch_job_log with newJobId until the status is SUCCESS, WARNING, or FAILURE.",
    }
    message = "Cloned {} as {}.".format(_describe(target), new_job.global_id)
    if cleanup_jobs:
        message += " {} deletes the failed server first.".format(
            ", ".join(outputs["cleanupJobIds"])
        )
    return {
        "status": "SUCCESS",
        "output_message": message,
        "error_message": "",
        "outputs": outputs,
    }
