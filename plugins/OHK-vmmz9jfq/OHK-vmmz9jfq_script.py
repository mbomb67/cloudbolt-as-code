"""Blueprint build item: gives a VM's port a stable in-cluster name with a ClusterIP Service.

The Service is named after the VM and selects it by label, so other tiers reach it at
<vm>.<namespace>.svc.cluster.local however often the VM's pod IP changes. It is labelled with the
CloudBolt resource so the teardown step can find it. The work is in
shared_modules/SHM-5t7bom9y (openshift_vm_network).

Inputs: tier (the server build item's name), port.
"""

from common.methods import set_progress
from shared_modules import openshift_vm_network as vmnet
from shared_modules import vm_tier_scripts as tiers

TIER = tiers.clean("{{ tier }}")
PORT = tiers.clean("{{ port }}")


def run(job, resource=None, **kwargs):
    if not TIER or not PORT.isdigit():
        return "FAILURE", "", "Set this build item's Tier to a server build item's name and its Port to a number."
    try:
        server = tiers.tier_server(resource, TIER)
        cluster, namespace = vmnet.for_server(server)
        fqdn = vmnet.expose_port(cluster, namespace, server.hostname, PORT, vmnet.owner_labels(resource))
    except (tiers.TierError, vmnet.OpenShiftApiError) as error:
        return "FAILURE", "", str(error)

    set_progress(f"Service {server.hostname} in namespace {namespace} selects VM {server.hostname} on port {PORT}")
    return "SUCCESS", f"{server.hostname} port {PORT} is reachable in the cluster at {fqdn}:{PORT}", ""
