"""
The credentials update as a CloudBolt job.

start_update() runs in the web request. It checks that each password matches
its confirmation, works the targets out again with targets.collect, refuses
when they no longer hash to the preview's token or the typed count is wrong,
and starts the plugin from hooks.py with OrchestrationHook.run_as_job, the
call CloudBolt uses to run any plugin as a job. One update runs at a time.

Secrets travel in the job's context. CloudBolt keeps a job's context in an
encrypted column (HookParameters._encrypted_arguments) that neither the job
page nor the API shows, and the passwords are named in __sensitive_keys, the
list CloudBolt's plug-in runner reads to print them as <stripped> where it
logs a plug-in's context (common/methods.py generate_string_from_template):
the treatment a password input on a server action gets. When the job ends it
blanks them in the job parameters, so the job keeps no copy and a rerun
cannot replay them.

run_update() runs inside that job. It writes the credentials onto each
server and image the preview showed as changing (apply.py), one object per
database transaction, reports progress every PROGRESS_EVERY objects and
each failure, and returns the counts as the job's outputs.
"""
from infrastructure.models import Server
from externalcontent.models import OSBuildAttribute
from jobs.models import HookParameters, Job
from utilities.logger import ThreadLogger
from utilities.models import SSHKey

from xui.credentials_manager import apply, hooks, targets
from xui.credentials_manager.targets import TargetError

logger = ThreadLogger(__name__)

SECRET_KEYS = ("cm_password", "cm_new_vm_password")
ACTIVE_STATUSES = ("INIT", "PENDING", "QUEUED", "RUNNING", "PAUSED", "STEP", "NEXT", "RETURN", "TO_CANCEL")
PROGRESS_EVERY = 100
# Failures listed in the job's outputs; the job log has every one.
FAILURES_KEPT = 200


def update_jobs():
    """Every job of the update plugin, newest first."""
    hook = hooks.existing_hook()
    if hook is None:
        return Job.objects.none()
    params = HookParameters.objects.filter(hook=hook).values("pk")
    return Job.objects.filter(job_parameters_id__in=params).order_by("-id")


def _confirmed_count(value):
    try:
        return int(str(value or "").replace(",", "").strip())
    except ValueError:
        return None


def start_update(data, profile):
    """Validate the request against a fresh look at the targets and start the
    job. Returns (job, number of objects it will change)."""
    changes = data.get("changes") or {}
    password = str(changes.get("password") or "")
    new_vm_password = str(changes.get("new_vm_password") or "")
    if password != str(changes.get("password_confirm") or ""):
        raise TargetError("The passwords do not match.")
    if new_vm_password != str(changes.get("new_vm_password_confirm") or ""):
        raise TargetError("The new VM passwords do not match.")

    filters, plan, server_rows, image_rows = targets.collect(data)
    include_servers, include_images = targets.parse_scope(data)
    if targets.token(server_rows, image_rows, plan) != data.get("token"):
        raise TargetError("The servers or images that match have changed since the preview. Preview again.")
    server_targets = [[r["id"], r["changes"]] for r in server_rows if r["changes"]]
    image_targets = [[r["id"], r["changes"]] for r in image_rows if r["changes"]]
    total = len(server_targets) + len(image_targets)
    if not total:
        raise TargetError("Nothing would change.")
    if _confirmed_count(data.get("confirm")) != total:
        raise TargetError("Type {} to confirm.".format(total))
    running = update_jobs().filter(status__in=ACTIVE_STATUSES).first()
    if running is not None:
        raise TargetError("Job {} is still applying an update; wait for it to finish.".format(running.id))

    created = hooks.update_hook().run_as_job(
        owner=profile,
        context={
            "cm_servers": server_targets,
            "cm_images": image_targets,
            "cm_username": plan["username"],
            "cm_ssh_key_id": plan["key"].id if plan["key"] else None,
            "cm_password": password,
            "cm_new_vm_password": new_vm_password if include_images else "",
            "cm_scope": targets.describe(filters, include_servers, include_images, plan),
            "__sensitive_keys": list(SECRET_KEYS),
        },
    )
    if not created:
        raise TargetError("CloudBolt did not start the update job; see the application log.")
    job = created[0]
    job.label = "Credentials Manager: {} server{}, {} image{}".format(
        len(server_targets), "" if len(server_targets) == 1 else "s",
        len(image_targets), "" if len(image_targets) == 1 else "s",
    )
    job.save(update_fields=["label"])
    return job, total


def _clear_secrets(job):
    """Blank the passwords in the job's stored context."""
    if job is None:
        return
    try:
        params = job.job_parameters.cast()
        arguments = dict(params.arguments)
        context = dict(arguments.get("context") or {})
        for key in SECRET_KEYS:
            if context.get(key):
                context[key] = ""
        arguments["context"] = context
        params.arguments = arguments
        params.save()
    except Exception:
        logger.exception("Could not clear the passwords from the parameters of job %s", job.id)


def _progress(job, message):
    if job is not None:
        job.set_progress(message)


def _run_kind(job, kind, items, model, apply_one, update, profile, outcome):
    objects = model.objects.in_bulk([obj_id for obj_id, _fields in items])
    for done, (obj_id, fields) in enumerate(items, 1):
        obj = objects.get(obj_id)
        if obj is None:
            outcome["missing"] += 1
            _progress(job, "Skipped {} {}: it no longer exists.".format(kind, obj_id))
        else:
            name = obj.hostname if kind == "server" else apply.image_name(obj)
            try:
                apply_one(obj, fields, update, profile, job)
                outcome[kind + "s_updated"] += 1
            except Exception as exc:
                logger.exception("Could not update the credentials of %s %s", kind, obj_id)
                outcome["failed"].append({"kind": kind, "id": obj_id, "name": name, "error": str(exc)})
                _progress(job, "Could not update {} {}: {}".format(kind, name, exc))
        if done % PROGRESS_EVERY == 0 and done < len(items):
            _progress(job, "{} of {} {}s done".format(done, len(items), kind))


def run_update(job, cm_servers=None, cm_images=None, cm_username="", cm_ssh_key_id=None,
               cm_password="", cm_new_vm_password="", cm_scope="", **kwargs):
    """Plugin entry point (via update_job.py). Returns the 4-tuple CloudBolt
    plugins use: status, output, errors, outputs."""
    server_targets = cm_servers or []
    image_targets = cm_images or []
    profile = getattr(job, "owner", None)
    outcome = {"scope": cm_scope, "servers_updated": 0, "images_updated": 0, "missing": 0, "failed": []}
    try:
        fields_used = {f for _obj_id, fields in server_targets + image_targets for f in fields}
        if ("password" in fields_used and not cm_password) or ("new_vm_password" in fields_used and not cm_new_vm_password):
            return "FAILURE", "", (
                "This update's passwords were cleared when it first ran, so it cannot run again. "
                "Start a new update from Admin > Credentials Manager."
            ), outcome
        update = {
            "username": cm_username,
            "password": cm_password,
            "new_vm_password": cm_new_vm_password,
            "key_cfv": None,
            "key_label": "",
        }
        if "ssh_key" in fields_used:
            key = SSHKey.objects.select_related("key_reference", "resource_handler").filter(id=cm_ssh_key_id).first()
            if key is None:
                return "FAILURE", "", "The SSH key this update sets was deleted; nothing was changed.", outcome
            update["key_cfv"] = key.key_reference
            update["key_label"] = targets.key_label(key)

        _progress(job, "Updating the credentials CloudBolt stores for {} server(s) and {} image(s). {}".format(
            len(server_targets), len(image_targets), cm_scope
        ))
        _run_kind(job, "server", server_targets, Server, apply.apply_to_server, update, profile, outcome)
        _run_kind(job, "image", image_targets, OSBuildAttribute, apply.apply_to_image, update, profile, outcome)
    finally:
        _clear_secrets(job)

    failed = outcome["failed"]
    updated = outcome["servers_updated"] + outcome["images_updated"]
    summary = "Updated {} server(s) and {} image(s).".format(outcome["servers_updated"], outcome["images_updated"])
    if outcome["missing"]:
        summary += " Skipped {} that no longer exist.".format(outcome["missing"])
    errors = ""
    if failed:
        errors = "{} could not be updated: {}".format(
            len(failed), "; ".join("{} {}: {}".format(f["kind"], f["name"], f["error"]) for f in failed[:20])
        )
    outcome["failed"] = failed[:FAILURES_KEPT]
    if failed and not updated:
        return "FAILURE", summary, errors, outcome
    if failed or outcome["missing"]:
        return "WARNING", summary, errors, outcome
    return "SUCCESS", summary, "", outcome


def recent_updates(limit=50):
    """Rows for the History tab, newest first."""
    rows = []
    for job in update_jobs().select_related("owner")[:limit]:
        outputs = job.outputs if isinstance(job.outputs, dict) else {}
        rows.append({
            "job": job,
            "owner": str(job.owner) if job.owner else "",
            "started": job.start_date or job.created_date,
            "active": job.status in ACTIVE_STATUSES,
            "status": job.status,
            "scope": outputs.get("scope") or job.label,
            "servers_updated": outputs.get("servers_updated"),
            "images_updated": outputs.get("images_updated"),
            "failed": len(outputs.get("failed") or []),
            "missing": outputs.get("missing") or 0,
        })
    return rows
