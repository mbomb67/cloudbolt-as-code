from django.urls import path

from xui.io_cloudbolt_prometheus.metrics import server_metrics_dispatch
from xui.io_cloudbolt_prometheus.views import prometheus_targets

xui_urlpatterns = [
    path("xui/io_cloudbolt_prometheus/api/servers/<int:server_id>/<slug:metric_type>/",
         server_metrics_dispatch),
    path("xui/io_cloudbolt_prometheus/api/targets/", prometheus_targets)
]

