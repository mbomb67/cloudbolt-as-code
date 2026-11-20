"""Run setup scripts on the server tiers of a blueprint's resource, over the OpenShift API tunnel.

A plug-in build item gets the resource, not a server, so each one names the server tier it works
on (the name of a server build item in the same blueprint) and this module finds that tier's VM.
Scripts travel through kubevirt_api_tunnel, so CloudBolt needs no route to the guests.

    from shared_modules import vm_tier_scripts as tiers

    server = tiers.tier_server(resource, "DB")
    output = tiers.run(server, tiers.ENABLE_PACKAGE_REPOS + "sudo dnf -y install mariadb-server")
    script = tiers.write_file("/etc/app/config.json", text, mode="0640", group="apache")

tier_server and run raise TierError with a message that can go straight to the job's error field.
"""

import base64

from common.methods import set_progress
from shared_modules import kubevirt_api_tunnel


class TierError(Exception):
    """A tier has no VM, or a script failed on it."""


def clean(value, default=""):
    """A plug-in input as text: an input left empty renders as an empty string or the word None."""
    value = (value or "").strip()
    return default if value in ("", "None") else value


def tier_servers(resource, tier):
    """The active servers a server build item built for this resource, by the build item's name."""
    return list(
        resource.server_set.filter(status="ACTIVE", service_item__name=tier).order_by("hostname")
    )


def tier_server(resource, tier):
    """The one VM of a tier. More than one is refused: these steps configure a single node."""
    if resource is None:
        raise TierError("This step runs as a blueprint build item and found no resource to work on.")
    servers = tier_servers(resource, tier)
    if not servers:
        built = sorted({s.service_item.name for s in resource.server_set.all() if s.service_item})
        raise TierError(
            f"The resource has no active server from a build item named '{tier}'. "
            f"Server build items that built servers: {', '.join(built) or 'none'}."
        )
    if len(servers) > 1:
        raise TierError(f"Tier '{tier}' has {len(servers)} servers; this step supports one.")
    return servers[0]


# Every script starts here: stop at the first failing command, and let first-boot configuration
# finish so a package install never collides with it. cloud-init exits non-zero for a degraded but
# finished boot, which is no reason to stop.
PREAMBLE = """set -euo pipefail
if command -v cloud-init >/dev/null 2>&1; then sudo cloud-init status --wait >/dev/null 2>&1 || true; fi
"""

# RHEL guests that are not registered with Red Hat have no package repositories. CentOS Stream 9
# carries the same package set, so its BaseOS and AppStream are added when dnf has no repository.
# A registered guest already has repositories and is left alone. Mixing Stream packages into RHEL is
# fine for a lab and unsupported for production: register the guest instead.
# Repository layout and keys: https://www.centos.org/download/mirrors/ https://www.centos.org/keys/
ENABLE_PACKAGE_REPOS = """if [ -z "$(sudo dnf -q repolist --enabled 2>/dev/null)" ]; then
  echo "No package repositories on this guest; adding CentOS Stream 9 BaseOS and AppStream."
  sudo tee /etc/yum.repos.d/centos-stream.repo >/dev/null <<'REPO'
[cs9-baseos]
name=CentOS Stream 9 - BaseOS
baseurl=https://mirror.stream.centos.org/9-stream/BaseOS/$basearch/os/
gpgcheck=1
gpgkey=https://www.centos.org/keys/RPM-GPG-KEY-CentOS-Official https://www.centos.org/keys/RPM-GPG-KEY-CentOS-Official-SHA256
enabled=1

[cs9-appstream]
name=CentOS Stream 9 - AppStream
baseurl=https://mirror.stream.centos.org/9-stream/AppStream/$basearch/os/
gpgcheck=1
gpgkey=https://www.centos.org/keys/RPM-GPG-KEY-CentOS-Official https://www.centos.org/keys/RPM-GPG-KEY-CentOS-Official-SHA256
enabled=1
REPO
fi
"""


def write_file(path, content, mode="0644", owner="root", group="root"):
    """Shell that writes text to a root-owned file on the guest.

    The text travels base64-encoded, so no quoting in it can break the script and a secret in it
    never appears in a process list.
    """
    encoded = base64.b64encode(content.encode()).decode()
    return (
        f"sudo install -d -m 0755 \"$(dirname '{path}')\"\n"
        f"echo '{encoded}' | base64 -d | sudo install -m {mode} -o {owner} -g {group} /dev/stdin '{path}'\n"
    )


def run(server, script, timeout=1800):
    """Run a script on a tier's VM and log what it printed; return the output.

    The script itself is never logged, because it can carry a password.
    """
    set_progress(f"Opening the API tunnel to {server.hostname}")
    try:
        exit_code, output = kubevirt_api_tunnel.run_script(server, PREAMBLE + script, timeout=timeout)
    except kubevirt_api_tunnel.TunnelError as error:
        raise TierError(str(error))
    set_progress(output or "(the script printed nothing)")
    if exit_code:
        last_lines = "\n".join(output.strip().splitlines()[-15:])
        raise TierError(f"The script on {server.hostname} exited with code {exit_code}:\n{last_lines}")
    return output


def remember(resource, name, value, label, cf_type="STR", show=False, description=""):
    """Store a value on the resource as a parameter, creating the parameter the first time.

    Build items that run in parallel can both try to create the same parameter; the loser of that
    race finds it already there.
    """
    from django.db import IntegrityError
    from infrastructure.models import CustomField

    defaults = dict(
        label=label, type=cf_type, description=description, show_on_servers=False, show_as_attribute=show
    )
    try:
        CustomField.objects.get_or_create(name=name, defaults=defaults)
    except IntegrityError:
        CustomField.objects.get(name=name)
    resource.set_value_for_custom_field(name, value)
