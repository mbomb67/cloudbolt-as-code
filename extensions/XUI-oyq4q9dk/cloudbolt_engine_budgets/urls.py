from django.urls import path

from . import views

xui_urlpatterns = [
    path(
        "xui/engine-budgets/<int:group_id>/state/",
        views.api_state,
        name="engine_budgets_state",
    ),
    path(
        "xui/engine-budgets/<int:group_id>/refresh/",
        views.api_refresh,
        name="engine_budgets_refresh",
    ),
    path(
        "xui/engine-budgets/<int:group_id>/register/",
        views.api_register,
        name="engine_budgets_register",
    ),
    path(
        "xui/engine-budgets/<int:group_id>/update/",
        views.api_update,
        name="engine_budgets_update",
    ),
    path(
        "xui/engine-budgets/<int:group_id>/remove/",
        views.api_remove,
        name="engine_budgets_remove",
    ),
]
