"""The tabs are auto-routed by @tab_extension; only the .xlsx download needs a route."""

from django.urls import re_path

from xui.technology_reference import views

xui_urlpatterns = [
    re_path(
        r"^technology_reference/(?P<scope>handler|environment)/(?P<obj_id>\d+)/export/$",
        views.technology_reference_export,
        name="technology_reference_export",
    ),
]
