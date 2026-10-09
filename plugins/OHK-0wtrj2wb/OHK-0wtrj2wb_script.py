"""
Sync From Source Control Repo (MCP Tool Action plugin).

Creates the same sourcecodereposync job that the REST endpoint
POST /api/v3/cmp/sourceCodeRepos/<id>/syncFromRepo/ creates, so an agent that
has pushed content to a branch can import or refresh it through MCP alone.
The job clones the branch once and runs CloudBolt's own serializers per
object (source_code_repos/services.py), importing each object's dependencies
from the same checkout.

Every declared input is read from its rendered template token in
_read_inputs(); keep each one referenced that way or CloudBolt's token scan
deletes the input when the script is saved. Output keys are camelCase so a
synchronous run and an asynchronous fetch_job read identically.
"""
import html
import re

from jobs.models import Job, SourceCodeRepoSyncParameters
from source_code_repos.models import SourceCodeRepo
from utilities.logger import ThreadLogger

logger = ThreadLogger(__name__)

# SourceCodeRepo.path_to_* field -> key of the sync job's objects_to_sync dict
# (source_code_repos.services.OBJECT_TYPE_MAPPINGS plus "blueprints").
TYPE_KEYS = {
    "path_to_blueprints": "blueprints",
    "path_to_plugins": "plugins",
    "path_to_shared_modules": "shared_modules",
    "path_to_resource_actions": "resource_actions",
    "path_to_server_actions": "server_actions",
    "path_to_orchestration_actions": "orchestration_actions",
    "path_to_flowcontrol_actions": "flow_control_actions",
    "path_to_recurring_jobs": "recurring_action_jobs",
    "path_to_webhooks": "inbound_web_hooks",
    "path_to_cit_tests": "cit_tests",
    "path_to_extensions": "extensions",
    "path_to_mcp_tool_actions": "mcp_tool_actions",
}
# Exported as top-level folders but importable only through a parent.
TRANSITIVE_ONLY = {
    "path_to_custom_forms": "Forms",
    "path_to_form_functions": "Form functions",
}
GLOBAL_ID_RE = re.compile(r"^[A-Z]{2,4}-[A-Za-z0-9]{6,16}$")


def _text(value):
    return html.unescape(value or "").strip()


def _bool(value, default):
    text = _text(value).lower()
    if not text:
        return default
    return text in ("true", "1", "yes", "y", "on")


def _split(value):
    return [part for part in re.split(r"[\s,]+", _text(value)) if part]


def _failure(message):
    return {
        "status": "FAILURE",
        "output_message": "",
        "error_message": message,
        "outputs": {"error": message},
    }


def _read_inputs():
    raw = {
        "sync_branch": """{{ sync_branch }}""",
        "sync_paths": """{{ sync_paths }}""",
        "sync_repo": """{{ sync_repo }}""",
        "sync_refresh_if_exists": """{{ sync_refresh_if_exists }}""",
        "sync_ignore_action_enabled": """{{ sync_ignore_action_enabled }}""",
    }
    return {
        "branch": _text(raw["sync_branch"]),
        "paths": _split(raw["sync_paths"]),
        "repo": _text(raw["sync_repo"]),
        "refresh_if_exists": _bool(raw["sync_refresh_if_exists"], True),
        "ignore_action_enabled": _bool(raw["sync_ignore_action_enabled"], False),
    }


def _repo_list(repos):
    return ", ".join("{} ({})".format(r.global_id, r.label) for r in repos) or "none"


def _resolve_repo(ref):
    repos = SourceCodeRepo.objects.all().order_by("id")
    if ref:
        repo = repos.filter(global_id=ref).first() or repos.filter(label__iexact=ref).first()
        if repo is None:
            return None, "No Source Control Repository matches '{}'. Available: {}.".format(
                ref, _repo_list(repos)
            )
        return repo, None
    if repos.count() == 1:
        return repos.first(), None
    return None, "sync_repo is required because {} repositories exist: {}.".format(
        repos.count(), _repo_list(repos)
    )


def _prefixes(repo):
    """[(normalized directory prefix, field name)], longest prefix first."""
    rows = []
    for field in list(TYPE_KEYS) + list(TRANSITIVE_ONLY):
        value = (getattr(repo, field, "") or "").strip().strip("/")
        if value:
            rows.append((value + "/", field))
    rows.sort(key=lambda row: -len(row[0]))
    return rows


def _classify(path, prefixes):
    """Return (objects_to_sync key, '<GID>/<GID>_metadata.json') or (None, reason)."""
    norm = re.sub(r"^(\./|/)+", "", path.replace("\\", "/").strip())
    for prefix, field in prefixes:
        if not norm.startswith(prefix):
            continue
        rest = norm[len(prefix):].split("/")
        gid = rest[0] if rest else ""
        if not GLOBAL_ID_RE.match(gid):
            return None, "'{}' has no <GLOBAL_ID> folder after '{}'.".format(path, prefix)
        if field in TRANSITIVE_ONLY:
            return None, (
                "{} import only with their parent; sync the blueprint or action "
                "that references {} instead.".format(TRANSITIVE_ONLY[field], gid)
            )
        return TYPE_KEYS[field], "{}/{}_metadata.json".format(gid, gid)
    return None, "'{}' is not under a content directory of the repository ({}).".format(
        path, ", ".join(prefix for prefix, _ in prefixes)
    )


def run(job, *args, **kwargs):
    profile = kwargs.get("profile")
    if profile is None and job is not None:
        profile = job.owner
    if profile is None:
        return _failure(
            "No calling user profile was supplied. Run this tool through the MCP "
            "server or the API as an authenticated user."
        )
    if not profile.is_cbadmin:
        return _failure("Only CloudBolt admins may sync content from a repository.")

    inputs = _read_inputs()
    if not inputs["branch"]:
        return _failure("sync_branch is required.")
    if "/" in inputs["branch"]:
        return _failure(
            "Branch '{}' contains a slash. CloudBolt reads the branch from one URL "
            "path segment, so push the same commit under a name without '/' "
            "(for example feature-x instead of claude/feature-x) and retry.".format(
                inputs["branch"]
            )
        )
    if not inputs["paths"]:
        return _failure(
            "sync_paths is required: one repo-relative content path per line, "
            "such as blueprints/BP-abc12345."
        )

    repo, error = _resolve_repo(inputs["repo"])
    if error:
        return _failure(error)

    prefixes = _prefixes(repo)
    objects_to_sync = {}
    skipped = []
    seen = set()
    for path in inputs["paths"]:
        type_key, result = _classify(path, prefixes)
        if type_key is None:
            skipped.append({"path": path, "reason": result})
            continue
        if (type_key, result) in seen:
            continue
        seen.add((type_key, result))
        objects_to_sync.setdefault(type_key, []).append(result)

    if not objects_to_sync:
        return _failure("Nothing to sync. " + " ".join(row["reason"] for row in skipped))

    params = SourceCodeRepoSyncParameters.objects.create(
        source_code_repo=repo,
        branch_or_tag=inputs["branch"],
        objects_to_sync=objects_to_sync,
        refresh_if_exists=inputs["refresh_if_exists"],
        ignore_action_enabled=inputs["ignore_action_enabled"],
    )
    sync_job = Job.objects.create(
        owner=profile, job_parameters=params, type="sourcecodereposync"
    )
    count = sum(len(paths) for paths in objects_to_sync.values())
    message = "Created sync job {} for {} object(s) from branch '{}' of {}.".format(
        sync_job.global_id, count, inputs["branch"], repo.label
    )
    logger.info(message)
    outputs = {
        "syncJobId": sync_job.global_id,
        "repoId": repo.global_id,
        "repoLabel": repo.label,
        "branch": inputs["branch"],
        "refreshIfExists": inputs["refresh_if_exists"],
        "objectsToSync": objects_to_sync,
        "skipped": skipped,
        "next": (
            "Poll fetch_job_log with syncJobId until the status is SUCCESS or "
            "FAILURE, then read syncResults for each object's status and message."
        ),
    }
    return {
        "status": "SUCCESS",
        "output_message": message,
        "error_message": "",
        "outputs": outputs,
    }
