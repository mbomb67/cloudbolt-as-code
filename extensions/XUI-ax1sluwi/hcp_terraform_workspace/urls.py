from django.urls import path

from . import views

xui_urlpatterns = [
    path(
        "hcp-terraform/<int:resource_id>/summary/",
        views.summary_panel,
        name="hcp_tfws_summary",
    ),
    path(
        "hcp-terraform/<int:resource_id>/runs/",
        views.runs_panel,
        name="hcp_tfws_runs",
    ),
    path(
        "hcp-terraform/<int:resource_id>/resources/",
        views.resources_panel,
        name="hcp_tfws_resources",
    ),
    path(
        "hcp-terraform/<int:resource_id>/variables/",
        views.variables_panel,
        name="hcp_tfws_variables",
    ),
    path(
        "hcp-terraform/<int:resource_id>/variable-sets/",
        views.variable_sets_panel,
        name="hcp_tfws_variable_sets",
    ),
    path(
        "hcp-terraform/<int:resource_id>/runs/<str:run_id>/discard/",
        views.discard_run,
        name="hcp_tfws_discard_run",
    ),
]
