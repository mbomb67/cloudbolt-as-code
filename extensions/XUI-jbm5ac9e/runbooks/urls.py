from django.urls import path

from . import action_views, archive_views, views

# The list page itself is the admin extension, served by CloudBolt at
# /extensions/admin/runbooks/workflows.
xui_urlpatterns = [
    # Editor pages
    path(
        "xui/runbooks/workflows/new/",
        views.workflow_editor,
        name="runbooks_workflow_add",
    ),
    path(
        "xui/runbooks/workflows/<str:workflow_id>/",
        views.workflow_editor,
        name="runbooks_workflow_edit",
    ),
    # Dialogs
    path(
        "xui/runbooks/dialogs/<str:workflow_id>/delete/",
        views.workflow_delete,
        name="runbooks_workflow_delete",
    ),
    # JSON API: CB Admin, session auth with CSRF
    path(
        "xui/runbooks/api/workflows/",
        views.api_workflows,
        name="runbooks_api_workflows",
    ),
    path(
        "xui/runbooks/api/workflows/<str:workflow_id>/",
        views.api_workflow,
        name="runbooks_api_workflow",
    ),
    path(
        "xui/runbooks/api/workflows/<str:workflow_id>/copy/",
        views.api_workflow_copy,
        name="runbooks_api_workflow_copy",
    ),
    path(
        "xui/runbooks/api/actions/",
        views.api_actions,
        name="runbooks_api_actions",
    ),
    path(
        "xui/runbooks/api/actions/<str:action_id>/",
        views.api_action,
        name="runbooks_api_action",
    ),
    # Actions tab. The Workflows tab is the extension's only admin page
    # (/extensions/admin/runbooks/workflows), so Admin lists Runbooks once.
    path("xui/runbooks/actions/", action_views.actions, name="runbooks_actions"),
    path(
        "xui/runbooks/actions/new/<str:kind>/",
        action_views.action_new,
        name="runbooks_action_new",
    ),
    path(
        "xui/runbooks/api/base-actions/",
        action_views.api_base_actions,
        name="runbooks_api_base_actions",
    ),
    # File Archives tab.
    path("xui/runbooks/file-archives/", archive_views.archives, name="runbooks_archives"),
    # Signed, short-lived links that VMs download from (no login). The
    # file_archives shared module builds these links from the same path.
    path(
        "xui/runbooks/file-archives/dl/<str:token>/<str:name>",
        archive_views.download,
        name="runbooks_archive_link",
    ),
    path("xui/runbooks/file-archives/settings/", archive_views.settings_edit, name="runbooks_archive_settings"),
    path("xui/runbooks/file-archives/rotate-key/", archive_views.rotate_key, name="runbooks_archive_rotate_key"),
    path("xui/runbooks/file-archives/archives/add/", archive_views.archive_edit, name="runbooks_archive_add"),
    path("xui/runbooks/file-archives/archives/<str:archive>/", archive_views.archive_detail, name="runbooks_archive"),
    path(
        "xui/runbooks/file-archives/archives/<str:archive>/edit/",
        archive_views.archive_edit,
        name="runbooks_archive_edit",
    ),
    path(
        "xui/runbooks/file-archives/archives/<str:archive>/delete/",
        archive_views.archive_delete,
        name="runbooks_archive_delete",
    ),
    path(
        "xui/runbooks/file-archives/archives/<str:archive>/upload/",
        archive_views.upload,
        name="runbooks_archive_upload",
    ),
    path(
        "xui/runbooks/file-archives/files/<str:archive>/<str:name>/",
        archive_views.file_detail,
        name="runbooks_archive_file",
    ),
    path(
        "xui/runbooks/file-archives/files/<str:archive>/<str:name>/upload/",
        archive_views.upload,
        name="runbooks_archive_upload_version",
    ),
    path(
        "xui/runbooks/file-archives/files/<str:archive>/<str:name>/delete/",
        archive_views.file_delete,
        name="runbooks_archive_file_delete",
    ),
    path(
        "xui/runbooks/file-archives/files/<str:archive>/<str:name>/<int:version>/download/",
        archive_views.version_download,
        name="runbooks_archive_version_download",
    ),
    path(
        "xui/runbooks/file-archives/files/<str:archive>/<str:name>/<int:version>/restore/",
        archive_views.version_restore,
        name="runbooks_archive_version_restore",
    ),
    path(
        "xui/runbooks/file-archives/files/<str:archive>/<str:name>/<int:version>/delete/",
        archive_views.version_delete,
        name="runbooks_archive_version_delete",
    ),
]
