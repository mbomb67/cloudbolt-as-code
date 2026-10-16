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

Parameters are the exception, because Source Control Repos has no folder for
them (parameters.py): export writes parameters/<CF-id>/ in the manifest commit
after pushing each parameter's generated-options actions, and sync syncs those
actions in their own jobs, then queues one job per parameter
(jobs.run_parameter_sync) in the same chain.
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

from xui.content_packages import hooks, parameters, store

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
# Parameters follow the actions, so their generated-options actions exist
# when they are linked.
SYNC_ORDER = {"shared_modules": 0, "plugins": 1, "parameters": 3, "blueprints": 4}
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
def sync_order(refs):
    """Members in the order their sync jobs run."""
    ranked = []
    for index, ref in enumerate(refs):
        member_type = store.parse_ref(ref)[0]
        ranked.append((SYNC_ORDER.get(member_type.dir, DEFAULT_SYNC_RANK), index, ref))
    return [ref for _, _, ref in sorted(ranked)]


def _sync_plan(refs, repo, branch):
    """[(ref, via)] in run order: every member (via None), plus each
    generated-options action a parameter member's metadata on the branch
    references and the package does not list (via is that parameter's ref),
    run with the other actions. Reads the parameters' metadata, so a
    parameter that was never exported fails here, before any job exists."""
    extra = OrderedDict()
    if any(not store.parse_ref(ref)[0].synced for ref in refs):
        repo, clone = branch_clone(repo, branch)
        for ref in refs:
            member_type, global_id = store.parse_ref(ref)
            if member_type.synced:
                continue
            for action_id in parameters.action_ids(parameters.read_metadata(clone, branch, global_id)):
                action_ref = "orchestration_actions/{}".format(action_id)
                if action_ref not in refs:
                    extra.setdefault(action_ref, ref)
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
        + list(extra.items())
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


def _parameter_sync_job(package, ref, repo, branch, refresh_if_exists, profile, previous):
    """The job that creates or updates one parameter member, behind previous:
    the HookParameters and Job OrchestrationHook.run_as_job creates for a
    plugin, plus the prereq_job link run_as_job cannot set."""
    params = HookParameters.objects.create(hook=hooks.parameter_hook())
    params.arguments = {
        "context": {
            "package_id": package["id"],
            "ref": ref,
            "source_code_repo_id": repo.id,
            "branch": branch,
            "refresh_if_exists": bool(refresh_if_exists),
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
    sourcecodereposync job for each unit, and for a parameter the extension's
    parameter job, preceded by OOB sync jobs for the generated-options
    actions it needs. Returns [(ref, Job, via)] in run order."""
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
            job = _parameter_sync_job(package, ref, repo, branch, refresh_if_exists, profile, previous)
        created.append((ref, job, via))
        previous = job
    store.record_sync(package["id"], repo.id, branch, [(ref, job.id, via) for ref, job, via in created])
    return created


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------
def _readme(package, resolved):
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
        "| Type | Name | Folder |",
        "|---|---|---|",
    ]
    for ref in package.get("contents") or []:
        member_type, global_id = store.parse_ref(ref)
        obj = resolved.get(ref)
        name = member_type.name_of(obj) if obj is not None else global_id
        lines.append("| {} | {} | [{}](../../{}/) |".format(
            member_type.singular, name.replace("|", "\\|"), ref, ref
        ))
    return "\n".join(lines) + "\n"


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
    result row. via is the parameter a generated-options action is pushed
    for."""
    name = member_type.name_of(obj)
    row = {"ref": ref, "type_title": member_type.singular, "prefix": member_type.prefix, "name": name, "via": via}
    # restore_docs finds this export's commits by the marker.
    detail = "{}\n\n{} {} {} ({})".format(
        commit_message, _commit_marker(package), member_type.singular, name, obj.global_id
    )
    if via:
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


def _parameter_files(cf, repo, global_id):
    """[(repo path, text, replace)] of a parameter member's folder: its
    metadata, rewritten on every export, and a README written only when the
    folder has none."""
    data = parameters.export_metadata(cf, repo, global_id)
    path = parameters.metadata_path(global_id)
    return [
        (path, parameters.dumps(data), True),
        (posixpath.join(posixpath.dirname(path), "README.md"), parameters.readme(cf, data), False),
    ]


def export_package(package, repo, branch, commit_message, profile, progress=None):
    """Push every member with CloudBolt's own per-object push, then commit the
    manifest (and a README the first time) to packages/<PKG-id>/, together
    with each parameter member's folder and the READMEs the pushes deleted.
    A parameter's generated-options actions are pushed with it unless the
    package lists them. A member that fails does not stop the others.
    progress, when given, is called with one line per step (the export job
    passes job.set_progress). Returns the outcome: one result per member, per
    action pushed for a parameter, and for the manifest, the restored files,
    and the PR link."""
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
    written = []  # (row, files) of the parameters, committed with the manifest
    actions_pushed = set()
    action_type = store.MEMBER_TYPES["orchestration_actions"]
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
        for action in parameters.gen_options_actions(obj):
            if action.global_id in listed or action.global_id in actions_pushed:
                continue
            actions_pushed.add(action.global_id)
            results.append(_push(
                service, package, action_type, action, store.ref_for(action_type, action), branch,
                commit_message, report, via=ref,
            ))
        try:
            written.append((row, _parameter_files(obj, repo, global_id)))
        except Exception as exc:
            logger.exception("Export of parameter %s failed", ref)
            row.update(status="failure", message=strip_tags(str(exc)) or type(exc).__name__)
            report("Could not export parameter {}: {}".format(ref, row["message"]))
        else:
            report("Writing parameter {} ({}) with the manifest".format(row["name"], ref))

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
                ("{}/README.md".format(folder), _readme(package, resolved), False),
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
            message = "{}\n\n{} manifest".format(commit_message, _commit_marker(package))
            if written:
                message += "\n\nWrites the parameters:\n{}".format("\n".join(row["ref"] for row, _ in written))
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
