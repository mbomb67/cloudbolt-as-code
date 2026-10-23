from django.urls import path

from . import views

xui_urlpatterns = [
    path(
        "aws-sg/<int:resource_id>/rules-panel/",
        views.sg_rules_panel,
        name="aws_sg_rules_panel",
    ),
    path(
        "aws-sg/<int:resource_id>/rules/add/<str:direction>/",
        views.add_rule,
        name="aws_sg_add_rule",
    ),
    path(
        "aws-sg/<int:resource_id>/rules/<str:rule_id>/edit/",
        views.edit_rule,
        name="aws_sg_edit_rule",
    ),
    path(
        "aws-sg/<int:resource_id>/rules/<str:rule_id>/delete/",
        views.delete_rule,
        name="aws_sg_delete_rule",
    ),
]
