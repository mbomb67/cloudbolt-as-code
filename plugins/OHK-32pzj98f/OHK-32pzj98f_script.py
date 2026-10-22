"""
CloudBolt build plugin: GKE Namespace.

Creates a governed namespace for a team on an existing GKE cluster and wires
it into CloudBolt so the team can order container blueprints into it, and
only into it. The work itself lives in ``gke_namespace.create_landing_zone``,
shared with the cluster-side Create GKE Namespace action.

On the cluster (through the Kubernetes container orchestrator the Google
Kubernetes Engine blueprint registered for the chosen cluster):
  1. Namespace with CloudBolt ownership labels and annotations
  2. ResourceQuota sized by the chosen tier (CPU, memory, storage, volume
     claims, pods)
  3. LimitRange with default container requests/limits (required with a quota)
  4. NetworkPolicies for the chosen ingress and egress policy, which GKE
     Dataplane V2 enforces

In CloudBolt:
  5. An Environment on the cluster's orchestrator, pinned to the namespace
     through its kubernetes_namespace value
  6. The ordering Group (and optionally one more) entitled to that Environment
  7. The tier mirrored into the Environment quota
  8. The namespace listed under the orchestrator's Namespaces tab

In Kubernetes RBAC, when Google principals are given:
  9. A RoleBinding per principal to the built-in edit, view, or admin
     ClusterRole, so their kubectl and pipelines work in this namespace and
     nowhere else
  https://kubernetes.io/docs/concepts/policy/resource-quotas/
  https://kubernetes.io/docs/concepts/services-networking/network-policies/
  https://cloud.google.com/kubernetes-engine/docs/how-to/role-based-access-control

Expected Action Inputs (declared in OHK-32pzj98f_metadata.json):
  - gke_ns_cluster (INT, required)            : Kubernetes Cluster resource (RBAC gate)
  - gke_ns_namespace_name (STR, required)     : DNS-1123 namespace name
  - gke_ns_size_tier (STR, required)          : small | medium | large | xlarge
  - gke_ns_ingress_policy (STR, required)     : AllowSameNamespace | AllowAll | DenyAll
  - gke_ns_egress_policy (STR, required)      : AllowAll | AllowSameNamespace | DenyAll
  - gke_ns_additional_group (INT, optional)   : extra CloudBolt Group to entitle
  - gke_ns_team_principals (TXT, optional)    : user:, group:, or serviceAccount: emails, one per line
  - gke_ns_team_access_level (STR, optional)  : edit | view | admin

Expiration comes from the blueprint's standard expiration_date parameter,
which CloudBolt stores on the Resource before this plugin runs; it is read
from there and mirrored onto the namespace as an annotation.

RBAC: end users pick a cluster, offered only when the ordering group can use
the cluster's own Environment; the GCP handler is never shown, and the
cluster is reached with the orchestrator's credentials server-side
(docs/agents/rbac-and-security.md).

Returns a 3-tuple: (status, output_msg, error_msg)
"""

from accounts.models import Group
from common.methods import set_progress
from resources.models import Resource
from shared_modules.gke_namespace import (
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

def generate_options_for_gke_ns_cluster(field, **kwargs):
    """GKE clusters the ordering group may order a namespace on.

    A cluster is offered when its Kubernetes orchestrator backs a cluster
    Environment in Group.get_available_environments(): the per-cluster
    environment the Google Kubernetes Engine blueprint creates, unconstrained
    (or, on an appliance that restricts new environments, in the Unassigned
    group) until the cluster's owners choose groups on its Groups tab.
    """
    group = resolve_group(kwargs.get("group"))
    clusters = cluster_resources_for_group(group)
    if not clusters:
        return [("", "------ No GKE clusters available to this group ------")]
    return [(resource.id, cluster_label(resource)) for resource, _ in clusters]


def generate_options_for_gke_ns_size_tier(field, **kwargs):
    """Tier dropdown; smallest tier preselected."""
    return tier_options(initial="small")


def generate_options_for_gke_ns_ingress_policy(field, **kwargs):
    """Ingress policy; traffic from the same namespace only, preselected."""
    return network_policy_options("AllowSameNamespace")


def generate_options_for_gke_ns_egress_policy(field, **kwargs):
    """Egress policy; all outbound traffic allowed, preselected."""
    return network_policy_options("AllowAll")


def generate_options_for_gke_ns_team_access_level(field, **kwargs):
    """Kubernetes role for the team's Google principals; Edit preselected."""
    return access_level_options(initial="edit")


def generate_options_for_gke_ns_additional_group(field, **kwargs):
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
    """Create the GKE namespace landing zone."""
    set_progress("Starting GKE Namespace build...")
    logger.info("GKE namespace build plugin started for job %s", job.id)

    ensure_custom_fields()

    cluster_id_str = _text("{{ gke_ns_cluster }}")
    namespace = _text("{{ gke_ns_namespace_name }}").lower()
    tier_key = _text("{{ gke_ns_size_tier }}").lower()
    ingress = _text("{{ gke_ns_ingress_policy }}") or "AllowSameNamespace"
    egress = _text("{{ gke_ns_egress_policy }}") or "AllowAll"
    additional_group_str = _text("{{ gke_ns_additional_group }}")
    principals_text = _text("""{{ gke_ns_team_principals }}""")
    access_level = _text("{{ gke_ns_team_access_level }}").lower() or "edit"

    # ---- Validate inputs ------------------------------------------------
    if not cluster_id_str:
        return "FAILURE", "", "A GKE cluster is required."
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
            f"Group '{ordering_group.name}' may not order namespaces on GKE cluster '{cluster.name}'; "
            "ask the cluster's owners to add the group on its Groups tab.",
        )

    orchestrator = load_orchestrator(orchestrator_id_for_cluster(cluster))
    if orchestrator is None:
        return (
            "FAILURE",
            "",
            f"Cluster '{cluster.name}' has no Kubernetes container orchestrator "
            "(container_orchestrator_id); run the Google Kubernetes Engine blueprint's discovery.",
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

    # ---- 9. Google access through Kubernetes RBAC ----------------------
    set_progress(f"Granting {len(principals)} Google principal(s) {access_level} access to '{namespace}'...")
    access_status, access_output, access_error = grant_team_access(resource, principals, access_level)
    if access_status != "SUCCESS":
        return (
            "FAILURE",
            output,
            f"The namespace is ready, but Google access could not be granted: {access_error} "
            "Fix the principals and run Manage GKE Namespace Access, or delete the resource.",
        )
    return status, f"{output} {access_output}", ""
