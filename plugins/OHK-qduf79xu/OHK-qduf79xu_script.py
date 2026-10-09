"""
Run Recurring Job (MCP Tool Action plugin).

Calls the recurring job's own spawn_new_job(), exactly what Run Now does on
the Recurring Jobs page (jobs.views.run_recurring_job), and records the
caller as the job's owner. Hook-based recurring jobs (RecurringActionJob)
override spawn_new_job to run their action; schedule-only ones spawn from
stored job parameters. A disabled recurring job still runs, as
in the UI.

Every declared input is read from its rendered template token in
_read_inputs(); keep each one referenced that way or CloudBolt's token scan
deletes the input when the script is saved. Output keys are camelCase so a
synchronous run and an asynchronous fetch_job read identically.
"""
import html

from jobs.models import RecurringJob


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
    raw = {"recurring_job_ref": """{{ recurring_job_ref }}"""}
    return {"ref": _text(raw["recurring_job_ref"])}


def _find_recurring_job(ref):
    recurring = (
        RecurringJob.objects.filter(global_id=ref).first()
        or RecurringJob.objects.filter(name__iexact=ref).first()
    )
    if recurring is not None:
        return recurring, None
    names = list(RecurringJob.objects.order_by("name").values_list("name", flat=True)[:30])
    return None, "No recurring job matches '{}'. Known names include: {}.".format(
        ref, ", ".join(names) or "none"
    )


def run(job, *args, **kwargs):
    profile = kwargs.get("profile")
    if profile is None and job is not None:
        profile = job.owner
    if profile is None:
        return _failure(
            "No calling user profile was supplied. Run this tool through the MCP "
            "server or the API as an authenticated user."
        )
    if not profile.is_cbadmin:
        return _failure("Only CloudBolt admins may run recurring jobs on demand.")

    inputs = _read_inputs()
    if not inputs["ref"]:
        return _failure("recurring_job_ref is required: an RJB-... global id or the exact name.")
    recurring, error = _find_recurring_job(inputs["ref"])
    if error:
        return _failure(error)

    # cast() matters: a RecurringActionJob (hook-based) overrides spawn_new_job
    # to run its action as a job; the base class uses stored job parameters.
    recurring = recurring.cast()
    try:
        new_job = recurring.spawn_new_job()
    except Exception as exc:
        return _failure("Could not start recurring job '{}': {}".format(recurring.name, exc))
    if isinstance(new_job, (list, tuple)):
        new_job = new_job[0]
    if new_job.owner_id is None:
        new_job.owner = profile
        new_job.save(update_fields=["owner"])
    outputs = {
        "jobId": new_job.global_id,
        "recurringJobId": recurring.global_id,
        "recurringJobName": recurring.name,
        "enabled": bool(recurring.enabled),
        "schedule": str(getattr(recurring, "schedule", "") or ""),
        "next": "Poll fetch_job_log with jobId until the status is SUCCESS, WARNING, or FAILURE.",
    }
    message = "Created job {} for recurring job '{}'.".format(
        new_job.global_id, recurring.name
    )
    return {
        "status": "SUCCESS",
        "output_message": message,
        "error_message": "",
        "outputs": outputs,
    }
