"""
Day-2 action for the Azure Kubernetes Service blueprint: create a namespace
landing zone on this cluster for a team, from the cluster resource.

The self-service path is the AKS Namespace blueprint, where a team orders a
namespace on a cluster it is entitled to. This action is the platform-admin
path: whoever may run actions on the cluster resource picks the team's
CloudBolt group, the tier, and the network policies, and the namespace is
filed as a resource of the AKS Namespace blueprint owned by that group and
nested under the cluster resource, so it gets the same resource type, day-2
actions, discovery, and teardown as an ordered one and shows on the
cluster's page. The work itself is ``aks_namespace.create_landing_zone``,
shared with the blueprint's build plugin: an AKS managed namespace with the
tier's quota and the chosen ingress and egress policies, a storage and pod
quota and default container limits on the cluster, and a CloudBolt
Environment pinned to the namespace and entitled to the group.
  https://learn.microsoft.com/en-us/azure/aks/concepts-managed-namespaces

Expected Action Inputs (declared in OHK-kb4pv2yq_metadata.json):
  - aks_ns_name (STR, required)           : DNS-1123 namespace name
  - aks_ns_owner_group_id (INT, required) : CloudBolt Group that owns the namespace
  - aks_ns_size_tier (STR, required)      : small | medium | large | xlarge
  - aks_ns_ingress_policy (STR, required) : AllowSameNamespace | AllowAll | DenyAll
  - aks_ns_egress_policy (STR, required)  : AllowAll | AllowSameNamespace | DenyAll

Unlike an order, the action has no expiration date and grants no Microsoft
Entra access; the namespace resource can be given both afterwards.
"""

from accounts.models import Group
from common.methods import set_progress
from shared_modules.aks_namespace import (
    CF_PREFIX,
    NETWORK_POLICY_CHOICES,
    create_landing_zone,
    ensure_custom_fields,
    get_tier,
    load_orchestrator,
    namespace_blueprint,
    network_policy_options,
    new_namespace_resource,
    orchestrator_id_for_cluster,
    tier_options,
    validate_namespace_name,
)
from shared_modules.azure_aks import is_aks_cluster
from utilities.logger import ThreadLogger

logger = ThreadLogger(__name__)


def generate_options_for_aks_ns_size_tier(field, **kwargs):
    """Tier dropdown; smallest tier preselected."""
    return tier_options(initial="small")


def generate_options_for_aks_ns_ingress_policy(field, **kwargs):
    """Ingress policy; traffic from the same namespace only, preselected."""
    return network_policy_options("AllowSameNamespace")


def generate_options_for_aks_ns_egress_policy(field, **kwargs):
    """Egress policy; all outbound traffic allowed, preselected."""
    return network_policy_options("AllowAll")


def generate_options_for_aks_ns_owner_group_id(field, **kwargs):
    """Every real CloudBolt Group; the chosen one owns the namespace and is entitled to it."""
    groups = Group.objects.exclude(name__startswith="Unassigned").order_by("name")
    return {"options": [(g.id, g.name) for g in groups], "sort": False}


def _text(value):
    """A rendered input as plain text; an input left empty may render as 'None'."""
    text = str(value or "").strip()
    return "" if text == "None" else text


def _owner_username(job):
    owner = getattr(job, "owner", None)
    user = getattr(owner, "user", None)
    return getattr(user, "username", "") or str(owner or "")


def run(job, resource=None, **kwargs):
    """Create a namespace landing zone on this cluster for the chosen group."""
    cluster = resource
    if cluster is None or not is_aks_cluster(cluster):
        return "FAILURE", "", "Run Create AKS Namespace on an AKS Kubernetes Cluster resource."
    set_progress(f"Creating a namespace landing zone on cluster '{cluster.name}'...")

    ensure_custom_fields()

    namespace = _text("{{ aks_ns_name }}").lower()
    tier_key = _text("{{ aks_ns_size_tier }}").lower()
    ingress = _text("{{ aks_ns_ingress_policy }}") or "AllowSameNamespace"
    egress = _text("{{ aks_ns_egress_policy }}") or "AllowAll"
    group_id_str = _text("{{ aks_ns_owner_group_id }}")

    # ---- Validate inputs ------------------------------------------------
    name_error = validate_namespace_name(namespace)
    if name_error:
        return "FAILURE", "", name_error
    try:
        get_tier(tier_key)
    except ValueError as exc:
        return "FAILURE", "", str(exc)
    for label, policy in (("Ingress", ingress), ("Egress", egress)):
        if policy not in NETWORK_POLICY_CHOICES:
            return (
                "FAILURE",
                "",
                f"{label} policy '{policy}' is not one of {', '.join(NETWORK_POLICY_CHOICES)}.",
            )
    if not group_id_str:
        return "FAILURE", "", "An owning CloudBolt group is required."
    try:
        group = Group.objects.filter(id=int(group_id_str)).first()
    except ValueError:
        group = None
    if group is None:
        return "FAILURE", "", f"Group id={group_id_str} not found."

    orchestrator = load_orchestrator(orchestrator_id_for_cluster(cluster))
    if orchestrator is None:
        return (
            "FAILURE",
            "",
            f"Cluster '{cluster.name}' has no Kubernetes container orchestrator "
            "(container_orchestrator_id); run the Azure Kubernetes Service blueprint's discovery.",
        )

    blueprint = namespace_blueprint()
    if blueprint is None:
        return (
            "FAILURE",
            "",
            "The AKS Namespace blueprint is not installed; sync it so the "
            "namespace can be filed under it.",
        )

    # ---- File the namespace as the group's resource, then build it ------
    namespace_resource = new_namespace_resource(
        blueprint, namespace, group, owner=getattr(job, "owner", None), cluster=cluster
    )
    set_progress(
        f"Created resource '{namespace}' under '{blueprint.name}' for group '{group.name}', "
        f"as a sub-resource of '{cluster.name}'."
    )

    status, output, error = create_landing_zone(
        namespace_resource,
        cluster,
        orchestrator,
        namespace,
        tier_key,
        owner=_owner_username(job),
        ingress=ingress,
        egress=egress,
    )

    if status == "FAILURE" and not namespace_resource.get_value_for_custom_field(
        CF_PREFIX + "resource_id"
    ):
        # Nothing was created in Azure, so leave no empty resource behind.
        namespace_resource.delete()
        set_progress("Removed the empty namespace resource.")
    elif status == "FAILURE":
        error += (
            f" The namespace resource '{namespace}' was kept so Delete can tear down "
            "what was created."
        )

    logger.info("Create namespace on %s finished %s: %s", cluster.name, status, output or error)
    return status, output, error
