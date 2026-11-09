from django.urls import path

from . import views

# HTML fragments the StormForge tabs load asynchronously; every route takes
# the cluster resource's id and re-checks the viewer's permission on it.
xui_urlpatterns = [
    path("stormforge/<int:resource_id>/cluster/", views.sfx_cluster_panel, name="sfx_cluster_panel"),
    path("stormforge/<int:resource_id>/nodes/", views.sfx_nodes_panel, name="sfx_nodes_panel"),
    path("stormforge/<int:resource_id>/namespace/", views.sfx_namespace_panel, name="sfx_namespace_panel"),
    path("stormforge/<int:resource_id>/workload/", views.sfx_workload_panel, name="sfx_workload_panel"),
    path("stormforge/<int:resource_id>/patches/", views.sfx_patches_panel, name="sfx_patches_panel"),
    path("stormforge/<int:resource_id>/agent/", views.sfx_agent_panel, name="sfx_agent_panel"),
    path("stormforge/<int:resource_id>/agent/log/", views.sfx_agent_log, name="sfx_agent_log"),
    path("stormforge/<int:resource_id>/namespaces/", views.sfx_namespaces_table, name="sfx_namespaces_table"),
    path("stormforge/<int:resource_id>/workloads/", views.sfx_workloads_table, name="sfx_workloads_table"),
]
