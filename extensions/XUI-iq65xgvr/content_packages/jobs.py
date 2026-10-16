"""
Export as a CloudBolt job.

start_export() runs in the web request. It checks the request, makes sure the
export plugin exists, and starts it with OrchestrationHook.run_as_job, the
call CloudBolt uses to run any plugin as a job. The plugin is a CloudBoltHook
whose module file is this extension's export_job.py, by absolute path (the
way CloudBolt's own built-in actions point at their files). It is created the
first time anyone exports, and re-pointed if the extension ever moves.

run_export() runs inside that job. It pushes the package through
services.export_package, reports each member as job progress, and returns
the per-member results as the job's outputs. The job runs as the user who
clicked Export, so pushes use that user's repository credentials.
"""
import os

from cbhooks.models import CloudBoltHook
from jobs.models import Job

from xui.content_packages import services, store

HOOK_NAME = "Content Package Manager: Export"
HOOK_DESCRIPTION = (
    "Pushes a content package to a repository. Created and run by the Content Package Manager UI "
    "extension; its code is the extension's export_job.py, so do not edit or delete it here."
)

ACTIVE_STATUSES = ("INIT", "PENDING", "QUEUED", "RUNNING", "PAUSED", "STEP", "NEXT", "RETURN", "TO_CANCEL")


def hook_file():
    """Absolute path of export_job.py in the installed extension."""
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "export_job.py")


def export_hook():
    """The export plugin, created on first use and kept pointing at this
    extension's export_job.py."""
    path = hook_file()
    hook = CloudBoltHook.objects.filter(name=HOOK_NAME).first()
    if hook is None:
        hook = CloudBoltHook(name=HOOK_NAME, description=HOOK_DESCRIPTION, shared=False)
        hook.module_file = path
        hook.save()
    elif str(hook.module_file) != path:
        hook.module_file = path
        hook.save()
    return hook


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
    created = export_hook().run_as_job(
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

    members = [r for r in outcome["results"] if r["prefix"] != "PKG"]
    pushed = sum(1 for r in members if r["status"] == "success")
    failed = [r for r in members if r["status"] == "failure"]
    manifest = outcome["results"][-1]
    summary = "Pushed {} of {} members to {}, branch {}.".format(pushed, len(members), repo.label, branch)
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
