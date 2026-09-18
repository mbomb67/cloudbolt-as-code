from django.shortcuts import render

from extensions.views import tab_extension, admin_extension, \
    TabExtensionDelegate
from infrastructure.models import Server
from utilities.decorators import json_view
from utilities.middleware import login_not_required

SERVER_TAB_TEMPLATE = "io_cloudbolt_prometheus/templates/server.html"
ADMIN_TEMPLATE = "io_cloudbolt_prometheus/templates/admin.html"
DEFAULT_EXPORTER_PORT = 9100
DEFAULT_WINDOWS_EXPORTER_PORT = 9182


class ServerTabDelegate(TabExtensionDelegate):

    def should_display(self):
        return "monitor" in self.instance.labels


@tab_extension(model=Server, title="Monitoring", delegate=ServerTabDelegate)
def server_view(request, server_id, **kwargs):
    return render(request, SERVER_TAB_TEMPLATE, context={
        "server_id": server_id
    })


@admin_extension(title="Monitoring Admin")
def admin_view(request, **kwargs):
    return render(request, ADMIN_TEMPLATE, context={
        "docstring": ""
    })


@json_view
@login_not_required
def prometheus_targets(request, **kwargs):
    config = []
    active_servers = Server.objects.filter(
        status='ACTIVE', power_status="POWERON")

    group_targets = dict()
    server: Server
    for server in active_servers:
        if "monitor" in server.labels and server.ip:
            if server.os_family.name == "Windows":
                port = DEFAULT_WINDOWS_EXPORTER_PORT
            else:
                port = DEFAULT_EXPORTER_PORT
            group = server.group.global_id
            environment = server.environment.global_id
            rh = server.environment.resource_handler.global_id
            nic = server.nics.first()
            ip_address = nic.private_ip if nic.private_ip else server.ip

            config.append(
                {
                    "labels": {
                        "group": group,
                        "environment": environment,
                        "server": f"{server.id}",
                        "name": f"{server.hostname}",
                        "rh": rh,
                        "job": "cloudbolt"
                    },
                    "targets": [
                        f"{ip_address}:{port}"
                    ]
                }
            )

    return config


