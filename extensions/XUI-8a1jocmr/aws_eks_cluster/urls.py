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
]
