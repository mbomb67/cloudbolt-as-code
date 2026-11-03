"""
Rerun Job (MCP Tool Action plugin).

Two modes, chosen by rerun_job_mode:

rerun (the default) does what the Rerun button on a job's page does
(jobs.views.clone) and what POST /api/v3/cmp/jobs/<id>/clone/ does
(JobViewSet.clone): the same checks in the same order, then
Job.clone(new_owner=caller). The clone reuses the old job's job parameters, so
a rerun build item runs the current plug-in code against the same resource,
servers and arguments. A failed provision job that left a server behind also
gets a decom job that runs first.

resume does what the Resume button on a paused job's page does
(jobs.views.resume with RUNNING) and what
PATCH /api/v3/cmp/jobs/<id>/resume/ does (JobViewSet.resume): CloudBolt
admins only, the job must be PAUSED, and its status becomes RUNNING. The
debugger values STEP, NEXT and RETURN are left out. A deploy_blueprint job
pauses when a build item fails, if the blueprint has Pause Job on Failure and
Misc Settings has Allow resuming failed Blueprint's Job on; on resume it
reruns the failed item (the latest rerun of it, skipped if that one succeeded)
and carries on with the next build items
(DeployBlueprintRunner.pause_deploy_bp_job). A provision job pauses the same
way when an orchestration action fails and retries that action on resume
(cbhooks.pause_job). The paused job waits in its job engine thread, so a job
engine restart cancels it.

The rule for which jobs may be rerun is Job.rerun_allowed(profile), the check
behind both the button and the endpoint. The v3 job field canBeRequeued is not
part of it; CloudBolt never reads that field. On top of CloudBolt's checks
this tool refuses to rerun a job that is still active, because rerun_allowed
lets an admin rerun a plug-in or provision job whatever its status, and
refuses to resume a deployment while a rerun of its failed item is still
active, because the resumed deployment would clone that running job again.

Every declared input is read from its rendered template token in
_read_inputs(); keep each one referenced that way or CloudBolt's token scan
deletes the input when the script is saved. Output keys are camelCase so a
synchronous run and an asynchronous fetch_job read identically.
"""
import html

from jobs.models import Job

MODES = ["rerun", "resume"]
FAILED_STATUSES = ["FAILURE", "CANCELED"]


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
    raw = {
        "rerun_job_id": """{{ rerun_job_id }}""",
        "rerun_job_mode": """{{ rerun_job_mode }}""",
    }
    return {
        "job_id": _text(raw["rerun_job_id"]),
        "mode": _text(raw["rerun_job_mode"]).lower() or MODES[0],
    }


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


def _paused_ancestor(job):
    parent = job.parent_job
    while parent is not None:
        if parent.status == "PAUSED":
            return parent
        parent = parent.parent_job
    return None


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


def _failed_item_reruns(paused):
    """
    For each failed child chain of a paused deployment, the job its resume
    will act on: the latest rerun of the failed child, as
    pause_deploy_bp_job finds it. Reruns keep the original's parent job, so
    every link of a chain is a child of the paused job.
    """
    children = list(Job.objects.filter(parent_job=paused).order_by("id"))
    rerun_ids = {child.rerun_job_id for child in children if child.rerun_job_id}
    latest = []
    for head in children:
        if head.id in rerun_ids or head.status not in FAILED_STATUSES:
            continue
        latest.append(_latest_rerun(head))
    return latest


def _rerun(target, profile):
    if not (profile.is_cbadmin or profile.has_permission("job.rerun", target)):
        return _failure("You do not have permission to rerun {}.".format(target.global_id))
    if target.status == "PAUSED":
        return _failure(
            "{} is paused, not finished. Run this tool with rerun_job_mode "
            "resume to resume it.".format(_describe(target))
        )
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
    paused = _paused_ancestor(target)

    outputs = {
        "mode": "rerun",
        "newJobId": new_job.global_id,
        "newJobPk": new_job.id,
        "rerunOfJobId": target.global_id,
        "rerunOfJobPk": target.id,
        "type": target.type,
        "label": target.label or str(target),
        "previousStatus": target.status,
        "parentJobId": target.parent_job.global_id if target.parent_job_id else None,
        "pausedAncestorJobId": paused.global_id if paused else None,
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
    if paused:
        message += (
            " {} is paused on this failure: once the rerun succeeds, resume it "
            "to carry on with the deployment."
        ).format(paused.global_id)
    return {
        "status": "SUCCESS",
        "output_message": message,
        "error_message": "",
        "outputs": outputs,
    }


def _resume(target, profile):
    if not profile.is_cbadmin:
        return _failure(
            "Only CloudBolt admins can resume a paused job; you cannot resume {}.".format(
                target.global_id
            )
        )
    if target.status != "PAUSED":
        message = "Only paused jobs can be resumed; {} is not paused.".format(
            _describe(target)
        )
        paused = _paused_ancestor(target)
        if paused:
            message += " Its parent {} is paused: resume that job instead.".format(
                _describe(paused)
            )
        elif target.type == "deploy_blueprint" and target.status in FAILED_STATUSES:
            message += (
                " A deployment pauses on a failed build item only when the "
                "blueprint has Pause Job on Failure and Misc Settings has "
                "Allow resuming failed Blueprint's Job on, and a job engine "
                "restart cancels a paused job."
            )
        return _failure(message)

    failed_items = _failed_item_reruns(target)
    running = [job for job in failed_items if job.is_active()]
    if running:
        return _failure(
            "{} is still rerunning the failed item of {}. Wait for it to "
            "finish, then resume; if it succeeds the deployment carries on "
            "without running the item again.".format(
                ", ".join(_describe(job) for job in running), target.global_id
            )
        )

    # A conditional update instead of the view's job.save(), so a job that
    # timed out or was canceled since it was read is not set back to RUNNING.
    resumed = Job.objects.filter(id=target.id, status="PAUSED").update(status="RUNNING")
    if not resumed:
        target.refresh_from_db()
        return _failure(
            "{} stopped being paused before it could be resumed.".format(
                _describe(target)
            )
        )
    target.refresh_from_db()

    to_rerun = [job for job in failed_items if job.status != "SUCCESS"]
    outputs = {
        "mode": "resume",
        "jobId": target.global_id,
        "jobPk": target.id,
        "type": target.type,
        "label": target.label or str(target),
        "previousStatus": "PAUSED",
        "jobStatus": target.status,
        "parentJobId": target.parent_job.global_id if target.parent_job_id else None,
        "failedItemJobIds": [job.global_id for job in to_rerun],
        "next": "Poll fetch_job_log with jobId until the status is SUCCESS, WARNING, or FAILURE.",
    }
    message = "Resumed {}; it is now {}.".format(target.global_id, target.status)
    if target.type == "deploy_blueprint":
        if to_rerun:
            message += " It reruns {} and carries on with the next build items.".format(
                ", ".join(outputs["failedItemJobIds"])
            )
        else:
            message += (
                " Its failed item was already rerun successfully, so it "
                "carries on with the next build items."
            )
    else:
        message += " It retries the step it paused on."
    return {
        "status": "SUCCESS",
        "output_message": message,
        "error_message": "",
        "outputs": outputs,
    }


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
    if inputs["mode"] not in MODES:
        return _failure(
            "rerun_job_mode must be one of {}; got '{}'.".format(
                ", ".join(MODES), inputs["mode"]
            )
        )
    if not inputs["job_id"]:
        return _failure("rerun_job_id is required: a JOB-... global id or the numeric job id.")
    target = _find_job(inputs["job_id"])
    if target is None:
        return _failure("No job matches '{}'.".format(inputs["job_id"]))

    if inputs["mode"] == "resume":
        return _resume(target, profile)
    return _rerun(target, profile)
