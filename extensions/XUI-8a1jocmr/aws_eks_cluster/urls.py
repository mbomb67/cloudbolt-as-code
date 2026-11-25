from django.urls import path

from . import views

xui_urlpatterns = [
    # HTML fragments the tabs load asynchronously
    path(
        "aws-eks/<int:resource_id>/cluster/",
        views.cluster_panel,
        name="aws_eks_cluster_panel",
    ),
    path(
        "aws-eks/<int:resource_id>/addons/",
        views.addons_panel,
        name="aws_eks_addons_panel",
    ),
    path(
        "aws-eks/<int:resource_id>/nodegroups/",
        views.nodegroups_panel,
        name="aws_eks_nodegroups_panel",
    ),
    path(
        "aws-eks/<int:resource_id>/workloads/",
        views.workloads_panel,
        name="aws_eks_workloads_panel",
    ),
    path(
        "aws-eks/<int:resource_id>/groups/",
        views.groups_panel,
        name="aws_eks_groups_panel",
    ),
    path(
        "aws-eks/namespaces/<int:resource_id>/connect/",
        views.namespace_connect_panel,
        name="aws_eks_namespace_connect_panel",
    ),
    path(
        "aws-eks/namespaces/<int:resource_id>/workloads/",
        views.namespace_workloads_panel,
        name="aws_eks_namespace_workloads_panel",
    ),
    # Dialogs that start the blueprint's resource actions as jobs
    path(
        "aws-eks/<int:resource_id>/addons/<str:addon_name>/upgrade/",
        views.upgrade_addon,
        name="aws_eks_upgrade_addon",
    ),
    path(
        "aws-eks/<int:resource_id>/nodegroups/<str:nodegroup_name>/scale/",
        views.scale_nodegroup,
        name="aws_eks_scale_nodegroup",
    ),
    path(
        "aws-eks/<int:resource_id>/nodegroups/<str:nodegroup_name>/delete/",
        views.delete_nodegroup,
        name="aws_eks_delete_nodegroup",
    ),
    # Dialogs that change the groups on the cluster's environment
    path(
        "aws-eks/<int:resource_id>/groups/add/",
        views.add_groups,
        name="aws_eks_add_groups",
    ),
    path(
        "aws-eks/<int:resource_id>/groups/allow-all/",
        views.allow_all_groups,
        name="aws_eks_allow_all_groups",
    ),
    path(
        "aws-eks/<int:resource_id>/groups/<str:group_id>/remove/",
        views.remove_group,
        name="aws_eks_remove_group",
    ),
]
