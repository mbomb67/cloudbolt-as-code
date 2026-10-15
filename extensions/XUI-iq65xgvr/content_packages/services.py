"""
Export, sync and import for content packages.

Everything here orchestrates CloudBolt's own Source Control Repos operations
one member at a time rather than reimplementing them:

- Export pushes each member with SourceCodeRepoService.push_blueprint_to_remote_source
  or push_action_to_remote_source, the calls behind the "Push to repository"
  dialogs; each push carries the member's plugins, shared modules and forms
  and re-points their Remote Source URLs. The package manifest is then
  committed to packages/<PKG-id>/ with the repository's own clone, commit and
  push helpers.
- Sync creates one sourcecodereposync job per member, the job the
  syncFromRepo API and "Create from Repository" run, chained with prereq_job
  so members import one at a time and shared dependencies are never created
  twice. A member whose job fails stops the chain; CloudBolt's Rerun on that
  job moves the waiting jobs onto the rerun.
- Import reads packages/*/PKG-*_metadata.json from the repository's global
  clone after a fetch, the technique behind the metadata file picker, and
  registers the manifest locally keyed by its PKG id.
"""
import json
import os
import re
from tempfile import TemporaryDirectory

from django.conf import settings
from django.core.exceptions import ValidationError
from django.utils.html import strip_tags

from cbhooks.models import OrchestrationHook
from jobs.models import Job, SourceCodeRepoSyncParameters
from servicecatalog.models import ServiceBlueprint
from source_code_repos.models import SourceCodeRepo
from source_code_repos.services import SourceCodeRepoService
from source_code_repos.validators import validate_branch_name
from utilities.logger import ThreadLogger

from xui.content_packages import store

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
SYNC_ORDER = {"shared_modules": 0, "plugins": 1, "blueprints": 3}


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
        ranked.append((SYNC_ORDER.get(member_type.dir, 2), index, ref))
    return [ref for _, _, ref in sorted(ranked)]


def sync_package(package, repo, branch, refresh_if_exists, ignore_action_enabled, profile):
    """Create one OOB sourcecodereposync job per member, each waiting for the
    one before it, and make the repository and branch the package's home.
    Returns [(ref, Job)] in run order."""
    branch = check_branch(branch)
    refs = list(package.get("contents") or [])
    if not refs:
        raise store.PackageError("The package has no members to sync.")
    require_branch(repo, branch)

    created, previous = [], None
    for ref in sync_order(refs):
        member_type, global_id = store.parse_ref(ref)
        params = SourceCodeRepoSyncParameters.objects.create(
            source_code_repo=repo,
            branch_or_tag=branch,
            objects_to_sync={SYNC_KEYS[member_type.dir]: ["{0}/{0}_metadata.json".format(global_id)]},
            refresh_if_exists=bool(refresh_if_exists),
            ignore_action_enabled=bool(ignore_action_enabled),
        )
        job = Job.objects.create(
            owner=profile,
            job_parameters=params,
            type="sourcecodereposync",
            prereq_job=previous,
            label="Content package {}: {}".format(package["name"], ref)[:256],
        )
        created.append((ref, job))
        previous = job
    store.record_sync(package["id"], repo.id, branch, [(ref, job.id) for ref, job in created])
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


def export_package(package, repo, branch, commit_message, profile):
    """Push every member with CloudBolt's own per-object push, then commit the
    manifest (and a README the first time) to packages/<PKG-id>/. A member
    that fails does not stop the others. Returns the outcome for display."""
    branch = check_branch(branch)
    commit_message = (commit_message or "").strip()
    if not commit_message:
        raise store.PackageError("Enter a commit message.")
    repo = repo.cast()
    service = SourceCodeRepoService(repo, profile)
    refs = list(package.get("contents") or [])
    resolved = store.resolve_refs(refs)

    results = []
    for ref in refs:
        member_type, global_id = store.parse_ref(ref)
        obj = resolved.get(ref)
        row = {"ref": ref, "type_title": member_type.singular, "prefix": member_type.prefix, "name": global_id}
        if obj is None:
            row.update(status="skipped", message="Not on this appliance, so there is nothing to push.")
            results.append(row)
            continue
        row["name"] = member_type.name_of(obj)
        detail = "{}\n\nContent package {} ({}): {} {} ({})".format(
            commit_message, package["name"], package["id"], member_type.singular, row["name"], global_id
        )
        try:
            status, message = _push_member(service, obj, branch, detail)
        except Exception as exc:
            logger.exception("Export of %s failed", ref)
            status, message = "failure", str(exc) or type(exc).__name__
        row.update(status="success" if status == "success" else "failure", message=strip_tags(message or ""))
        results.append(row)

    folder = "{}/{}".format(PACKAGES_DIR, package["id"])
    manifest_path = "{}/{}_metadata.json".format(folder, package["id"])
    manifest_row = {"ref": folder, "type_title": "Package manifest", "prefix": "PKG", "name": package["name"]}
    remote_url = repo.get_repo_url_for_file(branch, manifest_path)
    try:
        with TemporaryDirectory(dir=settings.REPO_DIR) as repo_dir:
            clone = repo.prepare_repo_from_url_and_branch(branch, profile, repo_dir=repo_dir)
            target = os.path.join(clone.working_dir, PACKAGES_DIR, package["id"])
            os.makedirs(target, exist_ok=True)
            with open(os.path.join(target, "{}_metadata.json".format(package["id"])), "w") as stream:
                stream.write(json.dumps(store.manifest(package), indent=4, sort_keys=True) + "\n")
            readme = os.path.join(target, "README.md")
            if not os.path.exists(readme):
                with open(readme, "w") as stream:
                    stream.write(_readme(package, resolved))
            repo.commit_and_push(
                branch,
                "{}\n\nContent package {} ({}): manifest".format(commit_message, package["name"], package["id"]),
                profile,
                repo_dir=repo_dir,
            )
    except Exception as exc:
        logger.exception("Export of the %s manifest failed", package["id"])
        manifest_row.update(status="failure", message=strip_tags(str(exc)) or type(exc).__name__)
    else:
        manifest_row.update(status="success", message="Committed {}.".format(manifest_path))
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
    }


# ---------------------------------------------------------------------------
# Import from repository
# ---------------------------------------------------------------------------
def _clone(repo):
    """The repository's global clone, fetched (get_branches fetches first)."""
    repo = repo.cast()
    branches = _branches(repo)
    return repo, repo.get_global_clone(), branches


def list_repo_packages(repo, branch):
    """Manifests under packages/ on a branch: [{ref, path, id, name, ...}]."""
    branch = check_branch(branch)
    repo, clone, branches = _clone(repo)
    if branch not in branches:
        raise store.PackageError("{} has no branch {}.".format(repo.label, branch))
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
    repo, clone, branches = _clone(repo)
    if branch not in branches:
        raise store.PackageError("{} has no branch {}.".format(repo.label, branch))
    try:
        data = json.loads(clone.git.show("origin/{}:{}".format(branch, path)))
    except Exception:
        raise store.PackageError("{} has no readable {} on branch {}.".format(repo.label, path, branch))
    if data.get("id") != pkg_id:
        raise store.PackageError("{} names package {}, not {}.".format(path, data.get("id"), pkg_id))
    return store.import_manifest(data, repo.id, branch, repo.get_repo_url_for_file(branch, path))
