"""
File Archives tab of the Runbooks page: versioned files that scripts
download, and the download view the VMs call.

views.py imports the page function, because CloudBolt looks for admin
extensions only in an extension's views.py.

The store, the signing of links and the activity log live in the
file_archives shared module (shared_modules/SHM-v8llc736), which Run Task also
uses to put the download step into a script. Every view here except the
download is for CB Admins and Super Admins.

The download view (/xui/runbooks/file-archives/dl/<token>/<file name>) needs no
CloudBolt login: CloudBolt's own login_not_required decorator exempts it from
the login middleware, and the view serves only the exact file version a valid,
unexpired token names. Anything else is a 404.
"""
from datetime import datetime

from django.contrib import messages
from django.core.files.uploadhandler import FileUploadHandler, SkipFile
from django.http import FileResponse, Http404, HttpResponseRedirect
from django.shortcuts import render
from django.urls import reverse
from django.views.decorators.csrf import csrf_exempt, csrf_protect
from django.views.decorators.http import require_safe

from extensions.views import admin_extension
from infrastructure.models import Server
from utilities.decorators import dialog_view
from utilities.events import add_server_event
from utilities.logger import ThreadLogger
from utilities.middleware import login_not_required
from utilities.permissions import cbadmin_required

logger = ThreadLogger(__name__)

TEMPLATES = "runbooks/templates/"
XUI_PACKAGE = "runbooks"
PAGE_VIEW = "archives"


def _fa():
    """The file_archives shared module, or a clear error when it is not on this CloudBolt."""
    try:
        from shared_modules import file_archives
    except ImportError:
        raise Http404(
            "The file_archives shared module (SHM-v8llc736) is not on this CloudBolt. "
            "Sync shared_modules/SHM-v8llc736 and restart the web service."
        )
    return file_archives


def _page_url():
    return reverse("render_admin_extension", kwargs={"package": XUI_PACKAGE, "view_id": PAGE_VIEW})


def _file_url(archive, name):
    return reverse("runbooks_archive_file", args=[archive, name])


def _archive_url(archive):
    return reverse("runbooks_archive", args=[archive])


def _activity_rows(events):
    """Activity entries with a link to each file that still exists."""
    for entry in events:
        archive, _, name = (entry.get("ref") or "").partition("/")
        entry["archive"] = archive
        entry["file"] = name
        entry["at_text"] = _when(entry.get("at"))
    return events


def _username(request):
    return request.user.username


def _human_size(size):
    size = float(size or 0)
    for unit in ("bytes", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{int(size)} {unit}" if unit == "bytes" else f"{size:.1f} {unit}"
        size /= 1024


def _when(stamp):
    """A stored UTC stamp (2026-10-17T02:15:13Z) as 2026-10-17 02:15 UTC."""
    try:
        return datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").strftime("%Y-%m-%d %H:%M UTC")
    except (TypeError, ValueError):
        return stamp or ""


def _base_url_info(request, fa):
    current = fa.get_settings()
    portal = fa.default_base_url()
    effective = current["base_url"] or portal
    return {
        "effective": effective,
        "source": "setting" if current["base_url"] else ("portal" if portal else ""),
        "suggestion": request.build_absolute_uri("/").rstrip("/"),
    }


# ---------------------------------------------------------------- pages


def _archive_summary(archive):
    """Counts and dates for an archive row: files, versions, stored bytes, last upload."""
    files = archive["files"]
    stored = sum(meta["size"] for item in files for meta in item["versions"])
    last = max((item["latest"] for item in files), key=lambda meta: meta["uploaded_at"], default=None)
    return {
        "name": archive["name"],
        "url": _archive_url(archive["name"]),
        "description": archive.get("description", ""),
        "file_count": len(files),
        "version_count": sum(len(item["versions"]) for item in files),
        "stored": _human_size(stored),
        "created_by": archive.get("created_by", ""),
        "created_text": _when(archive.get("created_at")),
        "last_upload": last,
        "last_upload_text": _when(last["uploaded_at"]) if last else "",
    }


@admin_extension(
    title="File Archives",
    description="Keep versioned installers, tarballs and config bundles on CloudBolt for scripts to "
    "download onto VMs through short-lived links.",
)
def archives(request):
    """The File Archives page: one paginated table of archives, then recent activity."""
    profile = request.get_user_profile()
    context = {
        "uplink": {"url": reverse("admin_home"), "label": "Admin"},
        "denied": not profile.is_cbadmin,
    }
    if not context["denied"]:
        fa = _fa()
        context.update(
            {
                "archives": [_archive_summary(archive) for archive in fa.list_archives()],
                "settings": fa.get_settings(),
                "base_url": _base_url_info(request, fa),
                "activity": _activity_rows(fa.activity(limit=500)),
            }
        )
    return render(request, TEMPLATES + "archives.html", context)


@cbadmin_required
def archive_detail(request, archive):
    """One archive: its files, its activity, and its tools."""
    fa = _fa()
    info = fa.archive_info(archive)
    if info is None:
        raise Http404
    for item in info["files"]:
        item["url"] = _file_url(archive, item["name"])
        item["size"] = _human_size(item["latest"]["size"])
        item["extractable"] = item["name"].lower().endswith(fa.EXTRACTABLE)
        item["uploaded_text"] = _when(item["latest"]["uploaded_at"])
    context = {
        "uplink": {"url": _page_url(), "label": "File Archives"},
        "archive": info,
        "summary": _archive_summary(info),
        "activity": _activity_rows(fa.activity(archive=archive, limit=500)),
    }
    return render(request, TEMPLATES + "archive.html", context)


@cbadmin_required
def file_detail(request, archive, name):
    """One file: its versions, its activity, and how to use it in a script."""
    fa = _fa()
    try:
        versions = fa.versions(archive, name)
    except fa.ArchiveError:
        raise Http404
    if not versions:
        raise Http404
    for meta in versions:
        meta["size_text"] = _human_size(meta["size"])
        meta["uploaded_text"] = _when(meta["uploaded_at"])
        meta["download_url"] = reverse("runbooks_archive_version_download", args=[archive, name, meta["version"]])
    latest = versions[-1]
    context = {
        "uplink": {"url": _archive_url(archive), "label": archive},
        "archive_url": _archive_url(archive),
        "archive": archive,
        "name": name,
        "ref": f"{archive}/{name}",
        "latest": latest,
        "versions": list(reversed(versions)),
        "activity": _activity_rows(fa.activity(ref=f"{archive}/{name}", limit=200)),
        "extractable": name.lower().endswith(fa.EXTRACTABLE),
    }
    return render(request, TEMPLATES + "archive_file.html", context)


@cbadmin_required
def version_download(request, archive, name, version):
    """An admin's own copy of a stored version."""
    fa = _fa()
    try:
        resolved = fa.resolve(f"{archive}/{name}@{version}")
    except fa.ArchiveError:
        raise Http404
    return FileResponse(open(resolved["path"], "rb"), as_attachment=True, filename=name)


# ---------------------------------------------------------------- dialogs


def _done(request, message, url=None):
    messages.success(request, message)
    return HttpResponseRedirect(url or _page_url())


@dialog_view(template_name=TEMPLATES + "archive_dialog.html")
@cbadmin_required
def archive_edit(request, archive=None):
    """Add an archive, or edit an archive's description."""
    fa = _fa()
    existing = None
    if archive:
        existing = fa.archive_info(archive)
        if existing is None:
            raise Http404
    values = {"name": archive or "", "description": existing.get("description", "") if existing else ""}
    error = None
    if request.method == "POST":
        values = {"name": archive or request.POST.get("name", "").strip(), "description": request.POST.get("description", "")}
        try:
            if existing:
                fa.update_archive(archive, values["description"])
            else:
                fa.create_archive(values["name"], values["description"], _username(request))
        except fa.ArchiveError as exc:
            error = str(exc)
        else:
            return _done(request, f'Archive "{values["name"]}" {"saved" if existing else "added"}.', _archive_url(values["name"]))
    return {
        "title": f"Edit archive {archive}" if existing else "Add archive",
        "action_url": reverse("runbooks_archive_edit", args=[archive]) if existing else reverse("runbooks_archive_add"),
        "use_ajax": True,
        "submit": "Save" if existing else "Add",
        "values": values,
        "existing": existing,
        "error": error,
    }


# Types the upload dialog accepts on top of CloudBolt's default upload
# allowlist (utilities/upload_handlers), which has zip but not gzip, tar or
# installers. Types as libmagic reports them.
ARCHIVE_CONTENT_TYPES = [
    "application/gzip",
    "application/x-gzip",
    "application/x-tar",
    "application/x-bzip2",
    "application/x-xz",
    "application/zstd",
    "application/x-7z-compressed",
    "application/java-archive",
    "application/x-rpm",
    "application/vnd.debian.binary-package",
    "application/x-debian-package",
    "application/x-msi",
    "application/vnd.ms-msi",
    "application/x-ole-storage",
    "application/CDFV2",
    "application/vnd.ms-cab-compressed",
    "application/x-dosexec",
    "application/vnd.microsoft.portable-executable",
    "application/x-executable",
    "application/x-pie-executable",
    "application/x-sharedlib",
    "application/json",
    "text/x-yaml",
    "text/csv",
]


class _SizeLimit(FileUploadHandler):
    """Skips a file as soon as it passes the File Archives upload limit, before it is written."""

    def __init__(self, request, limit_mb):
        super().__init__(request)
        self.limit_mb = limit_mb
        self.limit = limit_mb * 1024 * 1024
        self.seen = 0

    def new_file(self, *args, **kwargs):
        super().new_file(*args, **kwargs)
        self.seen = 0

    def receive_data_chunk(self, raw_data, start):
        self.seen += len(raw_data)
        if self.seen > self.limit:
            messages.error(self.request, f"The file is larger than the {self.limit_mb} MB upload limit in Settings.")
            raise SkipFile()
        return raw_data

    def file_complete(self, file_size):
        return None


def _upload_handlers(request, limit_mb):
    """
    The size limit, then CloudBolt's own upload handler with its antivirus scan
    and content-type check and any CONTENT_TYPE_CHECK_CONF setting, plus
    ARCHIVE_CONTENT_TYPES; then the rest of FILE_UPLOAD_HANDLERS as CloudBolt
    configures them. Raises if CloudBolt's handler is not where this expects.
    """
    from django.conf import settings
    from django.core.files.uploadhandler import load_handler
    from utilities.upload_handlers import av_service, content_type_config
    from utilities.upload_handlers.handler import CustomUploadHandler

    config = dict(content_type_config)
    extra = list(config.pop("additional_content_types", None) or []) + ARCHIVE_CONTENT_TYPES
    handlers = [
        _SizeLimit(request, limit_mb),
        CustomUploadHandler(request, av_service=av_service, additional_content_types=extra, **config),
    ]
    for path in settings.FILE_UPLOAD_HANDLERS:
        if path != "utilities.upload_handlers.custom_upload_handler":
            handlers.append(load_handler(path, request))
    return handlers


@csrf_exempt
def upload(request, archive, name=None):
    """
    Upload a new file to an archive, or a new version of one of its files.

    Upload handlers can only be replaced before anything reads the request
    body, and CloudBolt's CSRF middleware reads it first; so this outer view is
    exempt, swaps the handlers, and _upload checks CSRF itself.

    Fails closed: if the handlers cannot be built (CloudBolt moved its upload
    handler) or set, the request keeps CloudBolt's default handlers, which
    scan and check it as usual and refuse the archive types.
    """
    try:
        limit_mb = _fa().get_settings()["max_upload_mb"]
        request.upload_handlers = _upload_handlers(request, limit_mb)
    except Exception:
        logger.exception("File Archives: kept CloudBolt's default upload handlers")
    return _upload(request, archive, name)


@dialog_view(template_name=TEMPLATES + "archive_upload_dialog.html")
@csrf_protect
@cbadmin_required
def _upload(request, archive, name=None):
    fa = _fa()
    if fa.archive_info(archive) is None:
        raise Http404
    error = None
    note = ""
    if request.method == "POST":
        note = request.POST.get("note", "")
        upload_file = request.FILES.get("file")
        target = name or request.POST.get("name", "").strip() or (upload_file.name if upload_file else "")
        if upload_file is None:
            error = "No file arrived. If a message above gives a reason, fix that and upload again."
        else:
            try:
                meta = fa.add_version(archive, target, upload_file.chunks(), _username(request), note=note)
            except fa.ArchiveError as exc:
                error = str(exc)
            else:
                fa.record(f"{archive}/{target}", "uploaded", f"version {meta['version']}, {_human_size(meta['size'])}", _username(request))
                return _done(
                    request,
                    f"Stored {archive}/{target} version {meta['version']} ({_human_size(meta['size'])}).",
                    _file_url(archive, target),
                )
    return {
        "title": f"Upload a new version of {archive}/{name}" if name else f"Upload a file to {archive}",
        "action_url": reverse("runbooks_archive_upload_version", args=[archive, name]) if name else reverse("runbooks_archive_upload", args=[archive]),
        "use_ajax": True,
        "submit": "Upload",
        "archive": archive,
        "name": name,
        "note": note,
        "error": error,
    }


def _confirm(request, title, message, action_url, submit, run, success, theme="danger"):
    """A confirmation dialog that calls run() on POST."""
    error = None
    if request.method == "POST":
        fa = _fa()
        try:
            url = run()
        except fa.ArchiveError as exc:
            error = str(exc)
        else:
            return _done(request, success, url)
    return {
        "title": title,
        "action_url": action_url,
        "use_ajax": True,
        "submit": submit,
        "theme": theme,
        "message": message,
        "error": error,
    }


@dialog_view(template_name=TEMPLATES + "archive_confirm_dialog.html")
@cbadmin_required
def archive_delete(request, archive):
    fa = _fa()

    def run():
        fa.delete_archive(archive)
        fa.forget(f"{archive}/")

    return _confirm(
        request,
        f"Delete archive {archive}",
        f"Delete the archive {archive} and every file and version in it? Scripts that declare its files will fail.",
        reverse("runbooks_archive_delete", args=[archive]),
        "Delete",
        run,
        f'Archive "{archive}" deleted.',
    )


@dialog_view(template_name=TEMPLATES + "archive_confirm_dialog.html")
@cbadmin_required
def file_delete(request, archive, name):
    fa = _fa()

    def run():
        fa.delete_file(archive, name)
        fa.forget(f"{archive}/{name}")
        return _archive_url(archive)

    return _confirm(
        request,
        f"Delete {archive}/{name}",
        f"Delete {archive}/{name} and all of its versions? Scripts that declare it will fail.",
        reverse("runbooks_archive_file_delete", args=[archive, name]),
        "Delete",
        run,
        f"Deleted {archive}/{name}.",
    )


@dialog_view(template_name=TEMPLATES + "archive_confirm_dialog.html")
@cbadmin_required
def version_delete(request, archive, name, version):
    fa = _fa()

    def run():
        fa.delete_version(archive, name, version)
        fa.record(f"{archive}/{name}", "version deleted", f"version {version}", _username(request))
        return _file_url(archive, name) if fa.versions(archive, name) else _archive_url(archive)

    return _confirm(
        request,
        f"Delete version {version}",
        f"Delete version {version} of {archive}/{name}? Scripts pinned to @{version} will fail; links already "
        "issued for it stop working.",
        reverse("runbooks_archive_version_delete", args=[archive, name, version]),
        "Delete",
        run,
        f"Deleted version {version} of {archive}/{name}.",
    )


@dialog_view(template_name=TEMPLATES + "archive_confirm_dialog.html")
@cbadmin_required
def version_restore(request, archive, name, version):
    fa = _fa()

    def run():
        meta = fa.restore_version(archive, name, version, _username(request))
        fa.record(f"{archive}/{name}", "restored", f"version {version} as version {meta['version']}", _username(request))
        return _file_url(archive, name)

    return _confirm(
        request,
        f"Restore version {version}",
        f"Copy version {version} of {archive}/{name} to a new latest version? Scripts that use the latest "
        "version get it on their next run.",
        reverse("runbooks_archive_version_restore", args=[archive, name, version]),
        "Restore",
        run,
        f"Restored version {version} of {archive}/{name} as the latest version.",
        theme="primary",
    )


@dialog_view(template_name=TEMPLATES + "archive_confirm_dialog.html")
@cbadmin_required
def rotate_key(request):
    fa = _fa()

    def run():
        fa.rotate_key()

    return _confirm(
        request,
        "Revoke all links",
        "Replace the signing key? Every download link issued so far stops working, including links for "
        "scripts that are running now.",
        reverse("runbooks_archive_rotate_key"),
        "Revoke all links",
        run,
        "Signing key replaced: every earlier link is revoked.",
    )


@dialog_view(template_name=TEMPLATES + "archive_settings_dialog.html")
@cbadmin_required
def settings_edit(request):
    fa = _fa()
    values = fa.get_settings()
    error = None
    if request.method == "POST":
        values = {
            "base_url": request.POST.get("base_url", ""),
            "verify_tls": request.POST.get("verify_tls") == "on",
            "keep_versions": request.POST.get("keep_versions", ""),
            "link_minutes": request.POST.get("link_minutes", ""),
            "max_upload_mb": request.POST.get("max_upload_mb", ""),
        }
        try:
            fa.save_settings(**values)
        except fa.ArchiveError as exc:
            error = str(exc)
        else:
            return _done(request, "File Archives settings saved.")
    return {
        "title": "File Archives settings",
        "action_url": reverse("runbooks_archive_settings"),
        "use_ajax": True,
        "submit": "Save",
        "values": values,
        "base_url": _base_url_info(request, fa),
        "max_minutes": fa.MAX_LINK_MINUTES,
        "max_upload_limit": fa.MAX_UPLOAD_MB,
        "error": error,
    }


# ---------------------------------------------------------------- downloads


@login_not_required
@require_safe
def download(request, token, name):
    """
    Serve the file version a signed link names. No login: the token is the
    credential. Every refusal is a plain 404 so a caller learns nothing.
    """
    fa = _fa()
    client = request.META.get("REMOTE_ADDR", "unknown address")
    payload = fa.read_token(token)
    if payload is None:
        logger.info("File archives: refused an invalid or expired link from %s", client)
        raise Http404
    ref = payload.get("r", "")
    if not ref.endswith("/" + name):
        fa.record(ref, "refused", f"link used with the wrong file name, from {client}")
        raise Http404
    try:
        resolved = fa.resolve(f"{ref}@{payload.get('v')}")
    except fa.ArchiveError:
        fa.record(ref, "refused", f"version {payload.get('v')} no longer exists, from {client}")
        raise Http404
    server = Server.objects.filter(global_id=payload.get("s")).first() if payload.get("s") else None
    who = server.hostname if server else "no VM"
    fa.record(ref, "downloaded", f"version {resolved['version']} by {who} from {client}")
    if server is not None:
        try:
            add_server_event("MODIFICATION", server, f"Downloaded file archive {ref} version {resolved['version']}.")
        except Exception:
            logger.exception("Could not add a server event for a file archive download")
    response = FileResponse(open(resolved["path"], "rb"), as_attachment=True, filename=name)
    response["Cache-Control"] = "no-store"
    return response
