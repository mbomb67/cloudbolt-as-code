"""
The base actions behind the Actions tab of the Server Workflows page.

CloudBolt's UI creates a Remote Script or a CloudBolt Plug-in only inside a
server, resource or orchestration action, and deleting that action deletes
the script with it. This module creates them with no such context, so Run
Task and Run Workflow can use them directly, and lists every base action that
can be a workflow step with what CloudBolt's All Actions page leaves out:
OS families, groups, environments, whether Run Task offers it, and what uses
it (server, resource and orchestration actions, blueprints, workflows, ...).

Remote Scripts are built from the Server Task Templates on this appliance
(plugins/OHK-lhm2916o for Windows PowerShell, plugins/OHK-iub9a2jg for Linux
bash): the template's own helpers and error handling, one input line per
declared input in the template's form, and the admin's task body.

Inputs and options
  Every save of a plug-in or Remote Script rebuilds its inputs from the bare
  template variables in its file and deletes the rest; a variable's existing
  input survives, with its options. So the file is written and saved first,
  which creates one input per declared variable, and then each input gets its
  type, label, pattern and options. A final save checks that the options
  survive one.

Secrets
  CloudBolt logs a server action's custom-form payload and plain-text inputs,
  so neither this page's forms nor Run Task carry passwords: password inputs
  (PWD, ETXT) cannot be declared here.
"""
import keyword
import re
from collections import OrderedDict

from django.contrib.contenttypes.models import ContentType
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import transaction
from django.template import Template, TemplateSyntaxError
from django.urls import reverse

from accounts.models import Group
from cbhooks.models import CloudBoltHook, RemoteScriptHook, ServerAction
from common.methods import tokenize_template_string
from externalcontent.models import OSFamily
from infrastructure.models import Environment
from orders.models import CustomFieldValue
from utilities.logger import ThreadLogger

logger = ThreadLogger(__name__)

RUN_TASK_ACTION_ID = "SVA-4sin3s0q"

# The Server Task Templates (Remote Scripts) a new script is built from.
PLATFORMS = OrderedDict(
    [
        (
            "windows",
            {
                "label": "Windows (PowerShell)",
                "template_id": "OHK-lhm2916o",
                "template_name": "Server Task Template (Windows)",
                "root_family": "Windows",
                "extension": "ps1",
                "end_marker": "exit 0",
                "indent": "    ",
                "comment": "#",
            },
        ),
        (
            "linux",
            {
                "label": "Linux (bash)",
                "template_id": "OHK-iub9a2jg",
                "template_name": "Server Task Template (Linux)",
                "root_family": "Linux",
                "extension": "sh",
                "end_marker": "CB_TASK",
                "indent": "",
                "comment": "#",
            },
        ),
    ]
)
INPUTS_MARKER = "# ---- inputs ----"
BODY_MARKER = "# ---- task body ----"

# Input types Run Task and Run Workflow can carry (server_tasks.PLAIN_TYPES).
INPUT_TYPES = OrderedDict(
    [
        ("STR", "String"),
        ("INT", "Integer"),
        ("DEC", "Decimal"),
        ("IP", "IP address"),
        ("URL", "URL"),
        ("DT", "Date"),
        ("DTM", "Date and time"),
        ("BOOL", "Boolean"),
        ("TXT", "Multi-line text"),
        ("CODE", "Code"),
    ]
)
# Types that take a list of options.
OPTION_TYPES = {"STR", "INT", "DEC", "IP", "URL"}

INPUT_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,49}$")
NAME_MAX = 255
DESCRIPTION_MAX = 1000
LABEL_MAX = 255
MAX_INPUTS = 30
MAX_OPTIONS = 100
TIMEOUT_MIN, TIMEOUT_MAX = 30, 86400
# Azure Run Command and WinRM pass a Windows script on the powershell.exe
# command line (the template's Size convention).
WINDOWS_SCRIPT_BUDGET = 9000

# Names CloudBolt's context, Run Task or Run Workflow already use.
RESERVED_INPUT_NAMES = {
    "server", "servers", "resource", "resources", "job", "group", "environment", "blueprint",
    "blueprint_context", "outputs", "os_build", "os_family", "profile", "owner", "logger",
    "workflow", "task", "task_inputs", "script_username", "script_password", "targets",
}
# PowerShell automatic and template variables an input variable must not shadow.
POWERSHELL_RESERVED = {
    "args", "consolefilename", "err", "error", "erroractionpreference", "event", "eventargs",
    "eventsubscriber", "executioncontext", "false", "foreach", "home", "host", "input", "iscoreclr",
    "islinux", "ismacos", "iswindows", "lastexitcode", "matches", "myinvocation", "nestedpromptlevel",
    "null", "ofs", "os", "pid", "profile", "progresspreference", "psboundparameters", "pscmdlet",
    "pscommandpath", "psculture", "psdebugcontext", "pshome", "psitem", "psscriptroot", "pssenderinfo",
    "psuiculture", "psversiontable", "pwd", "sender", "shellid", "stacktrace", "switch", "this", "true",
}
# Shell variables an input variable must not overwrite, plus the template's own.
BASH_RESERVED = {
    "BASH", "BASHOPTS", "BASH_COMMAND", "BASH_VERSION", "CB_TASK", "CB_TASK_FILE", "CB_TASK_RC", "EUID",
    "GROUPS", "HISTFILE", "HOME", "HOSTNAME", "HOSTTYPE", "IFS", "LANG", "LINENO", "LOGNAME", "MAIL",
    "OLDPWD", "OPTARG", "OPTIND", "OSTYPE", "PATH", "PIPESTATUS", "PPID", "PS1", "PS2", "PS4", "PWD",
    "RANDOM", "SECONDS", "SHELL", "SHELLOPTS", "SHLVL", "SUDO", "TERM", "TMPDIR", "UID", "USER",
}
# Names the generated plug-in defines or imports.
PLUGIN_RESERVED = {"kwargs", "run", "set_progress"}
TEMPLATE_SYNTAX = ("{{", "{%", "{#", "%}", "#}")


class ActionError(Exception):
    """A request the Actions tab refuses. The message is for the admin."""


# ---- naming ---------------------------------------------------------------


def powershell_variable(name):
    """service_name -> $ServiceName."""
    return "$" + "".join(part[:1].upper() + part[1:] for part in name.split("_") if part)


def bash_variable(name):
    """service_name -> SERVICE_NAME."""
    return name.upper()


def variable_for(kind, platform, name):
    if kind == "plugin":
        return name
    return powershell_variable(name) if platform == "windows" else bash_variable(name)


# ---- templates --------------------------------------------------------------


def template_hook(platform):
    """The platform's Server Task Template on this appliance, or None."""
    info = PLATFORMS[platform]
    hooks = RemoteScriptHook.objects.all()
    return hooks.filter(global_id=info["template_id"]).first() or hooks.filter(name=info["template_name"]).first()


def _template_parts(platform):
    """
    The template split at its markers: (prologue, body, epilogue, template).
    prologue runs from the first code line through the inputs marker, body is
    the example task body, epilogue starts at the end marker. The header
    comment is dropped; the new script names the template instead.
    """
    info = PLATFORMS[platform]
    hook = template_hook(platform)
    if hook is None:
        raise ActionError(
            f"{info['template_name']} ({info['template_id']}) is not on this CloudBolt. Sync the Server "
            "Automation content package, or Run Task, first; it brings the templates."
        )
    try:
        lines = (hook.file_content() or "").splitlines()
    except Exception as err:
        raise ActionError(f"Could not read {hook.name}: {err}")
    stripped = [line.strip() for line in lines]

    def find(text, start=0):
        for index in range(start, len(stripped)):
            if stripped[index] == text:
                return index
        raise ActionError(
            f"{hook.name} has no line '{text}', which the Actions tab needs to build a script from it. "
            "Restore the template from the repo."
        )

    first_code = next((i for i, line in enumerate(stripped) if line and not line.startswith("#")), None)
    if first_code is None:
        raise ActionError(f"{hook.name} has no code.")
    inputs_at = find(INPUTS_MARKER, first_code)
    body_at = find(BODY_MARKER, inputs_at)
    end_at = find(info["end_marker"], body_at)
    shebang = [lines[0]] if lines and lines[0].startswith("#!") else []
    prologue = shebang, lines[first_code:inputs_at + 1]
    indent = info["indent"]
    body = [line[len(indent):] if line.startswith(indent) else line.lstrip() for line in lines[body_at + 1:end_at]]
    while body and not body[-1].strip():
        body.pop()
    return prologue, body, lines[end_at:], hook


def template_defaults(platform):
    """What the editor starts from: the template's task body, inputs and timeout."""
    _, body, _, hook = _template_parts(platform)
    return {
        "body": "\n".join(body),
        "timeout": hook.execution_timeout or 300,
        "template": {"id": hook.global_id, "name": hook.name, "url": _url(hook)},
        "inputs": [_input_spec(field) for field in _ordered_inputs(hook)],
    }


def _input_spec(field):
    try:
        options = [str(option) for option in field.get_global_options()]
    except Exception:
        options = []
    return {
        "name": field.simple_name,
        "label": field.label or "",
        "type": field.type,
        "required": bool(field.required),
        "description": field.description or "",
        "options": options,
        "pattern": field.value_pattern_string or "",
    }


def _ordered_inputs(hook):
    from shared_modules import server_tasks

    return server_tasks.task_fields(hook)


PLUGIN_BODY = "\n".join(
    [
        "for target in targets:",
        '    set_progress(f"Running on {target.hostname}")',
        'return "SUCCESS", "", ""',
    ]
)


def plugin_defaults():
    return {"body": PLUGIN_BODY, "timeout": None, "template": None, "inputs": []}


# ---- validating a request -------------------------------------------------


def _text(value, label, limit, required=False):
    text = str(value or "").strip()
    if required and not text:
        raise ActionError(f"{label} is required.")
    if len(text) > limit:
        raise ActionError(f"{label} must be {limit} characters or fewer.")
    if any(mark in text for mark in TEMPLATE_SYNTAX):
        # The name and description go into the script's header comment, which
        # CloudBolt renders as a template like the rest of the file.
        raise ActionError(f"{label} cannot contain template braces.")
    return text


def _objects(model, keys, label):
    keys = [str(key) for key in keys or [] if str(key).strip()]
    if not keys:
        return []
    found = {obj.global_id: obj for obj in model.objects.filter(global_id__in=keys)}
    missing = [key for key in keys if key not in found]
    if missing:
        raise ActionError(f"Unknown {label}: {', '.join(missing)}.")
    return [found[key] for key in dict.fromkeys(keys)]


def _lineage(family):
    seen = []
    while family is not None and family not in seen:
        seen.append(family)
        family = family.parent
    return seen


def root_family(platform):
    name = PLATFORMS[platform]["root_family"]
    return OSFamily.objects.filter(name=name, parent__isnull=True).first() or OSFamily.objects.filter(name=name).first()


def platform_families(platform):
    """The OS families a platform's scripts can name: its root family and every family under it."""
    root = root_family(platform)
    if root is None:
        return []
    return sorted((f for f in OSFamily.objects.all() if root in _lineage(f)), key=lambda f: f.name.lower())


def _typed(field_type, text, label):
    if field_type == "INT":
        try:
            return int(text)
        except ValueError:
            raise ActionError(f"{label}: '{text}' is not a whole number.")
    if field_type == "DEC":
        try:
            return float(text)
        except ValueError:
            raise ActionError(f"{label}: '{text}' is not a number.")
    return text


def clean_inputs(kind, platform, specs):
    """The declared inputs, checked; each a dict ready for sync_inputs."""
    if not isinstance(specs, list):
        raise ActionError("inputs must be a list.")
    if len(specs) > MAX_INPUTS:
        raise ActionError(f"An action can declare up to {MAX_INPUTS} inputs here.")
    cleaned, names, variables = [], set(), set()
    for index, spec in enumerate(specs, start=1):
        if not isinstance(spec, dict):
            raise ActionError(f"Input {index} must be an object.")
        name = str(spec.get("name") or "").strip()
        where = f"Input {index} ({name or 'no name'})"
        if not INPUT_NAME_RE.match(name):
            raise ActionError(
                f"{where}: the name must start with a lower-case letter and use only lower-case letters, digits "
                "and underscores (up to 50)."
            )
        if name in RESERVED_INPUT_NAMES:
            raise ActionError(f"{where}: '{name}' is a name CloudBolt already uses in the action's context.")
        if name in names:
            raise ActionError(f"{where}: another input already uses this name.")
        variable = variable_for(kind, platform, name)
        if kind == "plugin" and (keyword.iskeyword(name) or name in PLUGIN_RESERVED):
            raise ActionError(f"{where}: '{name}' is a Python keyword or a name the plug-in uses itself.")
        if kind == "script":
            reserved = POWERSHELL_RESERVED if platform == "windows" else BASH_RESERVED
            check = variable[1:].lower() if platform == "windows" else variable
            if check in reserved or variable in variables:
                raise ActionError(f"{where}: its script variable {variable} is reserved; choose another name.")
        names.add(name)
        variables.add(variable)
        field_type = str(spec.get("type") or "STR").strip().upper()
        if field_type in ("PWD", "ETXT"):
            raise ActionError(
                f"{where}: password inputs cannot be declared here. CloudBolt logs Run Task's and Run "
                "Workflow's inputs in plain text, so neither carries a password."
            )
        if field_type not in INPUT_TYPES:
            raise ActionError(f"{where}: type must be one of {', '.join(INPUT_TYPES)}.")
        pattern = str(spec.get("pattern") or "").strip()
        if pattern:
            if field_type in ("BOOL", "DT", "DTM"):
                raise ActionError(f"{where}: a {INPUT_TYPES[field_type]} input takes no pattern.")
            try:
                regex = re.compile(pattern)
            except re.error as err:
                raise ActionError(f"{where}: the pattern is not a valid regular expression ({err}).")
        options = spec.get("options") or []
        if isinstance(options, str):
            options = options.splitlines()
        options = list(dict.fromkeys(str(option).strip() for option in options if str(option).strip()))
        if options and field_type not in OPTION_TYPES:
            raise ActionError(f"{where}: a {INPUT_TYPES[field_type]} input takes no options.")
        if len(options) > MAX_OPTIONS:
            raise ActionError(f"{where}: up to {MAX_OPTIONS} options.")
        for option in options:
            if pattern and not regex.search(option):
                raise ActionError(f"{where}: the option '{option}' does not match the pattern.")
        cleaned.append(
            {
                "name": name,
                "label": _text(spec.get("label"), f"{where} label", LABEL_MAX) or name.replace("_", " ").capitalize(),
                "type": field_type,
                "required": bool(spec.get("required")),
                "description": _text(spec.get("description"), f"{where} description", DESCRIPTION_MAX),
                "options": [_typed(field_type, option, where) for option in options],
                "pattern": pattern,
            }
        )
    return cleaned


def clean_request(data):
    """A create or preview request, checked. Returns a dict for build_source and create_action."""
    kind = str(data.get("kind") or "").strip()
    if kind not in ("script", "plugin"):
        raise ActionError("kind must be script or plugin.")
    platform = str(data.get("platform") or "").strip() if kind == "script" else ""
    if kind == "script" and platform not in PLATFORMS:
        raise ActionError(f"platform must be one of {', '.join(PLATFORMS)}.")
    name = _text(data.get("name"), "Name", NAME_MAX, required=True)
    families = _objects(OSFamily, data.get("os_families"), "OS families")
    if kind == "script":
        root = root_family(platform)
        if root is None:
            raise ActionError(f"There is no {PLATFORMS[platform]['root_family']} OS family on this CloudBolt.")
        if not families:
            families = [root]
        outside = [f.name for f in families if root not in _lineage(f)]
        if outside:
            raise ActionError(
                f"A {PLATFORMS[platform]['label']} script runs on {root.name} servers; {', '.join(outside)} "
                f"is not under {root.name}."
            )
    timeout = None
    if kind == "script":
        try:
            timeout = int(str(data.get("timeout") or 300).strip())
        except ValueError:
            raise ActionError("The timeout must be a whole number of seconds.")
        if not TIMEOUT_MIN <= timeout <= TIMEOUT_MAX:
            raise ActionError(f"The timeout must be between {TIMEOUT_MIN} and {TIMEOUT_MAX} seconds.")
    body = str(data.get("body") or "").replace("\r\n", "\n").rstrip()
    if not body.strip():
        raise ActionError("The task body is empty.")
    return {
        "kind": kind,
        "platform": platform,
        "name": name,
        "description": _text(data.get("description"), "Description", DESCRIPTION_MAX),
        "os_families": families,
        "groups": _objects(Group, data.get("groups"), "groups"),
        "environments": _objects(Environment, data.get("environments"), "environments"),
        "timeout": timeout,
        "body": body,
        "inputs": clean_inputs(kind, platform, data.get("inputs") or []),
    }


# ---- building the source --------------------------------------------------


def _encoded(name):
    """The template's input form: a bare variable inside autoescape-off and urlencode filter tags."""
    return '{% autoescape off %}{% filter urlencode:"" %}{{ ' + name + " }}{% endfilter %}{% endautoescape %}"


def _input_line(platform, spec):
    name = spec["name"]
    if platform == "windows":
        return f"{powershell_variable(name)} = [uri]::UnescapeDataString('{_encoded(name)}')"
    return f"{bash_variable(name)}=\"$(urldecode '{_encoded(name)}')\""


def _header(request, template_name):
    lines = ["#", f"# {request['name']}", "#"]
    for paragraph in (request["description"] or "").splitlines():
        lines.append(f"# {paragraph}".rstrip())
    if request["description"]:
        lines.append("#")
    if template_name:
        lines.append(f"# Built on the Server Workflows Actions page from {template_name}. That")
        lines.append("# template's header lists the conventions this script follows.")
    else:
        lines.append("# Built on the Server Workflows Actions page.")
    return lines


def _script_source(request):
    platform = request["platform"]
    info = PLATFORMS[platform]
    (shebang, prologue), _, epilogue, hook = _template_parts(platform)
    indent = info["indent"]
    lines = list(shebang) + _header(request, hook.name) + [""] + list(prologue)
    lines += [indent + _input_line(platform, spec) for spec in request["inputs"]]
    lines.append("")
    lines.append(indent + BODY_MARKER)
    lines += [(indent + line) if line.strip() else "" for line in request["body"].split("\n")]
    lines.append("")
    lines += epilogue
    return "\n".join(lines) + "\n"


def _plugin_source(request):
    lines = _header(request, None)[1:]
    lines += [
        "# CloudBolt renders this file as a template before it runs, so write",
        "# template braces only on the inputs line below.",
        "from common.methods import set_progress",
        "",
    ]
    names = [spec["name"] for spec in request["inputs"]]
    if names:
        declared = " ".join('"{{ ' + name + ' }}"' for name in names)
        lines += [
            "# Inputs. CloudBolt makes one action input per variable on the next line,",
            "# which never renders; their values arrive as run() keyword arguments.",
            "# {% if False %}" + declared + "{% endif %}",
            "",
        ]
    lines += [
        "",
        "def _servers(job, server, servers):",
        '    """The server action\'s server, else the servers CloudBolt passed, else the job\'s."""',
        "    if server is not None:",
        "        return [server]",
        "    if servers:",
        "        return list(servers)",
        "    return list(job.server_set.all()) if job is not None else []",
        "",
        "",
        "def run(job=None, server=None, servers=None, **kwargs):",
    ]
    lines += [f'    {name} = kwargs.get("{name}")' for name in names]
    lines.append("    targets = _servers(job, server, servers)")
    lines += [("    " + line) if line.strip() else "" for line in request["body"].split("\n")]
    return "\n".join(lines) + "\n"


def build_source(request):
    """
    The new action's file, checked the way CloudBolt reads it: it must parse
    as a template, and its bare template variables must be exactly the
    declared inputs (any other would become an input of its own on save).
    Returns (source, warnings).
    """
    source = _script_source(request) if request["kind"] == "script" else _plugin_source(request)
    try:
        Template(source)
    except TemplateSyntaxError as err:
        raise ActionError(f"The task body breaks CloudBolt's template rendering: {err}")
    declared = {spec["name"] for spec in request["inputs"]}
    tokens = set(tokenize_template_string(source))
    extra = sorted(tokens - declared)
    if extra:
        raise ActionError(
            "The task body uses the template variable "
            + ", ".join(extra)
            + ", which CloudBolt would turn into an undeclared input. Declare it as an input and read its "
            "variable, or remove the braces."
        )
    missing = sorted(declared - tokens)
    if missing:
        raise ActionError(f"Internal error: no input line for {', '.join(missing)}.")
    warnings = []
    if "{%" in request["body"] or "{#" in request["body"]:
        warnings.append(
            "The task body contains template tag or comment syntax ({% or {#), which CloudBolt renders before "
            "the action runs."
        )
    if request["kind"] == "plugin":
        try:
            compile(source, request["name"], "exec")
        except SyntaxError as err:
            line = (err.lineno or 0) - _body_offset(source)
            where = f"task body line {line}" if line > 0 else "the generated code around the inputs"
            raise ActionError(f"The plug-in is not valid Python ({where}): {err.msg}")
    elif request["platform"] == "windows" and len(source) > WINDOWS_SCRIPT_BUDGET:
        warnings.append(
            f"The script is {len(source)} characters. Azure Run Command and WinRM pass a Windows script on the "
            f"powershell.exe command line, which fits about {WINDOWS_SCRIPT_BUDGET} once rendered."
        )
    return source, warnings


def _body_offset(source):
    """Lines before the task body in a generated plug-in, so a syntax error points into the body."""
    for index, line in enumerate(source.splitlines(), start=1):
        if line.strip() == "targets = _servers(job, server, servers)":
            return index
    return 0


# ---- creating -------------------------------------------------------------


def _set_options(field, options):
    """Replace the input's global options: CloudBolt's importer sets them only when it creates the input."""
    mapping = field.get_global_mapping()
    values = [CustomFieldValue.objects.get_or_create(field=field, value=option)[0] for option in options]
    mapping.options.set(values)


def sync_inputs(hook, specs):
    """
    Give each declared input its settings and options, in order. Call after the
    file is saved: that save created the inputs from the file's variables.
    """
    ids = []
    for spec in specs:
        field = hook.input_fields.filter(name=f"{spec['name']}_a{hook.id}").first()
        if field is None:
            field = hook._get_or_create_action_input(spec["name"])
        field.label = spec["label"]
        field.type = spec["type"]
        field.required = spec["required"]
        field.description = spec["description"] or None
        field.value_pattern_string = spec["pattern"] or None
        field.allow_multiple = False
        field.save()
        _set_options(field, spec["options"])
        ids.append(field.id)
    hook.sequence_action_inputs(ids)
    return ids


def option_snapshot(hook):
    return {field.simple_name: sorted(str(o) for o in field.get_global_options()) for field in hook.input_fields.all()}


def _unique_name(name):
    from cbhooks.models import OrchestrationHook

    if OrchestrationHook.objects.filter(name=name).exists():
        raise ActionError(f"An action named '{name}' already exists. Choose another name.")


def _file_name(request, hook):
    stem = re.sub(r"[^a-z0-9]+", "_", request["name"].lower()).strip("_")[:60] or "action"
    extension = PLATFORMS[request["platform"]]["extension"] if request["kind"] == "script" else "py"
    return f"{stem}_{hook.id}.{extension}"


def create_action(request, source, profile=None):
    """
    Create the Remote Script or plug-in, with no server, resource or
    orchestration action around it. The file goes first: its save makes the
    inputs from the file's variables (and would delete any made before). Then
    each input gets its settings and options, and one more save checks that
    the options survive it.
    """
    _unique_name(request["name"])
    model = RemoteScriptHook if request["kind"] == "script" else CloudBoltHook
    with transaction.atomic():
        fields = {"name": request["name"], "description": request["description"] or None}
        if model is RemoteScriptHook:
            fields.update(execution_timeout=request["timeout"], remove_after_run=True, run_with_sudo=False)
        hook = model.objects.create(**fields)
        hook.module_file = SimpleUploadedFile(_file_name(request, hook), source.encode("utf-8"))
        hook.save()
        hook.os_families.set(request["os_families"])
        hook.groups.set(request["groups"])
        hook.environments.set(request["environments"])
        sync_inputs(hook, request["inputs"])
        expected = option_snapshot(hook)
        hook.save()
        hook.refresh_from_db()
        after = option_snapshot(hook)
        if after != expected:
            logger.error("Inputs of '%s' changed on save: %s -> %s", hook.name, expected, after)
            raise ActionError(
                "CloudBolt changed the new action's inputs when it was saved again, so nothing was created. "
                "See the application log."
            )
    origin = template_hook(request["platform"]).name if request["kind"] == "script" else "a blank plug-in"
    try:
        hook.add_event("CREATION", f"Created on the Server Workflows Actions page from {origin}.", profile=profile)
    except Exception:
        logger.exception("Could not record the creation of '%s'", hook.name)
    return hook


# ---- listing --------------------------------------------------------------


def _url(obj):
    try:
        return obj.get_absolute_url()
    except Exception:
        return None


def _label(obj):
    return str(getattr(obj, "label", None) or getattr(obj, "name", None) or obj)


DEPENDENT_KINDS = OrderedDict(
    [
        ("server_actions", "Server action"),
        ("condition_server_actions", "Server action condition"),
        ("resource_actions", "Resource action"),
        ("condition_resource_actions", "Resource action condition"),
        ("orchestration_actions", "Orchestration action"),
        ("blueprints", "Blueprint"),
        ("historical_blueprints", "Historical blueprint"),
        ("rules", "Rule"),
        ("cit_tests", "CIT test"),
        ("inbound_web_hooks", "Inbound webhook"),
        ("flowcontrol_actions", "Flow control action"),
        ("mcp_tool_actions", "MCP tool action"),
        ("recurring_jobs", "Recurring job"),
        ("cloudbolt_plugins", "Plug-in"),
        ("shared_modules", "Shared module"),
    ]
)


def dependents(hook):
    """What uses the action, from CloudBolt's own OrchestrationHook.dependents."""
    try:
        found = hook.dependents
    except Exception:
        logger.exception("Could not read the dependents of '%s'", hook.name)
        return [{"kind": "Unknown", "name": "Could not read what uses this action", "url": None}]
    rows = OrderedDict()
    for key, kind in DEPENDENT_KINDS.items():
        for obj in found.get(key) or []:
            row = {"kind": kind, "name": _label(obj), "url": _url(obj)}
            if hasattr(obj, "enabled") and not obj.enabled:
                row["note"] = "disabled"
            # A blueprint with two items running the action is listed once per item.
            seen = rows.setdefault((kind, row["name"], row["url"]), dict(row, uses=0))
            seen["uses"] += 1
    return [dict(row, note="; ".join(filter(None, [row.get("note"), f"{row['uses']} uses" if row["uses"] > 1 else ""])))
            for row in rows.values()]


def workflow_uses(swf, hook, workflows):
    """The workflows with a step that runs hook, and whether hook still fits each one's filters."""
    rows = []
    for workflow in workflows:
        steps = [step for step in workflow.get("steps") or [] if step.get("action") == hook.global_id]
        if not steps:
            continue
        conflicts = swf.step_conflicts(hook, swf.resolve_filters(workflow.get("filters")))
        rows.append(
            {
                "kind": "Workflow",
                "name": workflow.get("name") or workflow["id"],
                "url": reverse("server_workflow_builder_edit", args=[workflow["id"]]),
                "note": "" if workflow.get("enabled", True) else "disabled",
                "steps": [step.get("name") or step.get("key") for step in steps],
                "conflict": " ".join(conflicts),
            }
        )
    return rows


def run_task_state():
    """Whether Run Task is on this appliance and enabled: None, or a sentence saying why not."""
    action = ServerAction.objects.filter(global_id=RUN_TASK_ACTION_ID).first()
    if action is None:
        return "Run Task (server_actions/SVA-4sin3s0q) is not on this CloudBolt, so it offers no tasks."
    if not action.enabled:
        return f"Run Task ({action.label}) is disabled, so it offers no tasks until it is enabled."
    return None


def run_task_verdict(server_tasks, hook):
    """Whether Run Task offers a Remote Script (on the servers its filters allow), and why not."""
    if not isinstance(hook, RemoteScriptHook):
        return {"offered": None, "text": "Remote Scripts only"}
    if hook.run_on_server_id:
        return {"offered": False, "text": "Not offered", "reason": "It always runs on its Run on Server."}
    blocking = server_tasks.task_panel(hook)["blocking"]
    if blocking:
        return {
            "offered": False,
            "text": "Not offered",
            "reason": f"It requires {', '.join(blocking)}, an input type Run Task cannot carry.",
        }
    return {"offered": True, "text": "Offered"}


def candidate_hooks(swf, include_builtin=False, ids=None):
    """Every Remote Script, plug-in, webhook, email and copy-file action (or those ids), cast, by name."""
    hooks = []
    for info in swf.STEP_TYPES.values():
        model = info["model"]
        queryset = model.objects.filter(real_type=ContentType.objects.get_for_model(model))
        if ids is not None:
            queryset = queryset.filter(global_id__in=ids)
        elif not include_builtin:
            queryset = queryset.exclude(is_ootb=True)
        names = ["groups", "environments", "resource_technologies", "configuration_managers", "input_fields"]
        if hasattr(model, "os_families"):
            names.append("os_families")
        hooks.extend(queryset.prefetch_related(*names))
    return sorted(hooks, key=lambda hook: hook.name.lower())


def _names(objects):
    return sorted(str(getattr(obj, "name", obj)) for obj in objects)


def action_row(swf, server_tasks, hook, workflows, steppable, server=None, with_dependents=False):
    """
    One action for the list. CloudBolt's dependents cost about 30 queries an
    action, so they come only with_dependents; the page asks for them for the
    rows it shows. Workflow uses are always there: they come from the store.
    """
    kind = swf.step_type(hook)
    info = swf.STEP_TYPES.get(kind, {})
    families = list(hook.os_families.all()) if hasattr(hook, "os_families") else []
    used_by = workflow_uses(swf, hook, workflows) + (dependents(hook) if with_dependents else [])
    row = {
        "id": hook.global_id,
        "pk": hook.id,
        "name": hook.name,
        "description": hook.description or "",
        "type": kind,
        "type_label": info.get("label", getattr(hook, "hook_type_label", "")),
        "icon": info.get("icon", "bi-gear"),
        "builtin": bool(hook.is_ootb),
        "os_families": _names(families),
        "groups": _names(hook.groups.all()),
        "environments": _names(hook.environments.all()),
        "technologies": _names(hook.resource_technologies.all()),
        "configuration_managers": _names(hook.configuration_managers.all()),
        "inputs": len(hook.input_fields.all()),
        "run_task": run_task_verdict(server_tasks, hook),
        "step": hook.global_id in steppable,
        "used_by": used_by,
        "dependents_loaded": with_dependents,
        "urls": {"detail": _url(hook), "edit": reverse("action_edit", args=[hook.id])},
    }
    if not row["step"]:
        if isinstance(hook, RemoteScriptHook) and hook.run_on_server_id:
            row["step_reason"] = "It always runs on its Run on Server."
        elif hook.global_id in swf.RUNNER_HOOK_IDS:
            row["step_reason"] = "It runs Run Task or Run Workflow itself."
        else:
            row["step_reason"] = "It serves another purpose (generated options, a condition, a build item, ...)."
    if server is not None:
        reason = server_tasks.refusal(hook, server)
        row["server_fit"] = {"fits": not reason, "reason": reason}
    return row


def action_rows(swf, server_tasks, include_builtin=False, server=None, ids=None, with_dependents=False):
    workflows = swf.list_workflows()
    steppable = {hook.global_id for hook in swf.candidate_actions(include_all=False)}
    return [
        action_row(swf, server_tasks, hook, workflows, steppable, server=server, with_dependents=with_dependents)
        for hook in candidate_hooks(swf, include_builtin=include_builtin, ids=ids)
    ]
