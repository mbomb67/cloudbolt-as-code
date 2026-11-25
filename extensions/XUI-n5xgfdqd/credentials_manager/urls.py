from django.urls import path

from . import views

# The page itself is the admin extension, served by CloudBolt at
# /extensions/admin/credentials_manager/credentials.
xui_urlpatterns = [
    path(
        "xui/credentials_manager/history/",
        views.history_panel,
        name="credentials_manager_history",
    ),
    path(
        "xui/credentials_manager/rows.csv",
        views.rows_csv,
        name="credentials_manager_rows_csv",
    ),
    # JSON API: CB Admin, session auth with CSRF
    path(
        "xui/credentials_manager/api/options/",
        views.api_options,
        name="credentials_manager_api_options",
    ),
    path(
        "xui/credentials_manager/api/preview/",
        views.api_preview,
        name="credentials_manager_api_preview",
    ),
    path(
        "xui/credentials_manager/api/rows/",
        views.api_rows,
        name="credentials_manager_api_rows",
    ),
    path(
        "xui/credentials_manager/api/apply/",
        views.api_apply,
        name="credentials_manager_api_apply",
    ),
]
