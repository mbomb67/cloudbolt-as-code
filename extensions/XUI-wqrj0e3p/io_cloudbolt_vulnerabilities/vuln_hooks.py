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

RESOURCE_SCAN_NAME = "Vulnerability Resource Scan"
RESOURCE_SCAN_DESCRIPTION = (
    "Scans every server of one resource as its own job, for the Vulnerabilities UI extension, when someone "
    "chooses Scan all servers on the resource's Vulnerabilities tab. Created by the extension; its code is the "
    "extension's vuln_resource_job.py, so do not edit or delete it here."
)
RESOURCE_SCAN_FILE = "vuln_resource_job.py"

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

PATCH_NAME = "Vulnerability Install Updates"
PATCH_DESCRIPTION = (
    "Installs the available updates on a Linux or Windows Server 2022 or 2025 server and scans it again, for the Vulnerabilities UI "
    "extension. Created by the extension when the setting Offer Install updates is turned on; its code is the "
    "extension's vuln_patch_job.py, so do not edit or delete it here."
)
PATCH_FILE = "vuln_patch_job.py"

CONDITION_NAME = "Vulnerability Install Updates Display Condition"
CONDITION_DESCRIPTION = (
    "Decides which servers show the Install Updates action: Linux and Windows servers that are not in an "
    "Unassigned group. Created by the extension; its code is the extension's vuln_patch_condition.py, so do "
    "not edit or delete it here."
)
CONDITION_FILE = "vuln_patch_condition.py"

PATCH_ACTION_LABEL = "Install Updates"
# The spelling of the server actions exported from CloudBolt (server_actions/*/*_metadata.json): Font Awesome
# 5 style and name, as in "fas fa-redo".
PATCH_ACTION_ICON = "fas fa-download"
PATCH_DIALOG_MESSAGE = (
    "Installs all available updates on this server from its configured repositories. The server may restart."
)
PATCH_SUBMIT_LABEL = "Install updates"
# The input is the token in vuln_patch_job.py; core names the HookInput <name>_a<hook id>.
RESTART_INPUT = "restart_if_needed"
RESTART_LABEL = "Restart if needed"
RESTART_DESCRIPTION = "Restart the server when an update needs it."

RESOURCE_PATCH_NAME = "Vulnerability Install Updates on All Servers"
RESOURCE_PATCH_DESCRIPTION = (
    "Installs the available updates, one server at a time, on the Linux and Windows servers of one resource, for "
    "the Vulnerabilities UI extension. Created by the extension when the setting Offer Install updates is turned "
    "on; its code is the extension's vuln_resource_patch_job.py, so do not edit or delete it here."
)
RESOURCE_PATCH_FILE = "vuln_resource_patch_job.py"

RESOURCE_CONDITION_NAME = "Vulnerability Install Updates on All Servers Display Condition"
RESOURCE_CONDITION_DESCRIPTION = (
    "Decides which resources show the Install Updates on All Servers action: resources with at least one Linux "
    "or Windows server that is not in an Unassigned group. Created by the extension; its code is the "
    "extension's vuln_resource_patch_condition.py, so do not edit or delete it here."
)
RESOURCE_CONDITION_FILE = "vuln_resource_patch_condition.py"

RESOURCE_ACTION_LABEL = "Install Updates on All Servers"
RESOURCE_DIALOG_MESSAGE = (
    "Installs all available updates on the servers of this resource, one server at a time. The run stops at the "
    "first server that fails. Servers may restart."
)
RESOURCE_SUBMIT_LABEL = "Install updates"

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


def resource_scan_hook():
    """The resource scan plugin, created on first use and kept pointing at vuln_resource_job.py."""
    return _hook(RESOURCE_SCAN_NAME, RESOURCE_SCAN_DESCRIPTION, RESOURCE_SCAN_FILE)


def provision_hook():
    """The scan-after-provisioning plugin, created on first use and kept pointing at vuln_provision_job.py."""
    return _hook(PROVISION_NAME, PROVISION_DESCRIPTION, PROVISION_FILE)


def patch_hook():
    """The install updates plugin, created on first use and kept pointing at vuln_patch_job.py."""
    return _hook(PATCH_NAME, PATCH_DESCRIPTION, PATCH_FILE)


def condition_hook():
    """The Display Condition plugin of the Install Updates action, created on first use and kept pointing at
    vuln_patch_condition.py."""
    return _hook(CONDITION_NAME, CONDITION_DESCRIPTION, CONDITION_FILE)


def resource_patch_hook():
    """The install updates on all servers plugin, created on first use and kept pointing at
    vuln_resource_patch_job.py."""
    return _hook(RESOURCE_PATCH_NAME, RESOURCE_PATCH_DESCRIPTION, RESOURCE_PATCH_FILE)


def resource_condition_hook():
    """The Display Condition plugin of the Install Updates on All Servers action, created on first use and kept
    pointing at vuln_resource_patch_condition.py."""
    return _hook(RESOURCE_CONDITION_NAME, RESOURCE_CONDITION_DESCRIPTION, RESOURCE_CONDITION_FILE)


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
        "last_run": _last_run(row),
        # jobs/models.py RecurringJob.get_absolute_url (line 2538): the recurring job's detail page.
        "url": row.get_absolute_url(),
    }


def _last_run(row):
    """When the recurring job last ran, by hand or on its schedule. RecurringJob.last_run is written
    only by the scheduler, so a job that was only ever run by hand would read "never"; the newest
    job it spawned (Job.recurring_job, related name spawned_jobs, jobs/models.py line 1054) covers both."""
    try:
        started = row.spawned_jobs.order_by("-id").values_list("start_date", flat=True).first()
    except Exception:
        started = None
    return started or row.last_run


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


# ---------------------------------------------------------------- install updates (server action)
def ensure_restart_input(hook):
    """The Restart if needed input of the install plugin, as a checkbox. hook.save() already made it as a
    text input from the token in vuln_patch_job.py (cbhooks/models.py lines 525-544 and 1836-1854); this
    turns it into what core's own actions use (c2_wrapper.py update_or_create_hook_input, lines 1985-2050):
    type BOOL, not required, and hide_if_default_value off, because a default would otherwise hide the
    control (cbhooks/forms.py lines 1565-1571). It makes the input itself when the token pass did not
    (update_action_inputs failures on a first save are only logged, cbhooks/models.py lines 1051-1058). An input
    that is already a BOOL is left alone, so a label an admin edited stays."""
    from cbhooks.models import HookInput

    name = "{}_a{}".format(RESTART_INPUT, hook.id)
    field = hook.input_fields.filter(name=name).first()
    if field is None:
        return HookInput.objects.create(
            hook=hook, name=name, label=RESTART_LABEL, description=RESTART_DESCRIPTION, type="BOOL",
            required=False, hide_if_default_value=False, allow_multiple=False,
        )
    if field.type != "BOOL":
        field.label = RESTART_LABEL
        field.description = RESTART_DESCRIPTION
        field.type = "BOOL"
        field.required = False
        field.hide_if_default_value = False
        field.save()
    return field


def _default_restart_on(action, field):
    """Give a newly made action the mapping of the input with the default True (the way c2_wrapper.py
    get_or_create_hook_input_mapping, line 1962, does: a default is written only on a mapping that did not
    exist, so one an admin changed is never reset). update_input_mappings (cbhooks/models.py line 2107) makes
    the mapping; the value is a CustomFieldValue of the input (c2_wrapper.py line 1140)."""
    from orders.models import CustomFieldValue

    had = action.input_mappings.filter(hook_input=field).exists()
    action.update_input_mappings()
    if had:
        return
    mapping = action.input_mappings.filter(hook_input=field).first()
    if mapping is not None and mapping.default_value is None:
        mapping.default_value = CustomFieldValue.objects.get_or_create(field=field, value=True)[0]
        mapping.save()


def patch_actions(hook):
    """The server actions that run the install plugin (normally one)."""
    from cbhooks.models import ServerAction

    return list(ServerAction.objects.filter(hook=hook))


def _apply_server_action(enabled, create_only=False):
    """Make the Install Updates server action match the patching setting. On: make the plugin, its Restart
    if needed input with the default True, the Display Condition and the action when the action does not
    exist; an existing one is only switched on. Off: switch an existing action off and nothing else.

    The caller decides when to call it (the settings view calls it on a change to on, or when the action is
    missing); calling it with an existing, switched-off action switches it on, unless create_only is true: then
    an existing action is left exactly as it is and only a missing one is made.

    Everything an admin owns on the action is written only when it is first made and never again: label,
    Requires approval, Auto submit order, the condition, the role grants and the default of the input.
    The OS families of the plugin are never touched either: the condition hides the action, and step 3
    (Windows) must need no database change. Raises RuntimeError when the action could not be made or changed."""
    from cbhooks.models import ServerAction

    try:
        if not enabled:
            hook = CloudBoltHook.objects.filter(name=PATCH_NAME).first()
            if hook is None:
                return
            for action in patch_actions(hook):
                if action.enabled:
                    action.enabled = False
                    action.save()
            return

        hook = patch_hook()
        actions = patch_actions(hook)
        if not actions:
            field = ensure_restart_input(hook)
            condition = condition_hook()
            with transaction.atomic():
                # ServerAction.hook is not unique, so two first enables could each make an action. A row lock
                # on the plugin (select_for_update, valid inside this atomic block) makes the second one wait,
                # and it looks again before making one.
                CloudBoltHook.objects.select_for_update().filter(pk=hook.pk).first()
                if patch_actions(hook):
                    return
                # The fields core sets when it makes a server action (c2_wrapper.py line 1878, ServerAction in
                # cbhooks/models.py line 2970): the plugin, a label, enabled and the dialog texts. A button for
                # every server needs no targets (infrastructure/models.py lines 3736-3741). is_synchronous stays
                # False because synchronous plus approval is refused (cbhooks/views.py line 3114).
                action = ServerAction.objects.create(
                    hook=hook, label=PATCH_ACTION_LABEL, enabled=True, extra_classes=PATCH_ACTION_ICON,
                    submit_button_label=PATCH_SUBMIT_LABEL, dialog_message=PATCH_DIALOG_MESSAGE,
                    is_synchronous=False, condition=condition,
                )
                _default_restart_on(action, field)
            return
        if create_only:
            return
        for action in actions:
            if not action.enabled:
                action.enabled = True
                action.save()
    except Exception as exc:
        logger.exception("Vulnerabilities: could not switch the Install Updates action")
        raise RuntimeError("The Install Updates action could not be switched.") from exc


def patch_action_state():
    """{"exists": bool, "enabled": bool, "action_id": int or None} for the Install Updates action. Reads only;
    never makes it and never raises."""
    try:
        hook = CloudBoltHook.objects.filter(name=PATCH_NAME).first()
        if hook is None:
            return {"exists": False, "enabled": False, "action_id": None}
        actions = patch_actions(hook)
        if not actions:
            return {"exists": False, "enabled": False, "action_id": None}
        chosen = next((a for a in actions if a.enabled), actions[0])
        return {"exists": True, "enabled": bool(chosen.enabled), "action_id": chosen.pk}
    except Exception:
        logger.exception("Vulnerabilities: could not read the Install Updates action")
        return {"exists": False, "enabled": False, "action_id": None}


def patch_button(server, profile, request=None):
    """{"url", "label", "action_id"} of the Install Updates button for this server and user, or None. Only
    when the setting is on, the action is enabled, its Display Condition accepts the server (server_action_dict
    returns None otherwise, cbhooks/models.py line 3257) and the user may run it (server_action_permitted,
    utilities/permissions.py line 437). Never raises."""
    try:
        from utilities.permissions import server_action_permitted
        from . import vuln_store

        if not vuln_store.get_settings().get("patching"):
            return None
        hook = CloudBoltHook.objects.filter(name=PATCH_NAME).first()
        if hook is None:
            return None
        for action in patch_actions(hook):
            if not action.enabled:
                continue
            shown = action.server_action_dict(server, request)
            if not shown or not shown.get("url"):
                continue
            if not server_action_permitted(profile, server, action):
                continue
            return {"url": shown["url"], "label": action.label or PATCH_ACTION_LABEL, "action_id": action.pk}
        return None
    except Exception:
        logger.exception("Vulnerabilities: could not work out the Install Updates button for server %s",
                         getattr(server, "id", ""))
        return None


# ---------------------------------------------------------------- install updates on all servers (resource action)
def resource_patch_actions(hook):
    """The resource actions that run the install on all servers plugin (normally one)."""
    from cbhooks.models import ResourceAction

    return list(ResourceAction.objects.filter(hook=hook))


def _apply_resource_action(enabled, create_only=False):
    """Make the Install Updates on All Servers resource action match the patching setting, under the rules of
    _apply_server_action: made once, then only switched on and off through enabled. Its plugin, its Restart if
    needed input with the default True and its Display Condition are made with it.

    ResourceAction has no resource type field and blueprints stays empty (cbhooks/models.py line 2683: none
    selected means every resource), so the action is offered to every resource and the condition decides
    (resources/models.py available_actions, lines 525-535). list_view_visible is False so it is not offered
    for a bulk run from a list (cbhooks/models.py line 2721); an admin may change that. Label, Requires approval,
    Auto submit order, the condition, list_view_visible, the role grants and the default of the input are written
    only when the action is made. Raises RuntimeError when it could not be made or changed."""
    from cbhooks.models import ResourceAction

    try:
        if not enabled:
            hook = CloudBoltHook.objects.filter(name=RESOURCE_PATCH_NAME).first()
            if hook is None:
                return
            for action in resource_patch_actions(hook):
                if action.enabled:
                    action.enabled = False
                    action.save()
            return

        hook = resource_patch_hook()
        actions = resource_patch_actions(hook)
        if not actions:
            field = ensure_restart_input(hook)
            condition = resource_condition_hook()
            with transaction.atomic():
                # ResourceAction.hook is not unique either: the row lock on the plugin makes a second first
                # enable wait and look again before it makes one.
                CloudBoltHook.objects.select_for_update().filter(pk=hook.pk).first()
                if resource_patch_actions(hook):
                    return
                # The idiom of cbhooks/views.py line 4039 (hook, enabled, label) plus the dialog texts and the
                # icon, as the server action. is_synchronous stays False because synchronous plus approval is
                # refused for resource actions (cbhooks/views.py line 4119).
                action = ResourceAction.objects.create(
                    hook=hook, label=RESOURCE_ACTION_LABEL, enabled=True, extra_classes=PATCH_ACTION_ICON,
                    submit_button_label=RESOURCE_SUBMIT_LABEL, dialog_message=RESOURCE_DIALOG_MESSAGE,
                    is_synchronous=False, list_view_visible=False, condition=condition,
                )
                _default_restart_on(action, field)
            return
        if create_only:
            return
        for action in actions:
            if not action.enabled:
                action.enabled = True
                action.save()
    except Exception as exc:
        logger.exception("Vulnerabilities: could not switch the Install Updates on All Servers action")
        raise RuntimeError("The Install Updates on All Servers action could not be switched.") from exc


def apply_patching_setting(enabled, create_only=False):
    """Make both Install Updates actions match the patching setting: the server action and the resource action
    Install Updates on All Servers. Each half runs in its own try, so one failing does not hide the other; a
    RuntimeError is raised afterwards when either failed. create_only leaves existing actions as they are and
    only makes missing ones: the settings view passes it when the setting did not change, so an action an admin
    switched off in the action editor stays off while a missing one (an appliance that had the server action
    before the resource action existed) is made."""
    failed = []
    for name, apply in (("server", _apply_server_action), ("resource", _apply_resource_action)):
        try:
            apply(enabled, create_only=create_only)
        except Exception:
            failed.append(name)
    if failed:
        raise RuntimeError("The Install Updates {} action could not be switched.".format(" and ".join(failed)))


def resource_patch_action_state():
    """{"exists": bool, "enabled": bool, "action_id": int or None} for the Install Updates on All Servers action.
    Reads only; never makes it and never raises."""
    try:
        hook = CloudBoltHook.objects.filter(name=RESOURCE_PATCH_NAME).first()
        if hook is None:
            return {"exists": False, "enabled": False, "action_id": None}
        actions = resource_patch_actions(hook)
        if not actions:
            return {"exists": False, "enabled": False, "action_id": None}
        chosen = next((a for a in actions if a.enabled), actions[0])
        return {"exists": True, "enabled": bool(chosen.enabled), "action_id": chosen.pk}
    except Exception:
        logger.exception("Vulnerabilities: could not read the Install Updates on All Servers action")
        return {"exists": False, "enabled": False, "action_id": None}


def resource_patch_button(resource, profile, request=None):
    """{"url", "label", "action_id"} of the Install Updates on All Servers dialog for this resource and user, or
    None. Only when the setting is on, the action is enabled, core lists it for this resource
    (Resource.available_actions, resources/models.py line 443, which applies the blueprint, plug-in and Display
    Condition rules and builds the dialog URL with reverse("run_resource_action", ...)) and the user may run it
    (resource_action_permitted, utilities/permissions.py line 527). An action an admin turned into a new-tab
    link has no dialog and gives None. Never raises."""
    try:
        from utilities.permissions import resource_action_permitted
        from . import vuln_store

        if not vuln_store.get_settings().get("patching"):
            return None
        hook = CloudBoltHook.objects.filter(name=RESOURCE_PATCH_NAME).first()
        if hook is None:
            return None
        mine = {action.pk: action for action in resource_patch_actions(hook) if action.enabled}
        if not mine:
            return None
        for shown in resource.available_actions(request):
            action = mine.get(getattr(shown.get("action"), "pk", None))
            if action is None or shown.get("not_dialog") or not shown.get("url"):
                continue
            if not resource_action_permitted(profile, resource, action):
                continue
            return {"url": shown["url"], "label": action.label or RESOURCE_ACTION_LABEL, "action_id": action.pk}
        return None
    except Exception:
        logger.exception("Vulnerabilities: could not work out the Install Updates on All Servers button for "
                         "resource %s", getattr(resource, "id", ""))
        return None


def server_patch_permitted(profile, server):
    """True when the enabled Install Updates server action is permitted for this user on this server
    (server_action_permitted, utilities/permissions.py line 437). The resource action's job asks this for every
    server, because core's check for a resource action says nothing about the servers inside it. Never raises."""
    try:
        from utilities.permissions import server_action_permitted

        hook = CloudBoltHook.objects.filter(name=PATCH_NAME).first()
        if hook is None:
            return False
        return any(server_action_permitted(profile, server, action)
                   for action in patch_actions(hook) if action.enabled)
    except Exception:
        logger.exception("Vulnerabilities: could not check the Install Updates permission for server %s",
                         getattr(server, "id", ""))
        return False


# ---------------------------------------------------------------- install updates on several servers (Servers view)
BULK_NO_ACTION = "The Install Updates action does not exist yet. Turn on installing updates in Vulnerability Settings."
BULK_DISABLED = "The Install Updates action is switched off."
BULK_NOT_ALLOWED = "The Install Updates action does not allow bulk operation. A CloudBolt admin can allow it on the action."
BULK_NOT_AT_ONCE = "The Install Updates action cannot be run on several servers at once as it is set up now."
BULK_UNREADABLE = "Install updates could not be checked. The CloudBolt application log has the details."


def _restart_default(hook, action):
    """The value the Restart if needed checkbox of the action's dialog starts with, so the Servers view's own
    checkbox starts the same (core reads it with get_default_values_for_hook_input, common/fields.py line 696: the
    mapping of the input on the action, a mapping without a default value counts as none). True when the input
    or its mapping cannot be found or read; False when the mapping carries no default (the dialog's checkbox
    starts unchecked then)."""
    try:
        from . import vuln_patch

        field = hook.input_fields.filter(name="{}_a{}".format(RESTART_INPUT, hook.id)).first()
        if field is None:
            return True
        mapping = action.input_mappings.filter(hook_input=field).first()
        if mapping is None:
            return True
        default = getattr(mapping, "default_value", None)
        if default is None:
            return False
        return bool(vuln_patch.parse_restart_flag(default.value, True))
    except Exception:
        logger.exception("Vulnerabilities: could not read the default of the Restart if needed input")
        return True


def patch_bulk_action():
    """(state, action) for running Install Updates on several servers from the Servers view. state is
    {"enabled", "why", "action_label", "restart_default", "requires_approval", "parallel"}; action is the ServerAction to run (the enabled one when
    several exist) when enabled, else None. why says in one sentence why it is not enabled (the sentences above and
    vuln_patch.PATCHING_OFF). The checks are the ones core's own bulk dialog applies before it lists an action
    (infrastructure/views.py servers_batch_server_actions: list_view_visible, no new_tab_url, a custom form leaves
    the dialog; a synchronous action runs inline there, not as a job) plus the setting and the action being enabled.
    requires_approval says the run will become an order that creates one job per server after approval, not one
    job for all (cbhooks/models.py _save_action_items). parallel is the setting "Servers updated at the same time".
    Reads the setting, the plugin and its action once;
    never raises."""
    state = {"enabled": False, "why": "", "action_label": PATCH_ACTION_LABEL, "restart_default": True,
             "requires_approval": False, "parallel": 5}
    try:
        from . import vuln_patch, vuln_store

        settings = vuln_store.get_settings()
        state["parallel"] = int(settings.get("patch_parallel") or vuln_store.DEFAULT_SETTINGS["patch_parallel"])
        if not settings.get("patching"):
            return dict(state, why=vuln_patch.PATCHING_OFF), None
        hook = CloudBoltHook.objects.filter(name=PATCH_NAME).first()
        actions = patch_actions(hook) if hook is not None else []
        if not actions:
            return dict(state, why=BULK_NO_ACTION), None
        action = next((a for a in actions if a.enabled), actions[0])
        state["action_label"] = action.label or PATCH_ACTION_LABEL
        state["requires_approval"] = bool(getattr(action, "requires_approval", False))
        if not action.enabled:
            return dict(state, why=BULK_DISABLED), None
        if not action.list_view_visible:
            return dict(state, why=BULK_NOT_ALLOWED), None
        if action.new_tab_url or action.custom_form_id or getattr(action, "is_synchronous", False):
            return dict(state, why=BULK_NOT_AT_ONCE), None
        return dict(state, enabled=True, restart_default=_restart_default(hook, action)), action
    except Exception:
        logger.exception("Vulnerabilities: could not read the Install Updates action for a bulk run")
        return dict(state, why=BULK_UNREADABLE), None


def patch_bulk_state():
    """The state half of patch_bulk_action(): {"enabled", "why", "action_label", "restart_default",
    "requires_approval"}. Never raises."""
    return patch_bulk_action()[0]


def queued_update_servers():
    """{server id: job id} of the servers that belong to an Install Updates job (the server action's or the
    resource action's plugin) that has not ended (Job.ACTIVE_STATUSES: initializing, pending, queued, running...).
    A bulk or resource run marks a server busy only while it works on it, so this finds the ones still waiting.
    Never one query per server. (1) The active jobs whose HookParameters.hook is one of the two plugins, reached
    from Job.job_parameters the way core's own queries reach a child's fields (job_parameters__triggerparameters__,
    cbhooks/views.py line 2275); (2) the active jobs of an approved order item of either action: with Requires
    approval core makes one ActionJobOrderItem per server (cbhooks/models.py _save_action_items), found by
    job_parameters__orderitem__actionjoborderitem__ (orders/models.py lines 1667 and 2362 use the same chain)
    with the action's content type and id; a server action's item names its server, a resource action's does not,
    so those jobs go through (3): Server.jobs, the link core makes for every server of a job
    (cbhooks/models.py run_as_job). The actions' ids are two small lookups. UNCONFIRMED: that an approved item's
    job has the order item itself as its job parameters (read from the model chain, not run). When a server is in
    several jobs the oldest is reported. Returns {} when it cannot be read (logged): an unknown queue is no queue."""
    try:
        from django.contrib.contenttypes.models import ContentType
        from django.db.models import Q
        from cbhooks.models import ResourceAction, ServerAction
        from infrastructure.models import Server
        from jobs.models import Job

        found = {}

        def note(server_id, job_id):
            if server_id not in found or job_id < found[server_id]:
                found[server_id] = job_id

        job_ids = set(Job.objects.filter(
            status__in=Job.ACTIVE_STATUSES,
            job_parameters__hookparameters__hook__name__in=[PATCH_NAME, RESOURCE_PATCH_NAME],
        ).values_list("id", flat=True))
        item = "job_parameters__orderitem__actionjoborderitem__"
        condition = None
        for model, name in ((ServerAction, PATCH_NAME), (ResourceAction, RESOURCE_PATCH_NAME)):
            ids = list(model.objects.filter(hook__name=name).values_list("pk", flat=True))
            if ids:
                part = Q(**{item + "content_type": ContentType.objects.get_for_model(model),
                            item + "action_type_id__in": ids})
                condition = part if condition is None else condition | part
        if condition is not None:
            for job_id, server_id in Job.objects.filter(condition, status__in=Job.ACTIVE_STATUSES).values_list(
                    "id", item + "server_id"):
                if server_id:
                    note(server_id, job_id)
                else:
                    job_ids.add(job_id)
        if job_ids:
            for server_id, job_id in Server.jobs.through.objects.filter(job_id__in=sorted(job_ids)).values_list(
                    "server_id", "job_id"):
                note(server_id, job_id)
        return found
    except Exception:
        logger.exception("Vulnerabilities: could not read the servers waiting in update jobs")
        return {}
