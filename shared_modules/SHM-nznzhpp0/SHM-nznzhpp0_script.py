"""
Server workflows: the shared logic behind the Run Workflow server action, its
form webhook, and the Server Workflows admin page.

A workflow is a named, ordered list of steps that run one after another
against a server. Each step is an existing CloudBolt action: a Remote Script,
a CloudBolt plug-in, a webhook, an email or a copy-file action. Each of a
step's inputs is set one of three ways:
  - fixed: the value saved with the workflow; the user is never asked;
  - prompt: asked for when the workflow runs (the default);
  - output: an output of an earlier step of the same run.
Admins build workflows on the Server Workflows admin page
(extensions/XUI-jbm5ac9e); users run them with the Run Workflow server action
(server_actions/SVA-j7rbsh39) and its form (forms/FRM-ecw50rd0), which calls
the Server Workflows webhook (webhooks/IWH-xjlrmvc8). Script steps run through
the server_tasks shared module of Run Task (shared_modules/SHM-571l7249).

Storage
  One CustomFieldValue per workflow, holding the workflow's JSON, on the
  server_workflow_store parameter (Multi-line Text, created on first write)
  in the server_workflows Namespace, which keeps it off the Parameters list
  and every parameter picker. The SWF- id inside the JSON is the key. Actions
  are stored by global ID and their inputs by name, never by database id,
  because CloudBolt recreates a plug-in's inputs whenever its code is saved.
  Deleting the parameter deletes every workflow.

Filters
  groups, environments, resource technologies, OS families, OS builds and
  configuration managers. A workflow is offered on a server when the server
  passes every filter that is set, with the checks CloudBolt makes for
  actions: the server's own group (no parent groups), its environment, its
  resource handler's technology, its OS family or a parent of it, its OS
  build (actions have no OS build filter; workflows add one), and any of its
  environment's configuration managers.

  The admin page offers a step only when the action can run on every server
  the workflow's filters allow: an action restricted on a filter needs the
  workflow restricted on that filter to values the action allows. Both
  checks are Run Task's (server_tasks.server_fits_filters, filters_refusal
  and refusal), which handle every action type; OS builds are checked here,
  because only workflows have them. Workflow
  environments stand in for resource technologies, and OS builds for OS
  families, when the workflow sets only the former. Remote Scripts always
  name OS families, so a workflow needs an OS family (or OS build) filter
  before it can hold one.

Running
  Every step and input is checked against the server before the first step
  starts. Remote Scripts run inside the workflow's job through
  server_tasks.run_task, which returns the script's output; the other
  action types run as child jobs of the workflow's job. A step that fails
  stops the run, unless it continues on error, in which case the run ends
  with WARNING. Cancelling the job stops it before the next step.

Outputs
  A Remote Script publishes an output with Run Task's convention, a line
      CB_OUTPUT name=value
  printed by the base templates' Write-TaskOutput (PowerShell) or task_output
  (bash) helper, last and before exit 0, and read by
  server_tasks.parse_outputs. Files attached to a step report their resolved
  versions the same way (archive_<file stem>_version). Every script step also
  publishes "stdout", the end of its output. A plug-in step publishes the
  "outputs" dict it returns. Each step's outputs are saved on the workflow's
  job under the step's key, so the job page shows them and later plug-in
  steps also receive them in their outputs keyword argument.

Files
  A script step can list archive files for the File Archives content to stage
  on the server before the script runs, as its entries
  "archive/file[@latest|@N] [extract]" (for example installers/agent.msi or
  configs/app.conf@4). They go to server_tasks.run_task as archive_refs. The
  file_archives shared module checks each entry and lists the files; without
  it a step cannot have files.

Secrets
  CloudBolt writes a server action's custom-form payload and its plain-text
  inputs to the job log, and this store is plain text, so password inputs
  (PWD, ETXT) can be neither asked for nor saved, the same rule as Run Task.
  A step that requires one cannot be added. Inputs that hold CloudBolt
  objects (networks, files, ...) are left out the same way.
"""
import html
import inspect
import json
import re
import time
from collections import OrderedDict
from contextlib import contextmanager
from datetime import datetime, timezone as dt_timezone

from django.contrib.contenttypes.models import ContentType
from django.db import transaction

from accounts.models import Group
from cbhooks.models import (
    CloudBoltHook,
    CopyFileAction,
    EmailHook,
    HookPointAction,
    InboundWebHook,
    MCPToolAction,
    OrchestrationHook,
    RecurringActionJob,
    RemoteScriptHook,
    ResourceAction,
    ServerAction,
    WebHook,
)
from cbhooks.services.parsing import get_hook_action_input_dict
from common.mixins import get_global_id_chars
from connectors.models import ConnectorConf
from externalcontent.models import OSBuild, OSFamily
from features.methods import is_active as feature_is_active
from infrastructure.models import CustomField, Environment, Namespace
from jobs.models import Job
from orders.models import CustomFieldValue
from resourcehandlers.models import ResourceTechnology
from servicecatalog.models import RunCloudBoltHookServiceItem, ServiceBlueprint, TearDownServiceItem
from utilities.events import add_server_event
from utilities.logger import ThreadLogger

from shared_modules import server_tasks

logger = ThreadLogger(__name__)

STORE_NAMESPACE = "server_workflows"
STORE_FIELD = "server_workflow_store"
STORE_FIELD_LABEL = "Server workflow store"
STORE_FIELD_DESCRIPTION = (
    "Managed by the Server Workflows admin page: one value per workflow. "
    "Do not delete; deleting it deletes every workflow on this appliance."
)
STORE_FIELD_TYPES = ("TXT", "CODE")

ID_PREFIX = "SWF"
WORKFLOW_ID_RE = re.compile(r"^SWF-[a-z0-9]{8,}$")
STEP_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,49}$")
# server_tasks.parse_outputs reads names of letters, digits and underscores.
OUTPUT_NAME_RE = re.compile(r"^[A-Za-z0-9_]{1,64}$")
MAX_ARCHIVES = 20
NAME_MAX = 255
DESCRIPTION_MAX = 1000
MAX_STEPS = 50
MAX_RETRIES = 10
MAX_RETRY_DELAY = 3600
MAX_TIMEOUT_MINUTES = 1440
STDOUT_LIMIT = 4000
# Job outputs keys a step key may not take: the run summary lives under one.
RESERVED_KEYS = {"workflow", "outputs", "server", "servers", "job"}

# The plug-ins behind Run Workflow and Run Task: never steps of a workflow.
RUNNER_HOOK_IDS = {"OHK-69gk5ne9", "OHK-o4tgygjs"}

MODES = ("fixed", "prompt", "output")

# Joins a step key and an input name in the run form's question names and in
# a flat workflow_inputs object.
PROMPT_SEPARATOR = "__"


class WorkflowError(Exception):
    """A request the workflow code refuses. The message is for the person using the page, form or API."""


class WorkflowNotFound(WorkflowError):
    pass


# ---- filters --------------------------------------------------------------


class FilterType(object):
    """One workflow filter: the objects it can name and how they are stored."""

    def __init__(self, key, label, singular, model, help_text, keyed_by_pk=False):
        self.key = key
        self.label = label
        self.singular = singular
        self.model = model
        self.help_text = help_text
        self.keyed_by_pk = keyed_by_pk

    def choices(self):
        """What the admin page offers, mirroring the filters on Admin > Actions."""
        queryset = self.model.objects.all()
        if self.model is ResourceTechnology:
            # Admin > Actions leaves out technologies whose feature is explicitly off.
            off = [rt.id for rt in queryset if feature_is_active(f"resourcehandler::{rt.slug}") is False]
            queryset = queryset.exclude(id__in=off)
        return sorted(queryset, key=lambda obj: self.label_of(obj).lower())

    def key_of(self, obj):
        return str(obj.pk) if self.keyed_by_pk else obj.global_id

    def label_of(self, obj):
        return str(getattr(obj, "name", "") or obj)

    def lookup(self, keys):
        """{key: object} for the keys that still exist."""
        keys = [str(key) for key in keys or []]
        if not keys:
            return {}
        if self.keyed_by_pk:
            ids = [int(key) for key in keys if key.isdigit()]
            return {str(obj.pk): obj for obj in self.model.objects.filter(pk__in=ids)}
        return {obj.global_id: obj for obj in self.model.objects.filter(global_id__in=keys)}


FILTERS = OrderedDict(
    (f.key, f)
    for f in (
        FilterType(
            "os_families", "OS families", "OS family", OSFamily,
            "Servers of these OS families, or of a family under one of them.",
        ),
        FilterType(
            "resource_technologies", "Resource technologies", "Resource technology", ResourceTechnology,
            "Servers whose resource handler uses one of these technologies.", keyed_by_pk=True,
        ),
        FilterType("environments", "Environments", "Environment", Environment, "Servers in these environments."),
        FilterType("groups", "Groups", "Group", Group, "Servers in these groups (not their subgroups)."),
        FilterType("os_builds", "OS builds", "OS build", OSBuild, "Servers built from these OS builds."),
        FilterType(
            "configuration_managers", "Configuration managers", "Configuration manager", ConnectorConf,
            "Servers whose environment uses one of these configuration managers.",
        ),
    )
)


def filter_choices():
    """Every filter with the values the admin page offers."""
    return [
        {
            "key": f.key,
            "label": f.label,
            "help": f.help_text,
            "options": [{"value": f.key_of(obj), "label": f.label_of(obj)} for obj in f.choices()],
        }
        for f in FILTERS.values()
    ]


def resolve_filters(filters):
    """{filter key: [objects]} for a workflow's stored filters; values that no longer exist are dropped."""
    filters = filters or {}
    resolved = {}
    for key, ftype in FILTERS.items():
        found = ftype.lookup(filters.get(key) or [])
        resolved[key] = [found[k] for k in (str(v) for v in filters.get(key) or []) if k in found]
    return resolved


def describe_filters(filters):
    """The filters that are set, by label, for display; missing values are listed apart."""
    described = []
    filters = filters or {}
    for key, ftype in FILTERS.items():
        keys = [str(v) for v in filters.get(key) or []]
        if not keys:
            continue
        found = ftype.lookup(keys)
        described.append(
            {
                "key": key,
                "label": ftype.label,
                "values": [ftype.label_of(found[k]) for k in keys if k in found],
                "missing": [k for k in keys if k not in found],
            }
        )
    return described


def _names(objects):
    return ", ".join(sorted(str(getattr(obj, "name", obj)) for obj in objects))


def workflow_refusal(workflow, server, resolved=None):
    """
    Why workflow is not offered on server, or '' when it is: Run Task's fit
    check (server_tasks.server_fits_filters) for every filter actions have,
    then the OS build filter, which only workflows have.
    """
    resolved = resolved if resolved is not None else resolve_filters(workflow.get("filters"))
    name = workflow.get("name") or workflow.get("id")
    fits = server_tasks.server_fits_filters(
        server,
        os_families=resolved["os_families"],
        technologies=resolved["resource_technologies"],
        environments=resolved["environments"],
        groups=resolved["groups"],
        configuration_managers=resolved["configuration_managers"],
    )
    if not fits:
        described = "; ".join(
            f"{f['label'].lower()}: {', '.join(f['values'])}"
            for f in describe_filters(workflow.get("filters"))
            if f["key"] != "os_builds" and f["values"]
        )
        return f"{server.hostname} is outside the filters of '{name}' ({described})."
    builds = resolved["os_builds"]
    if builds and server.os_build not in builds:
        return f"'{name}' runs on servers built from {_names(builds)} only."
    return ""


def workflows_for_servers(servers):
    """The enabled workflows offered on every one of servers; every enabled one when servers is empty."""
    workflows = [wf for wf in list_workflows() if wf.get("enabled", True)]
    if not servers:
        return workflows
    offered = []
    for wf in workflows:
        resolved = resolve_filters(wf.get("filters"))
        if not any(workflow_refusal(wf, server, resolved) for server in servers):
            offered.append(wf)
    return offered


def workflow_options(servers):
    """(value, label) pairs for the workflow dropdown: the workflow's id and name."""
    return [(wf["id"], wf["name"]) for wf in workflows_for_servers(servers)]


# ---- step actions ---------------------------------------------------------


STEP_TYPES = OrderedDict(
    [
        ("script", {"label": "Remote Script", "model": RemoteScriptHook, "icon": "bi-terminal"}),
        ("plugin", {"label": "CloudBolt Plug-in", "model": CloudBoltHook, "icon": "bi-plug"}),
        ("webhook", {"label": "Webhook", "model": WebHook, "icon": "bi-broadcast"}),
        ("email", {"label": "Email", "model": EmailHook, "icon": "bi-envelope"}),
        ("copy", {"label": "Copy File", "model": CopyFileAction, "icon": "bi-file-earmark-arrow-up"}),
    ]
)


def step_type(hook):
    """The STEP_TYPES key of an action, or None when it cannot be a step."""
    real = hook.real_type.model_class() if hook.real_type_id else type(hook)
    for key, info in STEP_TYPES.items():
        if real is info["model"]:
            return key
    return None


def _prefetch(queryset, model):
    names = ["groups", "environments", "resource_technologies", "configuration_managers"]
    if hasattr(model, "os_families"):
        names.append("os_families")
    return queryset.prefetch_related(*names)


def _special_purpose_hook_ids():
    """
    Plug-ins that serve another purpose and are no use as a step: generated
    options, display conditions, blueprint discovery, inbound webhooks, MCP
    tools and recurring jobs, plus blueprint build and teardown items and
    resource actions, which expect a resource, unless a server action or an
    orchestration action also runs them.
    """
    generated = HookPointAction.objects.filter(hook_point__name="generated_custom_field_options")
    ids = set(generated.values_list("hook_id", flat=True))
    resource_side = set(RunCloudBoltHookServiceItem.objects.values_list("hook_id", flat=True))
    resource_side.update(TearDownServiceItem.objects.values_list("hook_id", flat=True))
    resource_side.update(ResourceAction.objects.values_list("hook_id", flat=True))
    server_side = set(ServerAction.objects.values_list("hook_id", flat=True))
    server_side.update(HookPointAction.objects.exclude(pk__in=generated).values_list("hook_id", flat=True))
    ids.update(resource_side - server_side)
    ids.update(ServerAction.objects.exclude(condition=None).values_list("condition_id", flat=True))
    ids.update(ResourceAction.objects.exclude(condition=None).values_list("condition_id", flat=True))
    ids.update(ServiceBlueprint.objects.exclude(discovery_plugin=None).values_list("discovery_plugin_id", flat=True))
    for model in (InboundWebHook, MCPToolAction, RecurringActionJob):
        ids.update(model.objects.values_list("hook_id", flat=True))
    return ids


def candidate_actions(include_all=False):
    """Every action that can be a workflow step, each cast to its own type, by name."""
    special = set() if include_all else _special_purpose_hook_ids()
    hooks = []
    for key, info in STEP_TYPES.items():
        model = info["model"]
        queryset = model.objects.filter(real_type=ContentType.objects.get_for_model(model))
        if model is RemoteScriptHook:
            # Such a script always runs on its Run on Server host (server_tasks.refusal).
            queryset = queryset.filter(run_on_server__isnull=True)
        if model is CloudBoltHook and not include_all:
            queryset = queryset.exclude(is_ootb=True).exclude(id__in=special)
        for hook in _prefetch(queryset, model):
            if hook.global_id not in RUNNER_HOOK_IDS:
                hooks.append(hook)
    return sorted(hooks, key=lambda hook: hook.name.lower())


def resolve_action(value):
    """The step-able action that value names: its global ID or exact name."""
    text = str(value or "").strip()
    if not text:
        raise WorkflowError("Choose an action for every step.")
    hook = OrchestrationHook.objects.filter(global_id=text).first() or OrchestrationHook.objects.filter(
        name=text
    ).first()
    if hook is None:
        raise WorkflowError(f"There is no action '{text}' on this appliance.")
    hook = hook.cast()
    if step_type(hook) is None or hook.global_id in RUNNER_HOOK_IDS:
        kind = getattr(hook, "hook_type_label", "") or type(hook).__name__
        raise WorkflowError(f"'{hook.name}' is a {kind}, which cannot be a workflow step.")
    if isinstance(hook, RemoteScriptHook) and hook.run_on_server_id:
        raise WorkflowError(f"'{hook.name}' always runs on its Run on Server, so it cannot be a workflow step.")
    return hook


def _effective(resolved):
    """
    The technologies and OS families a workflow's servers can have: the
    filters themselves, else what its environments and OS builds imply.
    """
    technologies = set(resolved["resource_technologies"])
    if not technologies:
        for env in resolved["environments"]:
            handler = env.resource_handler
            if handler is not None and handler.resource_technology is not None:
                technologies.add(handler.resource_technology)
            else:
                technologies = set()
                break
    families = set(resolved["os_families"])
    if not families:
        for build in resolved["os_builds"]:
            if build.os_family is not None:
                families.add(build.os_family)
            else:
                families = set()
                break
    return technologies, families


def step_conflicts(hook, resolved):
    """
    Why hook cannot run on every server resolved (a workflow's resolved
    filters) allows, as a list of sentences; empty when it can. This is Run
    Task's fit check (server_tasks.filters_refusal), which covers every
    action type: an action restricted on a filter needs the workflow
    restricted on it to values the action allows.
    """
    technologies, families = _effective(resolved)
    reason = server_tasks.filters_refusal(
        hook,
        os_families=list(families),
        technologies=list(technologies),
        environments=resolved["environments"],
        groups=resolved["groups"],
        configuration_managers=resolved["configuration_managers"],
    )
    return [reason] if reason else []


def action_refusal(hook, server):
    """Why hook cannot run on server, or '' when it can: Run Task's fit check."""
    return server_tasks.refusal(hook, server)


def _supported(field):
    return field.type in server_tasks.PLAIN_TYPES and field.type not in server_tasks.SECRET_TYPES


def _source(hook):
    try:
        return hook.file_content() or ""
    except Exception:
        return ""


def _generated_inputs(hook, source=None):
    """Input names a plug-in fills its dropdown for with generate_options_for_<name>."""
    if not isinstance(hook, CloudBoltHook):
        return set()
    return set(re.findall(r"^def generate_options_for_(\w+)\(", source or _source(hook), re.MULTILINE))


def output_suggestions(hook, source=None):
    """
    Output names a script is known to publish, read from its CB_OUTPUT lines
    and its Write-TaskOutput / task_output calls, plus stdout.
    """
    if not isinstance(hook, RemoteScriptHook):
        return []
    pattern = re.compile(r"(?:CB_OUTPUT\s+|Write-TaskOutput\s+(?:-Name\s+)?['\"]?|task_output\s+['\"]?)([A-Za-z0-9_]+)")
    names = []
    for line in (source or _source(hook)).splitlines():
        if not line.strip().startswith("#"):
            names.extend(pattern.findall(line))
    return list(dict.fromkeys(names + ["stdout"]))


def input_info(field, generated=()):
    """An action input as the admin page and the run form show it."""
    return {
        "name": field.simple_name,
        "label": server_tasks.input_label(field),
        "type": field.type,
        "type_label": field.get_type_display(),
        "required": bool(field.required),
        "multiple": bool(field.allow_multiple),
        "description": field.description or "",
        "placeholder": field.placeholder or "",
        "options": server_tasks.input_options(field),
        "generated": field.simple_name in generated,
        "supported": _supported(field),
    }


def action_summary(hook):
    """An action as the Add step picker lists it."""
    kind = step_type(hook)
    families = list(hook.os_families.all()) if hasattr(hook, "os_families") else []
    blocking = [
        server_tasks.input_label(field)
        for field in server_tasks.task_fields(hook)
        if field.required and not _supported(field)
    ]
    return {
        "id": hook.global_id,
        "name": hook.name,
        "type": kind,
        "type_label": STEP_TYPES[kind]["label"],
        "icon": STEP_TYPES[kind]["icon"],
        "description": hook.description or "",
        "os_families": [f.name for f in families],
        "blocking": blocking,
    }


def action_detail(hook):
    """An action's inputs and known outputs, for configuring a step."""
    source = _source(hook)
    generated = _generated_inputs(hook, source)
    detail = action_summary(hook)
    detail["inputs"] = [input_info(field, generated) for field in server_tasks.task_fields(hook)]
    detail["outputs"] = output_suggestions(hook, source)
    try:
        detail["url"] = hook.get_absolute_url()
    except Exception:
        detail["url"] = None
    return detail


def actions_for_filters(filters, include_all=False):
    """The Add step picker: actions that fit the filters, and the others with the reasons."""
    resolved = resolve_filters(filters)
    available, unavailable = [], []
    for hook in candidate_actions(include_all=include_all):
        summary = action_summary(hook)
        reasons = step_conflicts(hook, resolved)
        if summary["blocking"]:
            reasons.append(f"'{hook.name}' requires {', '.join(summary['blocking'])}, which a workflow cannot carry.")
        if reasons:
            summary["reasons"] = reasons
            unavailable.append(summary)
        else:
            available.append(summary)
    return {"available": available, "unavailable": unavailable}


# ---- storage --------------------------------------------------------------


def _existing_field():
    return CustomField.objects.filter(name=STORE_FIELD).first()


def _field():
    """The store parameter, created on first use in its own Namespace."""
    namespace, _ = Namespace.objects.get_or_create(name=STORE_NAMESPACE)
    field, _ = CustomField.objects.get_or_create(
        name=STORE_FIELD,
        defaults=dict(
            label=STORE_FIELD_LABEL,
            description=STORE_FIELD_DESCRIPTION,
            type="TXT",
            namespace=namespace,
        ),
    )
    if field.type not in STORE_FIELD_TYPES:
        raise WorkflowError(
            f"The {STORE_FIELD} parameter must be Multi-line Text or Code; it is {field.type}."
        )
    if field.namespace_id != namespace.id:
        field.namespace = namespace
        field.save()
    return field


@contextmanager
def _locked():
    """A transaction holding a row lock on the store parameter, so writes run one at a time."""
    field = _field()
    with transaction.atomic():
        list(CustomField.objects.select_for_update().filter(pk=field.pk))
        yield field


def _parse(text):
    workflow = json.loads(text or "", object_pairs_hook=OrderedDict)
    if not isinstance(workflow, dict) or not WORKFLOW_ID_RE.match(str(workflow.get("id") or "")):
        raise ValueError("not a server workflow")
    workflow.setdefault("filters", {})
    workflow.setdefault("steps", [])
    workflow.setdefault("enabled", True)
    return workflow


def _rows(field):
    """[(CustomFieldValue, workflow)] oldest first; unreadable values and repeated ids are skipped."""
    if field is None:
        return []
    rows, seen = [], set()
    for cfv in CustomFieldValue.objects.filter(field=field).order_by("id"):
        try:
            workflow = _parse(cfv.txt_value)
        except ValueError:
            logger.warning("Skipping unreadable server workflow value %s", cfv.id)
            continue
        if workflow["id"] in seen:
            logger.warning("Ignoring duplicate server workflow %s in value %s", workflow["id"], cfv.id)
            continue
        seen.add(workflow["id"])
        rows.append((cfv, workflow))
    return rows


def _find(rows, workflow_id):
    for cfv, workflow in rows:
        if workflow["id"] == workflow_id:
            return cfv, workflow
    raise WorkflowNotFound(f"There is no workflow {workflow_id}.")


def _store(cfv, workflow):
    # Set through the typed value property and save, so an existing row is
    # updated in place rather than matched or re-created by value.
    cfv.value = json.dumps(workflow, indent=2)
    cfv.save()


def _now():
    # Real UTC: CloudBolt runs with USE_TZ = False, so timezone.now() is naive local time.
    return datetime.now(dt_timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _who(profile):
    if profile is None:
        return ""
    user = getattr(profile, "user", None)
    return str(getattr(user, "username", "") or profile)


def list_workflows():
    """Every stored workflow, by name."""
    workflows = [workflow for _, workflow in _rows(_existing_field())]
    return sorted(workflows, key=lambda wf: (wf.get("name") or "").lower())


def get_workflow(workflow_id):
    text = str(workflow_id or "").strip()
    if not WORKFLOW_ID_RE.match(text):
        raise WorkflowNotFound(f"There is no workflow {text or '(none chosen)'}.")
    return _find(_rows(_existing_field()), text)[1]


def resolve_workflow(value):
    """The workflow that value names: its SWF- id or exact name."""
    text = str(value or "").strip()
    if not text:
        raise WorkflowError("Choose a workflow.")
    if WORKFLOW_ID_RE.match(text):
        return get_workflow(text)
    for workflow in list_workflows():
        if workflow.get("name") == text:
            return workflow
    raise WorkflowNotFound(f"There is no workflow '{text}'.")


def _new_id(existing):
    while True:
        workflow_id = f"{ID_PREFIX}-{get_global_id_chars()}"
        if workflow_id not in existing:
            return workflow_id


def create_workflow(data, profile=None):
    with _locked() as field:
        stored = [wf for _, wf in _rows(field)]
        workflow = clean_workflow(data, others=stored)
        workflow["id"] = _new_id({wf["id"] for wf in stored})
        workflow["created_at"] = workflow["updated_at"] = _now()
        workflow["created_by"] = workflow["updated_by"] = _who(profile)
        workflow = _ordered(workflow)
        _store(CustomFieldValue(field=field), workflow)
    return workflow


def update_workflow(workflow_id, data, profile=None):
    with _locked() as field:
        rows = _rows(field)
        cfv, current = _find(rows, workflow_id)
        others = [wf for _, wf in rows if wf["id"] != workflow_id]
        workflow = clean_workflow(data, others=others)
        workflow["id"] = current["id"]
        workflow["created_at"] = current.get("created_at", "")
        workflow["created_by"] = current.get("created_by", "")
        workflow["updated_at"] = _now()
        workflow["updated_by"] = _who(profile)
        workflow = _ordered(workflow)
        _store(cfv, workflow)
    return workflow


def copy_workflow(workflow_id, profile=None):
    """A disabled copy named 'Copy of <name>', so it is not offered before it is reviewed."""
    source = get_workflow(workflow_id)
    names = {(wf.get("name") or "").lower() for wf in list_workflows()}
    base = f"Copy of {source['name']}"[:NAME_MAX]
    name, number = base, 2
    while name.lower() in names:
        name = f"{base[:NAME_MAX - 6]} ({number})"
        number += 1
    data = dict(source, name=name, enabled=False)
    return create_workflow(data, profile=profile)


def delete_workflow(workflow_id):
    with _locked() as field:
        cfv, _ = _find(_rows(field), workflow_id)
        cfv.delete()


def _ordered(workflow):
    keys = (
        "id", "name", "description", "enabled", "filters", "steps",
        "created_at", "created_by", "updated_at", "updated_by",
    )
    return OrderedDict((key, workflow.get(key)) for key in keys if key in workflow)


# ---- validation -----------------------------------------------------------


def _text(value, label, limit, required=False):
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise WorkflowError(f"{label} must be text.")
    value = " ".join(value.split())
    if required and not value:
        raise WorkflowError(f"A workflow needs a {label.lower()}.")
    if len(value) > limit:
        raise WorkflowError(f"Keep the {label.lower()} under {limit} characters.")
    return value


def _int(value, label, low, high, default):
    if value in (None, ""):
        return default
    try:
        number = int(str(value).strip())
    except ValueError:
        raise WorkflowError(f"{label} must be a whole number.")
    if number < low or number > high:
        raise WorkflowError(f"{label} must be between {low} and {high}.")
    return number


def _clean_filters(filters):
    if filters in (None, ""):
        filters = {}
    if not isinstance(filters, dict):
        raise WorkflowError("filters must be an object of filter names and lists of values.")
    unknown = sorted(set(filters) - set(FILTERS))
    if unknown:
        raise WorkflowError(f"Unknown filter(s): {', '.join(unknown)}. Filters: {', '.join(FILTERS)}.")
    cleaned = OrderedDict()
    for key, ftype in FILTERS.items():
        values = filters.get(key) or []
        if not isinstance(values, (list, tuple)):
            values = [values]
        keys = list(dict.fromkeys(str(v).strip() for v in values if str(v).strip()))
        found = ftype.lookup(keys)
        missing = [k for k in keys if k not in found]
        if missing:
            raise WorkflowError(f"No {ftype.singular.lower()} {', '.join(missing)} exists on this appliance.")
        cleaned[key] = keys
    return cleaned


def suggest_step_key(name, taken):
    """A step key from a step name: lower case, underscores, unique among taken."""
    base = re.sub(r"[^a-z0-9]+", "_", (name or "").lower()).strip("_") or "step"
    if not base[0].isalpha():
        base = f"step_{base}"
    base = base[:40]
    key, number = base, 2
    while key in taken or key in RESERVED_KEYS:
        key = f"{base}_{number}"
        number += 1
    return key


def _clean_input(step_label, field, config, earlier):
    """One input's configuration, checked against the input; None leaves it to be asked for."""
    label = f"{step_label}: {server_tasks.input_label(field)}"
    if config in (None, ""):
        config = {}
    if not isinstance(config, dict):
        raise WorkflowError(f"{label}: the input setting must be an object with a mode.")
    mode = str(config.get("mode") or "prompt").strip()
    if mode not in MODES:
        raise WorkflowError(f"{label}: mode must be one of {', '.join(MODES)}.")
    if not _supported(field):
        if mode != "prompt" or field.required:
            raise WorkflowError(
                f"{label} is a {field.get_type_display()} input, which a workflow cannot carry "
                "(CloudBolt logs workflow inputs in plain text)."
            )
        return None
    if mode == "prompt":
        return OrderedDict([("mode", "prompt")])
    if mode == "output":
        step = str(config.get("step") or "").strip()
        output = str(config.get("output") or "").strip()
        if step not in earlier:
            raise WorkflowError(f"{label}: choose an earlier step to take the output from.")
        if not OUTPUT_NAME_RE.match(output):
            raise WorkflowError(f"{label}: name the output (letters, digits and underscores).")
        return OrderedDict([("mode", "output"), ("step", step), ("output", output)])
    value = config.get("value")
    blank = value is None or (isinstance(value, str) and not value.strip()) or value == []
    if blank:
        if field.required:
            raise WorkflowError(f"{label} is required: give it a value or ask for it when the workflow runs.")
        return OrderedDict([("mode", "fixed"), ("value", "")])
    try:
        value = server_tasks.coerce_input(field, value)
    except server_tasks.TaskError as err:
        raise WorkflowError(f"{step_label}: {err}")
    return OrderedDict([("mode", "fixed"), ("value", value)])


def _clean_step(index, step, resolved, earlier, taken):
    if not isinstance(step, dict):
        raise WorkflowError(f"Step {index} must be an object.")
    hook = resolve_action(step.get("action"))
    name = _text(step.get("name") or hook.name, f"Step {index} name", NAME_MAX)
    label = f"Step {index} ({name})"
    key = str(step.get("key") or "").strip() or suggest_step_key(name, taken)
    if not STEP_KEY_RE.match(key):
        raise WorkflowError(
            f"{label}: the key must start with a letter and use only lower-case letters, digits and underscores."
        )
    if key in RESERVED_KEYS:
        raise WorkflowError(f"{label}: '{key}' is reserved; choose another key.")
    if key in taken:
        raise WorkflowError(f"{label}: another step already uses the key '{key}'.")
    conflicts = step_conflicts(hook, resolved)
    if conflicts:
        raise WorkflowError(f"{label}: {' '.join(conflicts)}")
    configs = step.get("inputs") or {}
    if not isinstance(configs, dict):
        raise WorkflowError(f"{label}: inputs must be an object of input names and settings.")
    fields = {field.simple_name: field for field in server_tasks.task_fields(hook)}
    unknown = sorted(name for name in configs if name not in fields)
    if unknown:
        raise WorkflowError(f"{label}: '{hook.name}' has no input named {', '.join(unknown)}.")
    inputs = OrderedDict()
    for input_name, field in fields.items():
        config = _clean_input(label, field, configs.get(input_name), earlier)
        if config is not None:
            inputs[input_name] = config
    archives = _clean_archives(label, hook, step.get("archives"))
    return OrderedDict(
        [
            ("key", key),
            ("name", name),
            ("action", hook.global_id),
            ("type", step_type(hook)),
            ("continue_on_error", bool(step.get("continue_on_error"))),
            ("retries", _int(step.get("retries"), f"{label} retries", 0, MAX_RETRIES, 0)),
            ("retry_delay", _int(step.get("retry_delay"), f"{label} retry delay", 0, MAX_RETRY_DELAY, 0)),
            (
                "timeout_minutes",
                _int(step.get("timeout_minutes"), f"{label} timeout", 1, MAX_TIMEOUT_MINUTES, None),
            ),
            ("inputs", inputs),
            ("archives", archives),
        ]
    )


def _file_archives():
    """The File Archives shared module, or None when that content is not installed."""
    try:
        from shared_modules import file_archives
    except ImportError:
        return None
    return file_archives


def archive_choices():
    """
    The files a step can attach, from file_archives.file_choices(): dicts with
    ref, archive, file, latest, versions and extractable. None when File
    Archives is not installed.
    """
    module = _file_archives()
    if module is None:
        return None
    try:
        return list(module.file_choices())
    except Exception:
        logger.exception("Could not list the File Archives files")
        return []


def _clean_archives(label, hook, archives):
    """A script step's attached files, each a File Archives entry such as installers/agent.msi@latest extract."""
    if archives in (None, "", []):
        return []
    if isinstance(archives, str):
        # An entry may hold a space ("... extract"), so only commas and new lines separate entries.
        archives = re.split(r"[,\n]+", archives)
    if not isinstance(archives, (list, tuple)):
        raise WorkflowError(f"{label}: files must be a list of File Archives entries.")
    entries = list(dict.fromkeys(" ".join(str(entry).split()) for entry in archives if str(entry).strip()))
    if not entries:
        return []
    if not isinstance(hook, RemoteScriptHook):
        raise WorkflowError(f"{label}: only Remote Script steps can have files attached.")
    if len(entries) > MAX_ARCHIVES:
        raise WorkflowError(f"{label}: attach at most {MAX_ARCHIVES} files.")
    module = _file_archives()
    if module is None:
        raise WorkflowError(f"{label}: files need the File Archives content, which is not installed on this appliance.")
    for entry in entries:
        try:
            module.validate_entry(entry, must_exist=True)
        except module.ArchiveError as err:
            raise WorkflowError(f"{label}: {entry}: {err}")
    return entries


def _run_task_takes_archives():
    """Whether this appliance's server_tasks.run_task accepts archive_refs (added by the File Archives content)."""
    try:
        return "archive_refs" in inspect.signature(server_tasks.run_task).parameters
    except (TypeError, ValueError):
        return False


def clean_workflow(data, others=()):
    """
    Validate a workflow from the admin page or the API and return it in the
    stored form, without id or timestamps. others are the other stored
    workflows, to keep the name unique.
    """
    if not isinstance(data, dict):
        raise WorkflowError("A workflow must be a JSON object.")
    name = _text(data.get("name"), "Name", NAME_MAX, required=True)
    for other in others:
        if (other.get("name") or "").lower() == name.lower():
            raise WorkflowError(f"A workflow named '{other['name']}' already exists.")
    description = _text(data.get("description"), "Description", DESCRIPTION_MAX)
    filters = _clean_filters(data.get("filters"))
    resolved = resolve_filters(filters)
    steps_in = data.get("steps") or []
    if not isinstance(steps_in, (list, tuple)):
        raise WorkflowError("steps must be a list.")
    if not steps_in:
        raise WorkflowError("Add at least one step.")
    if len(steps_in) > MAX_STEPS:
        raise WorkflowError(f"A workflow can have at most {MAX_STEPS} steps.")
    steps, earlier, taken = [], [], set()
    for index, step in enumerate(steps_in, start=1):
        cleaned = _clean_step(index, step, resolved, earlier, taken)
        steps.append(cleaned)
        earlier.append(cleaned["key"])
        taken.add(cleaned["key"])
    return OrderedDict(
        [
            ("name", name),
            ("description", description),
            ("enabled", data.get("enabled", True) is not False),
            ("filters", filters),
            ("steps", steps),
        ]
    )


def describe_workflow(workflow):
    """A stored workflow with names resolved, for the list page and the editor."""
    steps = []
    for step in workflow.get("steps") or []:
        hook = OrchestrationHook.objects.filter(global_id=step.get("action")).first()
        hook = hook.cast() if hook else None
        steps.append(
            dict(
                step,
                action_name=hook.name if hook else "",
                exists=hook is not None,
                type_label=STEP_TYPES.get(step.get("type"), {}).get("label", ""),
                icon=STEP_TYPES.get(step.get("type"), {}).get("icon", "bi-question-circle"),
            )
        )
    return dict(workflow, steps=steps, filter_summary=describe_filters(workflow.get("filters")))


# ---- run time: prompts ----------------------------------------------------


class Step(object):
    """A stored step joined to its action and the action's inputs, for one run."""

    def __init__(self, index, data):
        self.index = index
        self.data = data
        self.key = data["key"]
        self.name = data.get("name") or self.key
        self.hook = resolve_action(data.get("action"))
        self.kind = step_type(self.hook)
        self.fields = OrderedDict((field.simple_name, field) for field in server_tasks.task_fields(self.hook))
        self.configs = data.get("inputs") or {}

    @property
    def label(self):
        return f"Step {self.index} ({self.name})"

    def config(self, input_name):
        return self.configs.get(input_name) or {"mode": "prompt"}

    def prompts(self):
        """The inputs to ask for: those set to prompt, and any the action gained since the workflow was saved."""
        return [
            field
            for name, field in self.fields.items()
            if self.config(name).get("mode") == "prompt" and _supported(field)
        ]

    def blocking(self):
        """Required inputs a workflow cannot carry (added to the action after the workflow was saved)."""
        return [server_tasks.input_label(f) for f in self.fields.values() if f.required and not _supported(f)]


def load_steps(workflow):
    """The workflow's steps joined to their actions; WorkflowError names a step whose action is gone."""
    steps = []
    for index, data in enumerate(workflow.get("steps") or [], start=1):
        try:
            steps.append(Step(index, data))
        except WorkflowError as err:
            raise WorkflowError(f"Step {index} ({data.get('name') or data.get('key')}): {err}")
    if not steps:
        raise WorkflowError(f"Workflow '{workflow.get('name')}' has no steps.")
    return steps


def _choices(step, field, server, profile):
    """Dropdown values for a prompt: the input's own options, or a plug-in's generated ones for server."""
    options = server_tasks.input_options(field)
    if options or server is None or not isinstance(step.hook, CloudBoltHook):
        return [{"value": option, "text": option} for option in options]
    if field.simple_name not in _generated_inputs(step.hook):
        return []
    try:
        from common.methods import cf_generated_choices_list

        result = cf_generated_choices_list(
            field,
            environment=server.environment,
            group=server.group,
            profile=profile,
            server=server,
            servers=[server],
            action=step.hook,
        )
    except Exception as err:
        logger.warning("Options for %s of %s failed: %s", field.simple_name, step.hook.name, err)
        return []
    choices = []
    for option in (result or {}).get("options") or []:
        if isinstance(option, (list, tuple)) and option:
            value = option[0]
            text = option[1] if len(option) > 1 else option[0]
        else:
            value = text = option
        if value in (None, ""):
            continue
        choices.append({"value": str(value), "text": str(text)})
    return choices


def prompt_name(step, field):
    return f"{step.key}{PROMPT_SEPARATOR}{field.simple_name}"


def prompt_panel(workflow, servers, profile=None):
    """
    The run form's Dynamic Panel template for a workflow: one panel per step
    with something to ask, plus what blocks the run on these servers.
    """
    steps = load_steps(workflow)
    server = servers[0] if servers else None
    elements, blocking, summary = [], [], []
    for step in steps:
        summary.append({"key": step.key, "name": step.name, "action": step.hook.name, "type": STEP_TYPES[step.kind]["label"]})
        for target in servers:
            reason = action_refusal(step.hook, target)
            if reason:
                blocking.append(f"{step.label}: {reason}")
        if step.blocking():
            blocking.append(f"{step.label} requires {', '.join(step.blocking())}, which a workflow cannot carry.")
        questions = []
        for field in step.prompts():
            element = server_tasks.input_element(field)
            element["name"] = prompt_name(step, field)
            choices = _choices(step, field, server, profile)
            if choices and element.get("type") not in ("dropdown", "tagbox"):
                element["type"] = "tagbox" if field.allow_multiple else "dropdown"
                element.pop("inputType", None)
                element.pop("validators", None)
            if choices:
                element["choices"] = choices
            questions.append(element)
        if questions:
            elements.append(
                {
                    "type": "panel",
                    "name": f"step_{step.key}",
                    "title": f"{step.index}. {step.name}",
                    "description": step.hook.name,
                    "elements": questions,
                }
            )
    return {
        "title": workflow.get("name"),
        "description": workflow.get("description") or "",
        "elements": elements,
        "steps": summary,
        "blocking": blocking,
    }


def _payload_values(payload):
    """
    Return (values, strict): the submitted prompt values as {step key: {input
    name: value}}. payload is the form's Dynamic Panel (a list holding one
    dict with flat '<step>__<input>' names, which may still carry values of a
    workflow chosen earlier), or a JSON object from the API or MCP, nested by
    step or flat. strict marks the JSON, where unknown names are refused to
    catch typos.
    """
    if payload is None or payload == "" or payload == []:
        return {}, False
    strict = not isinstance(payload, list)
    if isinstance(payload, (dict, list)):
        data = payload
    else:
        text = str(payload).strip()
        if not text:
            return {}, True
        try:
            data = json.loads(text)
        except ValueError:
            # Some CloudBolt paths store a text input HTML-escaped (&quot;).
            try:
                data = json.loads(html.unescape(text))
            except ValueError as err:
                raise WorkflowError(f"Workflow inputs must be a JSON object of step keys and input values: {err}")
    if isinstance(data, list):
        strict = False
        merged = {}
        for item in data:
            if isinstance(item, dict):
                merged.update(item)
        data = merged
    if not isinstance(data, dict):
        raise WorkflowError("Workflow inputs must be a JSON object of step keys and input values.")
    values = {}
    for name, value in data.items():
        if isinstance(value, dict):
            values.setdefault(str(name), {}).update(value)
        elif PROMPT_SEPARATOR in str(name):
            step_key, input_name = str(name).split(PROMPT_SEPARATOR, 1)
            values.setdefault(step_key, {})[input_name] = value
        elif strict:
            raise WorkflowError(
                f"'{name}' is not a step key or a '<step key>{PROMPT_SEPARATOR}<input name>' name."
            )
    return values, strict


def _blank(value):
    return value is None or (isinstance(value, str) and not value.strip()) or value == []


def parse_prompt_values(workflow, steps, payload, fallback=None):
    """
    Validate the asked-for values and return {step key: {input name: value}}.
    fallback, when given, is called with an input name for each prompt left
    blank (a blueprint item fills prompts from same-named order values).
    """
    submitted, strict = _payload_values(payload)
    known = {step.key: step for step in steps}
    unknown = sorted(key for key in submitted if key not in known)
    if unknown and strict:
        raise WorkflowError(f"Workflow '{workflow.get('name')}' has no step {', '.join(unknown)}.")
    values = {}
    for step in steps:
        given = submitted.get(step.key) or {}
        prompts = {field.simple_name: field for field in step.prompts()}
        extra = sorted(name for name in given if name not in prompts)
        if extra and strict:
            raise WorkflowError(
                f"{step.label} does not ask for {', '.join(extra)}. It asks for: {', '.join(prompts) or 'nothing'}."
            )
        values[step.key] = {}
        for name, field in prompts.items():
            value = given.get(name)
            if _blank(value) and fallback is not None:
                value = fallback(name)
            if _blank(value):
                if field.required:
                    raise WorkflowError(f"{step.label}: {server_tasks.input_label(field)} is required.")
                continue
            try:
                values[step.key][name] = server_tasks.coerce_input(field, value)
            except server_tasks.TaskError as err:
                raise WorkflowError(f"{step.label}: {err}")
    return values


def prompt_skeleton(workflow):
    """
    The workflow's prompts as a JSON object of step keys and empty input
    values, for a blueprint order form to pre-fill; '' when it asks for nothing.
    """
    skeleton = OrderedDict()
    for step in load_steps(workflow):
        names = [field.simple_name for field in step.prompts()]
        if names:
            skeleton[step.key] = OrderedDict((name, "") for name in names)
    return json.dumps(skeleton, indent=2) if skeleton else ""


def order_values(resource=None, server=None):
    """
    A fallback for parse_prompt_values on a blueprint order: the value of a
    same-named parameter on the deployed resource, else on the server (order
    form fields land on one or the other).
    """

    def lookup(name):
        for obj in (resource, server):
            if obj is None:
                continue
            try:
                value = obj.get_value_for_custom_field(name)
            except Exception:
                value = None
            if not _blank(value):
                return value
        return None

    return lookup


# ---- run time: running ----------------------------------------------------


def _canceled(job):
    return job is not None and Job.objects.filter(pk=job.pk, status="TO_CANCEL").exists()


def _step_inputs(step, prompted, outputs):
    """The step's input values for this run, by input name; WorkflowError when one is missing."""
    values = {}
    for name, field in step.fields.items():
        if not _supported(field):
            continue
        config = step.config(name)
        mode = config.get("mode")
        if mode == "fixed":
            value = config.get("value")
            if _blank(value):
                continue
        elif mode == "output":
            source = outputs.get(config.get("step")) or {}
            value = source.get(config.get("output"))
            if _blank(value):
                if field.required:
                    raise WorkflowError(
                        f"{step.label}: {server_tasks.input_label(field)} takes output "
                        f"'{config.get('output')}' of step '{config.get('step')}', which did not publish it."
                    )
                continue
        else:
            value = prompted.get(name)
            if _blank(value):
                if field.required:
                    raise WorkflowError(f"{step.label}: {server_tasks.input_label(field)} is required.")
                continue
        try:
            values[name] = server_tasks.coerce_input(field, value)
        except server_tasks.TaskError as err:
            raise WorkflowError(f"{step.label}: {err}")
    return values


def _run_script(step, server, inputs, job):
    archives = step.data.get("archives") or []
    if archives:
        output = server_tasks.run_task(step.hook, server, inputs, job=job, archive_refs=archives)
    else:
        output = server_tasks.run_task(step.hook, server, inputs, job=job)
    # WinRM returns the output without adding it to the job, unlike the other transports.
    if job is not None and server.is_windows() and not server.use_tech_specific_script_execution():
        job.set_progress(f"Output of '{step.name}' on {server.hostname}", detailed_message=server_tasks.tail(output))
    outputs = OrderedDict(server_tasks.parse_outputs(output))
    outputs["stdout"] = server_tasks.tail(output, STDOUT_LIMIT)
    return "SUCCESS", outputs, ""


def _run_child(step, server, inputs, job):
    """Run a non-script step as a child job of job and wait for it."""
    context = get_hook_action_input_dict(step.hook, inputs, input_names_clean=True)
    jobs = step.hook.run_as_job(
        owner=job.owner if job is not None else None,
        servers=[server],
        parent_job=job,
        context=context,
    )
    if not jobs:
        raise WorkflowError(f"CloudBolt skipped '{step.hook.name}' on {server.hostname}: its filters do not match.")
    timeout = step.data.get("timeout_minutes")
    child = jobs[0].wait_for_completion(timeout=timeout * 60 if timeout else None)
    outputs = OrderedDict(child.outputs or {}) if isinstance(child.outputs, dict) else OrderedDict()
    if child.status in ("SUCCESS", "WARNING"):
        return child.status, outputs, ""
    detail = (child.errors or child.output or "").strip()
    message = f"Job {getattr(child, 'global_id', None) or child.id} ended {child.status}"
    return "FAILURE", outputs, f"{message}: {server_tasks.tail(detail, 1500)}" if detail else message


def _attempt(step, server, inputs, job):
    try:
        if step.kind == "script":
            return _run_script(step, server, inputs, job)
        return _run_child(step, server, inputs, job)
    except WorkflowError as err:
        return "FAILURE", {}, str(err)
    except server_tasks.TaskError as err:
        return "FAILURE", {}, str(err)
    except Exception as err:
        logger.exception("Workflow step '%s' failed on %s", step.name, server.hostname)
        return "FAILURE", {}, server_tasks.failure_text(err)


def _save_outputs(job, outputs, summary):
    if job is None:
        return
    # The same Job instance the plug-in received: CloudBolt merges the plug-in's
    # returned outputs into it at the end and saves it, so a stale copy would win.
    job.outputs = dict(outputs, workflow=summary)
    job.save(update_fields=["outputs"])


def preflight(workflow, steps, servers, prompted):
    """Every reason the run cannot start, checked before the first step."""
    problems = []
    for server in servers:
        reason = workflow_refusal(workflow, server)
        if reason:
            problems.append(f"{server.hostname}: {reason}")
        if server.power_status != "POWERON" and any(step.kind == "script" for step in steps):
            problems.append(f"{server.hostname} is not powered on.")
        for step in steps:
            reason = action_refusal(step.hook, server)
            if reason:
                problems.append(f"{server.hostname}: {step.label}: {reason}")
            elif step.kind == "script" and server_tasks.needs_credentials(step.hook, server):
                problems.append(
                    f"{server.hostname}: CloudBolt has no credentials for {step.label}. Save them from the "
                    "Run Workflow form, or set them on the server or on the Remote Script."
                )
    for step in steps:
        if step.blocking():
            problems.append(f"{step.label} requires {', '.join(step.blocking())}, which a workflow cannot carry.")
        if step.data.get("archives") and not _run_task_takes_archives():
            problems.append(
                f"{step.label} has files attached, but this appliance's server_tasks module cannot stage files "
                "yet; sync the File Archives content, or remove the files from the step."
            )
        for name, field in step.fields.items():
            config = step.config(name)
            if not _supported(field) or config.get("mode") != "fixed":
                continue
            if _blank(config.get("value")) and field.required:
                problems.append(f"{step.label}: {server_tasks.input_label(field)} is required and has no value.")
    return list(dict.fromkeys(problems))


def run_workflow(workflow, server, prompted, job=None):
    """
    Run workflow's steps in order on server. Returns (status, summary text,
    error text, outputs); outputs holds each step's outputs by step key plus
    the run summary under "workflow".
    """
    steps = load_steps(workflow)
    total = len(steps)
    outputs = OrderedDict()
    summary = OrderedDict(
        [
            ("id", workflow["id"]),
            ("name", workflow.get("name")),
            ("server", server.hostname),
            ("status", "RUNNING"),
            ("steps", []),
        ]
    )
    add_server_event("MODIFICATION", server, f"Workflow '{workflow.get('name')}' started.", job=job)
    warnings, failure = [], None
    for step in steps:
        record = OrderedDict(
            [("key", step.key), ("name", step.name), ("action", step.hook.name), ("status", "SKIPPED"), ("seconds", 0)]
        )
        summary["steps"].append(record)
        if failure is not None:
            continue
        if _canceled(job):
            failure = "The job was canceled."
            record["status"] = "CANCELED"
            continue
        if job is not None:
            job.set_progress(f"Step {step.index} of {total}: {step.name}", tasks_done=step.index - 1, total_tasks=total)
        started = time.time()
        attempts = int(step.data.get("retries") or 0) + 1
        status, step_outputs, error = "FAILURE", {}, ""
        try:
            inputs = _step_inputs(step, prompted.get(step.key) or {}, outputs)
        except WorkflowError as err:
            attempts, error = 0, str(err)
        for attempt in range(1, attempts + 1):
            status, step_outputs, error = _attempt(step, server, inputs, job)
            if status != "FAILURE" or attempt == attempts or _canceled(job):
                break
            delay = int(step.data.get("retry_delay") or 0)
            if job is not None:
                job.set_progress(
                    f"{step.label} failed (attempt {attempt} of {attempts}): {error.rstrip('.')}. Retrying in {delay}s."
                )
            time.sleep(delay)
        record["seconds"] = round(time.time() - started)
        record["attempts"] = max(attempts, 1)
        outputs[step.key] = step_outputs
        if status == "FAILURE":
            record["status"] = "FAILED"
            record["error"] = server_tasks.tail(error, 1500)
            if step.data.get("continue_on_error"):
                record["status"] = "FAILED (continued)"
                warnings.append(f"{step.label} failed and the workflow continued: {error}")
                if job is not None:
                    job.set_progress(f"{step.label} failed; continuing on error: {error}")
            else:
                failure = f"{step.label} failed: {error}"
                if job is not None:
                    job.set_progress(failure)
        else:
            record["status"] = status
            if status == "WARNING":
                warnings.append(f"{step.label} ended with warnings.")
            if job is not None:
                names = ", ".join(name for name in step_outputs if name != "stdout")
                job.set_progress(
                    f"{step.label} finished in {record['seconds']}s" + (f"; outputs: {names}" if names else ""),
                    tasks_done=step.index,
                    total_tasks=total,
                )
        _save_outputs(job, outputs, summary)

    if failure is not None:
        summary["status"] = "FAILURE"
        add_server_event("MODIFICATION", server, f"Workflow '{workflow.get('name')}' failed. {failure}", job=job)
    else:
        summary["status"] = "WARNING" if warnings else "SUCCESS"
        add_server_event("MODIFICATION", server, f"Workflow '{workflow.get('name')}' finished.", job=job)
    _save_outputs(job, outputs, summary)
    outputs["workflow"] = summary
    lines = [f"Workflow '{workflow.get('name')}' on {server.hostname}: {summary['status']}"]
    for index, record in enumerate(summary["steps"], start=1):
        lines.append(f"  {index}. {record['name']}: {record['status']} ({record['seconds']}s)")
    errors = failure or "\n".join(warnings)
    return summary["status"], "\n".join(lines), errors, outputs
