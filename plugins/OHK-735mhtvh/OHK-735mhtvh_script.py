"""
CloudBolt build plugin: AKS Namespace.

Creates a governed namespace for a team on an existing AKS cluster and wires
it into CloudBolt so the team can order container blueprints into it, and
only into it. The work itself lives in ``aks_namespace.create_landing_zone``,
shared with the cluster-side Create AKS Namespace action:

In Azure (an AKS managed namespace, through Azure Resource Manager):
  1. Managed namespace with CloudBolt ownership labels and annotations
  2. CPU and memory quota sized by the chosen tier (enforced by AKS)
  3. The chosen ingress and egress network policies (enforced by AKS)

On the cluster (through the Kubernetes container orchestrator the Azure
Kubernetes Service blueprint registered for the chosen cluster):
  4. ResourceQuota for storage, persistent volume claims, and pods
  5. LimitRange with default container requests/limits (required with a quota)

In CloudBolt:
  6. An Environment on the cluster's orchestrator, pinned to the namespace
     through its kubernetes_namespace value
  7. The ordering Group (and optionally one more) entitled to that Environment
  8. The tier mirrored into the Environment quota
  9. The namespace listed under the orchestrator's Namespaces tab

In Azure RBAC, when Microsoft Entra principals are given:
  10. Azure Kubernetes Service Namespace User plus an RBAC Writer, Reader, or
      Admin role per principal, both scoped to the managed namespace, so
      their kubectl and pipelines work in this namespace and nowhere else

  https://learn.microsoft.com/en-us/azure/aks/concepts-managed-namespaces
  https://learn.microsoft.com/en-us/azure/aks/managed-namespaces

Expected Action Inputs (declared in OHK-735mhtvh_metadata.json):
  - aks_ns_cluster_resource_id (INT, required)  : Kubernetes Cluster resource (RBAC gate)
  - aks_ns_name (STR, required)                 : DNS-1123 namespace name
  - aks_ns_size_tier (STR, required)            : small | medium | large | xlarge
  - aks_ns_ingress_policy (STR, required)       : AllowSameNamespace | AllowAll | DenyAll
  - aks_ns_egress_policy (STR, required)        : AllowAll | AllowSameNamespace | DenyAll
  - aks_ns_additional_group_id (INT, optional)  : extra CloudBolt Group to entitle
  - aks_ns_team_principals (TXT, optional)      : Entra object IDs, one per line
  - aks_ns_team_access_level (STR, optional)    : edit | view | admin

Expiration comes from the blueprint's standard expiration_date parameter,
which CloudBolt stores on the Resource before this plugin runs; it is read
from there and mirrored onto the namespace as an annotation.

RBAC: end users pick a cluster, offered only when the ordering group can use
the cluster's own Environment; the Azure handler is never shown and is used
only server-side, to call Azure Resource Manager
(docs/agents/rbac-and-security.md).

Returns a 3-tuple: (status, output_msg, error_msg)
"""

from accounts.models import Group
from common.methods import set_progress
from resources.models import Resource
from shared_modules.aks_namespace import (
    CLUSTER_RESOURCE_TYPE,
    NETWORK_POLICY_CHOICES,
    access_level_options,
    cluster_label,
    cluster_resources_for_group,
    create_landing_zone,
    ensure_custom_fields,
    get_access_level,
    get_tier,
    grant_team_access,
    load_orchestrator,
    network_policy_options,
    orchestrator_id_for_cluster,
    parse_principals,
    resolve_group,
    resource_expiration,
    tier_options,
    validate_namespace_name,
)
from utilities.logger import ThreadLogger

logger = ThreadLogger(__name__)


# ---------------------------------------------------------------------------
# Order-form option generators
# ---------------------------------------------------------------------------

def generate_options_for_aks_ns_cluster_resource_id(field, **kwargs):
    """AKS clusters the ordering group may order a namespace on.

    A cluster is offered when its Kubernetes orchestrator backs a cluster
    Environment in Group.get_available_environments(): the per-cluster
    environment the Azure Kubernetes Service blueprint creates, unconstrained
    (or, on an appliance that restricts new environments, in the Unassigned
    group) until a platform team assigns groups to it.
    """
    group = resolve_group(kwargs.get("group"))
    clusters = cluster_resources_for_group(group)
    if not clusters:
        return [("", "------ No AKS clusters available to this group ------")]
    return [(resource.id, cluster_label(resource)) for resource, _ in clusters]


def generate_options_for_aks_ns_size_tier(field, **kwargs):
    """Tier dropdown; smallest tier preselected."""
    return tier_options(initial="small")


def generate_options_for_aks_ns_ingress_policy(field, **kwargs):
    """Ingress policy; traffic from the same namespace only, preselected."""
    return network_policy_options("AllowSameNamespace")


def generate_options_for_aks_ns_egress_policy(field, **kwargs):
    """Egress policy; all outbound traffic allowed, preselected."""
    return network_policy_options("AllowAll")


def generate_options_for_aks_ns_team_access_level(field, **kwargs):
    """Access level for the team's Microsoft Entra principals; Writer preselected."""
    return access_level_options(initial="edit")


def generate_options_for_aks_ns_additional_group_id(field, **kwargs):
    """Every real CloudBolt Group; the ordering group is entitled regardless."""
    groups = Group.objects.exclude(name__startswith="Unassigned").order_by("name")
    options = [("", "------ None (ordering group only) ------")]
    options.extend((g.id, g.name) for g in groups)
    return {"options": options, "sort": False}


# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------

def _text(value):
    """A rendered input as plain text; an input left empty may render as 'None'."""
    text = str(value or "").strip()
    return "" if text == "None" else text


def _owner_username(job):
    owner = getattr(job, "owner", None)
    user = getattr(owner, "user", None)
    return getattr(user, "username", "") or str(owner or "")


def run(job, resource, **kwargs):
    """Create the AKS namespace landing zone."""
    set_progress("Starting AKS Namespace build...")
    logger.info("AKS namespace build plugin started for job %s", job.id)

    ensure_custom_fields()

    cluster_id_str = _text("{{ aks_ns_cluster_resource_id }}")
    namespace = _text("{{ aks_ns_name }}").lower()
    tier_key = _text("{{ aks_ns_size_tier }}").lower()
    ingress = _text("{{ aks_ns_ingress_policy }}") or "AllowSameNamespace"
    egress = _text("{{ aks_ns_egress_policy }}") or "AllowAll"
    additional_group_str = _text("{{ aks_ns_additional_group_id }}")
    principals_text = _text("""{{ aks_ns_team_principals }}""")
    access_level = _text("{{ aks_ns_team_access_level }}").lower() or "edit"

    # ---- Validate inputs ------------------------------------------------
    if not cluster_id_str:
        return "FAILURE", "", "An AKS cluster is required."
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

    for label, policy in (("Ingress", ingress), ("Egress", egress)):
        if policy not in NETWORK_POLICY_CHOICES:
            return (
                "FAILURE",
                "",
                f"{label} policy '{policy}' is not one of {', '.join(NETWORK_POLICY_CHOICES)}.",
            )

    principals, principal_errors = parse_principals(principals_text)
    if principal_errors:
        return "FAILURE", "", " ".join(principal_errors)
    if principals:
        try:
            get_access_level(access_level)
        except ValueError as exc:
            return "FAILURE", "", str(exc)

    additional_group = None
    if additional_group_str:
        try:
            additional_group = Group.objects.filter(id=int(additional_group_str)).first()
        except ValueError:
            additional_group = None
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
            "(container_orchestrator_id); run the Azure Kubernetes Service blueprint's discovery.",
        )

    status, output, error = create_landing_zone(
        resource,
        cluster,
        orchestrator,
        namespace,
        tier_key,
        owner=_owner_username(job),
        expires=resource_expiration(resource),
        ingress=ingress,
        egress=egress,
        additional_group=additional_group,
    )
    if status != "SUCCESS" or not principals:
        return status, output, error

    # ---- 10. Microsoft Entra access through Azure RBAC -------------------
    set_progress(f"Granting {len(principals)} Microsoft Entra principal(s) {access_level} access to '{namespace}'...")
    access_status, access_output, access_error = grant_team_access(resource, principals, access_level)
    if access_status != "SUCCESS":
        return (
            "FAILURE",
            output,
            f"The namespace is ready, but Microsoft Entra access could not be granted: {access_error} "
            "Fix the principals and run Manage AKS Namespace Access, or delete the resource.",
        )
    return status, f"{output} {access_output}", ""
