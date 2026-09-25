"""
HCP Terraform Workspace XUI

Adds two tabs to CloudBolt resources provisioned by the HCP Terraform
blueprints (HCP Terraform VM, BP-b0qm83lh; HCP Terraform No-Code Module,
BP-00meiwwz) -- any resource carrying a tfc_workspace_id custom field:

- Terraform: workspace summary (lock, Terraform version, execution mode,
  VCS repo/branch or no-code module, drift assessment, cost estimate), a
  pending-run banner with a permission-gated Discard, the run history with
  plan counts and links to the owning CloudBolt jobs, and the resources
  Terraform manages.
- Terraform Variables: read-only workspace variables (sensitive values are
  never rendered) plus the variable sets the workspace inherits.

Editing variables deliberately stays in the blueprints' resource actions
(Terraform Update, Resize, Update Variables), so every change goes through a
plan-approval job with an audit trail.

Every HCP Terraform call goes through the tfc_api shared module
(SHM-jlguerjr) using the ConnectionInfo recorded on the resource
(tfc_connection_info). The tabs therefore run under that team token, not the
viewer's identity, so every endpoint re-checks the viewer's CloudBolt
permission on the resource. Panels load asynchronously so a slow or
unreachable HCP Terraform never stalls the resource page.

Vendor API references (each tfc_api call site cites its own doc):
    https://developer.hashicorp.com/terraform/cloud-docs/api-docs/workspaces
    https://developer.hashicorp.com/terraform/cloud-docs/api-docs/run
    https://developer.hashicorp.com/terraform/cloud-docs/api-docs/workspace-resources
    https://developer.hashicorp.com/terraform/cloud-docs/api-docs/workspace-variables
    https://developer.hashicorp.com/terraform/cloud-docs/api-docs/variable-sets
    https://developer.hashicorp.com/terraform/cloud-docs/api-docs/assessment-results
    https://developer.hashicorp.com/terraform/cloud-docs/api-docs/cost-estimates
"""
import re
from urllib.parse import urlencode

from django.contrib import messages
from django.http import HttpResponseRedirect
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.utils.html import format_html, mark_safe

from extensions.views import tab_extension, TabExtensionDelegate
from jobs.models import Job
from resources.models import Resource
from utilities.decorators import dialog_view
from utilities.logger import ThreadLogger

from shared_modules.tfc_api import (
    RUN_CLASS_ABANDONED,
    RUN_CLASS_APPLIED,
    RUN_CLASS_CONFIRMABLE,
    RUN_CLASS_ERRORED,
    RUN_CLASS_IN_FLIGHT,
    RUN_CLASS_NO_CHANGES,
    RUN_CLASS_POLICY_OVERRIDE,
    TFCConflictError,
    TFCError,
    classify_run,
    get_options_client,
    parse_job_id_from_run_message,
)

logger = ThreadLogger(__name__)

TEMPLATES = "hcp_terraform_workspace/templates/"
RUNS_PAGE_SIZE = 20
RUN_ID_RE = re.compile(r"^run-[A-Za-z0-9]+$")

# Viewing the tabs needs VIEW_PERMISSION on the resource. Discarding a pending
# run needs DISCARD_PERMISSION on the resource, or CloudBolt admin.
VIEW_PERMISSION = "resource.view"
DISCARD_PERMISSION = "resource.manage_parameters"

# Run classification (tfc_api.classify_run) -> pill style + Bootstrap icon.
RUN_STYLES = {
    RUN_CLASS_APPLIED: ("ok", "bi-check-circle-fill"),
    RUN_CLASS_NO_CHANGES: ("ok", "bi-check-circle"),
    RUN_CLASS_CONFIRMABLE: ("warn", "bi-hourglass-split"),
    RUN_CLASS_POLICY_OVERRIDE: ("warn", "bi-shield-exclamation"),
    RUN_CLASS_IN_FLIGHT: ("live", "bi-arrow-repeat"),
    RUN_CLASS_ABANDONED: ("muted", "bi-slash-circle"),
    RUN_CLASS_ERRORED: ("bad", "bi-x-circle-fill"),
}

# Run "source" values documented on the Runs API page.
RUN_SOURCES = {
    "tfe-ui": "HCP Terraform UI",
    "tfe-api": "API (CloudBolt)",
    "tfe-configuration-version": "VCS push",
}


# ---------------------------------------------------------------------------
# Resource / permission helpers
# ---------------------------------------------------------------------------
def _attr(resource, name):
    """Value of a custom field on the resource (exposed as an attribute), or None."""
    try:
        value = getattr(resource, name)
    except AttributeError:
        return None
    if value in ("", None):
        return None
    return value


def _has_permission(profile, permission, resource):
    if getattr(profile, "is_cbadmin", False):
        return True
    try:
        return bool(profile.has_permission(permission, resource))
    except Exception:
        logger.exception("Permission check '%s' failed for resource %s", permission, resource.id)
        return False


def _can_view(profile, resource):
    return _has_permission(profile, VIEW_PERMISSION, resource)


def _can_discard(profile, resource):
    return _has_permission(profile, DISCARD_PERMISSION, resource)


def _client_for(resource):
    """TFCClient bound to the connection and organization recorded on the resource.

    get_options_client is the tfc_api seam without job progress messages --
    this runs in a web request, not a job. It raises TFCConfigError when the
    ConnectionInfo is missing or its token was redacted by a repo sync.
    """
    return get_options_client(
        _attr(resource, "tfc_connection_info"),
        organization=_attr(resource, "tfc_organization"),
    )


def _load(request, resource_id):
    """Resolve the resource and viewer for a panel; returns (resource, profile, error)."""
    resource = get_object_or_404(Resource, pk=resource_id)
    profile = request.get_user_profile()
    if not _can_view(profile, resource):
        return resource, profile, "You do not have permission to view this resource."
    if not _attr(resource, "tfc_workspace_id"):
        return resource, profile, "This resource has no HCP Terraform workspace recorded."
    return resource, profile, None


def _panel(request, template, context):
    """Render a panel fragment (always HTTP 200 so jQuery .load() injects it)."""
    return render(request, TEMPLATES + template, context)


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------
def _when(value):
    """ISO-8601 TFC timestamp -> 'YYYY-MM-DD HH:MM TZ' in CloudBolt's time zone."""
    if not value:
        return ""
    try:
        parsed = parse_datetime(value)
    except (TypeError, ValueError):
        parsed = None
    if parsed is None:
        return str(value)
    if timezone.is_aware(parsed):
        parsed = timezone.localtime(parsed)
    return parsed.strftime("%Y-%m-%d %H:%M %Z").strip()


def _money(value, signed=False):
    """Cost-estimate amounts arrive as decimal strings (USD per month)."""
    if value in (None, ""):
        return None
    try:
        amount = float(value)
    except (TypeError, ValueError):
        return str(value)
    if amount < 0:
        prefix = "-"
    elif signed and amount > 0:
        prefix = "+"
    else:
        prefix = ""
    return "{}${:,.2f}".format(prefix, abs(amount))


def _relationship_id(document, name):
    """(id, type) of a JSON:API to-one relationship, or (None, None)."""
    data = ((document.get("relationships") or {}).get(name) or {}).get("data") or {}
    return data.get("id"), data.get("type")


def _jobs_for(runs):
    """CloudBolt Jobs referenced by the run messages, keyed by id (one query)."""
    ids = set()
    for run in runs:
        job_id = parse_job_id_from_run_message((run.get("attributes") or {}).get("message"))
        if job_id:
            ids.add(job_id)
    if not ids:
        return {}
    return {job.id: job for job in Job.objects.filter(id__in=ids)}


def _job_is_active(job):
    try:
        return bool(job.is_active())
    except Exception:
        return False


def _run_row(run, included, jobs_by_id, client, workspace_name):
    """Display dict for one Runs-API document (plan side-loaded via ``included``)."""
    attrs = run.get("attributes") or {}
    plan_id, plan_type = _relationship_id(run, "plan")
    plan = included.get((plan_type, plan_id)) if plan_id else None
    job_id = parse_job_id_from_run_message(attrs.get("message"))
    job = jobs_by_id.get(job_id)
    klass = classify_run(run)
    style, icon = RUN_STYLES.get(klass, ("muted", "bi-circle"))
    url = ""
    if workspace_name:
        try:
            url = client.run_app_url(run.get("id"), workspace_name)
        except TFCError:
            url = ""
    return {
        "id": run.get("id"),
        "status": (attrs.get("status") or "unknown").replace("_", " "),
        "confirmable": klass == RUN_CLASS_CONFIRMABLE,
        "style": style,
        "icon": icon,
        "message": attrs.get("message") or "",
        "source": RUN_SOURCES.get(attrs.get("source"), attrs.get("source") or ""),
        "is_destroy": bool(attrs.get("is-destroy")),
        "plan_only": bool(attrs.get("plan-only")),
        "created": _when(attrs.get("created-at")),
        "plan": {
            "add": plan.get("resource-additions"),
            "change": plan.get("resource-changes"),
            "destroy": plan.get("resource-destructions"),
        } if plan else None,
        "job": job,
        "job_id": job_id,
        "job_active": _job_is_active(job) if job else False,
        "url": url,
    }


def _cost_estimate(run, included):
    """Cost-estimate summary side-loaded with a run, or None when not enabled."""
    ce_id, ce_type = _relationship_id(run, "cost-estimate")
    if not ce_id:
        return None
    attrs = included.get((ce_type, ce_id))
    if attrs is None:
        return {"status": "unavailable", "error": ""}
    return {
        "status": attrs.get("status") or "unknown",
        "prior": _money(attrs.get("prior-monthly-cost")),
        "proposed": _money(attrs.get("proposed-monthly-cost")),
        "delta": _money(attrs.get("delta-monthly-cost"), signed=True),
        "matched": attrs.get("matched-resources-count"),
        "unmatched": attrs.get("unmatched-resources-count"),
        "error": attrs.get("error-message") or "",
    }


# ---------------------------------------------------------------------------
# Tabs
# ---------------------------------------------------------------------------
class TerraformTabDelegate(TabExtensionDelegate):
    def should_display(self):
        return bool(_attr(self.instance, "tfc_workspace_id"))


def _tab_context(resource):
    workspace_name = _attr(resource, "tfc_workspace_name")
    workspace_url = ""
    if workspace_name:
        try:
            # Builds the client only (a ConnectionInfo lookup, no HTTP call).
            workspace_url = _client_for(resource).workspace_app_url(workspace_name)
        except TFCError:
            # No ConnectionInfo / token yet: the summary panel explains it.
            workspace_url = ""
    return {
        "resource": resource,
        "workspace_name": workspace_name or _attr(resource, "tfc_workspace_id"),
        "workspace_id": _attr(resource, "tfc_workspace_id"),
        "workspace_url": workspace_url,
        "organization": _attr(resource, "tfc_organization"),
        "project": _attr(resource, "tfc_project"),
        "repo_identifier": _attr(resource, "tfc_repo_identifier"),
        "branch": _attr(resource, "tfc_branch"),
        "working_directory": _attr(resource, "tfc_working_directory"),
        "nocode_module_id": _attr(resource, "tfc_nocode_module_id"),
        "nocode_module_version": _attr(resource, "tfc_nocode_module_version"),
        "last_run_url": _attr(resource, "tfc_run_url"),
    }


@tab_extension(
    model=Resource,
    title="Terraform",
    delegate=TerraformTabDelegate,
    description="HCP Terraform workspace summary, runs, and managed resources",
)
def terraform_tab(request, obj_id):
    resource = get_object_or_404(Resource, pk=obj_id)
    context = _tab_context(resource)
    context.update({
        "summary_url": reverse("hcp_tfws_summary", args=[resource.id]),
        "runs_url": reverse("hcp_tfws_runs", args=[resource.id]),
        "resources_url": reverse("hcp_tfws_resources", args=[resource.id]),
    })
    return render(request, TEMPLATES + "terraform_tab.html", context)


@tab_extension(
    model=Resource,
    title="Terraform Variables",
    delegate=TerraformTabDelegate,
    description="Read-only HCP Terraform workspace variables and variable sets",
)
def variables_tab(request, obj_id):
    resource = get_object_or_404(Resource, pk=obj_id)
    context = _tab_context(resource)
    context.update({
        "variables_url": reverse("hcp_tfws_variables", args=[resource.id]),
        "variable_sets_url": reverse("hcp_tfws_variable_sets", args=[resource.id]),
    })
    return render(request, TEMPLATES + "variables_tab.html", context)


# ---------------------------------------------------------------------------
# Panels (HTML fragments loaded asynchronously by the tabs)
# ---------------------------------------------------------------------------
def summary_panel(request, resource_id):
    """Workspace state, latest run, drift, cost estimate, and pending runs."""
    resource, profile, error = _load(request, resource_id)
    context = {"resource": resource, "warnings": []}
    if error:
        context["error"] = error
        return _panel(request, "summary_panel.html", context)

    workspace_id = _attr(resource, "tfc_workspace_id")
    try:
        client = _client_for(resource)
        workspace = client.get_workspace(workspace_id)
    except TFCError as exc:
        context["error"] = str(exc)
        return _panel(request, "summary_panel.html", context)

    attrs = workspace.get("attributes") or {}
    vcs = attrs.get("vcs-repo") or {}
    workspace_name = attrs.get("name") or _attr(resource, "tfc_workspace_name")
    context["workspace"] = {
        "name": workspace_name,
        "locked": bool(attrs.get("locked")),
        "terraform_version": attrs.get("terraform-version") or "",
        "execution_mode": attrs.get("execution-mode") or "",
        "auto_apply": bool(attrs.get("auto-apply")),
        "resource_count": attrs.get("resource-count"),
        "updated": _when(attrs.get("updated-at")),
        "latest_change": _when(attrs.get("latest-change-at")),
        "vcs_identifier": vcs.get("identifier") or "",
        "vcs_branch": vcs.get("branch") or "",
        "working_directory": attrs.get("working-directory") or "",
    }

    # Latest run, with its plan and cost estimate side-loaded.
    current_run_id, _ = _relationship_id(workspace, "current-run")
    context["current"] = None
    context["cost"] = None
    if current_run_id:
        try:
            run, included = client.get_run_with_related(current_run_id)
            context["current"] = _run_row(run, included, _jobs_for([run]), client, workspace_name)
            context["cost"] = _cost_estimate(run, included)
        except TFCError as exc:
            context["warnings"].append("Latest run unavailable: {}".format(exc))

    # Drift (health assessments; HCP Terraform Standard/Premium only).
    assessment_id, _ = _relationship_id(workspace, "current-assessment-result")
    context["assessment"] = None
    if assessment_id:
        try:
            result = client.get_assessment_result(assessment_id)
            context["assessment"] = {
                "drifted": bool(result.get("drifted")),
                "succeeded": bool(result.get("succeeded")),
                "error": result.get("error-msg") or "",
                "created": _when(result.get("created-at")),
            }
        except TFCError as exc:
            context["warnings"].append("Drift assessment unavailable: {}".format(exc))

    # Pending (non-final) runs: the day-2 fail-fast guard and teardown both
    # key off this list, so surface it with the owning CloudBolt job.
    context["pending"] = []
    can_discard = _can_discard(profile, resource)
    try:
        pending = client.list_non_final_runs(workspace_id)
    except TFCError as exc:
        pending = []
        context["warnings"].append("Pending runs unavailable: {}".format(exc))
    if pending:
        documents = [
            {"id": r["id"], "attributes": {"status": r["status"], "message": r["message"]}}
            for r in pending
        ]
        jobs_by_id = _jobs_for(documents)
        for document in documents:
            row = _run_row(document, {}, jobs_by_id, client, workspace_name)
            offer_discard = (
                can_discard
                and not row["job_active"]
                and RUN_ID_RE.match(row["id"] or "") is not None
            )
            row["discard_url"] = (
                reverse("hcp_tfws_discard_run", args=[resource.id, row["id"]])
                if offer_discard else ""
            )
            context["pending"].append(row)
    return _panel(request, "summary_panel.html", context)


def runs_panel(request, resource_id):
    """One page of the workspace's run history, newest first."""
    resource, profile, error = _load(request, resource_id)
    try:
        page = max(1, int(request.GET.get("page", 1)))
    except (TypeError, ValueError):
        page = 1
    query = (request.GET.get("q") or "").strip()[:200]
    context = {"resource": resource, "page": page, "q": query, "rows": [], "total": None}
    if error:
        context["error"] = error
        return _panel(request, "runs_panel.html", context)

    workspace_id = _attr(resource, "tfc_workspace_id")
    workspace_name = _attr(resource, "tfc_workspace_name")
    try:
        client = _client_for(resource)
        result = client.list_runs_page(
            workspace_id, page_number=page, page_size=RUNS_PAGE_SIZE, search=query or None,
        )
    except TFCError as exc:
        context["error"] = str(exc)
        return _panel(request, "runs_panel.html", context)

    runs = result["runs"]
    jobs_by_id = _jobs_for(runs)
    context["rows"] = [
        _run_row(run, result["included"], jobs_by_id, client, workspace_name) for run in runs
    ]
    total = result["total"]
    context["total"] = total
    context["has_prev"] = page > 1
    if total is not None:
        context["has_next"] = page * RUNS_PAGE_SIZE < total
    else:
        context["has_next"] = len(runs) >= RUNS_PAGE_SIZE
    base = reverse("hcp_tfws_runs", args=[resource.id])
    context["prev_url"] = "{}?{}".format(base, urlencode({"page": page - 1, "q": query}))
    context["next_url"] = "{}?{}".format(base, urlencode({"page": page + 1, "q": query}))
    return _panel(request, "runs_panel.html", context)


def resources_panel(request, resource_id):
    """Resources in the workspace's state, as TFC indexes them (no state file read)."""
    resource, profile, error = _load(request, resource_id)
    context = {"resource": resource, "rows": []}
    if error:
        context["error"] = error
        return _panel(request, "resources_panel.html", context)
    try:
        client = _client_for(resource)
        items = client.list_workspace_resources(_attr(resource, "tfc_workspace_id"))
    except TFCError as exc:
        context["error"] = str(exc)
        return _panel(request, "resources_panel.html", context)
    rows = []
    for item in items:
        attrs = item.get("attributes") or {}
        rows.append({
            "address": attrs.get("address") or attrs.get("name") or "",
            "type": attrs.get("provider-type") or "",
            "provider": attrs.get("provider") or "",
            "module": attrs.get("module") or "root",
            "updated": _when(attrs.get("updated-at")),
        })
    rows.sort(key=lambda r: (r["module"] != "root", r["module"], r["address"]))
    context["rows"] = rows
    return _panel(request, "resources_panel.html", context)


def variables_panel(request, resource_id):
    """Workspace-scoped variables. Sensitive values are never rendered."""
    resource, profile, error = _load(request, resource_id)
    context = {"resource": resource, "rows": []}
    if error:
        context["error"] = error
        return _panel(request, "variables_panel.html", context)
    try:
        client = _client_for(resource)
        records = client.list_variable_records(_attr(resource, "tfc_workspace_id"))
    except TFCError as exc:
        context["error"] = str(exc)
        return _panel(request, "variables_panel.html", context)
    rows = []
    for record in records:
        attrs = record.get("attributes") or {}
        sensitive = bool(attrs.get("sensitive"))
        value = attrs.get("value")
        rows.append({
            "key": attrs.get("key") or "",
            # Never rely on the API to blank a sensitive value: drop it here.
            "value": "" if sensitive or value is None else str(value),
            "sensitive": sensitive,
            "hcl": bool(attrs.get("hcl")),
            "category": attrs.get("category") or "",
            "description": attrs.get("description") or "",
        })
    rows.sort(key=lambda r: (r["category"] != "terraform", r["category"], r["key"].lower()))
    context["rows"] = rows
    context["terraform_count"] = sum(1 for r in rows if r["category"] == "terraform")
    context["env_count"] = sum(1 for r in rows if r["category"] == "env")
    return _panel(request, "variables_panel.html", context)


def variable_sets_panel(request, resource_id):
    """Variable sets applied to the workspace (global, project, or direct)."""
    resource, profile, error = _load(request, resource_id)
    context = {"resource": resource, "rows": []}
    if error:
        context["error"] = error
        return _panel(request, "variable_sets_panel.html", context)
    try:
        client = _client_for(resource)
        items = client.list_variable_sets(_attr(resource, "tfc_workspace_id"))
    except TFCError as exc:
        context["error"] = str(exc)
        return _panel(request, "variable_sets_panel.html", context)
    rows = []
    for item in items:
        attrs = item.get("attributes") or {}
        rows.append({
            "name": attrs.get("name") or item.get("id") or "",
            "description": attrs.get("description") or "",
            "global": bool(attrs.get("global")),
            "priority": bool(attrs.get("priority")),
            "var_count": attrs.get("var-count"),
            "project_count": attrs.get("project-count") or 0,
            "workspace_count": attrs.get("workspace-count") or 0,
            "updated": _when(attrs.get("updated-at")),
        })
    rows.sort(key=lambda r: (not r["priority"], not r["global"], r["name"].lower()))
    context["rows"] = rows
    return _panel(request, "variable_sets_panel.html", context)


# ---------------------------------------------------------------------------
# Discard a pending run (dialog)
# ---------------------------------------------------------------------------
def _dialog_message(title, text, css="text-danger"):
    return {"title": title, "content": format_html('<p class="{}">{}</p>', css, text)}


@dialog_view
def discard_run(request, resource_id, run_id):
    """Discard a run awaiting confirmation on this resource's workspace.

    Refused when the viewer lacks DISCARD_PERMISSION, when the run belongs to
    another workspace, or when a running CloudBolt job still owns the run (its
    approval pause would otherwise fail out from under the job).
    """
    resource = get_object_or_404(Resource, pk=resource_id)
    profile = request.get_user_profile()
    title = "Discard run {}".format(run_id)
    if not _can_discard(profile, resource):
        return _dialog_message(title, "You do not have permission to discard runs on this resource.")
    if not RUN_ID_RE.match(run_id or ""):
        return _dialog_message(title, "Invalid run ID.")
    workspace_id = _attr(resource, "tfc_workspace_id")
    try:
        client = _client_for(resource)
        run = client.get_run(run_id)
    except TFCError as exc:
        return _dialog_message(title, "Could not read the run from HCP Terraform: {}".format(exc))
    run_workspace_id, _ = _relationship_id(run, "workspace")
    if run_workspace_id != workspace_id:
        return _dialog_message(title, "This run does not belong to the resource's workspace.")
    attrs = run.get("attributes") or {}
    job = _jobs_for([run]).get(parse_job_id_from_run_message(attrs.get("message")))
    if job is not None and _job_is_active(job):
        return _dialog_message(
            title,
            format_html(
                'CloudBolt <a href="{}">Job {}</a> is still running and owns this run. '
                "Cancel that job instead; it discards the run itself.",
                job.get_absolute_url(), job.id,
            ),
        )

    action_url = reverse("hcp_tfws_discard_run", args=[resource.id, run_id])
    if request.method == "POST":
        try:
            client.discard_run(
                run_id, comment="Discarded from CloudBolt resource {}".format(resource.global_id),
            )
            logger.info(
                "User %s discarded HCP Terraform run %s on workspace %s (resource %s).",
                getattr(profile, "username", "?"), run_id, workspace_id, resource.global_id,
            )
            messages.success(request, "Discarded HCP Terraform run {}.".format(run_id))
        except TFCConflictError:
            messages.error(
                request,
                "Run {} is not awaiting confirmation (it may be planning or applying). "
                "Cancel it in HCP Terraform instead.".format(run_id),
            )
        except TFCError as exc:
            logger.exception("Failed to discard HCP Terraform run %s", run_id)
            messages.error(request, "Failed to discard run {}: {}".format(run_id, exc))
        return HttpResponseRedirect(request.META.get("HTTP_REFERER") or resource.get_absolute_url())

    status = (attrs.get("status") or "unknown").replace("_", " ")
    content = format_html(
        "<p>Discard run <b>{}</b> (status: {}) on workspace <b>{}</b>?</p>"
        "<p>The run's plan is thrown away and the workspace is unblocked for new "
        "runs. Nothing is changed in the cloud.</p>",
        run_id, status, _attr(resource, "tfc_workspace_name") or workspace_id,
    )
    if attrs.get("message"):
        content += format_html('<p class="text-muted"><small>{}</small></p>', attrs.get("message"))
    if job is not None:
        content += format_html(
            '<p class="text-muted"><small>Created by CloudBolt <a href="{}">Job {}</a> '
            "({}), which is no longer running.</small></p>",
            job.get_absolute_url(), job.id, getattr(job, "status", ""),
        )
    return {
        "title": title,
        "content": mark_safe(content),
        "action_url": action_url,
        "submit": "Discard",
    }
