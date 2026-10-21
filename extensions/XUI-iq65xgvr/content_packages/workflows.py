"""
Runbooks workflows in content packages.

A workflow of the Runbooks content (extensions/XUI-jbm5ac9e, stored by the
runbook_workflows shared module) is not CloudBolt content, so Source Control
Repos cannot push or sync it. A package may list one as
"runbook_workflows/SWF-<id>"; this extension then writes and reads
runbook_workflows/SWF-<id>/SWF-<id>_metadata.json itself, in the form
runbook_workflows.export_workflow returns, plus the repository folders of the
actions its steps run under dependencies.

- Export: each step's action is a plugin that nothing else exports, so it is
  pushed with CloudBolt's own per-object push, like a parameter's
  generated-options actions, unless the package lists it.
- Sync: those plugins sync first, each in its own sourcecodereposync job;
  then a job of this extension runs runbook_workflows.import_workflow, which
  keeps the workflow's SWF- id. A different workflow with the same name is
  never overwritten: that job ends with a warning and the chain goes on.
- Pins: a blueprint member whose Run Workflow build item pins a workflow
  carries that workflow the same way, unless the package lists it.
  blueprint_pins reads the pins from the blueprint's exported metadata and
  custom form, so the Dependencies panel (a scratch export), Export (the
  branch after the blueprint's push) and Sync (the branch) read the same
  files.

The Runbooks content does the work, so it has to be on the appliance; without
it, workflows are neither offered nor exported nor synced.
"""
import importlib.util
import json
import os
import posixpath
import re
from datetime import date

from cbhooks.models import OrchestrationHook, SharedModule
from utilities.logger import ThreadLogger

from xui.content_packages import store

logger = ThreadLogger(__name__)

MODULE_NAME = "runbook_workflows"

# The Run Workflow plugin (plugins/OHK-69gk5ne9). An appliance where CloudBolt
# matched it by name keeps its own global ID, so the name counts too.
RUNNER_HOOK_ID = "OHK-69gk5ne9"
RUNNER_NAME = "Run Workflow"
# A build item default on its workflow input: parameter_defaults names the
# input's parameter, workflow_a<N> (not workflow_inputs_a<N>).
PIN_DEFAULT_RE = re.compile(r"^workflow_a\d+$")
# The custom form question for that input of build item BDI-<item>.
PIN_QUESTION_RE = re.compile(r"^plugin-bdi-(?P<item>[a-z0-9]+)\.workflow$", re.IGNORECASE)
WORKFLOW_ID_RE = re.compile(r"^SWF-[a-z0-9]{8,}$")


def metadata_path(global_id):
    return "{0}/{1}/{1}_metadata.json".format(store.WORKFLOWS_DIR, global_id)


def dumps(data):
    """The file's text, formatted like every CloudBolt export."""
    return json.dumps(data, indent=4, sort_keys=True, default=str) + "\n"


def _load_file():
    """runbook_workflows loaded from its file as it is now, under a private
    name, or None when it has no file. Export and sync run in the job engine,
    which keeps the copy it imported first until it restarts, and a package
    sync may have replaced the file a moment ago; sys.modules is left alone,
    so other running jobs keep theirs."""
    shm = SharedModule.objects.filter(module_name=MODULE_NAME).first()
    path = shm.module_file.path if shm is not None and shm.module_file else ""
    if not path or not os.path.isfile(path):
        return None
    spec = importlib.util.spec_from_file_location("content_packages_" + MODULE_NAME, path)
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return loaded


def module():
    """runbook_workflows as its file holds it now, or PackageError when the
    Runbooks content is missing or too old to carry workflows. Each call
    loads it again, so catch its exceptions with the copy that raised them:
    pass one module through a whole import."""
    try:
        found = _load_file()
    except Exception:
        logger.exception("Could not load the %s shared module from its file", MODULE_NAME)
        found = None
    if found is None:
        found = store.workflows_module()
    if found is None or not hasattr(found, "import_workflow"):
        raise store.PackageError(
            "The Runbooks content on this appliance cannot carry workflows in packages. Sync the Runbooks "
            "package (shared module runbook_workflows) from a branch that has export_workflow and "
            "import_workflow, then try again."
        )
    return found


def step_hooks(workflow, runbooks=None):
    """The actions workflow's steps run, each cast to its own type, in step
    order. A step whose action is gone is skipped here; export_metadata
    refuses it."""
    hooks = []
    for global_id in (runbooks or module()).step_action_ids(workflow.data):
        hook = OrchestrationHook.objects.filter(global_id=global_id).first()
        if hook is not None:
            hooks.append(hook.cast())
    return hooks


def _plugin_ref(hook, repo):
    folder = (getattr(repo, "path_to_plugins", "") or "plugins/").strip("/")
    return posixpath.join(folder, hook.global_id)


def export_metadata(workflow, repo):
    """The metadata file for workflow, as a dict. repo supplies the folder of
    the step plugins."""
    runbooks = module()
    data = runbooks.export_workflow(workflow.data)
    data["dependencies"] = {"hooks": [_plugin_ref(hook, repo) for hook in step_hooks(workflow, runbooks)]}
    data["last_updated"] = date.today().isoformat()
    return data


def readme(workflow, data):
    """The README written the first time a workflow's folder is exported."""
    lines = ["# {}".format(workflow.name), ""]
    description = " ".join((workflow.description or "").split())
    if description:
        lines += [description, ""]
    lines += [
        "Runbooks workflow `{}`. Source Control Repos does not sync workflows: the Content Package Manager UI "
        "extension writes this folder when it exports a package that lists the workflow, and its Sync creates "
        "the workflow with this ID, or updates it. Needs the Runbooks content.".format(workflow.global_id),
        "",
        "Steps:",
        "",
    ]
    for index, step in enumerate(data.get("steps") or [], start=1):
        name = (step.get("name") or step.get("key") or "").replace("]", "\\]")
        lines.append("{}. {} ({})".format(index, name, step.get("action_name") or step.get("action")))
    return "\n".join(lines).rstrip("\n") + "\n"


# ---------------------------------------------------------------------------
# Workflows a blueprint pins
# ---------------------------------------------------------------------------
def _json(read, path):
    try:
        text = read(path)
    except Exception:
        return None
    try:
        return json.loads(text) if text else None
    except ValueError:
        return None


def unit_file(ref):
    """The metadata file of a "<dir>/<ID>" ref."""
    ref = ref.strip("/")
    return "{}/{}_metadata.json".format(ref, posixpath.basename(ref))


def _is_runner(read, hook_ref):
    if posixpath.basename(hook_ref.strip("/")) == RUNNER_HOOK_ID:
        return True
    return (_json(read, unit_file(hook_ref)) or {}).get("name") == RUNNER_NAME


def _form_elements(node):
    """Every SurveyJS element (question or panel) in a form definition."""
    if isinstance(node, dict):
        if isinstance(node.get("name"), str) and "type" in node:
            yield node
        for value in node.values():
            if isinstance(value, (dict, list)):
                yield from _form_elements(value)
    elif isinstance(node, list):
        for value in node:
            yield from _form_elements(value)


def blueprint_pins(read, metadata_path):
    """
    The values (SWF- ids or exact names) a blueprint pins workflows with, read
    from its exported metadata at metadata_path. For each Run Workflow build
    item: a default on its workflow input, and a default on the item's
    plugin-bdi-<item>.workflow question in the blueprint's custom form (a
    custom form replaces the item's defaults, so the pin may live in either).
    read(path) returns a repository file's text, or None.
    """
    data = _json(read, metadata_path)
    if not isinstance(data, dict):
        return []
    pins, items = [], set()
    for item in data.get("deployment_items") or []:
        item = item if isinstance(item, dict) else {}
        hook = (item.get("dependencies") or {}).get("hook")
        if not isinstance(hook, str) or not _is_runner(read, hook):
            continue
        item_id = str(item.get("id") or "")
        if item_id.startswith("BDI-"):
            items.add(item_id[4:].lower())
        for default in item.get("parameter_defaults") or []:
            default = default if isinstance(default, dict) else {}
            if PIN_DEFAULT_RE.match(str(default.get("name") or "")):
                pins.append(str(default.get("value") or "").strip())
    form_ref = (data.get("dependencies") or {}).get("custom_form")
    if items and isinstance(form_ref, str):
        form = _json(read, unit_file(form_ref)) or {}
        try:
            survey = json.loads(form.get("json") or "{}")
        except (TypeError, ValueError):
            survey = {}
        for element in _form_elements(survey):
            match = PIN_QUESTION_RE.match(element["name"])
            if match and match.group("item").lower() in items:
                pins.append(str(element.get("defaultValue") or "").strip())
    return [pin for pin in dict.fromkeys(pins) if pin]


def find_workflow(value, workflows):
    """The workflow among workflows (store.Workflow objects) that a pin names:
    by SWF- id, else by exact name, as Run Workflow resolves it."""
    for workflow in workflows:
        if workflow.global_id == value:
            return workflow
    for workflow in workflows:
        if workflow.name == value:
            return workflow
    return None


def branch_workflow_ids(clone, branch, value):
    """The SWF- ids of the runbook_workflows/ folders on branch that a pin
    names: the folder with that id, else every folder whose workflow has that
    exact name."""
    if WORKFLOW_ID_RE.match(value):
        try:
            clone.git.cat_file("-e", "origin/{}:{}".format(branch, metadata_path(value)))
        except Exception:
            return []
        return [value]
    try:
        listing = clone.git.ls_tree("--name-only", "origin/{}".format(branch), "--", store.WORKFLOWS_DIR + "/")
    except Exception:
        return []
    found = []
    for folder in listing.splitlines():
        global_id = posixpath.basename(folder.strip("/"))
        if not WORKFLOW_ID_RE.match(global_id):
            continue
        try:
            data = json.loads(clone.git.show("origin/{}:{}".format(branch, metadata_path(global_id))))
        except Exception:
            continue
        if isinstance(data, dict) and data.get("name") == value:
            found.append(global_id)
    return found


# ---------------------------------------------------------------------------
# Sync
# ---------------------------------------------------------------------------
def read_metadata(clone, branch, global_id):
    """The workflow's metadata on branch, read from a fetched global clone.
    Raises PackageError when it is missing or unreadable."""
    path = metadata_path(global_id)
    try:
        data = json.loads(clone.git.show("origin/{}:{}".format(branch, path)))
    except Exception:
        raise store.PackageError(
            "Branch {} has no readable {}. Export the package from an appliance that has the workflow "
            "to write it.".format(branch, path)
        )
    if not isinstance(data, dict) or data.get("id") != global_id:
        raise store.PackageError("{} on branch {} does not describe workflow {}.".format(path, branch, global_id))
    return data


def hook_ids(data):
    """Global IDs of the step plugins a workflow's metadata references, in
    order."""
    ids = []
    for ref in (data.get("dependencies") or {}).get("hooks") or []:
        if isinstance(ref, str) and ref.strip("/"):
            global_id = posixpath.basename(ref.strip("/"))
            if global_id.startswith("OHK-") and global_id not in ids:
                ids.append(global_id)
    return ids


def import_metadata(runbooks, data, refresh_if_exists, ignore_enabled, profile=None):
    """Create or update the workflow data describes with runbooks (module()).
    Returns (workflow, outcome, notes) as runbook_workflows.import_workflow
    does; raises runbooks.WorkflowConflict when another workflow here has the
    name."""
    carried = {key: value for key, value in data.items() if key not in ("dependencies", "last_updated")}
    return runbooks.import_workflow(
        carried, profile=profile, refresh_if_exists=refresh_if_exists, ignore_enabled=ignore_enabled
    )
