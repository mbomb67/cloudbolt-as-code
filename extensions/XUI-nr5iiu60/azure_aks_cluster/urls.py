from django.urls import path

from . import views

# Cluster routes are azure-aks/<resource id>/...; the namespace landing-zone
# tabs (Connect, Workloads) use azure-aks/namespaces/<resource id>/...
xui_urlpatterns = [
    # HTML fragments the cluster tabs load asynchronously
    path(
        "azure-aks/<int:resource_id>/cluster/",
        views.cluster_panel,
        name="azure_aks_cluster_panel",
    ),
    path(
        "azure-aks/<int:resource_id>/node-pools/",
        views.node_pools_panel,
        name="azure_aks_node_pools_panel",
    ),
    path(
        "azure-aks/<int:resource_id>/workloads/",
        views.workloads_panel,
        name="azure_aks_workloads_panel",
    ),
    path(
        "azure-aks/<int:resource_id>/groups/",
        views.groups_panel,
        name="azure_aks_cluster_groups_panel",
    ),
    path(
        "azure-aks/namespaces/<int:resource_id>/connect/",
        views.namespace_connect_panel,
        name="azure_aks_namespace_connect_panel",
    ),
    path(
        "azure-aks/namespaces/<int:resource_id>/workloads/",
        views.namespace_workloads_panel,
        name="azure_aks_namespace_workloads_panel",
    ),
    # Dialogs that start the blueprint's resource actions as jobs
    path(
        "azure-aks/<int:resource_id>/node-pools/<str:pool_name>/scale/",
        views.scale_node_pool,
        name="azure_aks_scale_node_pool",
    ),
    path(
        "azure-aks/<int:resource_id>/groups/add/",
        views.add_cluster_groups,
        name="azure_aks_add_cluster_groups",
    ),
    path(
        "azure-aks/<int:resource_id>/groups/allow-all/",
        views.allow_all_cluster_groups,
        name="azure_aks_allow_all_cluster_groups",
    ),
    path(
        "azure-aks/<int:resource_id>/groups/<str:group_id>/remove/",
        views.remove_cluster_group,
        name="azure_aks_remove_cluster_group",
    ),
]
