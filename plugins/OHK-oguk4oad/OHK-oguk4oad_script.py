"""
Day-2 action for the AWS EKS blueprint: create a namespace landing zone on
this cluster for a team, from the cluster resource.

The self-service path is the AWS EKS Namespace blueprint, where a team orders
a namespace on a cluster it is entitled to. This action is the platform-admin
path: whoever may run actions on the cluster resource picks the team's
CloudBolt group and the tier, and the namespace is filed as a resource of the
AWS EKS Namespace blueprint owned by that group and nested under the cluster
resource, so it gets the same resource type, day-2 actions, discovery, and
teardown as an ordered one and shows on the cluster's page. The work
itself is ``eks_namespace.create_landing_zone``, shared with the blueprint's
build plugin.

Expected Action Inputs (declared in OHK-oguk4oad_metadata.json):
  - namespace_name (STR, required)             : DNS-1123 namespace name
  - size_tier (STR, required)                  : small | medium | large | xlarge
  - owner_group_id (INT, required)             : CloudBolt Group that owns the namespace
  - allow_cross_namespace_ingress (BOOL, opt.) : skip the default-deny policies

Unlike an order, the action has no expiration date; the namespace resource
can still be given one afterwards.
"""

from accounts.models import Group
from common.methods import set_progress
from shared_modules.eks_namespace import (
    CF_PREFIX,
    create_landing_zone,
    ensure_custom_fields,
    get_tier,
    load_orchestrator,
    namespace_blueprint,
    new_namespace_resource,
    orchestrator_id_for_cluster,
    tier_options,
    truthy,
    validate_namespace_name,
)
from utilities.logger import ThreadLogger

logger = ThreadLogger(__name__)


def generate_options_for_size_tier(field, **kwargs):
    """Tier dropdown; smallest tier preselected."""
    return tier_options(initial="small")


def generate_options_for_owner_group_id(field, **kwargs):
    """Every real CloudBolt Group; the chosen one owns the namespace and is entitled to it."""
    groups = Group.objects.exclude(name__startswith="Unassigned").order_by("name")
    return {"options": [(g.id, g.name) for g in groups], "sort": False}


def _owner_username(job):
    owner = getattr(job, "owner", None)
    user = getattr(owner, "user", None)
    return getattr(user, "username", "") or str(owner or "")


def run(job, resource, **kwargs):
    """Create a namespace landing zone on this cluster for the chosen group."""
    cluster = resource
    set_progress(f"Creating a namespace landing zone on cluster '{cluster.name}'...")

    ensure_custom_fields()

    namespace = "{{ namespace_name }}".strip().lower()
    tier_key = "{{ size_tier }}".strip().lower()
    group_id_str = "{{ owner_group_id }}".strip()
    network_isolation = not truthy("{{ allow_cross_namespace_ingress }}")

    # ---- Validate inputs ------------------------------------------------
    name_error = validate_namespace_name(namespace)
    if name_error:
        return "FAILURE", "", name_error
    try:
        get_tier(tier_key)
    except ValueError as exc:
        return "FAILURE", "", str(exc)
    if not group_id_str:
        return "FAILURE", "", "An owning CloudBolt group is required."
    group = Group.objects.filter(id=int(group_id_str)).first()
    if group is None:
        return "FAILURE", "", f"Group id={group_id_str} not found."

    orchestrator = load_orchestrator(orchestrator_id_for_cluster(cluster))
    if orchestrator is None:
        return (
            "FAILURE",
            "",
            f"Cluster '{cluster.name}' has no Kubernetes container orchestrator "
            "(container_orchestrator_id); run the AWS EKS blueprint's discovery.",
        )

    blueprint = namespace_blueprint()
    if blueprint is None:
        return (
            "FAILURE",
            "",
            "The AWS EKS Namespace blueprint is not installed; sync it so the "
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
        network_isolation=network_isolation,
    )

    if status == "FAILURE" and not namespace_resource.get_value_for_custom_field(
        CF_PREFIX + "uid"
    ):
        # Nothing was created on the cluster, so leave no empty resource behind.
        namespace_resource.delete()
        set_progress("Removed the empty namespace resource.")
    elif status == "FAILURE":
        error += (
            f" The namespace resource '{namespace}' was kept so Delete can tear down "
            "what was created."
        )

    logger.info("Create namespace on %s finished %s: %s", cluster.name, status, output or error)
    return status, output, error
