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

The Runbooks content does the work, so it has to be on the appliance; without
it, workflows are neither offered nor exported nor synced.
"""
import importlib.util
import json
import os
import posixpath
from datetime import date

from cbhooks.models import OrchestrationHook, SharedModule
from utilities.logger import ThreadLogger

from xui.content_packages import store

logger = ThreadLogger(__name__)

MODULE_NAME = "runbook_workflows"


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
