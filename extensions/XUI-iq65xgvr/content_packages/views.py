"""
Content Package Manager XUI

An admin page (Admin > Custom Extensions > Content Packages) for defining
content packages: named sets of blueprints, actions, plugins, shared modules
and UI extensions that belong together. Definitions live in store.py; export,
sync and import run through services.py. This module renders the list page, a
details page per package, the Add/Edit, Delete, Export, Sync and Import
dialogs, and the JSON API under xui/content_packages/api/.

Members are resolved against the appliance on every render. A member that no
longer exists shows red; one whose Remote Source URL is not on the package's
home repository and branch shows amber. The details page also lists every
dependency an export would write, worked out by dependencies.py.

Export runs inline in the request, like CloudBolt's own "Push to repository"
dialog; Sync creates CloudBolt sync jobs and returns at once. Everything here
requires CB Admin.
"""
import functools
import json
from urllib.parse import urlencode

from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.http import Http404, HttpResponseRedirect, JsonResponse
from django.shortcuts import render
from django.urls import reverse
from django.utils.dateparse import parse_datetime
from django.utils.html import format_html

from extensions.views import admin_extension
from jobs.models import Job
from source_code_repos.models import SourceCodeRepo
from utilities.decorators import dialog_view
from utilities.logger import ThreadLogger
from utilities.permissions import cbadmin_required

from xui.content_packages import dependencies, services, store

logger = ThreadLogger(__name__)

TEMPLATES = "content_packages/templates/"

# Folder name of this XUI and the admin view's function name; together they
# form the list page URL /extensions/admin/<package>/<view>.
XUI_PACKAGE = "content_packages"
PAGE_VIEW = "packages"

# Members listed in a Contents cell before "+N more".
CONTENTS_SHOWN = 5

# Prefixes a Remote Source URL can carry between the repository URL and the
# branch: GitHub blob, GitLab blob/raw, and the bare form CloudBolt accepts.
BRANCH_SEPARATORS = ("/blob/", "/-/blob/", "/-/raw/", "/raw/", "/")

# Job status -> (pill modifier from _assets.html, text).
JOB_PILLS = {
    "SUCCESS": ("ok", "Succeeded"),
    "WARNING": ("warn", "Succeeded with warnings"),
    "FAILURE": ("bad", "Failed"),
    "CANCELED": ("", "Canceled"),
    "TO_CANCEL": ("", "Canceling"),
}
ACTIVE_JOB_STATUSES = ("INIT", "PENDING", "QUEUED", "RUNNING", "PAUSED", "STEP", "NEXT", "RETURN")


def _page_url():
    return reverse("render_admin_extension", kwargs={"package": XUI_PACKAGE, "view_id": PAGE_VIEW})


def _get_or_404(pkg_id):
    try:
        return store.get_package(pkg_id)
    except store.PackageNotFound as exc:
        raise Http404(str(exc))


def _home_repo(package):
    repo_id = package["local"].get("source_code_repo_id")
    if not isinstance(repo_id, int):
        return None
    return SourceCodeRepo.objects.filter(id=repo_id).first()


# ---------------------------------------------------------------------------
# Resolving packages for display and the API
# ---------------------------------------------------------------------------
def _on_branch(url, repo, branch):
    """True when a Remote Source URL points into repo on branch."""
    if not url or not branch:
        return False
    base = (repo.repo_url or "").rstrip("/")
    if base.endswith(".git"):
        base = base[:-4]
    return any(url.startswith("{}{}{}/".format(base, sep, branch)) for sep in BRANCH_SEPARATORS)


def _member(ref, obj, repo, branch):
    """One package member as a JSON-safe dict with its display state:
    "ok", "missing" (red) or "drift" (amber)."""
    try:
        member_type, global_id = store.parse_ref(ref)
    except store.PackageError as exc:
        return {
            "ref": ref, "type": "", "type_title": "Unknown", "prefix": "?", "global_id": "",
            "name": str(ref), "exists": False, "drift": False, "state": "missing", "pill": "bad",
            "note": str(exc), "url": None, "remote_source_url": "",
        }
    info = {
        "ref": ref,
        "type": member_type.dir,
        "type_title": member_type.singular,
        "prefix": member_type.prefix,
        "global_id": global_id,
        "name": global_id,
        "exists": obj is not None,
        "drift": False,
        "state": "ok",
        "pill": "",
        "note": "{} {}".format(member_type.singular, global_id),
        "url": None,
        "remote_source_url": "",
    }
    if obj is None:
        info.update(state="missing", pill="bad", note="No {} with global ID {} exists on this appliance.".format(
            member_type.singular.lower(), global_id
        ))
        return info

    info["name"] = member_type.name_of(obj)
    try:
        info["url"] = obj.get_absolute_url()
    except Exception:
        info["url"] = None
    remote_url = getattr(obj, "remote_source_url", "") or ""
    info["remote_source_url"] = remote_url
    if repo is not None and not _on_branch(remote_url, repo, branch):
        info.update(drift=True, state="drift", pill="warn")
        if remote_url:
            info["note"] = "Linked to {}, not to {} branch {}.".format(remote_url, repo.label, branch)
        else:
            info["note"] = "Not linked to a repository, so it is not on {} branch {} yet.".format(repo.label, branch)
    return info


def _describe(packages):
    """Packages with resolved members and home repository, JSON-safe."""
    resolved = store.resolve_refs([ref for p in packages for ref in p.get("contents") or []])
    repo_ids = set()
    for package in packages:
        repo_id = package["local"].get("source_code_repo_id")
        if isinstance(repo_id, int):
            repo_ids.add(repo_id)
    repos = SourceCodeRepo.objects.in_bulk(list(repo_ids)) if repo_ids else {}

    described = []
    for package in packages:
        local = package["local"]
        repo_id = local.get("source_code_repo_id")
        repo = repos.get(repo_id)
        branch = local.get("branch") or ""
        item = dict(package)
        item["members"] = [
            _member(ref, resolved.get(ref), repo, branch) for ref in package.get("contents") or []
        ]
        item["repository"] = (
            {"id": repo.id, "label": repo.label, "url": repo.repo_url, "branch": branch} if repo else None
        )
        item["repository_missing"] = bool(repo_id) and repo is None
        described.append(item)
    return described


def _when(value):
    return parse_datetime(value) if isinstance(value, str) and value else None


def _jobs_for(items):
    """Every last-sync job the items reference, by pk, in one query."""
    ids = [j.get("job_id") for item in items for j in item["local"].get("last_sync_jobs") or []]
    ids = [i for i in ids if isinstance(i, int)]
    return Job.objects.in_bulk(ids) if ids else {}


def _sync_jobs(local, jobs):
    """The last sync's jobs in run order with a display status. A pending job
    behind one that failed is shown as blocked."""
    rows, blocked = [], False
    for entry in local.get("last_sync_jobs") or []:
        job = jobs.get(entry.get("job_id"))
        # A failed job that was rerun hands its place in the chain to the rerun.
        while job is not None and job.rerun_job_id:
            job = job.rerun_job
        row = {"ref": entry.get("ref"), "job_id": entry.get("job_id"), "url": None, "status": "MISSING",
               "pill": "", "text": "Job deleted"}
        if job is not None:
            row.update(url=job.get_absolute_url(), status=job.status)
            if job.status in JOB_PILLS:
                row["pill"], row["text"] = JOB_PILLS[job.status]
            elif job.status in ("PENDING", "QUEUED", "INIT") and blocked:
                row["pill"], row["text"] = "bad", "Blocked by a failed job"
            elif job.status in ("PENDING", "QUEUED", "INIT"):
                row["pill"], row["text"] = "live", "Waiting"
            else:
                row["pill"], row["text"] = "live", "Running"
            if job.status == "FAILURE":
                blocked = True
        rows.append(row)
    return rows


def _row(item, jobs=None):
    """Add what the templates need on top of the JSON shape."""
    local = item["local"]
    job_id = local.get("last_sync_job_id")
    item["exported_at"] = _when(local.get("last_exported"))
    item["synced_at"] = _when(local.get("last_synced"))
    item["sync_job_url"] = reverse("job_detail", args=[str(job_id)]) if job_id else None
    item["hidden_count"] = max(len(item["members"]) - CONTENTS_SHOWN, 0)
    item["problems"] = sum(1 for m in item["members"] if m["state"] != "ok")
    item["sync_jobs"] = _sync_jobs(local, jobs or {})
    members = {m["ref"]: m for m in item["members"]}
    for job in item["sync_jobs"]:
        member = members.get(job["ref"]) or {}
        job["name"] = member.get("name") or job["ref"]
        job["prefix"] = member.get("prefix") or ""
    statuses = [j["status"] for j in item["sync_jobs"]]
    if "FAILURE" in statuses:
        item["sync_state"] = ("bad", "Sync failed")
    elif any(status in ACTIVE_JOB_STATUSES for status in statuses):
        item["sync_state"] = ("live", "Syncing")
    else:
        item["sync_state"] = None
    return item


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------
@admin_extension(
    title="Content Packages",
    description="Define named sets of blueprints, actions, plugins, shared modules and UI extensions "
    "that move between repositories and appliances together.",
)
def packages(request):
    profile = request.get_user_profile()
    context = {
        "uplink": {"url": reverse("admin_home"), "label": "Admin"},
        "denied": not profile.is_cbadmin,
        "rows": [],
        "contents_shown": CONTENTS_SHOWN,
    }
    if not context["denied"]:
        items = _describe(store.list_packages())
        jobs = _jobs_for(items)
        context["rows"] = [_row(item, jobs) for item in items]
    return render(request, TEMPLATES + "packages.html", context)


@cbadmin_required
def package_detail(request, pkg_id):
    """One package: its members and everything an export would write."""
    item = _describe([_get_or_404(pkg_id)])[0]
    row = _row(item, _jobs_for([item]))
    return render(request, TEMPLATES + "detail.html", {
        "row": row,
        "uplink": {"url": _page_url(), "label": "Content Packages"},
        "edit_url": "{}?{}".format(
            reverse("content_packages_edit", args=[row["id"]]), urlencode({"next": request.path})
        ),
        "dependencies_url": reverse("content_packages_dependencies", args=[row["id"]]),
    })


@cbadmin_required
def dependencies_panel(request, pkg_id):
    """HTML fragment the details page loads: the export's dependency closure."""
    package = _get_or_404(pkg_id)
    try:
        context = dependencies.closure(package, _home_repo(package))
    except Exception as exc:
        logger.exception("Could not work out the dependencies of %s", pkg_id)
        context = {"dependencies": [], "errors": [], "error": "Could not work out the dependencies: {}".format(exc)}
    context["member_count"] = len(package.get("contents") or [])
    return render(request, TEMPLATES + "dependencies_panel.html", context)


# ---------------------------------------------------------------------------
# Dialogs
# ---------------------------------------------------------------------------
def _picker(selected):
    """Picker sections, one per member type, plus an "Other members" section
    for selected refs the lists do not offer (deleted content, or a plugin
    that is not standalone)."""
    selected = list(selected or [])
    chosen = set(selected)
    offered = set()
    sections = []
    for member_type in store.MEMBER_TYPES.values():
        items = []
        for obj in member_type.pickable():
            ref = store.ref_for(member_type, obj)
            offered.add(ref)
            items.append({
                "ref": ref,
                "name": member_type.name_of(obj),
                "global_id": obj.global_id,
                "repo_label": obj.source_code_repo.label if obj.source_code_repo_id else "",
                "checked": ref in chosen,
            })
        items.sort(key=lambda i: i["name"].lower())
        sections.append({
            "key": member_type.dir,
            "title": member_type.title,
            "items": items,
            "selected": sum(1 for i in items if i["checked"]),
        })

    leftovers = [ref for ref in selected if ref not in offered]
    if leftovers:
        resolved = store.resolve_refs(leftovers)
        items = []
        for ref in leftovers:
            member = _member(ref, resolved.get(ref), None, "")
            items.append({
                "ref": ref,
                "name": member["name"],
                "global_id": member["global_id"] or ref,
                "repo_label": "",
                "checked": True,
                "missing": member["state"] == "missing",
            })
        sections.append({"key": "other", "title": "Other members", "items": items, "selected": len(items)})
    return sections


@dialog_view(template_name=TEMPLATES + "edit_dialog.html")
@cbadmin_required
def package_edit(request, pkg_id=None):
    """Add a package, or edit one's name, description, version and members."""
    package = _get_or_404(pkg_id) if pkg_id else None
    values = {
        "name": package["name"] if package else "",
        "description": package.get("description", "") if package else "",
        "version": package.get("version", store.DEFAULT_VERSION) if package else store.DEFAULT_VERSION,
        "contents": list(package.get("contents") or []) if package else [],
    }
    error = None
    if request.method == "POST":
        fields = {
            "name": request.POST.get("name", ""),
            "description": request.POST.get("description", ""),
            "version": request.POST.get("version", ""),
            "contents": request.POST.getlist("contents"),
        }
        try:
            if package:
                saved = store.update_package(package["id"], **fields)
            else:
                saved = store.create_package(**fields)
        except store.PackageError as exc:
            error = str(exc)
            values = fields
        else:
            messages.success(
                request,
                'Content package "{}" ({}) {}.'.format(saved["name"], saved["id"], "saved" if package else "added"),
            )
            return HttpResponseRedirect(_safe_next(request) or _page_url())

    sections = _picker(values["contents"])
    if package:
        title = "Edit content package {}".format(package["name"])
        action_url = reverse("content_packages_edit", args=[package["id"]])
    else:
        title = "Add content package"
        action_url = reverse("content_packages_add")
    return {
        "title": title,
        "action_url": action_url,
        "use_ajax": True,
        "submit": "Save" if package else "Add",
        "package": package,
        "values": values,
        "error": error,
        "sections": sections,
        "selected_count": sum(s["selected"] for s in sections),
        "next": _safe_next(request),
    }


def _safe_next(request):
    """Where to land after a dialog: the details page when opened from it."""
    candidate = request.POST.get("next") if request.method == "POST" else request.GET.get("next")
    if candidate and candidate.startswith("/xui/content_packages/"):
        return candidate
    return ""


@dialog_view
@cbadmin_required
def package_delete(request, pkg_id):
    """Remove a package's local definition; members and repositories stay."""
    package = _get_or_404(pkg_id)
    if request.method == "POST":
        store.delete_package(package["id"])
        messages.success(request, 'Content package "{}" ({}) deleted.'.format(package["name"], package["id"]))
        return HttpResponseRedirect(_page_url())
    count = len(package.get("contents") or [])
    content = format_html(
        "<p>Delete the definition of <b>{}</b> ({}) from this appliance?</p>"
        "<p>Only the package definition is removed. Its {} member{} and their dependencies stay on this "
        "appliance unchanged, and any <code>packages/{}</code> folder in a repository is left alone.</p>",
        package["name"], package["id"], count, "" if count == 1 else "s", package["id"],
    )
    return {
        "title": "Delete content package",
        "content": content,
        "action_url": reverse("content_packages_delete", args=[package["id"]]),
        "use_ajax": True,
        "submit": "Delete",
        "theme": "danger",
    }


# ---------------------------------------------------------------------------
# Export, Sync and Import dialogs
# ---------------------------------------------------------------------------
def _repo_choices():
    return list(SourceCodeRepo.objects.all().order_by("label"))


def _repo_context(package, values, repos):
    """Context for the repository and branch fields shared by the dialogs."""
    home = _home_repo(package) if package else None
    selected = values.get("repo") or (str(home.id) if home else (str(repos[0].id) if repos else ""))
    return {
        "repos": repos,
        "selected_repo": str(selected),
        "selected_branch": values.get("branch") or (package["local"].get("branch") if package and home else ""),
        "branches_url": reverse("json_branches_by_repo_id", args=[999999999]),
    }


def _no_repos(title):
    return {
        "title": title,
        "content": format_html(
            '<p class="text-danger">Add a Source Code Repository first (Admin &gt; Source Code Repositories).</p>'
        ),
    }


def _detail_url(pkg_id):
    return reverse("content_packages_detail", args=[pkg_id])


@dialog_view(template_name=TEMPLATES + "export_dialog.html")
@cbadmin_required
def package_export(request, pkg_id):
    """Push every member and the package manifest to a repository branch."""
    package = _get_or_404(pkg_id)
    title = "Export {}".format(package["name"])
    repos = _repo_choices()
    if not repos:
        return _no_repos(title)
    values = {"commit_message": "Export content package {} {}".format(package["name"], package.get("version") or "")}
    error = None
    if request.method == "POST":
        values = {
            "repo": request.POST.get("repo", ""),
            "branch": request.POST.get("branch", ""),
            "new_branch": request.POST.get("new_branch", ""),
            "commit_message": request.POST.get("commit_message", ""),
        }
        branch = values["new_branch"] if values["branch"] == "__new__" else values["branch"]
        try:
            outcome = services.export_package(
                package, services.get_repo(values["repo"]), branch, values["commit_message"],
                request.get_user_profile(),
            )
        except store.PackageError as exc:
            error = str(exc)
        else:
            return {
                "title": title,
                "outcome": outcome,
                "package": package,
                "reload_on_close": True,
            }
    resolved = store.resolve_refs(package.get("contents") or [])
    context = {
        "title": title,
        "action_url": reverse("content_packages_export", args=[package["id"]]),
        "use_ajax": True,
        "submit": "Export",
        "package": package,
        "values": values,
        "error": error,
        "allow_new_branch": True,
        "member_count": len(resolved),
        "missing": [ref for ref, obj in resolved.items() if obj is None],
    }
    context.update(_repo_context(package, values, repos))
    return context


@dialog_view(template_name=TEMPLATES + "sync_dialog.html")
@cbadmin_required
def package_sync(request, pkg_id):
    """Create one CloudBolt sync job per member from a repository branch."""
    package = _get_or_404(pkg_id)
    title = "Sync {}".format(package["name"])
    repos = _repo_choices()
    if not repos:
        return _no_repos(title)
    values = {"refresh_if_exists": True, "ignore_action_enabled": False}
    error = None
    if request.method == "POST":
        values = {
            "repo": request.POST.get("repo", ""),
            "branch": request.POST.get("branch", ""),
            "refresh_if_exists": bool(request.POST.get("refresh_if_exists")),
            "ignore_action_enabled": bool(request.POST.get("ignore_action_enabled")),
        }
        try:
            created = services.sync_package(
                package, services.get_repo(values["repo"]), values["branch"],
                values["refresh_if_exists"], values["ignore_action_enabled"], request.get_user_profile(),
            )
        except store.PackageError as exc:
            error = str(exc)
        else:
            messages.success(
                request,
                "Started {} sync job{} for {}, one per member; they run one after another.".format(
                    len(created), "" if len(created) == 1 else "s", package["name"]
                ),
            )
            return HttpResponseRedirect(_detail_url(package["id"]))
    context = {
        "title": title,
        "action_url": reverse("content_packages_sync", args=[package["id"]]),
        "use_ajax": True,
        "submit": "Sync",
        "package": package,
        "values": values,
        "error": error,
        "notice": request.GET.get("imported") == "1",
        "member_count": len(package.get("contents") or []),
        "has_blueprints": any(ref.startswith("blueprints/") for ref in package.get("contents") or []),
        "has_extensions": any(ref.startswith("extensions/") for ref in package.get("contents") or []),
    }
    context.update(_repo_context(package, values, repos))
    return context


@dialog_view(template_name=TEMPLATES + "import_dialog.html")
@cbadmin_required
def package_import(request):
    """Register a package manifest found under packages/ on a branch, then
    open its Sync dialog."""
    title = "Import content package from repository"
    repos = _repo_choices()
    if not repos:
        return _no_repos(title)
    values = {}
    error = None
    if request.method == "POST":
        values = {
            "repo": request.POST.get("repo", ""),
            "branch": request.POST.get("branch", ""),
            "ref": request.POST.get("ref", ""),
        }
        try:
            package, created = services.import_repo_package(
                services.get_repo(values["repo"]), values["branch"], values["ref"]
            )
        except store.PackageError as exc:
            error = str(exc)
        else:
            messages.success(request, 'Content package "{}" ({}) {}.'.format(
                package["name"], package["id"], "registered" if created else "updated from the repository"
            ))
            return {"nextURL": "{}?imported=1".format(reverse("content_packages_sync", args=[package["id"]]))}
    context = {
        "title": title,
        "action_url": reverse("content_packages_import"),
        "use_ajax": True,
        "submit": "Import",
        "values": values,
        "error": error,
        "list_url": reverse("content_packages_api_repo_packages", args=[999999999]),
    }
    context.update(_repo_context(None, values, repos))
    return context


# ---------------------------------------------------------------------------
# JSON API (session auth with CSRF, CB Admin only)
# ---------------------------------------------------------------------------
def _api(view):
    """Turn store and permission errors into JSON error responses."""

    @functools.wraps(view)
    def wrapper(request, *args, **kwargs):
        try:
            return view(request, *args, **kwargs)
        except PermissionDenied as exc:
            return JsonResponse({"error": str(exc) or "CB Admin required."}, status=403)
        except store.PackageNotFound as exc:
            return JsonResponse({"error": str(exc)}, status=404)
        except store.PackageError as exc:
            return JsonResponse({"error": str(exc)}, status=400)
        except Exception:
            logger.exception("Content package API call failed: %s %s", request.method, request.path)
            return JsonResponse({"error": "Unexpected error; see the CloudBolt application log."}, status=500)

    return wrapper


def _method_not_allowed(allowed):
    response = JsonResponse({"error": "Use {}.".format(" or ".join(allowed))}, status=405)
    response["Allow"] = ", ".join(allowed)
    return response


def _body(request):
    if not request.body:
        return {}
    try:
        data = json.loads(request.body.decode("utf-8"))
    except ValueError:
        raise store.PackageError("The request body must be a JSON object.")
    if not isinstance(data, dict):
        raise store.PackageError("The request body must be a JSON object.")
    return data


@_api
@cbadmin_required
def api_packages(request):
    """GET: every package with resolved members. POST: create one from
    {name, description, version, contents[]}."""
    if request.method == "GET":
        return JsonResponse({"packages": _describe(store.list_packages())})
    if request.method == "POST":
        package = store.create_package(**_body(request))
        return JsonResponse(_describe([package])[0], status=201)
    return _method_not_allowed(["GET", "POST"])


@_api
@cbadmin_required
def api_package(request, pkg_id):
    """GET one package, PATCH any of name, description, version and contents,
    or DELETE its local definition."""
    if request.method == "GET":
        return JsonResponse(_describe([store.get_package(pkg_id)])[0])
    if request.method == "PATCH":
        package = store.update_package(pkg_id, **_body(request))
        return JsonResponse(_describe([package])[0])
    if request.method == "DELETE":
        store.delete_package(pkg_id)
        return JsonResponse({"deleted": pkg_id})
    return _method_not_allowed(["GET", "PATCH", "DELETE"])


@_api
@cbadmin_required
def api_dependencies(request, pkg_id):
    """Everything exporting the package writes besides its members."""
    if request.method != "GET":
        return _method_not_allowed(["GET"])
    package = store.get_package(pkg_id)
    return JsonResponse(dependencies.closure(package, _home_repo(package)))


@_api
@cbadmin_required
def api_content(request):
    """Everything the picker offers, grouped by type."""
    if request.method != "GET":
        return _method_not_allowed(["GET"])
    types = []
    for member_type in store.MEMBER_TYPES.values():
        items = [
            {
                "ref": store.ref_for(member_type, obj),
                "name": member_type.name_of(obj),
                "type_title": member_type.singular,
                "repo_label": obj.source_code_repo.label if obj.source_code_repo_id else "",
            }
            for obj in member_type.pickable()
        ]
        items.sort(key=lambda i: i["name"].lower())
        types.append({"type": member_type.dir, "prefix": member_type.prefix, "title": member_type.title, "items": items})
    return JsonResponse({"types": types})


def _repo_from(data):
    return services.get_repo(data.get("source_code_repo_id") or data.get("source_code_repo"))


@_api
@cbadmin_required
def api_export(request, pkg_id):
    """POST {source_code_repo_id, branch, commit_message}: push every member
    and the manifest; returns one result per member plus the PR link."""
    if request.method != "POST":
        return _method_not_allowed(["POST"])
    data = _body(request)
    package = store.get_package(pkg_id)
    outcome = services.export_package(
        package, _repo_from(data), data.get("branch"), data.get("commit_message"), request.get_user_profile()
    )
    return JsonResponse(outcome, status=200 if not outcome["failed"] else 207)


@_api
@cbadmin_required
def api_sync(request, pkg_id):
    """POST {source_code_repo_id?, branch?, refresh_if_exists?,
    ignore_action_enabled?}: one sync job per member, chained. Repository and
    branch default to the package's home."""
    if request.method != "POST":
        return _method_not_allowed(["POST"])
    data = _body(request)
    package = store.get_package(pkg_id)
    if data.get("source_code_repo_id") or data.get("source_code_repo"):
        repo = _repo_from(data)
    else:
        repo = _home_repo(package)
        if repo is None:
            raise store.PackageError("The package has no home repository yet; pass source_code_repo_id.")
    branch = data.get("branch") or package["local"].get("branch")
    created = services.sync_package(
        package, repo, branch,
        data.get("refresh_if_exists", True), data.get("ignore_action_enabled", False),
        request.get_user_profile(),
    )
    return JsonResponse({
        "jobs": [
            {"ref": ref, "job_id": job.id, "global_id": getattr(job, "global_id", ""), "url": job.get_absolute_url()}
            for ref, job in created
        ],
    }, status=201)


@_api
@cbadmin_required
def api_repo_packages(request, repo_id):
    """GET ?branch=: package manifests under packages/ on that branch."""
    if request.method != "GET":
        return _method_not_allowed(["GET"])
    repo = services.get_repo(repo_id)
    return JsonResponse({"packages": services.list_repo_packages(repo, request.GET.get("branch"))})


@_api
@cbadmin_required
def api_repo_import(request, repo_id):
    """POST {branch, ref}: register or update (by PKG id) that manifest."""
    if request.method != "POST":
        return _method_not_allowed(["POST"])
    data = _body(request)
    package, created = services.import_repo_package(services.get_repo(repo_id), data.get("branch"), data.get("ref"))
    return JsonResponse(dict(_describe([package])[0], created=created), status=201 if created else 200)
