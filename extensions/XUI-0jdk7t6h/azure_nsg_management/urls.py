from django.urls import path

from . import views

xui_urlpatterns = [
    path(
        "azure-nsg/<int:resource_id>/rules-json/<str:direction>/",
        views.nsg_rules_json,
        name="azure_nsg_rules_json",
    ),
    path(
        "azure-nsg/<int:resource_id>/rules/add/<str:direction>/",
        views.add_rule,
        name="azure_nsg_add_rule",
    ),
    path(
        "azure-nsg/<int:resource_id>/rules/<str:rule_name>/edit/",
        views.edit_rule,
        name="azure_nsg_edit_rule",
    ),
    path(
        "azure-nsg/<int:resource_id>/rules/<str:rule_name>/delete/",
        views.delete_rule,
        name="azure_nsg_delete_rule",
    ),
]
