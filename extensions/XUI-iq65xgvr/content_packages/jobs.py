"""
Export and parameter sync as CloudBolt jobs.

start_export() runs in the web request. It checks the request, makes sure the
export plugin exists (hooks.py), and starts it with
OrchestrationHook.run_as_job, the call CloudBolt uses to run any plugin as a
job.

run_export() runs inside that job. It pushes the package through
services.export_package, reports each member as job progress, and returns
the per-member results as the job's outputs. The job runs as the user who
clicked Export, so pushes use that user's repository credentials.

run_parameter_sync() runs inside the job services.sync_package queues for a
parameter member, once the jobs before it in the package's sync chain have
finished. It reads the parameter's metadata from the branch and creates or
updates the parameter through parameters.import_metadata.
"""
from jobs.models import Job
from utilities.logger import ThreadLogger

from xui.content_packages import hooks, parameters, services, store

logger = ThreadLogger(__name__)

ACTIVE_STATUSES = ("INIT", "PENDING", "QUEUED", "RUNNING", "PAUSED", "STEP", "NEXT", "RETURN", "TO_CANCEL")


def running_export(package):
    """The package's export job when it has not finished yet, else None."""
    job_id = package["local"].get("last_export_job_id")
    if not isinstance(job_id, int):
        return None
    return Job.objects.filter(id=job_id, status__in=ACTIVE_STATUSES).first()


def start_export(package, repo, branch, commit_message, profile):
    """Start the export job and record it on the package. Returns the Job."""
    branch = services.check_branch(branch)
    commit_message = (commit_message or "").strip()
    if not commit_message:
        raise store.PackageError("Enter a commit message.")
    if not package.get("contents"):
        raise store.PackageError("The package has no members to export.")
    running = running_export(package)
    if running is not None:
        raise store.PackageError(
            "Job {} is still exporting this package; wait for it to finish.".format(running.id)
        )
    created = hooks.export_hook().run_as_job(
        owner=profile,
        context={
            "package_id": package["id"],
            "source_code_repo_id": repo.id,
            "branch": branch,
            "commit_message": commit_message,
        },
    )
    if not created:
        raise store.PackageError("CloudBolt did not start the export job; see the application log.")
    job = created[0]
    job.label = "Content package {}: export to {}".format(package["name"], branch)[:256]
    job.save(update_fields=["label"])
    store.record_export_job(package["id"], job.id, repo.id, branch)
    return job


def run_export(job, package_id=None, source_code_repo_id=None, branch=None, commit_message=None, **kwargs):
    """Plugin entry point (via export_job.py). Returns the 4-tuple CloudBolt
    plugins use: status, output, errors, outputs."""
    def progress(message):
        if job is not None:
            job.set_progress(message)

    profile = kwargs.get("profile") or getattr(job, "owner", None)
    try:
        package = store.get_package(package_id)
        repo = services.get_repo(source_code_repo_id)
    except store.PackageError as exc:
        return "FAILURE", "", str(exc)

    progress("Exporting content package {} ({}) to {}, branch {}".format(
        package["name"], package["id"], repo.label, branch
    ))
    try:
        outcome = services.export_package(package, repo, branch, commit_message, profile, progress=progress)
    except store.PackageError as exc:
        return "FAILURE", "", str(exc)

    # Rows with "via" are generated-options actions pushed for a parameter.
    members = [r for r in outcome["results"] if r["prefix"] != "PKG" and not r.get("via")]
    exported = sum(1 for r in members if r["status"] == "success")
    failed = [r for r in outcome["results"] if r["status"] == "failure" and r["prefix"] != "PKG"]
    manifest = outcome["results"][-1]
    summary = "Exported {} of {} members to {}, branch {}.".format(exported, len(members), repo.label, branch)
    if outcome["restored"]:
        summary += " Restored {} README file{} the pushes deleted.".format(
            len(outcome["restored"]), "" if len(outcome["restored"]) == 1 else "s"
        )
    elif outcome["restore_note"]:
        summary += " " + outcome["restore_note"]
    if manifest["status"] != "success":
        return "FAILURE", summary, "The package manifest was not committed: {}".format(manifest["message"]), outcome
    if failed:
        errors = "; ".join("{} ({}): {}".format(r["name"], r["ref"], r["message"]) for r in failed)
        return "WARNING", summary, errors, outcome
    return "SUCCESS", summary, "", outcome


def run_parameter_sync(job, package_id=None, ref=None, source_code_repo_id=None, branch=None,
                       refresh_if_exists=True, **kwargs):
    """Plugin entry point (via parameter_job.py) for one parameter member."""
    def progress(message):
        if job is not None:
            job.set_progress(message)

    try:
        member_type, global_id = store.parse_ref(ref)
        if member_type.synced:
            raise store.PackageError("{} is not a parameter.".format(ref))
        branch = services.check_branch(branch)
        repo, clone = services.branch_clone(services.get_repo(source_code_repo_id), branch)
        progress("Reading {} from {}, branch {}".format(parameters.metadata_path(global_id), repo.label, branch))
        data = parameters.read_metadata(clone, branch, global_id)
        progress("Creating or updating parameter {}".format(data["name"]))
        cf, outcome, notes = parameters.import_metadata(data, refresh_if_exists)
    except store.PackageError as exc:
        return "FAILURE", "", str(exc)
    except Exception as exc:
        logger.exception("Sync of parameter %s failed", ref)
        return "FAILURE", "", "Could not create or update the parameter: {}".format(str(exc) or type(exc).__name__)
    store.record_alias(ref, cf.global_id)
    summary = "Parameter {} ({}) {} from {}, branch {}.".format(cf.name, cf.global_id, outcome, repo.label, branch)
    if cf.global_id != global_id:
        summary += " It kept its own global ID; package {} now resolves {} to it.".format(package_id or "", ref)
    for note in notes:
        progress(note)
    if notes:
        return "WARNING", summary, " ".join(notes)
    return "SUCCESS", summary, ""
