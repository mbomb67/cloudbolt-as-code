"""Blueprint build item: makes sure CloudBolt can attach the pod-network NIC of a new OpenShift VM.

Works around a CloudBolt defect. After it builds an OpenShift Virtualization VM, CloudBolt looks up
the "Pod networking" network under the namespace of that VM, but it imports that network under the
namespace "default" only. On an appliance that imported its OpenShift networks after that change,
every VM built in another namespace fails at "Adjusting NICs based on provisioning parameters" with
"OVirtNetwork matching query does not exist".

Run this step before the server build items. For each OpenShift Virtualization environment chosen
in the order it adds the missing network record for that environment's namespace. It changes
nothing where the record exists, and it never fails the order: if it cannot do its work, the server
build reports the original error.

No inputs.
"""

from common.methods import set_progress
from orders.models import BlueprintOrderItem
from resourcehandlers.ovirt.models import OVirtNetwork, OVirtResourceHandler
from utilities.logger import ThreadLogger

logger = ThreadLogger(__name__)

POD_NETWORK = "Pod networking"
MAX_PARENT_JOBS = 6


def blueprint_order_item(job):
    """The order item of the blueprint order this job belongs to, or None.

    The deploy job above this one carries it; the id in this job's own context is the fallback.
    """
    current = job
    for _ in range(MAX_PARENT_JOBS):
        if current is None:
            break
        item = getattr(current, "order_item", None)
        if isinstance(item, BlueprintOrderItem):
            return item
        current = getattr(current, "parent_job", None)
    try:
        context = job.job_parameters.cast().arguments.get("context") or {}
    except Exception:
        return None
    item_id = context.get("blueprint_order_item")
    return BlueprintOrderItem.objects.filter(id=item_id).first() if item_id else None


def order_environments(job):
    """The environments chosen for the order's build items, each one once."""
    item = blueprint_order_item(job)
    if item is None:
        return []
    environments = {}
    for arguments in item.blueprintitemarguments_set.exclude(environment=None):
        environments[arguments.environment.id] = arguments.environment
    return list(environments.values())


def ensure_pod_network(environment):
    """Add the pod network record for the environment's namespace; return the namespace if added."""
    handler = environment.resource_handler.cast() if environment.resource_handler else None
    if not isinstance(handler, OVirtResourceHandler):
        return None
    # The same attribute CloudBolt reads when it attaches the NIC.
    namespace = getattr(environment, "ovirt_namespace", None)
    if not namespace:
        return None
    if OVirtNetwork.objects.filter(network=POD_NETWORK, namespace=namespace).exists():
        return None

    # Copy what CloudBolt stored for the record it imported under "default".
    imported = handler.networks.filter(network=POD_NETWORK).first()
    OVirtNetwork.objects.create(
        name=POD_NETWORK,
        network=POD_NETWORK,
        namespace=namespace,
        network_type=getattr(imported, "network_type", None) or "Masquerade",
        addressing_schema=getattr(imported, "addressing_schema", None) or "dhcp",
        resource_technology=getattr(imported, "resource_technology", None),
        tenant=handler.tenant,
    )
    return namespace


def run(job, resource=None, **kwargs):
    added = []
    try:
        environments = order_environments(job)
        for environment in environments:
            namespace = ensure_pod_network(environment)
            if namespace:
                added.append(namespace)
    except Exception as error:
        logger.exception("Could not check the pod network records")
        set_progress(f"Could not check the pod network records: {error}")
        return "SUCCESS", "Skipped the pod network check; the server builds go ahead.", ""

    if added:
        message = f"Added the pod network record for namespace {', '.join(sorted(added))}."
    elif environments:
        message = "The pod network record of every namespace in this order exists."
    else:
        message = "Found no environments in this order; nothing to check."
    set_progress(message)
    return "SUCCESS", message, ""
