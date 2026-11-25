"""Blueprint build item: limits what can connect to a VM with a NetworkPolicy.

After this step the VM accepts the given port from the given source and nothing else, apart from
SSH from the KubeVirt API pods, which CloudBolt's own scripts need. The source is another server
tier's name, or the word router for the cluster's ingress routers. The work is in
shared_modules/SHM-5t7bom9y (openshift_vm_network).

Inputs: tier (the server build item's name), port, allowed_from (a tier name, or router).
"""

from common.methods import set_progress
from shared_modules import openshift_vm_network as vmnet
from shared_modules import vm_tier_scripts as tiers

TIER = tiers.clean("{{ tier }}")
PORT = tiers.clean("{{ port }}")
ALLOWED_FROM = tiers.clean("{{ allowed_from }}")


def run(job, resource=None, **kwargs):
    if not TIER or not ALLOWED_FROM or not PORT.isdigit():
        return "FAILURE", "", "Set this build item's Tier, its Port (a number) and Allowed From (a tier name, or router)."
    try:
        server = tiers.tier_server(resource, TIER)
        if ALLOWED_FROM.lower() == "router":
            rule, source = vmnet.from_router(PORT), "the cluster's ingress routers"
        else:
            source_server = tiers.tier_server(resource, ALLOWED_FROM)
            rule, source = vmnet.from_vm(source_server.hostname, PORT), f"VM {source_server.hostname}"
        cluster, namespace = vmnet.for_server(server)
        name = vmnet.restrict_ingress(cluster, namespace, server.hostname, [rule], vmnet.owner_labels(resource))
    except (tiers.TierError, vmnet.OpenShiftApiError) as error:
        return "FAILURE", "", str(error)

    set_progress(f"NetworkPolicy {name} in namespace {namespace} selects VM {server.hostname}")
    return "SUCCESS", f"{server.hostname} now accepts port {PORT} only from {source}.", ""
