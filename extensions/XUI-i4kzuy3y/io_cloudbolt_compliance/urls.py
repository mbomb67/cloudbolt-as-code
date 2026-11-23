from django.urls import path

from . import views

# The tabs load their panels asynchronously and every route that takes a server
# or resource id re-checks the viewer's permission on it. The upload route is the one
# route without a CloudBolt login: the signed token in its X-Oscap-Token header is the
# credential, and it is used only by the local result repository.
xui_urlpatterns = [
    # HTML fragment the tab loads
    path(
        "compliance/servers/<int:server_id>/panel/",
        views.compliance_panel,
        name="compliance_panel",
    ),
    # JSON: start a scan or a readiness check as a job
    path(
        "compliance/servers/<int:server_id>/scan/",
        views.compliance_start,
        name="compliance_start",
    ),
    # Stored result files: report, XCCDF results, ARF
    path(
        "compliance/servers/<int:server_id>/scans/<str:scan_id>/<str:kind>/",
        views.compliance_artifact,
        name="compliance_artifact",
    ),
    # HTML fragment the resource tab loads
    path(
        "compliance/resources/<int:resource_id>/panel/",
        views.compliance_resource_panel,
        name="compliance_resource_panel",
    ),
    # JSON: start a scan or a readiness check on the chosen servers of a resource, as one job
    path(
        "compliance/resources/<int:resource_id>/scan/",
        views.compliance_resource_start,
        name="compliance_resource_start",
    ),
    # Targets upload their files here with a signed token (local backend only)
    path(
        "compliance/upload/<str:scan_id>/<str:name>",
        views.compliance_upload,
        name="compliance_upload",
    ),
]
