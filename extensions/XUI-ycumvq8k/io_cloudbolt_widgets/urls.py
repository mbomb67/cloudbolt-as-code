from django.urls import path

from xui.io_cloudbolt_widgets.api import fetch_env_data

API_BASE = "xui/io_cloudbolt_widgets/api"

xui_urlpatterns = [
    path(f"{API_BASE}/envs", fetch_env_data),
]
