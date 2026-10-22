"""
Export, sync and import for content packages.

Everything here orchestrates CloudBolt's own Source Control Repos operations
one member at a time rather than reimplementing them:

- Export pushes each member with SourceCodeRepoService.push_blueprint_to_remote_source
  or push_action_to_remote_source, the calls behind the "Push to repository"
  dialogs; each push carries the member's plugins, shared modules and forms
  and re-points their Remote Source URLs. The package manifest is then
  committed to packages/<PKG-id>/ with the repository's own clone, commit and
  push helpers. Each push deletes the member's folders before rewriting them,
  which removes hand-written READMEs; the manifest commit puts back every
  markdown file those pushes deleted, and any file such a README links to,
  as it was on the branch before the export.
- Sync creates one sourcecodereposync job per member, the job the
  syncFromRepo API and "Create from Repository" run, chained with prereq_job
  so members import one at a time and shared dependencies are never created
  twice. A member whose job fails stops the chain; CloudBolt's Rerun on that
  job moves the waiting jobs onto the rerun.
- Import reads packages/*/PKG-*_metadata.json from the repository's global
  clone after a fetch, the technique behind the metadata file picker, and
  registers the manifest locally keyed by its PKG id.

Parameters and Runbooks workflows are the exceptions, because Source Control
Repos has no folder for them (parameters.py, workflows.py): export writes
parameters/<CF-id>/ or runbook_workflows/<SWF-id>/ in the manifest commit
after pushing what each one needs (a parameter's generated-options actions, a
workflow's step plugins), and sync syncs those in their own jobs, then queues
one job of this extension per parameter or workflow (jobs.run_parameter_sync,
jobs.run_workflow_sync) in the same chain. A workflow that a blueprint
member's Run Workflow build item pins travels the same way without being
listed (workflows.blueprint_pins).
"""
import json
import os
import posixpath
import re
from collections import OrderedDict
from tempfile import TemporaryDirectory
from urllib.parse import unquote, urlsplit

from django.conf import settings
from django.core.exceptions import ValidationError
from django.utils.html import strip_tags

from cbhooks.models import OrchestrationHook
from jobs.models import HookParameters, Job, SourceCodeRepoSyncParameters
from servicecatalog.models import ServiceBlueprint
from source_code_repos.models import SourceCodeRepo
from source_code_repos.services import SourceCodeRepoService
from source_code_repos.validators import validate_branch_name
from utilities.logger import ThreadLogger

from xui.content_packages import hooks, parameters, store, workflows

logger = ThreadLogger(__name__)

# Fixed top-level folder for package manifests; SourceCodeRepo has no
# path_to_* field for it.
PACKAGES_DIR = "packages"
MANIFEST_RE = re.compile(r"^packages/(PKG-[a-z0-9]{8,})/(PKG-[a-z0-9]{8,})_metadata\.json$")

# Package member dir -> objects_to_sync key of a sourcecodereposync job
# (source_code_repos.services.OBJECT_TYPE_MAPPINGS plus "blueprints").
SYNC_KEYS = {
    "blueprints": "blueprints",
    "resource_actions": "resource_actions",
    "server_actions": "server_actions",
    "orchestration_actions": "orchestration_actions",
    "flowcontrol_actions": "flow_control_actions",
    "recurring_jobs": "recurring_action_jobs",
    "cit_tests": "cit_tests",
    "webhooks": "inbound_web_hooks",
    "mcp_tool_actions": "mcp_tool_actions",
    "shared_modules": "shared_modules",
    "plugins": "plugins",
    "extensions": "extensions",
}

# Sync order: libraries first, blueprints last (as sync_from_repo does), so
# whatever a blueprint references is already in place when it imports.
# Parameters and workflows follow the actions, so a parameter's
# generated-options actions and a workflow's step plugins exist when they
# are linked.
SYNC_ORDER = {"shared_modules": 0, "plugins": 1, "parameters": 3, "runbook_workflows": 3, "blueprints": 4}
DEFAULT_SYNC_RANK = 2

# Files a push deletes and never writes back, restored after an export: hand
# written markdown, plus whatever such a file links to by relative path.
DOC_SUFFIXES = (".md", ".markdown")
DOC_LINK_RE = re.compile(r"""\]\(\s*<?([^)\s>]+)|\bsrc\s*=\s*["']([^"']+)["']""")

# Where a branch the first push creates is cut from: the remote's default
# branch, as prepare_repo_from_url_and_branch does.
DEFAULT_BRANCH_REFS = ("origin/HEAD", "origin/main", "origin/master")


# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------
def get_repo(value):
    """A SourceCodeRepo by primary key or SCR- global id."""
    repo = None
    if isinstance(value, int) or (isinstance(value, str) and value.isdigit()):
        repo = SourceCodeRepo.objects.filter(id=int(value)).first()
    elif isinstance(value, str) and value:
        repo = SourceCodeRepo.objects.filter(global_id=value).first()
    if repo is None:
        raise store.PackageError("No Source Code Repository {} on this appliance.".format(value or ""))
    return repo


def check_branch(branch):
    """CloudBolt's branch-name rules, plus no slash: CloudBolt reads the branch
    from one segment of a Remote Source URL, so content on a branch with a
    slash cannot be synced or refreshed afterwards."""
    branch = (branch or "").strip()
    if not branch:
        raise store.PackageError("Choose a branch.")
    try:
        validate_branch_name(branch)
    except ValidationError as exc:
        raise store.PackageError(" ".join(exc.messages))
    if "/" in branch:
        raise store.PackageError(
            "Use a branch name without a slash: CloudBolt reads the branch from one part of each "
            "Remote Source URL, so content on {} could not be synced or refreshed later.".format(branch)
        )
    return branch


def _branches(repo):
    """Branch names, fetched; git errors become a readable PackageError."""
    try:
        return repo.get_branches()
    except Exception as exc:
        logger.exception("Could not list the branches of %s", repo.label)
        raise store.PackageError("Could not read {}: {}".format(repo.label, strip_tags(str(exc)) or type(exc).__name__))


def require_branch(repo, branch):
    """The branch must already exist in the repository (fetches first)."""
    if branch not in _branches(repo.cast()):
        raise store.PackageError("{} has no branch {}.".format(repo.label, branch))


def _clone(repo):
    """The repository's global clone, fetched (get_branches fetches first)."""
    repo = repo.cast()
    branches = _branches(repo)
    return repo, repo.get_global_clone(), branches


def branch_clone(repo, branch):
    """(repo, global clone) after a fetch, to read files on branch, which
    must exist."""
    branch = check_branch(branch)
    repo, clone, branches = _clone(repo)
    if branch not in branches:
        raise store.PackageError("{} has no branch {}.".format(repo.label, branch))
    return repo, clone


def _member_object(obj):
    # The repo operations registry is keyed by the concrete hook type.
    return obj.cast() if isinstance(obj, OrchestrationHook) else obj


# ---------------------------------------------------------------------------
# Sync
# ---------------------------------------------------------------------------
def blueprint_metadata_path(repo, global_id):
    """Where a blueprint's metadata sits in repo."""
    folder = (getattr(repo, "path_to_blueprints", "") or "blueprints/").strip("/")
    return "{0}/{1}/{1}_metadata.json".format(folder, global_id)


def branch_reader(clone, branch):
    """read(path) for files on branch of a fetched global clone: the file's
    text, or None when it is not there."""
    def read(path):
        try:
            return clone.git.show("origin/{}:{}".format(branch, path))
        except Exception:
            return None
    return read


def _pinned_refs(repo, clone, branch, blueprint_id, local_workflows):
    """Refs of the runbook_workflows/ folders on branch that blueprint
    blueprint_id pins. A pin with no folder there is left out when this
    appliance already has the workflow; otherwise PackageError, before any
    job exists."""
    found = []
    pins = workflows.blueprint_pins(branch_reader(clone, branch), blueprint_metadata_path(repo, blueprint_id))
    for value in pins:
        ids = workflows.branch_workflow_ids(clone, branch, value)
        if ids:
            found.extend("{}/{}".format(store.WORKFLOWS_DIR, global_id) for global_id in ids)
        elif workflows.find_workflow(value, local_workflows) is None:
            raise store.PackageError(
                "Blueprint {} pins workflow {}, which branch {} does not carry and this appliance does not "
                "have. Export the package again from an appliance that has the workflow.".format(
                    blueprint_id, value, branch
                )
            )
    return found


def _carried_refs(member_type, clone, branch, global_id):
    """Refs of the units a written member needs synced before it, read from
    its metadata on branch: a parameter's generated-options actions, a
    workflow's step plugins."""
    if member_type.dir == store.WORKFLOWS_DIR:
        data = workflows.read_metadata(clone, branch, global_id)
        return ["plugins/{}".format(hook_id) for hook_id in workflows.hook_ids(data)]
    data = parameters.read_metadata(clone, branch, global_id)
    return ["orchestration_actions/{}".format(action_id) for action_id in parameters.action_ids(data)]


def sync_order(refs):
    """Members in the order their sync jobs run."""
    ranked = []
    for index, ref in enumerate(refs):
        member_type = store.parse_ref(ref)[0]
        ranked.append((SYNC_ORDER.get(member_type.dir, DEFAULT_SYNC_RANK), index, ref))
    return [ref for _, _, ref in sorted(ranked)]


def _sync_plan(refs, repo, branch):
    """[(ref, via)] in run order: every member (via None), plus what the
    package does not list but its members need, each with via the ref that
    needs it: the generated-options actions of a parameter member and the
    step plugins of a workflow, with the other actions, then the workflows a
    blueprint member pins, before the members that come after the actions.
    Reads the members' metadata on the branch, so a parameter or workflow that
    was never exported fails here, before any job exists."""
    carried = OrderedDict()  # actions and plugins
    pinned = OrderedDict()  # workflows blueprint members pin
    workflow_type = store.MEMBER_TYPES[store.WORKFLOWS_DIR]
    if any(not store.parse_ref(ref)[0].synced or store.parse_ref(ref)[0].dir == "blueprints" for ref in refs):
        repo, clone = branch_clone(repo, branch)
        local_workflows = None
        for ref in refs:
            member_type, global_id = store.parse_ref(ref)
            if member_type.dir == "blueprints":
                if local_workflows is None:
                    local_workflows = list(workflow_type.resolvable())
                for workflow_ref in _pinned_refs(repo, clone, branch, global_id, local_workflows):
                    if workflow_ref not in refs:
                        pinned.setdefault(workflow_ref, ref)
            elif not member_type.synced:
                for unit_ref in _carried_refs(member_type, clone, branch, global_id):
                    if unit_ref not in refs:
                        carried.setdefault(unit_ref, ref)
        for workflow_ref in pinned:
            for unit_ref in _carried_refs(workflow_type, clone, branch, store.parse_ref(workflow_ref)[1]):
                if unit_ref not in refs:
                    carried.setdefault(unit_ref, workflow_ref)
    else:
        require_branch(repo, branch)
    ordered = sync_order(refs)
    split = next(
        (i for i, ref in enumerate(ordered)
         if SYNC_ORDER.get(store.parse_ref(ref)[0].dir, DEFAULT_SYNC_RANK) > DEFAULT_SYNC_RANK),
        len(ordered),
    )
    return (
        [(ref, None) for ref in ordered[:split]]
        + list(carried.items())
        + list(pinned.items())
        + [(ref, None) for ref in ordered[split:]]
    )


def _repo_sync_job(package, ref, repo, branch, refresh_if_exists, ignore_action_enabled, profile, previous):
    """The OOB sourcecodereposync job for one unit, as the syncFromRepo API
    creates it, behind previous."""
    member_type, global_id = store.parse_ref(ref)
    params = SourceCodeRepoSyncParameters.objects.create(
        source_code_repo=repo,
        branch_or_tag=branch,
        objects_to_sync={SYNC_KEYS[member_type.dir]: ["{0}/{0}_metadata.json".format(global_id)]},
        refresh_if_exists=bool(refresh_if_exists),
        ignore_action_enabled=bool(ignore_action_enabled),
    )
    return Job.objects.create(
        owner=profile,
        job_parameters=params,
        type="sourcecodereposync",
        prereq_job=previous,
        label="Content package {}: {}".format(package["name"], ref)[:256],
    )


def _written_sync_job(package, ref, repo, branch, refresh_if_exists, ignore_action_enabled, profile, previous):
    """The job that creates or updates one parameter or workflow member,
    behind previous: the HookParameters and Job OrchestrationHook.run_as_job
    creates for a plugin, plus the prereq_job link run_as_job cannot set."""
    member_type = store.parse_ref(ref)[0]
    hook = hooks.workflow_hook() if member_type.dir == store.WORKFLOWS_DIR else hooks.parameter_hook()
    params = HookParameters.objects.create(hook=hook)
    params.arguments = {
        "context": {
            "package_id": package["id"],
            "ref": ref,
            "source_code_repo_id": repo.id,
            "branch": branch,
            "refresh_if_exists": bool(refresh_if_exists),
            "ignore_action_enabled": bool(ignore_action_enabled),
        }
    }
    params.save()
    return Job.objects.create(
        owner=profile,
        job_parameters=params,
        type="orchestration_hook",
        prereq_job=previous,
        label="Content package {}: {}".format(package["name"], ref)[:256],
    )


def sync_package(package, repo, branch, refresh_if_exists, ignore_action_enabled, profile):
    """Create one job per member, each waiting for the one before it, and
    make the repository and branch the package's home: the OOB
    sourcecodereposync job for each unit, and for a parameter or workflow the
    extension's own job, preceded by OOB sync jobs for the generated-options
    actions or step plugins it needs. Returns [(ref, Job, via)] in run
    order."""
    branch = check_branch(branch)
    refs = list(package.get("contents") or [])
    if not refs:
        raise store.PackageError("The package has no members to sync.")
    plan = _sync_plan(refs, repo, branch)

    created, previous = [], None
    for ref, via in plan:
        if store.parse_ref(ref)[0].synced:
            job = _repo_sync_job(
                package, ref, repo, branch, refresh_if_exists, ignore_action_enabled, profile, previous
            )
        else:
            job = _written_sync_job(
                package, ref, repo, branch, refresh_if_exists, ignore_action_enabled, profile, previous
            )
        created.append((ref, job, via))
        previous = job
    store.record_sync(package["id"], repo.id, branch, [(ref, job.id, via) for ref, job, via in created])
    return created


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------
MEMBER_TABLE_HEADER = "| Type | Name | Folder |"


def _member_rows(package, resolved):
    """The README's member table: its header, separator, and one row per member."""
    lines = [MEMBER_TABLE_HEADER, "|---|---|---|"]
    for ref in package.get("contents") or []:
        member_type, global_id = store.parse_ref(ref)
        obj = resolved.get(ref)
        name = member_type.name_of(obj) if obj is not None else global_id
        lines.append("| {} | {} | [{}](../../{}/) |".format(
            member_type.singular, name.replace("|", "\\|"), ref, ref
        ))
    return lines


def _readme(package, resolved):
    """The README a package's first export writes."""
    lines = [
        "# {}".format(package["name"]),
        "",
    ]
    if package.get("description"):
        lines += [package["description"], ""]
    lines += [
        "Content package `{}`, version {}. Sync it from the Content Packages admin page "
        "(Content Package Manager UI extension).".format(package["id"], package.get("version") or ""),
        "",
    ]
    lines += _member_rows(package, resolved)
    return "\n".join(lines) + "\n"


def refresh_readme(text, package, resolved):
    """
    An existing package README with its member table rebuilt from the current
    members, so members added after the first export (a parameter, say) are
    listed. The table is the block from the "| Type | Name | Folder |" header
    through the last row after it; every other line, hand-written ones
    included, is kept. A README without the table gets one at the end. The
    file's own line endings are kept.
    """
    newline = "\r\n" if "\r\n" in text else "\n"
    lines = text.splitlines()
    rows = _member_rows(package, resolved)
    start = next((i for i, line in enumerate(lines) if line.strip() == MEMBER_TABLE_HEADER), None)
    if start is None:
        while lines and not lines[-1].strip():
            lines.pop()
        lines += ([""] if lines else []) + rows
    else:
        end = start + 1
        while end < len(lines) and lines[end].lstrip().startswith("|"):
            end += 1
        lines[start:end] = rows
    return newline.join(lines) + newline


def _push_member(service, obj, branch, message):
    obj = _member_object(obj)
    if isinstance(obj, ServiceBlueprint):
        return service.push_blueprint_to_remote_source(obj, branch, message)
    return service.push_action_to_remote_source(obj, branch, message)


def _commit_marker(package):
    """Text every commit of a package export carries in its message."""
    return "Content package {} ({}):".format(package["name"], package["id"])


def _export_base(repo, branch):
    """The commit an export starts from, read before the first push: the
    branch's tip, or the default branch's tip when the first push will create
    the branch. None when the repository cannot be read; READMEs are then not
    restored."""
    try:
        branches = repo.get_branches()  # fetches the global clone
        clone = repo.get_global_clone()
    except Exception:
        logger.exception("Could not read %s before exporting", repo.label)
        return None
    candidates = ["origin/{}".format(branch)] if branch in branches else list(DEFAULT_BRANCH_REFS)
    for ref in candidates:
        try:
            return clone.git.rev_parse("--verify", ref + "^{commit}")
        except Exception:
            continue
    logger.warning("Found none of %s in %s; READMEs will not be restored", ", ".join(candidates), repo.label)
    return None


def _doc_links(text, path):
    """Repository paths that markdown at path links to by relative path."""
    found = set()
    for match in DOC_LINK_RE.finditer(text):
        parts = urlsplit(match.group(1) or match.group(2))
        if parts.scheme or parts.netloc or not parts.path:
            continue
        base = "" if parts.path.startswith("/") else posixpath.dirname(path)
        found.add(posixpath.normpath(posixpath.join(base, unquote(parts.path).lstrip("/"))))
    return found


def restore_docs(clone, base, package):
    """Put back, in clone's working tree, the markdown files this package's
    export commits deleted after base, and the files those link to, as they
    were at base. Files deleted by anyone else's commits stay deleted.
    Returns the restored paths."""
    output = clone.git.log(
        "-z", "--no-renames", "--format=", "--name-only", "--diff-filter=D",
        "--fixed-strings", "--grep", _commit_marker(package), "{}..HEAD".format(base),
    )
    deleted = {p for p in re.split(r"[\0\n]", output) if p}
    gone = {p for p in deleted if not os.path.lexists(os.path.join(clone.working_dir, p))}
    docs = {p for p in gone if p.lower().endswith(DOC_SUFFIXES)}
    linked = set()
    for path in docs:
        linked |= _doc_links(clone.git.show("{}:{}".format(base, path)), path) & gone
    restored = sorted(docs | linked)
    if restored:
        clone.git.checkout(base, "--", *restored)
    return restored


def _push(service, package, member_type, obj, ref, branch, commit_message, report, via=None):
    """Push one unit with CloudBolt's own per-object push and return its
    result row. via is the parameter a generated-options action, or the
    workflow a step plugin, is pushed for."""
    name = member_type.name_of(obj)
    row = {"ref": ref, "type_title": member_type.singular, "prefix": member_type.prefix, "name": name, "via": via}
    # restore_docs finds this export's commits by the marker.
    detail = "{}\n\n{} {} {} ({})".format(
        commit_message, _commit_marker(package), member_type.singular, name, obj.global_id
    )
    if via and via.startswith(store.WORKFLOWS_DIR + "/"):
        detail += ", which a step of {} runs".format(via)
    elif via:
        detail += ", which generates the options of {}".format(via)
    report("Pushing {} {} ({}){}".format(
        member_type.singular, name, obj.global_id, " for {}".format(via) if via else ""
    ))
    try:
        status, message = _push_member(service, obj, branch, detail)
    except Exception as exc:
        logger.exception("Export of %s failed", ref)
        status, message = "failure", str(exc) or type(exc).__name__
    row.update(status="success" if status == "success" else "failure", message=strip_tags(message or ""))
    report("  {}{}".format("pushed" if row["status"] == "success" else "failed: ", "" if row["status"] == "success" else row["message"]))
    return row


def _written_files(member_type, obj, repo, global_id):
    """[(repo path, text, replace)] of a parameter or workflow member's
    folder: its metadata, rewritten on every export, and a README written
    only when the folder has none."""
    if member_type.dir == store.WORKFLOWS_DIR:
        data = workflows.export_metadata(obj, repo)
        path = workflows.metadata_path(global_id)
        text, readme = workflows.dumps(data), workflows.readme(obj, data)
    else:
        data = parameters.export_metadata(obj, repo, global_id)
        path = parameters.metadata_path(global_id)
        text, readme = parameters.dumps(data), parameters.readme(obj, data)
    return [
        (path, text, True),
        (posixpath.join(posixpath.dirname(path), "README.md"), readme, False),
    ]


def carried_units(member_type, obj):
    """[(MemberType, unit)] a written member's export pushes with it: a
    parameter's generated-options actions, a workflow's step plugins."""
    if member_type.dir == store.WORKFLOWS_DIR:
        plugin_type = store.MEMBER_TYPES["plugins"]
        return [(plugin_type, hook) for hook in workflows.step_hooks(obj)]
    action_type = store.MEMBER_TYPES["orchestration_actions"]
    return [(action_type, hpa) for hpa in parameters.gen_options_actions(obj)]


def _export_pinned(service, package, repo, branch, commit_message, report, blueprint_rows, listed, pushed, written):
    """Export the workflows that the pushed blueprints of blueprint_rows pin
    and the package does not list, as workflow members are: push their step
    plugins (unless pushed already or listed, both sets of global IDs), and
    add their folders to written for the manifest commit. The pins are read
    from the branch after the blueprints' pushes, the files a sync reads.
    Returns the result rows; via is the pinning blueprint's ref."""
    workflow_type = store.MEMBER_TYPES[store.WORKFLOWS_DIR]
    try:
        _repo, clone = branch_clone(repo, branch)
    except store.PackageError as exc:
        report("Could not read the pinned workflows from {}: {}".format(branch, exc.public_message))
        return [{"ref": store.WORKFLOWS_DIR, "type_title": workflow_type.singular, "prefix": workflow_type.prefix,
                 "name": "Pinned workflows", "via": None, "status": "failure", "message": exc.public_message}]
    read = branch_reader(clone, branch)
    local_workflows = list(workflow_type.resolvable())
    rows, done = [], set()
    for blueprint_row in blueprint_rows:
        blueprint_id = store.parse_ref(blueprint_row["ref"])[1]
        for value in workflows.blueprint_pins(read, blueprint_metadata_path(repo, blueprint_id)):
            workflow = workflows.find_workflow(value, local_workflows)
            if workflow is None:
                message = "{} pins workflow {}, which is not on this appliance.".format(blueprint_row["name"], value)
                report(message)
                rows.append({
                    "ref": "{}/{}".format(store.WORKFLOWS_DIR, value) if workflows.WORKFLOW_ID_RE.match(value)
                    else store.WORKFLOWS_DIR,
                    "type_title": workflow_type.singular, "prefix": workflow_type.prefix, "name": value,
                    "via": blueprint_row["ref"], "status": "failure", "message": message,
                })
                continue
            if workflow.global_id in listed or workflow.global_id in done:
                continue
            done.add(workflow.global_id)
            ref = store.ref_for(workflow_type, workflow)
            row = {"ref": ref, "type_title": workflow_type.singular, "prefix": workflow_type.prefix,
                   "name": workflow.name, "via": blueprint_row["ref"], "status": "pending", "message": ""}
            rows.append(row)
            report("{} pins workflow {} ({})".format(blueprint_row["name"], workflow.name, workflow.global_id))
            try:
                for unit_type, unit in carried_units(workflow_type, workflow):
                    if unit.global_id in listed or unit.global_id in pushed:
                        continue
                    pushed.add(unit.global_id)
                    rows.append(_push(
                        service, package, unit_type, unit, store.ref_for(unit_type, unit), branch,
                        commit_message, report, via=ref,
                    ))
                written.append((row, _written_files(workflow_type, workflow, repo, workflow.global_id)))
            except Exception as exc:
                logger.exception("Export of pinned workflow %s failed", ref)
                message = exc.public_message if isinstance(exc, store.PackageError) else str(exc)
                row.update(status="failure", message=strip_tags(message or "") or type(exc).__name__)
                report("Could not export pinned workflow {}: {}".format(ref, row["message"]))
            else:
                report("Writing pinned workflow {} ({}) with the manifest".format(workflow.name, ref))
    return rows


def export_package(package, repo, branch, commit_message, profile, progress=None):
    """Push every member with CloudBolt's own per-object push, then commit the
    manifest and the README (written whole the first time, then with only its
    member table rebuilt) to packages/<PKG-id>/, together
    with each parameter and workflow member's folder and the READMEs the
    pushes deleted. A parameter's generated-options actions and a workflow's
    step plugins are pushed with it unless the package lists them, and so are
    the workflows blueprint members pin (with their step plugins). A member
    that fails does not stop the others. progress, when given, is called with
    one line per step (the export job passes job.set_progress). Returns the
    outcome: one result per member, per unit pushed for a parameter or
    workflow, and for the manifest, the restored files, and the PR link."""
    report = progress or (lambda message: None)
    branch = check_branch(branch)
    commit_message = (commit_message or "").strip()
    if not commit_message:
        raise store.PackageError("Enter a commit message.")
    repo = repo.cast()
    service = SourceCodeRepoService(repo, profile)
    refs = list(package.get("contents") or [])
    resolved = store.resolve_package(package)
    listed = {store.parse_ref(ref)[1] for ref in refs}
    base = _export_base(repo, branch)
    restore_note = "" if base else "Could not read the branch before the export, so READMEs were not restored."
    if base:
        report("Starting from commit {} of {}".format(base[:12], branch))
    else:
        report(restore_note)

    results = []
    written = []  # (row, files) of the parameters and workflows, committed with the manifest
    carried_pushed = set()
    for ref in refs:
        member_type, global_id = store.parse_ref(ref)
        obj = resolved.get(ref)
        if obj is None:
            results.append({
                "ref": ref, "type_title": member_type.singular, "prefix": member_type.prefix, "name": global_id,
                "via": None, "status": "skipped", "message": "Not on this appliance, so there is nothing to export.",
            })
            report("Skipped {}: not on this appliance".format(ref))
            continue
        if member_type.synced:
            results.append(_push(service, package, member_type, obj, ref, branch, commit_message, report))
            continue

        row = {"ref": ref, "type_title": member_type.singular, "prefix": member_type.prefix,
               "name": member_type.name_of(obj), "via": None, "status": "pending", "message": ""}
        results.append(row)
        kind = member_type.singular.lower()
        try:
            for unit_type, unit in carried_units(member_type, obj):
                if unit.global_id in listed or unit.global_id in carried_pushed:
                    continue
                carried_pushed.add(unit.global_id)
                results.append(_push(
                    service, package, unit_type, unit, store.ref_for(unit_type, unit), branch,
                    commit_message, report, via=ref,
                ))
            written.append((row, _written_files(member_type, obj, repo, global_id)))
        except Exception as exc:
            logger.exception("Export of %s failed", ref)
            message = exc.public_message if isinstance(exc, store.PackageError) else str(exc)
            row.update(status="failure", message=strip_tags(message or "") or type(exc).__name__)
            report("Could not export {} {}: {}".format(kind, ref, row["message"]))
        else:
            report("Writing {} {} ({}) with the manifest".format(kind, row["name"], ref))

    blueprint_rows = [row for row in results if row["prefix"] == "BP" and row["status"] == "success"]
    if blueprint_rows:
        results.extend(_export_pinned(
            service, package, repo, branch, commit_message, report, blueprint_rows, listed, carried_pushed, written
        ))

    folder = "{}/{}".format(PACKAGES_DIR, package["id"])
    manifest_path = "{}/{}_metadata.json".format(folder, package["id"])
    manifest_row = {"ref": folder, "type_title": "Package manifest", "prefix": "PKG", "name": package["name"],
                    "via": None}
    remote_url = repo.get_repo_url_for_file(branch, manifest_path)
    report("Committing the package manifest to {}".format(manifest_path))
    restored = []
    try:
        with TemporaryDirectory(dir=settings.REPO_DIR) as repo_dir:
            clone = repo.prepare_repo_from_url_and_branch(branch, profile, repo_dir=repo_dir)
            if base:
                try:
                    restored = restore_docs(clone, base, package)
                except Exception as exc:
                    logger.exception("Could not restore the READMEs of the %s export", package["id"])
                    restore_note = "READMEs were not restored: {}".format(strip_tags(str(exc)) or type(exc).__name__)
                    report(restore_note)
                for path in restored:
                    report("  restoring {}".format(path))
            files = [
                (manifest_path, json.dumps(store.manifest(package), indent=4, sort_keys=True) + "\n", True),
            ]
            for _row, parameter_files in written:
                files.extend(parameter_files)
            for path, text, replace in files:
                target = os.path.join(clone.working_dir, *path.split("/"))
                if not replace and os.path.exists(target):
                    continue
                os.makedirs(os.path.dirname(target), exist_ok=True)
                with open(target, "w") as stream:
                    stream.write(text)
            # The package README is written whole the first time. Afterwards only
            # its member table is rebuilt, so new members appear and the text
            # around the table is kept.
            readme = os.path.join(clone.working_dir, folder, "README.md")
            if os.path.exists(readme):
                with open(readme, newline="") as stream:
                    current = stream.read()
                text = refresh_readme(current, package, resolved)
                if text != current:
                    report("Updating the member table in {}/README.md".format(folder))
                    with open(readme, "w", newline="") as stream:
                        stream.write(text)
            else:
                with open(readme, "w") as stream:
                    stream.write(_readme(package, resolved))
            message = "{}\n\n{} manifest".format(commit_message, _commit_marker(package))
            if written:
                message += "\n\nWrites:\n{}".format("\n".join(row["ref"] for row, _ in written))
            if restored:
                message += "\n\nRestores the READMEs the pushes deleted:\n{}".format("\n".join(restored))
            repo.commit_and_push(branch, message, profile, repo_dir=repo_dir)
    except Exception as exc:
        logger.exception("Export of the %s manifest failed", package["id"])
        manifest_row.update(status="failure", message=strip_tags(str(exc)) or type(exc).__name__)
        report("  failed: {}".format(manifest_row["message"]))
        if restored:
            restore_note = "The READMEs the pushes deleted were not restored because the manifest commit failed."
        restored = []
        for row, _files in written:
            row.update(status="failure", message="Not written: the manifest commit failed.")
    else:
        manifest_row.update(status="success", message="Committed {}.".format(manifest_path))
        for row, parameter_files in written:
            row.update(status="success", message="Committed {}.".format(parameter_files[0][0]))
        store.record_export(package["id"], repo.id, branch, remote_url)
    results.append(manifest_row)

    try:
        pr_url = repo.get_pull_request_url(branch)
    except Exception:
        pr_url = ""
    return {
        "repository": repo.label,
        "branch": branch,
        "results": results,
        "failed": sum(1 for r in results if r["status"] == "failure"),
        "pr_url": pr_url,
        "pr_term": getattr(repo, "pull_request_term", "Pull Request"),
        "service_name": getattr(repo, "service_name", ""),
        "manifest_url": remote_url if manifest_row["status"] == "success" else "",
        "restored": restored,
        "restore_note": restore_note,
    }


# ---------------------------------------------------------------------------
# Import from repository
# ---------------------------------------------------------------------------
def list_repo_packages(repo, branch):
    """Manifests under packages/ on a branch: [{ref, path, id, name, ...}]."""
    branch = check_branch(branch)
    repo, clone = branch_clone(repo, branch)
    try:
        raw = clone.git.ls_tree("--name-only", "-r", "origin/{}".format(branch), "--", PACKAGES_DIR + "/")
    except Exception as exc:
        raise store.PackageError("Could not list {}/ on {}: {}".format(PACKAGES_DIR, branch, exc))
    registered = {p["id"]: p for p in store.list_packages()}
    found = []
    for path in raw.splitlines():
        match = MANIFEST_RE.match(path)
        if not match or match.group(1) != match.group(2):
            continue
        try:
            data = json.loads(clone.git.show("origin/{}:{}".format(branch, path)))
        except Exception:
            logger.warning("Skipping unreadable package manifest %s on %s", path, branch)
            continue
        pkg_id = match.group(1)
        local = registered.get(pkg_id)
        found.append({
            "ref": "{}/{}".format(PACKAGES_DIR, pkg_id),
            "path": path,
            "id": pkg_id,
            "name": data.get("name") or pkg_id,
            "description": data.get("description") or "",
            "version": data.get("version") or "",
            "member_count": len(data.get("contents") or []),
            "registered": bool(local),
            "registered_name": local["name"] if local else "",
        })
    return sorted(found, key=lambda p: p["name"].lower())


def import_repo_package(repo, branch, ref):
    """Register (or update, by PKG id) the manifest at packages/<PKG-id> on the
    branch. Returns (package, created)."""
    branch = check_branch(branch)
    match = re.match(r"^packages/(PKG-[a-z0-9]{8,})/?$", ref or "")
    if not match:
        raise store.PackageError('Pick a package folder such as "packages/PKG-abcd1234".')
    pkg_id = match.group(1)
    path = "{}/{}/{}_metadata.json".format(PACKAGES_DIR, pkg_id, pkg_id)
    repo, clone = branch_clone(repo, branch)
    try:
        data = json.loads(clone.git.show("origin/{}:{}".format(branch, path)))
    except Exception:
        raise store.PackageError("{} has no readable {} on branch {}.".format(repo.label, path, branch))
    if data.get("id") != pkg_id:
        raise store.PackageError("{} names package {}, not {}.".format(path, data.get("id"), pkg_id))
    return store.import_manifest(data, repo.id, branch, repo.get_repo_url_for_file(branch, path))
