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
- Files: the File Archives files the steps attach travel in the workflow's
  folder, files/<archive>/<file>, each up to MAX_FILE_MB, listed with their
  SHA-256 under "files" in the metadata. Sync checks each file against its
  checksum and CloudBolt's antivirus scan and stores it in File Archives
  before it creates the workflow, so the workflow runs without an upload.
  A larger file is listed by checksum only and has to be uploaded.
- Pins: a blueprint member whose Run Workflow build item pins a workflow
  carries that workflow the same way, unless the package lists it.
  blueprint_pins reads the pins from the blueprint's exported metadata and
  custom form, so the Dependencies panel (a scratch export), Export (the
  branch after the blueprint's push) and Sync (the branch) read the same
  files.

The Runbooks content does the work, so it has to be on the appliance; without
it, workflows are neither offered nor exported nor synced.
"""
import hashlib
import importlib.util
import json
import os
import posixpath
import re
from collections import OrderedDict
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
# Attached files up to this size travel in the workflow's folder; a larger one
# is listed by checksum only, so binaries do not bloat the repository.
MAX_FILE_MB = 10
FILES_DIR = "files"
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


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
    return export_folder(workflow, repo)[0]


def export_folder(workflow, repo):
    """
    (metadata, [(repo path, bytes)], notes) of workflow's folder: the
    metadata with its files list, the contents of the attached files that
    travel, and what could not be carried. repo supplies the folder of the
    step plugins.
    """
    runbooks = module()
    data = runbooks.export_workflow(workflow.data)
    data["dependencies"] = {"hooks": [_plugin_ref(hook, repo) for hook in step_hooks(workflow, runbooks)]}
    files, contents, notes = export_files(data, posixpath.dirname(metadata_path(workflow.global_id)))
    if files:
        data["files"] = files
    data["last_updated"] = date.today().isoformat()
    return data, contents, notes


# ---------------------------------------------------------------------------
# Attached files
# ---------------------------------------------------------------------------
def _file_archives():
    """The File Archives shared module, or None when that content is not here."""
    try:
        from shared_modules import file_archives
    except ImportError:
        return None
    return file_archives


def entry_ref(entry):
    """The archive/file[@version] part of a step's file entry ("installers/agent.msi@3 extract")."""
    parts = str(entry or "").split()
    return parts[0] if parts else ""


def attached_refs(data):
    """The distinct archive/file[@version] refs the steps of a workflow's data attach, in step order."""
    refs = []
    for step in data.get("steps") or []:
        for entry in (step if isinstance(step, dict) else {}).get("archives") or []:
            ref = entry_ref(entry)
            if ref and ref not in refs:
                refs.append(ref)
    return refs


def sha256_of(content):
    return hashlib.sha256(content).hexdigest()


def export_files(data, folder):
    """
    The files the steps of data (export_workflow's form) attach, as
    (files list for the metadata, [(repo path, bytes)], notes). Each entry
    names the ref as the steps write it, the archive, file, version, SHA-256
    and size of the stored file it resolves to here, and its path in the
    workflow's folder: files/<archive>/<file>, or files/<archive>/<version>/<file>
    when the steps attach two versions of one file; null when the file is
    larger than MAX_FILE_MB.
    """
    refs = attached_refs(data)
    if not refs:
        return [], [], []
    fa = _file_archives()
    if fa is None:
        return [], [], ["File Archives is not installed here, so the files the steps attach were not carried."]
    found, notes = [], []
    for ref in refs:
        try:
            found.append((ref, fa.resolve(ref)))
        except fa.ArchiveError as exc:
            notes.append("{} was not carried: {}".format(ref, exc))
    versions_of = {}
    for _ref, resolved in found:
        versions_of.setdefault((resolved["archive"], resolved["file"]), set()).add(resolved["version"])
    files, contents, written = [], [], set()
    for ref, resolved in found:
        entry = OrderedDict([
            ("archive", resolved["archive"]),
            ("file", resolved["file"]),
            ("path", None),
            ("ref", ref),
            ("sha256", resolved["sha256"]),
            ("size", resolved["size"]),
            ("version", resolved["version"]),
        ])
        files.append(entry)
        if resolved["size"] > MAX_FILE_MB * 1024 * 1024:
            notes.append("{} is larger than {} MB, so it is listed by checksum only; upload it to File Archives "
                         "on the appliance that syncs the package.".format(ref, MAX_FILE_MB))
            continue
        if len(versions_of[(resolved["archive"], resolved["file"])]) > 1:
            path = posixpath.join(FILES_DIR, resolved["archive"], str(resolved["version"]), resolved["file"])
        else:
            path = posixpath.join(FILES_DIR, resolved["archive"], resolved["file"])
        if path not in written:
            with open(resolved["path"], "rb") as stream:
                content = stream.read()
            if sha256_of(content) != resolved["sha256"]:
                notes.append("{} was not carried: its stored copy no longer matches its checksum.".format(ref))
                continue
            contents.append((posixpath.join(folder, path), content))
            written.add(path)
        entry["path"] = path
    return files, contents, notes


def prune_files(working_dir, global_id, keep):
    """Delete the files under the workflow's files/ folder in a checkout that
    keep (repo paths) does not list: files its steps no longer attach."""
    root = os.path.join(working_dir, store.WORKFLOWS_DIR, global_id, FILES_DIR)
    removed = []
    for current, dirs, names in os.walk(root, topdown=False):
        for name in names:
            path = os.path.join(current, name)
            repo_path = os.path.relpath(path, working_dir).replace(os.sep, "/")
            if repo_path not in keep:
                os.remove(path)
                removed.append(repo_path)
        if current != root and not os.listdir(current):
            os.rmdir(current)
    return removed


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


def branch_file_reader(clone, branch):
    """read(path): the exact bytes of a file on branch in a fetched clone, or
    None. Read from the commit, so neither line endings nor a trailing newline
    change."""
    commit = clone.commit("origin/{}".format(branch))

    def read(path):
        try:
            return (commit.tree / path).data_stream.read()
        except KeyError:
            return None

    return read


def _scan(content):
    """Why CloudBolt's antivirus refuses content, or "" when it passes: the
    scan File Archives uploads get. Fails closed."""
    try:
        from utilities.upload_handlers import av_service
    except Exception:
        return "CloudBolt's antivirus service could not be loaded"
    try:
        found = av_service.scan_stream(content)
    except Exception as exc:
        return "CloudBolt's antivirus service could not scan it ({})".format(str(exc) or type(exc).__name__)
    return "CloudBolt's antivirus scan found malicious content in it" if found else ""


def _username(profile):
    user = getattr(profile, "user", None)
    return getattr(user, "username", "") or "content-package-manager"


def _renumbered(data, renumber):
    """data with every step file entry whose ref renumber maps rewritten, options kept."""
    if not renumber:
        return data
    steps = []
    for step in data.get("steps") or []:
        if isinstance(step, dict) and step.get("archives"):
            entries = []
            for entry in step["archives"]:
                ref = entry_ref(entry)
                entries.append(str(entry).replace(ref, renumber[ref], 1) if ref in renumber else entry)
            step = dict(step, archives=entries)
        steps.append(step)
    return dict(data, steps=steps)


def import_files(read, data, refresh_if_exists, profile=None, source=""):
    """
    Store the files data's files list carries in File Archives before the
    workflow is created, so its steps find them. read(path) returns a file's
    bytes on the branch, or None; source names the package and branch for
    each new version's note.

    - An unpinned ref (archive/file or @latest) needs the newest version here
      to be the carried copy. A pinned one (@N) needs any version that is,
      and its step entries are renumbered to that version, because File
      Archives numbers versions per appliance.
    - The copy is stored as a new version only when no suitable version has
      its checksum and, for a file that already exists here, while refreshing
      existing content is on.
    - It must match the checksum in the metadata and pass CloudBolt's
      antivirus scan, or it is not stored.

    Returns (data, with pinned entries renumbered, progress lines, notes on
    the files that were not stored).
    """
    entries = [entry for entry in data.get("files") or [] if isinstance(entry, dict)]
    if not entries:
        return data, [], []
    fa = _file_archives()
    if fa is None:
        return data, [], ["File Archives is not installed here, so the files the steps attach were not stored."]
    folder = posixpath.dirname(metadata_path(data.get("id") or ""))
    who = _username(profile)
    renumber, lines, notes = {}, [], []
    for entry in entries:
        ref = str(entry.get("ref") or "")
        archive, name = str(entry.get("archive") or ""), str(entry.get("file") or "")
        sha256 = str(entry.get("sha256") or "").lower()
        label = "{}/{}".format(archive, name)
        match = fa.REF.match(ref)
        if not match or (match.group("archive"), match.group("file")) != (archive, name) or not SHA256_RE.match(sha256):
            notes.append("Files entry {} is malformed, so it was not stored.".format(ref or label))
            continue
        pinned = match.group("version") not in (None, "latest")
        try:
            history = fa.versions(archive, name)
            newest = history[-1] if history else None
            same = next((meta for meta in reversed(history) if meta.get("sha256") == sha256), None)
            if pinned and same is not None:
                renumber[ref] = "{}@{}".format(label, same["version"])
                lines.append("{} is already here as version {}.".format(label, same["version"]))
                continue
            if not pinned and newest is not None and newest.get("sha256") == sha256:
                lines.append("{} is already here as version {}.".format(label, newest["version"]))
                continue
            if history and not refresh_if_exists:
                notes.append("{} here differs from the package's copy and was left unchanged because refreshing "
                             "existing content was off.".format(label))
                continue
            path = entry.get("path")
            if not path:
                notes.append("{} travels by checksum only (larger than {} MB); upload it to File Archives "
                             "before running the workflow.".format(label, MAX_FILE_MB))
                continue
            content = read(posixpath.join(folder, path))
            if content is None:
                notes.append("{}/{} is missing on the branch, so {} was not stored.".format(folder, path, label))
                continue
            if sha256_of(content) != sha256:
                notes.append("{}/{} does not match the SHA-256 in the metadata, so {} was not stored. Export the "
                             "package again, or fix the files entry.".format(folder, path, label))
                continue
            refused = _scan(content)
            if refused:
                notes.append("{} was not stored: {}.".format(label, refused))
                continue
            if fa.archive_info(archive) is None:
                fa.create_archive(archive, "Created by the Content Package Manager.", who)
            meta = fa.add_version(archive, name, [content], who, note="From {}".format(source) if source else "")
        except fa.ArchiveError as exc:
            notes.append("{} was not stored: {}".format(label, exc))
            continue
        try:
            fa.record(label, "uploaded", "version {} from {}".format(meta["version"], source or "a content package"), who)
        except Exception:
            logger.exception("Could not record the File Archives activity of %s", label)
        if pinned:
            renumber[ref] = "{}@{}".format(label, meta["version"])
        lines.append("Stored {} as version {} in File Archives.".format(label, meta["version"]))
    return _renumbered(data, renumber), lines, notes


def import_metadata(runbooks, data, refresh_if_exists, ignore_enabled, profile=None):
    """Create or update the workflow data describes with runbooks (module()).
    Returns (workflow, outcome, notes) as runbook_workflows.import_workflow
    does; raises runbooks.WorkflowConflict when another workflow here has the
    name."""
    carried = {key: value for key, value in data.items() if key not in ("dependencies", "files", "last_updated")}
    return runbooks.import_workflow(
        carried, profile=profile, refresh_if_exists=refresh_if_exists, ignore_enabled=ignore_enabled
    )
