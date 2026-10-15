"""
CloudBolt build plugin: AWS EKS Namespace.

Creates a governed namespace for a team on an existing EKS cluster and wires
it into CloudBolt so the team can order container blueprints into it, and
only into it. The work itself lives in ``eks_namespace.create_landing_zone``,
shared with the cluster-side Create AWS EKS Namespace action:

On the cluster (through the Kubernetes container orchestrator the AWS EKS
blueprint registered for the chosen cluster):
  1. Namespace with CloudBolt ownership labels and annotations
  2. ResourceQuota sized by the chosen tier (enforced by the API server)
  3. LimitRange with default container requests/limits (required with a quota)
  4. Default-deny NetworkPolicy set, unless cross-namespace ingress is allowed

In CloudBolt:
  5. An Environment on the cluster's orchestrator, pinned to the namespace
     through its kubernetes_namespace value
  6. The ordering Group (and optionally one more) entitled to that Environment
  7. The tier mirrored into the Environment quota
  8. The namespace listed under the orchestrator's Namespaces tab

On AWS, when IAM principals are given:
  9. An EKS access entry per principal (created only when it has none) with
     the chosen access policy scoped to this namespace, so their kubectl
     and pipelines work without touching the aws-auth ConfigMap

Expected Action Inputs (declared in OHK-u9jm7p8h_metadata.json):
  - cluster_resource_id (INT, required)        : Kubernetes Cluster resource (RBAC gate)
  - namespace_name (STR, required)             : DNS-1123 namespace name
  - size_tier (STR, required)                  : small | medium | large | xlarge
  - additional_group_id (INT, optional)        : extra CloudBolt Group to entitle
  - allow_cross_namespace_ingress (BOOL, opt.) : skip the default-deny policies
  - eks_ns_team_principal_arns (TXT, optional) : IAM role/user ARNs, one per line
  - eks_ns_team_access_level (STR, optional)   : edit | view | admin

Expiration comes from the blueprint's standard expiration_date parameter,
which CloudBolt stores on the Resource before this plugin runs; it is read
from there and mirrored onto the namespace as an annotation.

RBAC: end users pick a cluster, offered only when the ordering group can use
the cluster's own Environment; the AWS handler is never shown and is used
only indirectly, to mint the cluster token (docs/agents/rbac-and-security.md).

Returns a 3-tuple: (status, output_msg, error_msg)
"""

from accounts.models import Group
from common.methods import set_progress
from resources.models import Resource
from shared_modules.eks_namespace import (
    CLUSTER_RESOURCE_TYPE,
    access_level_options,
    cluster_label,
    cluster_resources_for_group,
    create_landing_zone,
    ensure_custom_fields,
    get_access_level,
    get_tier,
    grant_team_access,
    load_orchestrator,
    orchestrator_id_for_cluster,
    parse_principal_arns,
    resolve_group,
    resource_expiration,
    tier_options,
    truthy,
    validate_namespace_name,
)
from utilities.logger import ThreadLogger

logger = ThreadLogger(__name__)


# ---------------------------------------------------------------------------
# Order-form option generators
# ---------------------------------------------------------------------------

def generate_options_for_cluster_resource_id(field, **kwargs):
    """EKS clusters the ordering group may order a namespace on.

    A cluster is offered when its Kubernetes orchestrator backs a cluster
    Environment in Group.get_available_environments(): the per-cluster
    environment the AWS EKS blueprint creates, unconstrained (or, on an
    appliance that restricts new environments, in the Unassigned group)
    until a platform team assigns groups to it.
    """
    group = resolve_group(kwargs.get("group"))
    clusters = cluster_resources_for_group(group)
    if not clusters:
        return [("", "------ No EKS clusters available to this group ------")]
    return [(resource.id, cluster_label(resource)) for resource, _ in clusters]


def generate_options_for_size_tier(field, **kwargs):
    """Tier dropdown; smallest tier preselected."""
    return tier_options(initial="small")


def generate_options_for_eks_ns_team_access_level(field, **kwargs):
    """Access level for the team's IAM principals; Edit preselected."""
    return access_level_options(initial="edit")


def generate_options_for_additional_group_id(field, **kwargs):
    """Every real CloudBolt Group; the ordering group is entitled regardless."""
    groups = Group.objects.exclude(name__startswith="Unassigned").order_by("name")
    options = [("", "------ None (ordering group only) ------")]
    options.extend((g.id, g.name) for g in groups)
    return {"options": options, "sort": False}


# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------

def _owner_username(job):
    owner = getattr(job, "owner", None)
    user = getattr(owner, "user", None)
    return getattr(user, "username", "") or str(owner or "")


def run(job, resource, **kwargs):
    """Create the EKS namespace landing zone."""
    set_progress("Starting AWS EKS Namespace build...")
    logger.info("EKS namespace build plugin started for job %s", job.id)

    ensure_custom_fields()

    cluster_id_str = "{{ cluster_resource_id }}".strip()
    namespace = "{{ namespace_name }}".strip().lower()
    tier_key = "{{ size_tier }}".strip().lower()
    additional_group_str = "{{ additional_group_id }}".strip()
    network_isolation = not truthy("{{ allow_cross_namespace_ingress }}")
    principals_text = """{{ eks_ns_team_principal_arns }}"""
    access_level = "{{ eks_ns_team_access_level }}".strip().lower() or "edit"

    # ---- Validate inputs ------------------------------------------------
    if not cluster_id_str:
        return "FAILURE", "", "An EKS cluster is required."
    try:
        cluster_id = int(cluster_id_str)
    except ValueError:
        return "FAILURE", "", f"Invalid cluster resource id '{cluster_id_str}'."

    name_error = validate_namespace_name(namespace)
    if name_error:
        return "FAILURE", "", name_error

    try:
        get_tier(tier_key)
    except ValueError as exc:
        return "FAILURE", "", str(exc)

    principals, principal_errors = parse_principal_arns(principals_text)
    if principal_errors:
        return "FAILURE", "", " ".join(principal_errors)
    if principals:
        try:
            get_access_level(access_level)
        except ValueError as exc:
            return "FAILURE", "", str(exc)

    additional_group = None
    if additional_group_str:
        additional_group = Group.objects.filter(id=int(additional_group_str)).first()
        if additional_group is None:
            return "FAILURE", "", f"Additional group id={additional_group_str} not found."

    # ---- Resolve the cluster and its orchestrator (RBAC gate) -----------
    cluster = Resource.objects.filter(
        id=cluster_id, resource_type__name=CLUSTER_RESOURCE_TYPE, lifecycle="ACTIVE"
    ).first()
    if cluster is None:
        return "FAILURE", "", f"No active Kubernetes Cluster resource with id={cluster_id}."
    ordering_group = getattr(resource, "group", None)
    if ordering_group is not None and cluster.id not in [
        r.id for r, _ in cluster_resources_for_group(ordering_group)
    ]:
        return (
            "FAILURE",
            "",
            f"Group '{ordering_group.name}' cannot use the environment of cluster "
            f"'{cluster.name}'; entitle it to that environment first.",
        )

    orchestrator = load_orchestrator(orchestrator_id_for_cluster(cluster))
    if orchestrator is None:
        return (
            "FAILURE",
            "",
            f"Cluster '{cluster.name}' has no Kubernetes container orchestrator "
            "(container_orchestrator_id); run the AWS EKS blueprint's discovery.",
        )

    status, output, error = create_landing_zone(
        resource,
        cluster,
        orchestrator,
        namespace,
        tier_key,
        owner=_owner_username(job),
        expires=resource_expiration(resource),
        network_isolation=network_isolation,
        additional_group=additional_group,
    )
    if status != "SUCCESS" or not principals:
        return status, output, error

    # ---- 9. IAM access through EKS access entries ------------------------
    set_progress(f"Granting {len(principals)} IAM principal(s) {access_level} access to '{namespace}'...")
    access_status, access_output, access_error = grant_team_access(resource, principals, access_level)
    if access_status != "SUCCESS":
        return (
            "FAILURE",
            output,
            f"The namespace is ready, but IAM access could not be granted: {access_error} "
            "Fix the ARNs and run Manage Namespace Access, or delete the resource.",
        )
    return status, f"{output} {access_output}", ""
