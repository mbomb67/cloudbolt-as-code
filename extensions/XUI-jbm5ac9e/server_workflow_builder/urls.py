from django.urls import path

from . import action_views, views

# The list page itself is the admin extension, served by CloudBolt at
# /extensions/admin/server_workflow_builder/workflows.
xui_urlpatterns = [
    # Editor pages
    path(
        "xui/server_workflow_builder/workflows/new/",
        views.workflow_editor,
        name="server_workflow_builder_add",
    ),
    path(
        "xui/server_workflow_builder/workflows/<str:workflow_id>/",
        views.workflow_editor,
        name="server_workflow_builder_edit",
    ),
    # Dialogs
    path(
        "xui/server_workflow_builder/dialogs/<str:workflow_id>/delete/",
        views.workflow_delete,
        name="server_workflow_builder_delete",
    ),
    # JSON API: CB Admin, session auth with CSRF
    path(
        "xui/server_workflow_builder/api/workflows/",
        views.api_workflows,
        name="server_workflow_builder_api_workflows",
    ),
    path(
        "xui/server_workflow_builder/api/workflows/<str:workflow_id>/",
        views.api_workflow,
        name="server_workflow_builder_api_workflow",
    ),
    path(
        "xui/server_workflow_builder/api/workflows/<str:workflow_id>/copy/",
        views.api_workflow_copy,
        name="server_workflow_builder_api_copy",
    ),
    path(
        "xui/server_workflow_builder/api/actions/",
        views.api_actions,
        name="server_workflow_builder_api_actions",
    ),
    path(
        "xui/server_workflow_builder/api/actions/<str:action_id>/",
        views.api_action,
        name="server_workflow_builder_api_action",
    ),
    # Actions tab: the page is the admin extension
    # /extensions/admin/server_workflow_builder/actions.
    path(
        "xui/server_workflow_builder/actions/new/<str:kind>/",
        action_views.action_new,
        name="server_workflow_builder_action_new",
    ),
    path(
        "xui/server_workflow_builder/api/base-actions/",
        action_views.api_base_actions,
        name="server_workflow_builder_api_base_actions",
    ),
]
