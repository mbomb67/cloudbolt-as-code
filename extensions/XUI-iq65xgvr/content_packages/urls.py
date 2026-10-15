from django.urls import path

from . import views

# The list page itself is the admin extension, served by CloudBolt at
# /extensions/admin/content_packages/packages.
xui_urlpatterns = [
    # Details page and the dependency panel it loads asynchronously
    path(
        "xui/content_packages/packages/<str:pkg_id>/",
        views.package_detail,
        name="content_packages_detail",
    ),
    path(
        "xui/content_packages/packages/<str:pkg_id>/dependencies/",
        views.dependencies_panel,
        name="content_packages_dependencies",
    ),
    # Dialogs
    path(
        "xui/content_packages/dialogs/add/",
        views.package_edit,
        name="content_packages_add",
    ),
    path(
        "xui/content_packages/dialogs/<str:pkg_id>/edit/",
        views.package_edit,
        name="content_packages_edit",
    ),
    path(
        "xui/content_packages/dialogs/<str:pkg_id>/delete/",
        views.package_delete,
        name="content_packages_delete",
    ),
    # JSON API: CB Admin, session auth with CSRF
    path(
        "xui/content_packages/api/packages/",
        views.api_packages,
        name="content_packages_api_packages",
    ),
    path(
        "xui/content_packages/api/packages/<str:pkg_id>/",
        views.api_package,
        name="content_packages_api_package",
    ),
    path(
        "xui/content_packages/api/packages/<str:pkg_id>/dependencies/",
        views.api_dependencies,
        name="content_packages_api_dependencies",
    ),
    path(
        "xui/content_packages/api/content/",
        views.api_content,
        name="content_packages_api_content",
    ),
]
