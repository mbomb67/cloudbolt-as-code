"""
Credentials Manager XUI

An admin page (Admin > Custom Extensions > Credentials Manager) for updating
the credentials CloudBolt stores for many servers and images at once, for
example after a domain service account's password changes. It changes only
CloudBolt's records: nothing runs on the servers and the images themselves
are untouched.

The New update tab sends the filters to the preview API (targets.py), which
answers with counts, a breakdown per filter, warnings and a token; the page
pages through the matching rows with the rows API and downloads them as CSV.
The preview never receives a password, only whether one was entered. Apply
posts the passwords once, with the token and the typed count, and jobs.py
starts the update job. The History tab lists the update jobs.

Everything here requires CB Admin.
"""
import csv
import functools
import json

from django.core.exceptions import PermissionDenied
from django.http import HttpResponse, JsonResponse
from django.shortcuts import render
from django.urls import reverse

from extensions.views import admin_extension
from utilities.logger import ThreadLogger
from utilities.permissions import cbadmin_required

from xui.credentials_manager import jobs, targets
from xui.credentials_manager.targets import TargetError

logger = ThreadLogger(__name__)

TEMPLATES = "credentials_manager/templates/"

JOB_PILLS = {
    "SUCCESS": ("ok", "Succeeded"),
    "WARNING": ("warn", "Succeeded with warnings"),
    "FAILURE": ("bad", "Failed"),
    "CANCELED": ("", "Canceled"),
}


def _api(view):
    """Turn permission and request errors into JSON error responses. Request
    bodies are never logged: an apply request carries passwords."""

    @functools.wraps(view)
    def wrapper(request, *args, **kwargs):
        try:
            return view(request, *args, **kwargs)
        except PermissionDenied:
            return JsonResponse({"error": "This requires CB Admin or Super Admin."}, status=403)
        except TargetError as exc:
            return JsonResponse({"error": exc.public_message}, status=400)
        except Exception:
            logger.exception("Credentials Manager API call failed: %s %s", request.method, request.path)
            return JsonResponse({"error": "Unexpected error; see the CloudBolt application log."}, status=500)

    return wrapper


def _body(request):
    if request.method != "POST":
        raise TargetError("Use POST.")
    try:
        data = json.loads(request.body.decode("utf-8") or "{}")
    except ValueError:
        raise TargetError("The request body must be a JSON object.")
    if not isinstance(data, dict):
        raise TargetError("The request body must be a JSON object.")
    return data


# ---------------------------------------------------------------------------
# Page
# ---------------------------------------------------------------------------
@admin_extension(
    title="Credentials Manager",
    description="Update the username, password or SSH key CloudBolt stores for many servers and images at once, "
    "chosen by resource technology, handler, OS build, environment, group, blueprint or current username.",
)
def credentials(request):
    profile = request.get_user_profile()
    return render(request, TEMPLATES + "credentials.html", {
        "uplink": {"url": reverse("admin_home"), "label": "Admin"},
        "denied": not profile.is_cbadmin,
        "ssh_keys_url": reverse("sshkey_list"),
    })


@cbadmin_required
def history_panel(request):
    """HTML fragment for the History tab."""
    rows = jobs.recent_updates()
    for row in rows:
        row["pill"] = ("live", "Running") if row["active"] else JOB_PILLS.get(row["status"], ("", row["status"].title()))
    return render(request, TEMPLATES + "history_panel.html", {"rows": rows})


# ---------------------------------------------------------------------------
# JSON API (session auth with CSRF, CB Admin only)
# ---------------------------------------------------------------------------
@_api
@cbadmin_required
def api_options(request):
    """GET: the values each filter picker offers."""
    return JsonResponse(targets.options())


@_api
@cbadmin_required
def api_preview(request):
    """POST {apply_to, filters, changes}: counts, breakdowns, warnings and the
    token Apply must send back. changes.password and changes.new_vm_password
    are booleans here."""
    return JsonResponse(targets.preview(_body(request)))


@_api
@cbadmin_required
def api_rows(request):
    """POST the preview body plus {kind, page, page_size, search, facet,
    warning, status}: one page of matching rows."""
    return JsonResponse(targets.page(_body(request)))


@cbadmin_required
def rows_csv(request):
    """POST form field payload (the preview body plus kind): every matching
    row as CSV."""
    try:
        data = json.loads(request.POST.get("payload") or "{}")
    except ValueError:
        data = None
    if not isinstance(data, dict):
        return HttpResponse("The payload must be a JSON object.", status=400, content_type="text/plain")
    try:
        _filters, _plan, server_rows, image_rows = targets.collect(data)
    except TargetError as exc:
        return HttpResponse(exc.public_message, status=400, content_type="text/plain")
    kind = "image" if data.get("kind") == "image" else "server"
    rows = targets.narrow(image_rows if kind == "image" else server_rows, data)
    columns = (
        ("name", "Image"), ("handler", "Resource handler"), ("os_build", "OS build"), ("username", "Current username"),
    ) if kind == "image" else (
        ("name", "Server"), ("handler", "Resource handler"), ("os_build", "OS build"), ("environment", "Environment"),
        ("group", "Group"), ("blueprint", "Blueprint"), ("username", "Current username"), ("power", "Power"),
    )
    response = HttpResponse(content_type="text/csv")
    response["Content-Disposition"] = 'attachment; filename="credentials-update-{}s.csv"'.format(kind)
    writer = csv.writer(response)
    writer.writerow(["ID"] + [label for _key, label in columns] + ["Changes", "Warnings"])
    for row in rows:
        writer.writerow(
            [row["id"]] + [row[key] for key, _label in columns]
            + [", ".join(targets.CHANGE_LABELS[c] for c in row["changes"]) or "Nothing",
               " | ".join(targets.WARNINGS[w] for w in row["warnings"])]
        )
    return response


@_api
@cbadmin_required
def api_apply(request):
    """POST the preview body with the real passwords and their confirmations
    in changes, plus {token, confirm}: start the update job."""
    job, total = jobs.start_update(_body(request), request.get_user_profile())
    return JsonResponse({
        "job_id": job.id,
        "url": job.get_absolute_url(),
        "total": total,
    }, status=202)
