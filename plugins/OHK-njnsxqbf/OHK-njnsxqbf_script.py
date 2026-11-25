"""Blueprint build item: publishes a VM's HTTPS port outside the cluster with an OpenShift Route.

The Route points at the Service named after the VM and re-encrypts: the router shows browsers its
own trusted certificate and opens a second TLS connection to the VM. The URL is stored on the
resource as a link (web_app_url). The work is in shared_modules/SHM-5t7bom9y (openshift_vm_network).

Inputs: tier (the server build item's name, default Web), port (default 443).
"""

from common.methods import set_progress
from shared_modules import openshift_vm_network as vmnet
from shared_modules import vm_tier_scripts as tiers

TIER = tiers.clean("{{ tier }}", "Web")
PORT = tiers.clean("{{ port }}", "443")


def run(job, resource=None, **kwargs):
    if not PORT.isdigit():
        return "FAILURE", "", f"Port '{PORT}' must be a number."
    try:
        server = tiers.tier_server(resource, TIER)
        cluster, namespace = vmnet.for_server(server)
        host = vmnet.publish_route(cluster, namespace, server.hostname, PORT, vmnet.owner_labels(resource))
    except (tiers.TierError, vmnet.OpenShiftApiError) as error:
        return "FAILURE", "", str(error)

    url = f"https://{host}/"
    tiers.remember(resource, "web_app_url", url, "Application URL", "URL", show=True)
    # Kept so teardown can still find the namespace if the VMs are deleted first.
    tiers.remember(resource, "web_app_namespace", namespace, "OpenShift Namespace", show=True)
    tiers.remember(
        resource, "web_app_openshift_rh_id", server.resource_handler.id, "OpenShift Resource Handler ID", "INT"
    )
    set_progress(f"Route {server.hostname} in namespace {namespace} publishes {url}")
    return "SUCCESS", f"{server.hostname} port {PORT} is published at {url}", ""
