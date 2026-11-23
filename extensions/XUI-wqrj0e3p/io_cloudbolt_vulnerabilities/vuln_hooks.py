"""
The plugins the Vulnerabilities jobs run, the two recurring jobs that run them, and
the post-provisioning action.

Each plugin is a CloudBoltHook whose module file is one of this extension's files, by
absolute path (the way CloudBolt's own built-in actions point at their
files). It is created the first time a scan or setup starts and re-pointed if
the extension ever moves, so nothing else has to be synced.

The nightly re-match and the weekly inventory are RecurringActionJob rows made
by ensure_schedules(), always off: an admin turns them on. The scan after
provisioning is a HookPointAction on the Post-Provision hook point, made by
apply_provision_setting() only when the setting is turned on.
"""
import os

from django.db import IntegrityError, transaction

from cbhooks.models import CloudBoltHook
from utilities.logger import ThreadLogger

logger = ThreadLogger(__name__)

SCAN_NAME = "Vulnerability Scan"
SCAN_DESCRIPTION = (
    "Collects the software list of one server and matches it against the vulnerability database for the "
    "Vulnerabilities UI extension, and records the result. Created and run by the extension; its code is the "
    "extension's vuln_job.py, so do not edit or delete it here."
)
SCAN_FILE = "vuln_job.py"

SETUP_NAME = "Vulnerability Scanner Setup"
SETUP_DESCRIPTION = (
    "Installs or updates the scanner and the vulnerability database for the Vulnerabilities UI extension. "
    "Created and run by the extension; its code is the extension's vuln_setup_job.py, so do not edit or "
    "delete it here."
)
SETUP_FILE = "vuln_setup_job.py"

REMATCH_NAME = "Vulnerability Nightly Re-match"
REMATCH_DESCRIPTION = (
    "Matches the stored software lists of servers against the newest vulnerability database for the "
    "Vulnerabilities UI extension, and removes the findings of deleted servers. Created by the extension; its "
    "code is the extension's vuln_rematch_job.py, so do not edit or delete it here."
)
REMATCH_FILE = "vuln_rematch_job.py"

INVENTORY_NAME = "Vulnerability Weekly Inventory"
INVENTORY_DESCRIPTION = (
    "Collects a fresh software list from every Linux server that can be scanned without a person, for the "
    "Vulnerabilities UI extension. Created by the extension; its code is the extension's vuln_inventory_job.py, "
    "so do not edit or delete it here."
)
INVENTORY_FILE = "vuln_inventory_job.py"

PROVISION_NAME = "Vulnerability Scan After Provisioning"
PROVISION_DESCRIPTION = (
    "Starts a vulnerability scan of each new server as its own job when a build finishes, for the "
    "Vulnerabilities UI extension. Created by the extension when the setting is turned on; its code is the "
    "extension's vuln_provision_job.py, so do not edit or delete it here."
)
PROVISION_FILE = "vuln_provision_job.py"

# The hook point CloudBolt runs at the end of a provisioning job (initialize/cb_minimal.py: name post_provision).
PROVISION_HOOK_POINT = "post_provision"
PROVISION_ACTION_NAME = "Vulnerability scan of new servers"
PROVISION_ACTION_DESCRIPTION = (
    "Starts a vulnerability scan of each new server as its own job. The build does not wait for the scan and "
    "never fails because of it. Turned on and off by the setting Scan new servers after they are built on "
    "the Vulnerability Settings page."
)

# key, name (the job's and its plugin's), cron text, what the job does in one sentence.
SCHEDULES = (
    ("rematch", REMATCH_NAME, "0 3 * * *",
     "Matches the stored software lists against the newest vulnerability database and removes the findings "
     "of deleted servers."),
    ("inventory", INVENTORY_NAME, "0 1 * * 0",
     "Collects a fresh software list from every Linux server that can be scanned without a person."),
)


def _hook(name, description, filename):
    """The plugin called name, created on first use and kept pointing at filename."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), filename)
    hook = CloudBoltHook.objects.filter(name=name).first()
    if hook is None:
        hook = CloudBoltHook(name=name, description=description, shared=False)
        hook.module_file = path
        try:
            hook.save()
        except IntegrityError:
            # Another request created it first.
            hook = CloudBoltHook.objects.filter(name=name).first()
            if hook is None:
                raise
    elif str(hook.module_file) != path:
        hook.module_file = path
        hook.save()
    return hook


def scan_hook():
    """The per-server plugin, created on first use and kept pointing at vuln_job.py."""
    return _hook(SCAN_NAME, SCAN_DESCRIPTION, SCAN_FILE)


def setup_hook():
    """The scanner setup plugin, created on first use and kept pointing at vuln_setup_job.py."""
    return _hook(SETUP_NAME, SETUP_DESCRIPTION, SETUP_FILE)


def rematch_hook():
    """The nightly re-match plugin, created on first use and kept pointing at vuln_rematch_job.py."""
    return _hook(REMATCH_NAME, REMATCH_DESCRIPTION, REMATCH_FILE)


def inventory_hook():
    """The weekly inventory plugin, created on first use and kept pointing at vuln_inventory_job.py."""
    return _hook(INVENTORY_NAME, INVENTORY_DESCRIPTION, INVENTORY_FILE)


def provision_hook():
    """The scan-after-provisioning plugin, created on first use and kept pointing at vuln_provision_job.py."""
    return _hook(PROVISION_NAME, PROVISION_DESCRIPTION, PROVISION_FILE)


# ---------------------------------------------------------------- recurring jobs
def _ensure_schedule(key, name, cron, description):
    """The RecurringActionJob called name, made off when it does not exist. An existing row keeps
    its schedule and its enabled flag; only its plugin is re-pointed when it uses another one."""
    from cbhooks.models import RecurringActionJob

    hook = {"rematch": rematch_hook, "inventory": inventory_hook}[key]()
    row = RecurringActionJob.objects.filter(name=name).first()
    if row is None:
        try:
            # The idiom of cbhooks/views.py create_recurring_job_action (line 4255) and initialize/create_objects.py
            # create_recurring_job (line 1133): type is the hook's type_slug, and enabled is passed explicitly as
            # False so the Auto Enable Actions preference never switches it on. name is unique on RecurringJob
            # (jobs/models.py line 2391), so a second creator raises IntegrityError and re-reads the winner's row.
            with transaction.atomic():
                row = RecurringActionJob.objects.create(
                    hook=hook, name=name, type=hook.type_slug, owner=None, enabled=False, schedule=cron,
                    description=description,
                )
        except IntegrityError:
            row = RecurringActionJob.objects.filter(name=name).first()
            if row is None:
                raise
    elif row.hook_id != hook.pk:
        row.hook = hook
        row.save(update_fields=["hook"])
    return {
        "key": key,
        "name": name,
        "schedule": row.schedule,
        "enabled": bool(row.enabled),
        # A naive datetime in the appliance's local time (USE_TZ is off), or None before the first run.
        "last_run": row.last_run,
        # jobs/models.py RecurringJob.get_absolute_url (line 2538): the recurring job's detail page.
        "url": row.get_absolute_url(),
    }


def ensure_schedules():
    """The two recurring jobs, made when missing, always off. Returns one dict per job:
    key ("rematch" or "inventory"), name, schedule (cron text), enabled, last_run (datetime or None) and
    url (the job's detail page). A job that could not be made or read comes back with an empty schedule
    and url, enabled False and error set to a sentence; this function never raises."""
    found = []
    for key, name, cron, description in SCHEDULES:
        try:
            found.append(_ensure_schedule(key, name, cron, description))
        except Exception:
            logger.exception("Vulnerabilities: could not make or read the recurring job %s", name)
            found.append({
                "key": key, "name": name, "schedule": "", "enabled": False, "last_run": None, "url": "",
                "error": "The recurring job could not be made or read. The CloudBolt application log has the details.",
            })
    return found


# ---------------------------------------------------------------- scan after provisioning
def _provision_actions(hook):
    """The post-provision actions that run the scan plugin (normally one)."""
    from cbhooks.models import HookPointAction

    return list(HookPointAction.objects.filter(hook=hook, hook_point__name=PROVISION_HOOK_POINT))


def apply_provision_setting(enabled):
    """Make the Post-Provision action match the scan_on_provision setting. On: make it when it does
    not exist (on, and carrying on when it fails, so a scan problem never fails a build) and switch an
    existing one on. Off: switch an existing one off; nothing is ever made while the setting is off."""
    from cbhooks.models import HookPoint, HookPointAction

    if not enabled:
        hook = CloudBoltHook.objects.filter(name=PROVISION_NAME).first()
        if hook is None:
            return
        for action in _provision_actions(hook):
            if action.enabled:
                action.enabled = False
                action.save()
        return

    hook = provision_hook()
    actions = _provision_actions(hook)
    if not actions:
        hook_point = HookPoint.objects.filter(name=PROVISION_HOOK_POINT).first()
        if hook_point is None:
            raise RuntimeError("CloudBolt has no Post-Provision hook point.")
        # The fields c2_wrapper.py (lines 1830-1865) sets when it makes an action at a hook point: the plugin
        # (hook), the hook point, a name and enabled. cbhooks/models.py HookPointAction (line 3707): save() fills
        # run_seq; run_on_statuses "" means every job status (cbhooks/__init__.py line 241); continue_on_failure
        # stops a failing action from failing the build (cbhooks/__init__.py lines 130-155).
        HookPointAction.objects.create(
            hook=hook, hook_point=hook_point, name=PROVISION_ACTION_NAME, description=PROVISION_ACTION_DESCRIPTION,
            enabled=True, continue_on_failure=True, run_on_statuses="",
        )
        return
    for action in actions:
        if not action.enabled:
            action.enabled = True
            action.save()


def provision_action_state():
    """{"exists": bool, "enabled": bool} for the Post-Provision action. Reads only; never makes it."""
    try:
        hook = CloudBoltHook.objects.filter(name=PROVISION_NAME).first()
        if hook is None:
            return {"exists": False, "enabled": False}
        actions = _provision_actions(hook)
        return {"exists": bool(actions), "enabled": any(action.enabled for action in actions)}
    except Exception:
        logger.exception("Vulnerabilities: could not read the Post-Provision action")
        return {"exists": False, "enabled": False}
