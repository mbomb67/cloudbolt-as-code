"""
Runbook tasks: the shared logic behind the Run Task action.

Run Task (server_actions/SVA-4sin3s0q) is one button on a VM. Its dropdown
lists the Remote Scripts that can run on that VM, and it runs the chosen
one there with CloudBolt's own Remote Script rendering and transport (VMware
Tools, WinRM, SSH, Azure Run Command or AWS SSM). Its custom form
(forms/FRM-a5edrdj4) calls the Runbook Tasks webhook (webhooks/IWH-cdf543g3)
for the task list, for the chosen task's inputs, and to save credentials on a
server that has none.

A Remote Script is offered on a VM when it passes the checks CloudBolt
makes before it runs a Remote Script on a VM:
  - no Run on Server is set (such a script always runs on that one host);
  - the VM's resource handler can run scripts on servers;
  - its OS families are empty, or include the VM's OS family or a parent
    of it;
  - its resource technologies, environments, groups and configuration
    managers are empty, or include the VM's.
A bulk run offers only the tasks that pass on every selected VM.

Credentials, in CloudBolt's order: the Remote Script's saved credentials, then
the VM's. A VM whose handler runs scripts without guest credentials
(tech-specific execution on Azure or AWS) needs none. Any other VM with no
usable username and password (or SSH key, for Linux over SSH) needs
credentials, and the form offers to save them on the VM, as CloudBolt's
own "save credentials" option does.

Secrets: CloudBolt writes a server action's custom-form payload and its
plain-text inputs to the job log, so Run Task never carries a password. A
task's password-type inputs (PWD, ETXT) are not offered, and a task that
requires one cannot run from Run Task.
"""
import html
import json
import os
import re
import time

from behavior_mapping.models import CustomFieldMapping
from cbhooks.models import CopyFileAction, OrchestrationHook, RemoteScriptHook
from common.methods import set_progress
from cbhooks.services.parsing import get_hook_action_input_dict
from infrastructure.models import Server
from utilities.events import add_server_event
from utilities.logger import ThreadLogger
from utilities.permissions import get_cb_permissions

logger = ThreadLogger(__name__)

SECRET_TYPES = {"PWD", "ETXT"}
# Inputs that hold CloudBolt objects (networks, LDAP utilities, files, ...)
# have no text form Run Task can collect.
PLAIN_TYPES = {"STR", "INT", "DEC", "IP", "DT", "DTM", "TXT", "CODE", "BOOL", "URL"}
OUTPUT_LIMIT = 4000
# Azure's reply when a VM is already running a Run Command; only one script
# runs at a time (https://learn.microsoft.com/azure/virtual-machines/windows/run-command,
# Restrictions). Seen live as "(Conflict) Run command extension execution is in progress".
AZURE_BUSY = "Run command extension execution is in progress"
BUSY_WAIT_SECONDS = 20
BUSY_WAIT_LIMIT = 15


class TaskError(Exception):
    """A request Run Task refuses. The message is for the person running it."""


# ---- servers and permissions ----------------------------------------------


def resolve_server(value, profile=None):
    """
    Return the active Server that value names (numeric id or SVR- global ID).
    With a profile, the profile must be allowed to see the VM.
    """
    text = str(value or "").strip()
    servers = Server.objects.exclude(status="HISTORICAL")
    if text.isdigit():
        servers = servers.filter(id=int(text))
    else:
        servers = servers.filter(global_id=text)
    if profile is not None:
        servers = servers.filter(id__in=Server.objects_for_profile(profile).values("id"))
    server = servers.first()
    if server is None:
        raise TaskError(f"There is no VM '{text}' that you can see.")
    return server


def can_manage_credentials(profile, server):
    """The permission CloudBolt's own credential prompt checks before saving."""
    return "server.manage_credentials" in get_cb_permissions(profile, server)


# ---- which Remote Scripts fit a VM, or a filter set ---------------------


def _lineage(os_family):
    """The OS family and its parents (Windows 2022 -> Windows)."""
    families = []
    while os_family is not None and os_family not in families:
        families.append(os_family)
        os_family = os_family.parent
    return families


def _names(objects):
    return ", ".join(sorted(str(getattr(obj, "name", obj)) for obj in objects))


class _Facts:
    """
    What the fit check compares, read once: one VM, or a filter set (the
    OS families, technologies, environments, groups and configuration
    managers a set of servers may have, such as a workflow's filters; an
    empty kind allows any value).

    An action fits a filter set only when it can run on every VM the set
    allows: a script restricted to Windows does not fit a set that allows any
    OS family.
    """

    def __init__(
        self, os_families=(), technologies=(), environments=(), groups=(), configuration_managers=(), server=None
    ):
        self.server = server
        self.handler = None
        self._managers = None
        if server is not None:
            handler = server.resource_handler
            self.handler = handler.cast() if handler else None
            os_families = [server.os_family]
            technologies = [handler.resource_technology if handler else None]
            environments = [server.environment]
            groups = [server.group]
        else:
            self._managers = set(configuration_managers)
        self.os_families = [family for family in os_families]
        self.os_lineages = [set(_lineage(family)) for family in self.os_families]
        self.technologies = list(technologies)
        self.environments = list(environments)
        self.groups = list(groups)

    @property
    def managers(self):
        """The VM's environment's configuration managers, or the filter set's."""
        if self._managers is None:
            env = self.server.environment if self.server is not None else None
            self._managers = set(env.get_connector_confs()) if env else set()
        return self._managers

    def describe(self, values, kind):
        if self.server is not None:
            value = values[0] if values else None
            return f"{self.server.hostname} has {value or f'no {kind}'}"
        listed = [value for value in values if value is not None]
        return f"the filters allow {_names(listed) if listed else f'any {kind}'}"


def _allows(allowed, values):
    """A restriction (empty = any) allows values when it holds every one of them."""
    return not allowed or (bool(values) and all(value in allowed for value in values))


def _concrete(hook):
    """The action's own type (RemoteScriptHook, CloudBoltHook, ...), not the base OrchestrationHook."""
    return hook.cast() if type(hook) is OrchestrationHook else hook


def refusal(hook, server=None, facts=None):
    """
    Why the action cannot run on server (or on every VM a filter set's
    facts allow), or '' when it can. Works for any action type: the OS family
    and Run on Server checks apply to the types that have them, and the
    can-run-scripts check to Remote Scripts and Copy File actions.
    """
    hook = _concrete(hook)
    facts = facts or _Facts(server=server)
    if getattr(hook, "run_on_server_id", None):
        return f"'{hook.name}' always runs on its Run on Server, not on the VM it is started from."
    if facts.server is not None and isinstance(hook, (RemoteScriptHook, CopyFileAction)):
        if not facts.handler or not facts.handler.can_run_scripts_on_servers:
            return f"The resource handler of {facts.server.hostname} cannot run scripts on servers."
    families = set(hook.os_families.all()) if hasattr(hook, "os_families") else set()
    if families and not (facts.os_lineages and all(lineage & families for lineage in facts.os_lineages)):
        return (
            f"'{hook.name}' runs on {_names(families)} VMs only, and "
            f"{facts.describe(facts.os_families, 'OS family')}."
        )
    technologies = set(hook.resource_technologies.all())
    if not _allows(technologies, facts.technologies):
        return f"'{hook.name}' runs on {_names(technologies)} only, and {facts.describe(facts.technologies, 'technology')}."
    environments = set(hook.environments.all())
    if not _allows(environments, facts.environments):
        return f"'{hook.name}' runs in {_names(environments)} only, and {facts.describe(facts.environments, 'environment')}."
    groups = set(hook.groups.all())
    if not _allows(groups, facts.groups):
        return f"'{hook.name}' runs for {_names(groups)} only, and {facts.describe(facts.groups, 'group')}."
    managers = set(hook.configuration_managers.all())
    if managers:
        # A VM needs any one of the action's managers (CloudBolt's own rule);
        # a filter set must allow only managers the action accepts.
        fits = facts.managers.intersection(managers) if facts.server is not None else _allows(managers, list(facts.managers))
        if not fits:
            return f"'{hook.name}' runs on VMs managed by {_names(managers)} only."
    return ""


def _remote_scripts():
    return (
        RemoteScriptHook.objects.filter(run_on_server__isnull=True)
        .prefetch_related("os_families", "resource_technologies", "environments", "groups", "configuration_managers")
        .order_by("name")
    )


def tasks_for_servers(servers):
    """The Remote Scripts that can run on every one of VMs, by name."""
    facts = [_Facts(server=server) for server in servers]
    if not facts:
        return []
    return [hook for hook in _remote_scripts() if not any(refusal(hook, facts=f) for f in facts)]


def task_options(servers):
    """(value, label) pairs for the task dropdown: the Remote Script's global ID and name."""
    return [(hook.global_id, hook.name) for hook in tasks_for_servers(servers)]


def filters_refusal(hook, os_families=(), technologies=(), environments=(), groups=(), configuration_managers=()):
    """Why the action cannot run on every VM a filter set allows, or '' when it can."""
    return refusal(hook, facts=_Facts(os_families, technologies, environments, groups, configuration_managers))


def tasks_for_filters(os_families=(), technologies=(), environments=(), groups=(), configuration_managers=()):
    """The Remote Scripts that can run on every VM a filter set allows, by name."""
    facts = _Facts(os_families, technologies, environments, groups, configuration_managers)
    return [hook for hook in _remote_scripts() if not refusal(hook, facts=facts)]


def server_fits_filters(
    server, os_families=(), technologies=(), environments=(), groups=(), configuration_managers=()
):
    """Whether server is one of the VMs a filter set allows (an empty kind allows any)."""
    facts = _Facts(server=server)
    if os_families and not facts.os_lineages[0].intersection(os_families):
        return False
    if configuration_managers and not facts.managers.intersection(configuration_managers):
        return False
    return (
        _allows(set(technologies), facts.technologies)
        and _allows(set(environments), facts.environments)
        and _allows(set(groups), facts.groups)
    )


def resolve_task(value):
    """Return the Remote Script that value names: its global ID, numeric id or exact name."""
    text = str(value or "").strip()
    if not text:
        raise TaskError("Choose a task.")
    hooks = RemoteScriptHook.objects.all()
    hook = hooks.filter(global_id=text).first()
    if hook is None and text.isdigit():
        hook = hooks.filter(id=int(text)).first()
    if hook is None:
        hook = hooks.filter(name=text).first()
    if hook is None:
        raise TaskError(f"There is no Remote Script '{text}'.")
    return hook


def check_task(hook, servers):
    """Raise TaskError unless hook can run on every one of VMs."""
    reasons = [reason for reason in (refusal(hook, server) for server in servers) if reason]
    if reasons:
        raise TaskError(" ".join(reasons))


# ---- credentials ----------------------------------------------------------


def needs_credentials(hook, server):
    """
    True when CloudBolt has no way to log in to server for hook: no
    tech-specific execution without credentials, and no username with a
    password (or an SSH key, for Linux over SSH) on the Remote Script or the
    server. Mirrors Server.credentials_for_script, field by field.
    """
    if server.use_tech_specific_script_execution() and not server.use_tech_specific_script_credentials():
        return False
    script = hook.get_credentials()
    try:
        saved = server.get_credentials(raise_exception_if_keyfile_not_on_cb=False, arm_for_use=False)
    except Exception as err:
        logger.warning("Could not read the credentials of %s: %s", server.hostname, err)
        saved = {}
    username = script.get("username") or saved.get("username")
    password = script.get("password") or saved.get("password")
    keyfile = script.get("keyfile") or saved.get("keyfile")
    # WinRM and VMware Tools log in with a password; SSH can use a key instead.
    password_only = server.is_windows() or server.use_tech_specific_script_execution()
    return not (username and (password or (keyfile and not password_only)))


def save_credentials(server, profile, username, password):
    """Save a username and password on server, as CloudBolt's own prompt does."""
    if profile is None or not can_manage_credentials(profile, server):
        raise TaskError(f"You do not have permission to manage the credentials of {server.hostname}.")
    username = str(username or "").strip()
    password = str(password or "")
    if not username or not password:
        raise TaskError("Enter both a username and a password.")
    server.set_value_for_custom_field("username", username)
    server.set_value_for_custom_field("password", password)
    add_server_event(
        "MODIFICATION", server, f"Credentials for user {username} saved from Run Task.", profile=profile
    )


# ---- task inputs ----------------------------------------------------------


def task_fields(hook):
    """The Remote Script's inputs, in its display order."""
    fields = list(hook.input_fields.all())
    order = [part.strip() for part in (hook.sequenced_action_inputs or "").split(",") if part.strip()]
    rank = {value: index for index, value in enumerate(order)}

    def position(field):
        # Without a saved sequence, keep the order the inputs were created in.
        index = rank.get(str(field.id), rank.get(str(getattr(field, "global_id", "")), len(order)))
        return index, field.id

    return sorted(fields, key=position)


def input_label(field):
    return field.label or field.simple_name


def input_regex(field):
    """
    The input's validation regex. CloudBolt keeps it on the field's global
    constraint mapping (regex_constraint, applied with re.match to text
    inputs); value_pattern_string is a display format string, not a regex.
    """
    try:
        mapping = CustomFieldMapping.global_constraints.filter(custom_field=field).first()
    except Exception:
        return ""
    return (getattr(mapping, "regex_constraint", "") or "") if mapping else ""


def input_options(field):
    try:
        return [str(option) for option in field.get_global_options()]
    except Exception:
        return []


def _default(field):
    try:
        default = field.get_global_default()
    except Exception:
        return None
    value = getattr(default, "value", default)
    return None if value is None else str(value)


def input_element(field):
    """One SurveyJS question for a task input, mapped as CloudBolt's own form builder maps types."""
    element = {"name": field.simple_name, "title": input_label(field), "isRequired": bool(field.required)}
    if field.description:
        element["description"] = field.description
    if field.placeholder:
        element["placeholder"] = field.placeholder
    options = input_options(field)
    if options:
        element["type"] = "tagbox" if field.allow_multiple else "dropdown"
        element["choices"] = options
    elif field.type == "BOOL":
        element["type"] = "boolean"
    elif field.type in ("TXT", "CODE"):
        element["type"] = "comment"
    else:
        element["type"] = "text"
        input_type = {"INT": "number", "DEC": "number", "DT": "date", "DTM": "datetime-local", "URL": "url"}
        if field.type in input_type:
            element["inputType"] = input_type[field.type]
    pattern = input_regex(field)
    if pattern and element["type"] in ("text", "comment"):
        # CloudBolt matches from the start (re.match); a JavaScript regex searches anywhere.
        element["validators"] = [{"type": "regex", "regex": f"^(?:{pattern})", "text": f"Must match {pattern}"}]
    default = _default(field)
    if default not in (None, ""):
        element["defaultValue"] = default
    return element


def task_panel(hook):
    """
    The chosen task's inputs as a Dynamic Panel template, plus the inputs Run
    Task leaves out: secret and object-typed inputs. 'blocking' lists the
    required ones among them, which stop the task from running here.
    """
    elements, omitted, blocking = [], [], []
    for field in task_fields(hook):
        if field.type in SECRET_TYPES or field.type not in PLAIN_TYPES:
            (blocking if field.required else omitted).append(input_label(field))
            continue
        elements.append(input_element(field))
    return {
        "title": hook.name,
        "description": hook.description or "",
        "elements": elements,
        "omitted": omitted,
        "blocking": blocking,
    }


def _payload_values(payload):
    """Return (values, strict) from what reached the plug-in. strict marks API/JSON input."""
    if payload is None or payload == "" or payload == []:
        return {}, False
    if isinstance(payload, dict):
        return payload, True
    if isinstance(payload, list):
        values = {}
        for item in payload:
            if isinstance(item, dict):
                values.update(item)
        return values, False
    text = str(payload).strip()
    if not text:
        return {}, True
    try:
        values = json.loads(text)
    except ValueError as err:
        # Some CloudBolt paths store a text input HTML-escaped (&quot;). Only
        # then unescape: a value that parses as typed keeps its own entities.
        try:
            values = json.loads(html.unescape(text))
        except ValueError:
            raise TaskError(f"Task inputs must be a JSON object of input names and values: {err}")
    if isinstance(values, list):
        return _payload_values(values)[0], True
    if not isinstance(values, dict):
        raise TaskError("Task inputs must be a JSON object of input names and values.")
    return values, True


def coerce_input(field, value):
    label = input_label(field)
    if field.allow_multiple:
        items = value if isinstance(value, list) else [value]
        return [_coerce_one(field, label, item) for item in items]
    if isinstance(value, list):
        raise TaskError(f"{label} takes one value.")
    return _coerce_one(field, label, value)


def _coerce_one(field, label, value):
    if field.type == "BOOL":
        if isinstance(value, bool):
            return value
        text = str(value).strip().lower()
        if text in ("true", "yes", "1", "on"):
            return True
        if text in ("false", "no", "0", "off"):
            return False
        raise TaskError(f"{label} must be true or false.")
    if field.type == "INT":
        try:
            return int(str(value).strip())
        except ValueError:
            raise TaskError(f"{label} must be a whole number.")
    if field.type == "DEC":
        try:
            return float(str(value).strip())
        except ValueError:
            raise TaskError(f"{label} must be a number.")
    text = str(value)
    if field.type not in ("TXT", "CODE"):
        text = text.strip()
    options = input_options(field)
    if options and text not in options:
        raise TaskError(f"{label} must be one of: {', '.join(options)}.")
    pattern = input_regex(field)
    if pattern and not re.match(pattern, text):
        raise TaskError(f"{label} must match {pattern}.")
    return text


def parse_task_inputs(hook, payload):
    """
    Validate the chosen task's input values and return them by input name.

    payload is the Run Task plug-in's task_inputs value: the custom form's
    Dynamic Panel (a list holding one dict, which may still carry the values
    of a task chosen earlier: those are ignored), or a JSON object string
    from the API, MCP or the built-in dialog (unknown names are refused there,
    to catch typos). CloudBolt validates nothing on a custom-form submit, so
    required inputs, options, patterns and types are all checked here.
    """
    values, strict = _payload_values(payload)
    fields = {field.simple_name: field for field in task_fields(hook)}
    unknown = sorted(name for name in values if name not in fields)
    if unknown and strict:
        raise TaskError(
            f"'{hook.name}' has no input named {', '.join(unknown)}. Its inputs: "
            f"{', '.join(fields) or 'none'}."
        )
    result = {}
    for name, field in fields.items():
        value = values.get(name)
        blank = value is None or (isinstance(value, str) and not value.strip()) or value == []
        if field.type in SECRET_TYPES or field.type not in PLAIN_TYPES:
            if not blank:
                raise TaskError(
                    f"{input_label(field)} is a {field.get_type_display()} input, which Run Task does not "
                    "carry (CloudBolt logs this action's inputs in plain text)."
                )
            if field.required:
                raise TaskError(
                    f"'{hook.name}' requires {input_label(field)}, a {field.get_type_display()} input Run "
                    "Task cannot carry. Run the script from its own action instead."
                )
            continue
        if blank:
            if field.required:
                raise TaskError(f"{input_label(field)} is required.")
            continue
        result[name] = coerce_input(field, value)
    return result


# Earlier private names, kept for callers that still use them.
_label, _options, _coerce = input_label, input_options, coerce_input


# ---- running a task -------------------------------------------------------


def _file_extension(hook, server):
    try:
        extension = os.path.splitext(hook.get_abs_file_path() or "")[1][1:]
    except Exception:
        extension = ""
    return extension or ("ps1" if server.is_windows() else "sh")


def tail(text, limit=OUTPUT_LIMIT):
    text = (text or "").strip()
    return text if len(text) <= limit else "...\n" + text[-limit:]


def _with_archives(script, server, job, archive_refs, timeout, attempts):
    """
    Put the file archives download step (shared_modules/SHM-v8llc736) into a
    script that declares files with cb-archive header lines, or gets files
    attached as archive_refs. Returns the script and the CB_OUTPUT lines
    naming the version of each file, to append to the output after a
    successful run (the versions are known here, before the script runs).
    """
    if not archive_refs and "cb-archive" not in script:
        return script, ""
    try:
        from shared_modules import file_archives
    except ImportError:
        raise TaskError(
            "This task uses file archives, but the file_archives shared module "
            "(SHM-v8llc736) is not on this CloudBolt."
        )
    try:
        script, outputs = file_archives.prepare(
            script, server, job, extra_refs=archive_refs,
            minutes=file_archives.link_lifetime(timeout, attempts),
        )
    except file_archives.ArchiveError as err:
        raise TaskError(str(err))
    return script, file_archives.output_lines(outputs)


def run_task(hook, server, inputs, job=None, archive_refs=None):
    """
    Run hook on server the way a Remote Script action does
    (RemoteScriptHook.run_hook_on_server) and return the script's output.

    The script is rendered by the Remote Script itself, with the inputs
    marked untrusted, and runs through Server.execute_script with the Remote
    Script's timeout, sudo and clean-up settings and its saved credentials
    (else the VM's). execute_script raises when the script fails or
    exits non-zero. Retries follow the Remote Script's max_retries.

    Files the script declares in cb-archive header lines, plus archive_refs
    (files a workflow step attached, "archive/file[@version] [extract]"),
    are downloaded onto the VM before the script body runs; see the
    file_archives shared module (SHM-v8llc736).
    """
    if server.power_status != "POWERON":
        raise TaskError(f"{server.hostname} is not powered on.")
    context = get_hook_action_input_dict(hook, inputs, input_names_clean=True)
    context.update({"server": server, "servers": [server]})
    if job is not None:
        context["job"] = job
    script, _stripped = hook.render_module_file(server, context)
    args = hook.get_script_args(server, context) or None
    username, password, key = hook.get_runas_credentials(None)
    attempts = max(int(hook.max_retries or 0), 0) + 1
    # Once, before the retry loop: the links must outlive every attempt.
    script, archive_lines = _with_archives(script, server, job, archive_refs, hook.execution_timeout, attempts)
    attempt, busy_waits = 1, 0
    while True:
        try:
            output = server.execute_script(
                script_contents=script,
                script_args=args,
                runas_username=username,
                runas_password=password,
                runas_key=key,
                timeout=hook.execution_timeout,
                file_extension=_file_extension(hook, server),
                run_with_sudo=hook.run_with_sudo,
                remove_after_run=hook.remove_after_run,
            )
        except Exception as err:
            # Azure runs one Run Command per VM at a time and refuses a second
            # with Conflict; wait for the first instead of failing.
            if AZURE_BUSY in str(err) and busy_waits < BUSY_WAIT_LIMIT:
                busy_waits += 1
                set_progress(f"{server.hostname} is running another script; waiting {BUSY_WAIT_SECONDS} seconds")
                time.sleep(BUSY_WAIT_SECONDS)
                continue
            if attempt >= attempts:
                add_server_event("MODIFICATION", server, f"Run Task '{hook.name}' failed.", job=job)
                raise
            logger.warning("Run Task '%s' on %s failed (attempt %s): %s", hook.name, server.hostname, attempt, err)
            attempt += 1
            continue
        add_server_event("MODIFICATION", server, f"Run Task '{hook.name}' succeeded.", job=job)
        return (output or "") + archive_lines


OUTPUT_LINE = re.compile(r"^\s*CB_OUTPUT\s+([A-Za-z0-9_]+)=(.*?)\s*$", re.MULTILINE)


def parse_outputs(text):
    """
    The values a script published with CB_OUTPUT name=value lines, as a dict
    (a later line for the same name wins). The base templates print them with
    a helper, last, on stdout, before exit 0: WinRM drops stdout when a script
    fails, and Azure Run Command keeps only the last 4 KB of output.
    """
    return {name: value for name, value in OUTPUT_LINE.findall(text or "")}


def failure_text(err):
    """The useful part of a failed script: its ERROR lines, else the end of its output."""
    output = str(getattr(err, "output", "") or "").strip()
    message = str(err).strip()
    for text in (output, message):
        errors = [line.strip() for line in text.splitlines() if line.strip().startswith("ERROR:")]
        if errors:
            return "\n".join(dict.fromkeys(errors))
    return tail(output or message, 1500)
