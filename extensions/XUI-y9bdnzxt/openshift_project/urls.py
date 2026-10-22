from django.urls import path

from . import views

# HTML fragments the project tabs (Connect, Workloads) load asynchronously.
xui_urlpatterns = [
    path(
        "openshift-project/<int:resource_id>/connect/",
        views.connect_panel,
        name="openshift_project_connect_panel",
    ),
    path(
        "openshift-project/<int:resource_id>/workloads/",
        views.workloads_panel,
        name="openshift_project_workloads_panel",
    ),
]
