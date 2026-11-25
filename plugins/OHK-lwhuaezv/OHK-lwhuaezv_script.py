"""Blueprint build item: installs Apache httpd with mod_ssl on a server tier, ready to proxy.

Runs on the VM through the OpenShift API tunnel (shared_modules/SHM-wkubiao7). mod_ssl starts with
the self-signed certificate it generates for itself; a later step replaces it.

Inputs: tier (the server build item's name, default Web).
"""

from common.methods import set_progress
from shared_modules import vm_tier_scripts as tiers

TIER = tiers.clean("{{ tier }}", "Web")


def run(job, resource=None, **kwargs):
    try:
        server = tiers.tier_server(resource, TIER)
    except tiers.TierError as error:
        return "FAILURE", "", str(error)

    script = (
        tiers.ENABLE_PACKAGE_REPOS
        + "sudo dnf -y install httpd mod_ssl\n"
        # Lets httpd open the connection to the server it proxies to.
        + "sudo setsebool -P httpd_can_network_connect 1\n"
        + "sudo systemctl enable httpd\n"
        + "sudo systemctl restart httpd\n"
        + "echo '== Installed'\n"
        + "rpm -q httpd mod_ssl\n"
        + 'echo "httpd is $(systemctl is-active httpd)"\n'
        + "sudo ss -ltn | grep ':443 '\n"
    )

    set_progress(f"Installing Apache with mod_ssl on {server.hostname}")
    try:
        tiers.run(server, script)
    except tiers.TierError as error:
        return "FAILURE", "", str(error)
    return "SUCCESS", f"Apache is serving HTTPS on {server.hostname}.", ""
