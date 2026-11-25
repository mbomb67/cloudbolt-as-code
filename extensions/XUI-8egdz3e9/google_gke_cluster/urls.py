from django.urls import path

from . import views

# Cluster routes are google-gke/<resource id>/...; the namespace landing-zone
# tabs (Connect, Workloads) use google-gke/namespaces/<resource id>/...
xui_urlpatterns = [
    # HTML fragments the cluster tabs load asynchronously
    path(
        "google-gke/<int:resource_id>/cluster/",
        views.cluster_panel,
        name="google_gke_cluster_panel",
    ),
    path(
        "google-gke/<int:resource_id>/node-pools/",
        views.node_pools_panel,
        name="google_gke_node_pools_panel",
    ),
    path(
        "google-gke/<int:resource_id>/workloads/",
        views.workloads_panel,
        name="google_gke_workloads_panel",
    ),
    path(
        "google-gke/<int:resource_id>/groups/",
        views.groups_panel,
        name="google_gke_cluster_groups_panel",
    ),
    path(
        "google-gke/namespaces/<int:resource_id>/connect/",
        views.namespace_connect_panel,
        name="google_gke_namespace_connect_panel",
    ),
    path(
        "google-gke/namespaces/<int:resource_id>/workloads/",
        views.namespace_workloads_panel,
        name="google_gke_namespace_workloads_panel",
    ),
    # Dialogs that start the blueprint's resource actions as jobs
    path(
        "google-gke/<int:resource_id>/node-pools/<str:pool_name>/scale/",
        views.scale_node_pool,
        name="google_gke_scale_node_pool",
    ),
    path(
        "google-gke/<int:resource_id>/groups/add/",
        views.add_cluster_groups,
        name="google_gke_add_cluster_groups",
    ),
    path(
        "google-gke/<int:resource_id>/groups/allow-all/",
        views.allow_all_cluster_groups,
        name="google_gke_allow_all_cluster_groups",
    ),
    path(
        "google-gke/<int:resource_id>/groups/<str:group_id>/remove/",
        views.remove_cluster_group,
        name="google_gke_remove_cluster_group",
    ),
]
