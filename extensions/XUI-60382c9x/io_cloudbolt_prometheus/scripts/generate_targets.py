import json

if __name__ == '__main__':
    import django

    django.setup()

from infrastructure.models import Server

DEFAULT_EXPORTER_PORT = 9100
DEFAULT_WINDOWS_EXPORTER_PORT = 9182


def generate_targets():
    config = []
    active_servers = Server.objects.filter(status='ACTIVE',
                                           power_status="POWERON")

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
                        f"{server.ip}:{port}"
                    ]
                }
            )
    return config


if __name__ == '__main__':
    targets = generate_targets()
    print(json.dumps(targets, indent=True))
