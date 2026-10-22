from django.urls import path

from . import views

# Admins also reach the page as the admin extension at
# /extensions/admin/mcp_agent_activity/agent_activity.
xui_urlpatterns = [
    path("xui/mcp-agent-activity/", views.agent_activity_page, name="mcp_agent_activity_page"),
    path(
        "xui/mcp-agent-activity/sessions/<str:session>/",
        views.session_page,
        name="mcp_agent_activity_session",
    ),
    path("xui/mcp-agent-activity/api/search/", views.api_search, name="mcp_agent_activity_search"),
    path("xui/mcp-agent-activity/api/facets/", views.api_facets, name="mcp_agent_activity_facets"),
    path("xui/mcp-agent-activity/api/recent-sessions/", views.api_recent, name="mcp_agent_activity_recent"),
    path(
        "xui/mcp-agent-activity/api/sections/<str:section>/",
        views.api_section,
        name="mcp_agent_activity_section",
    ),
    path(
        "xui/mcp-agent-activity/api/sessions/<str:session>/log/",
        views.api_session_log,
        name="mcp_agent_activity_log",
    ),
    path(
        "xui/mcp-agent-activity/export/agent-activity.xlsx",
        views.export_xlsx,
        name="mcp_agent_activity_xlsx",
    ),
    path(
        "xui/mcp-agent-activity/export/<str:section>.csv",
        views.export_csv,
        name="mcp_agent_activity_export",
    ),
]
