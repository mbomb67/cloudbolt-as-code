from django.urls import path

from . import views

xui_urlpatterns = [
    # Signed, short-lived links that servers download from (no login).
    path("xui/file_archives/dl/<str:token>/<str:name>", views.download, name="file_archives_download"),
    path("xui/file_archives/settings/", views.settings_edit, name="file_archives_settings"),
    path("xui/file_archives/rotate-key/", views.rotate_key, name="file_archives_rotate_key"),
    path("xui/file_archives/archives/add/", views.archive_edit, name="file_archives_archive_add"),
    path("xui/file_archives/archives/<str:archive>/", views.archive_detail, name="file_archives_archive"),
    path("xui/file_archives/archives/<str:archive>/edit/", views.archive_edit, name="file_archives_archive_edit"),
    path("xui/file_archives/archives/<str:archive>/delete/", views.archive_delete, name="file_archives_archive_delete"),
    path("xui/file_archives/archives/<str:archive>/upload/", views.upload, name="file_archives_upload"),
    path("xui/file_archives/files/<str:archive>/<str:name>/", views.file_detail, name="file_archives_file"),
    path("xui/file_archives/files/<str:archive>/<str:name>/upload/", views.upload, name="file_archives_upload_version"),
    path("xui/file_archives/files/<str:archive>/<str:name>/delete/", views.file_delete, name="file_archives_file_delete"),
    path(
        "xui/file_archives/files/<str:archive>/<str:name>/<int:version>/download/",
        views.version_download,
        name="file_archives_version_download",
    ),
    path(
        "xui/file_archives/files/<str:archive>/<str:name>/<int:version>/restore/",
        views.version_restore,
        name="file_archives_version_restore",
    ),
    path(
        "xui/file_archives/files/<str:archive>/<str:name>/<int:version>/delete/",
        views.version_delete,
        name="file_archives_version_delete",
    ),
]
