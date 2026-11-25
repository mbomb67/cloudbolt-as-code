"""Blueprint build item: makes the web tier's Apache a reverse proxy for another tier.

The proxy target is the backend tier's Service name, never an IP address, because a VM's pod IP
changes when it restarts. Runs through the OpenShift API tunnel (shared_modules/SHM-wkubiao7).

Inputs: tier (default Web), backend_tier (default App), backend_port (default 8080).
"""

from common.methods import set_progress
from shared_modules import openshift_vm_network as vmnet
from shared_modules import vm_tier_scripts as tiers

TIER = tiers.clean("{{ tier }}", "Web")
BACKEND_TIER = tiers.clean("{{ backend_tier }}", "App")
BACKEND_PORT = tiers.clean("{{ backend_port }}", "8080")
CONF_PATH = "/etc/httpd/conf.d/zz-reverse-proxy.conf"


def run(job, resource=None, **kwargs):
    if not BACKEND_PORT.isdigit():
        return "FAILURE", "", f"Backend port '{BACKEND_PORT}' must be a number."
    try:
        server = tiers.tier_server(resource, TIER)
        backend = tiers.tier_server(resource, BACKEND_TIER)
    except tiers.TierError as error:
        return "FAILURE", "", str(error)

    target = f"http://{vmnet.service_fqdn(backend.hostname, backend.ovirtserverinfo.namespace)}:{BACKEND_PORT}/"
    # Directives in the main server are inherited by the TLS virtual host (ProxyPassInherit is on
    # by default): https://httpd.apache.org/docs/2.4/mod/mod_proxy.html#proxypassinherit
    conf = (
        "# Written by CloudBolt. Do not edit: the next deployment step replaces it.\n"
        "ProxyPreserveHost On\n"
        f"ProxyPass / {target}\n"
        f"ProxyPassReverse / {target}\n"
    )
    script = (
        tiers.write_file(CONF_PATH, conf)
        + "sudo apachectl configtest\n"
        + "sudo systemctl reload httpd\n"
        + "sleep 2\n"
        + "code=$(curl -sk -o /dev/null --max-time 20 -w '%{http_code}' https://localhost/)\n"
        + f"echo \"https://localhost/ now answers $code through {target}\"\n"
        # The backend may still be deploying its app in a parallel step, so any answer that came
        # from it counts; 502 and 503 mean the proxy could not reach it.
        + "[ \"$code\" -ge 200 ] && [ \"$code\" -lt 500 ]\n"
    )

    set_progress(f"Pointing Apache on {server.hostname} at {target}")
    try:
        tiers.run(server, script)
    except tiers.TierError as error:
        return "FAILURE", "", str(error)
    return "SUCCESS", f"{server.hostname} proxies HTTPS requests to {target}", ""
