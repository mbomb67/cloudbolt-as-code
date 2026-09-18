from django.urls import path
from . import views

xui_urlpatterns = [
    path(
        "azure-patches/<int:server_id>/inventory-json/",
        views.inventory_json,
        name="az_patches_inventory_json",
    ),
    path(
        "azure-patches/<int:server_id>/patches/scan/",
        views.scan_for_patches,
        name="az_scan_for_patches",
    ),
    path(
        "azure-patches/<int:server_id>/patches/apply/",
        views.apply_all_patches,
        name="az_apply_all_patches",
    ),
]