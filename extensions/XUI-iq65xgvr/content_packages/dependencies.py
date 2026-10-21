"""
Everything a package export writes.

A package stores only the units an admin picked; their plugins, shared
modules, forms, form functions and management actions travel with them. To
show that closure without guessing it, each member is run through the same
serializer call CloudBolt's own "Push to repository" uses
(export_to_filesystem_as_unzipped_files_flattened, with the kwargs
push_to_remote_source builds) into a throwaway directory, and every
<GLOBAL_ID>_metadata.json the export writes is read back. A parameter or
workflow member has no such export; the units the package export pushes with
it (a parameter's generated-options actions, a workflow's step plugins) are
run through it instead. A blueprint member's export is also read for the
workflows its Run Workflow build items pin, which the package export writes
too, and their step plugins are run through it. Nothing is cloned, committed
or saved; the directory is deleted afterwards.
"""
import json
import os
import re
import tempfile
from collections import OrderedDict

from cbhooks.models import OrchestrationHook
from servicecatalog.models import ServiceBlueprint
from source_code_repos.models import SourceCodeRepo
from utilities.logger import ThreadLogger

from xui.content_packages import services, store, workflows

logger = ThreadLogger(__name__)

METADATA_RE = re.compile(r"^(?P<id>(?P<prefix>[A-Z]{2,4})-[a-z0-9]{8,})_metadata\.json$")

# Type names for every prefix an export can write, including the two that
# only travel as dependencies.
TYPE_TITLES = dict(
    [(t.prefix, t.singular) for t in store.MEMBER_TYPES.values()]
    + [("FRM", "Custom form"), ("FJS", "Form function")]
)


def _export(obj, repo, root):
    """Run the OOB flattened export of obj into root, laid out with repo's
    path_to_* folders, exactly as push_to_remote_source would."""
    if isinstance(obj, ServiceBlueprint):
        from servicecatalog.api.v3.serializers import ServiceBlueprintSerializer

        ServiceBlueprintSerializer().export_to_filesystem_as_unzipped_files_flattened(
            obj,
            destination_path=os.path.join(root, repo.path_to_blueprints, obj.global_id),
            repo_root_path=root,
            source_code_repo=repo,
        )
        return

    from cbhooks.api.v3.serializers import BaseActionSerializer

    if isinstance(obj, OrchestrationHook):
        # The repo operations registry is keyed by the concrete hook type.
        obj = obj.cast()
    info = obj.get_source_repo_operations_details()
    serializer = info.serializer_cls(context=info.serializer_context or None)
    kwargs = {"repo_root_path": root, "source_code_repo": repo}
    if isinstance(serializer, BaseActionSerializer):
        kwargs["is_shared_module"] = info.repo_path_field == "path_to_shared_modules"
    else:
        kwargs["destination_path"] = os.path.join(root, getattr(repo, info.repo_path_field), obj.global_id)
    serializer.export_to_filesystem_as_unzipped_files_flattened(obj, **kwargs)


def _written(root):
    """Every unit the export wrote under root: {global_id: {path, name, ...}}."""
    found = {}
    for dirpath, _dirs, files in os.walk(root):
        for filename in files:
            match = METADATA_RE.match(filename)
            if not match:
                continue
            path = os.path.join(dirpath, filename)
            try:
                with open(path) as stream:
                    metadata = json.load(stream)
            except (OSError, ValueError):
                metadata = {}
            global_id, prefix = match.group("id"), match.group("prefix")
            found[global_id] = {
                "global_id": global_id,
                "prefix": prefix,
                "type_title": TYPE_TITLES.get(prefix, prefix),
                "name": metadata.get("label") or metadata.get("name") or global_id,
                "path": os.path.relpath(dirpath, root).replace(os.sep, "/"),
            }
    return found


def _dir_reader(root):
    """read(path) for repository-relative files under an export root."""
    def read(path):
        try:
            with open(os.path.join(root, *path.split("/"))) as stream:
                return stream.read()
        except OSError:
            return None
    return read


def _pinned(blueprint, repo, root, local_workflows):
    """Export the step plugins of the workflows blueprint pins into root (its
    scratch export) and return (workflows, unknown pins)."""
    found, unknown = [], []
    pins = workflows.blueprint_pins(_dir_reader(root), services.blueprint_metadata_path(repo, blueprint.global_id))
    for value in pins:
        workflow = workflows.find_workflow(value, local_workflows)
        if workflow is None:
            unknown.append(value)
            continue
        found.append(workflow)
        for hook in workflows.step_hooks(workflow):
            _export(hook, repo, root)
    return found, unknown


def closure(package, repo=None):
    """What exporting package writes besides its members' own folders.

    Returns {"dependencies": [...], "errors": [...]} where each dependency
    carries global_id, prefix, type_title, name, repo path, the member names
    that pull it in ("via") and whether it is also a member. repo supplies
    the path_to_* layout; without one CloudBolt's default folders apply."""
    repo = repo.cast() if repo is not None else SourceCodeRepo()
    refs = list(package.get("contents") or [])
    resolved = store.resolve_package(package)
    member_ids = {}
    for ref in refs:
        try:
            member_ids[store.parse_ref(ref)[1]] = ref
        except store.PackageError:
            continue

    dependencies = OrderedDict()
    errors = []
    workflow_type = store.MEMBER_TYPES[store.WORKFLOWS_DIR]
    local_workflows = None
    with tempfile.TemporaryDirectory(prefix="content-package-") as scratch:
        for index, ref in enumerate(refs):
            obj = resolved.get(ref)
            if obj is None:
                continue
            member_type = store.parse_ref(ref)[0]
            member_name = member_type.name_of(obj)
            root = os.path.join(scratch, str(index))
            pinned = []
            try:
                if member_type.synced:
                    _export(obj, repo, root)
                    if member_type.dir == "blueprints":
                        if local_workflows is None:
                            local_workflows = list(workflow_type.resolvable())
                        pinned, unknown = _pinned(obj, repo, root, local_workflows)
                        for value in unknown:
                            errors.append({"ref": ref, "name": member_name, "error": (
                                "pins workflow {}, which is not on this appliance, so the export cannot carry "
                                "it.".format(value)
                            )})
                else:
                    for _unit_type, unit in services.carried_units(member_type, obj):
                        _export(unit, repo, root)
            except Exception as exc:
                logger.exception("Dependency export of %s failed", ref)
                errors.append({"ref": ref, "name": member_name, "error": str(exc) or type(exc).__name__})
                continue
            units = _written(root)
            for workflow in pinned:
                units[workflow.global_id] = {
                    "global_id": workflow.global_id,
                    "prefix": workflow_type.prefix,
                    "type_title": workflow_type.singular,
                    "name": workflow.name,
                    "path": "{}/{}".format(store.WORKFLOWS_DIR, workflow.global_id),
                }
            for global_id, unit in sorted(units.items()):
                if global_id == obj.global_id:
                    continue
                entry = dependencies.setdefault(global_id, dict(unit, via=[], member=global_id in member_ids))
                if member_name not in entry["via"]:
                    entry["via"].append(member_name)

    ordered = sorted(dependencies.values(), key=lambda d: (d["type_title"], d["name"].lower()))
    return {"dependencies": ordered, "errors": errors}
