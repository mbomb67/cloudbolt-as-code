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
]
