from django.urls import path
from . import views

xui_urlpatterns = [
    path(
        "ssm-inventory/<int:server_id>/inventory-json/",
        views.inventory_json,
        name="ssm_inventory_json",
    ),
    path(
        "ssm-inventory/<int:server_id>/patch-ec2/",
        views.patch_ec2,
        name="ssm_inventory_patch_ec2",
    ),
    path(
        "ssm-inventory/<int:server_id>/patch-json/",
        views.patch_json,
        name="ssm_patch_json",
    ),
]