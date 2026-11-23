from django.urls import path

from . import views

# The tabs load their panels asynchronously and every route that takes a server
# or resource id re-checks the viewer's permission on it. The upload route is the one
# route without a CloudBolt login: the signed token in its X-Oscap-Token header is the
# credential, and it is used only by the local result repository.
xui_urlpatterns = [
    # HTML fragment the tab loads
    path(
        "openscap/servers/<int:server_id>/panel/",
        views.openscap_panel,
        name="openscap_panel",
    ),
    # JSON: start a scan or a readiness check as a job
    path(
        "openscap/servers/<int:server_id>/scan/",
        views.openscap_start,
        name="openscap_start",
    ),
    # Stored result files: report, XCCDF results, ARF
    path(
        "openscap/servers/<int:server_id>/scans/<str:scan_id>/<str:kind>/",
        views.openscap_artifact,
        name="openscap_artifact",
    ),
    # HTML fragment the resource tab loads
    path(
        "openscap/resources/<int:resource_id>/panel/",
        views.openscap_resource_panel,
        name="openscap_resource_panel",
    ),
    # JSON: start a scan or a readiness check on the chosen servers of a resource, as one job
    path(
        "openscap/resources/<int:resource_id>/scan/",
        views.openscap_resource_start,
        name="openscap_resource_start",
    ),
    # Targets upload their files here with a signed token (local backend only)
    path(
        "openscap/upload/<str:scan_id>/<str:name>",
        views.openscap_upload,
        name="openscap_upload",
    ),
]
