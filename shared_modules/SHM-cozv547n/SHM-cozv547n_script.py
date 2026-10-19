"""
AKS Namespace shared module.

Shared by the "AKS Namespace" blueprint's build, teardown, discovery, and
day-2 plugins, the Azure Kubernetes Service blueprint's Create AKS Namespace
action, and the Azure AKS Cluster UI extension.

Each landing zone is an AKS **managed namespace**: an Azure Resource Manager
resource (managedClusters/managedNamespaces) from which AKS creates and
reconciles the Kubernetes namespace, its CPU and memory ResourceQuota, and its
ingress and egress NetworkPolicies, and on which Azure RBAC roles are assigned.
  https://learn.microsoft.com/en-us/azure/aks/concepts-managed-namespaces
  https://learn.microsoft.com/en-us/azure/aks/managed-namespaces
  https://learn.microsoft.com/en-us/rest/api/aks/managed-namespaces
The module has four parts:

1. Managed namespaces over ARM (create, read, update, delete, list,
   credential), plus a thin Kubernetes API client on the cluster's container
   orchestrator for what AKS does not manage: a second ResourceQuota for
   storage, persistent volume claims, and pods, and a LimitRange that gives
   containers default requests and limits.

2. CloudBolt-side helpers -- the size-tier catalog, the custom fields stored
   on each namespace Resource, the RBAC-gated list of AKS clusters a group
   may order a namespace on, and the Environment pinned to the namespace: one
   Environment per landing zone that points at the cluster's orchestrator,
   carries the namespace as its ``kubernetes_namespace`` value, is entitled
   to the ordering Group, and mirrors the tier into its quota.

3. Namespace access through Azure RBAC -- Microsoft Entra users, groups, and
   service principals get the Azure Kubernetes Service Namespace User role
   (see the namespace, download its kubeconfig) and an RBAC Reader, Writer,
   or Admin role (what they may do inside it), both scoped to the managed
   namespace.

4. Pipeline identities and connection details -- a user-assigned managed
   identity a GitHub Actions workflow signs in as through OIDC (federated
   credentials, no secret), and the commands and kubeconfig the Connection
   Details action and the extension's Connect tab show.

This is the AKS twin of the EKS Namespace module (shared_modules/SHM-sa5yqm9j).

Import from plugins as::

    from shared_modules.aks_namespace import create_landing_zone, TIERS, ...

Shared-module code is cached by the running CloudBolt process: after changing
this file and syncing, restart CloudBolt for the change to take effect.
"""

import datetime
import hashlib
import json
import re
import time
from collections import OrderedDict

from accounts.models import Group
from common.methods import set_progress
from infrastructure.models import CustomField, Environment
from resources.models import Resource
from shared_modules.azure_aks import (
    AKSError,
    MSI_API_VERSION,
    assign_role,
    cluster_for_resource,
    delete_role_assignment,
    guid_or_none,
    is_aks_cluster,
    list_role_assignments,
)
from utilities.exceptions import CloudBoltException
from utilities.logger import ThreadLogger

logger = ThreadLogger(__name__)

# ---------------------------------------------------------------------------
# Naming constants shared by every plugin in the blueprint
# ---------------------------------------------------------------------------

# Labels / annotations set on the managed namespace (AKS applies them to the
# Kubernetes namespace). The same keys as the EKS Namespace blueprint, so a
# landing zone reads the same on either cloud.
LANDING_ZONE_LABEL = "cloudbolt.io/landing-zone"
LABEL_TIER = "cloudbolt.io/size-tier"
LABEL_GROUP = "cloudbolt.io/group"
LABEL_RESOURCE_ID = "cloudbolt.io/resource-id"
ANNOTATION_EXPIRES = "cloudbolt.io/lease-expires"
ANNOTATION_OWNER = "cloudbolt.io/owner"
ANNOTATION_ENVIRONMENT = "cloudbolt.io/environment-id"
ANNOTATION_GROUP_ID = "cloudbolt.io/group-id"

# The objects CloudBolt adds inside the namespace through the Kubernetes API.
QUOTA_NAME = "cloudbolt-landing-zone"
LIMIT_RANGE_NAME = "cloudbolt-landing-zone"

# Namespaces Kubernetes and AKS reserve; AKS refuses to manage them.
# https://kubernetes.io/docs/concepts/overview/working-with-objects/namespaces/#initial-namespaces
# https://learn.microsoft.com/en-us/azure/aks/managed-namespaces#limitations
RESERVED_NAMESPACE_PREFIXES = ("kube-",)
RESERVED_NAMESPACE_NAMES = {"default", "kube-system", "kube-public", "kube-node-lease", "gatekeeper-system",
                            "app-routing-system", "istio-system", "calico-system", "tigera-operator"}

# DNS-1123 label, which is also the managed namespace name pattern.
# https://kubernetes.io/docs/concepts/overview/working-with-objects/names/#dns-label-names
NAMESPACE_NAME_RE = re.compile(r"^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?$")

CLUSTER_RESOURCE_TYPE = "kubernetes_cluster"
CLUSTER_ORCHESTRATOR_FIELD = "container_orchestrator_id"
ENV_NAMESPACE_FIELD = "kubernetes_namespace"

CF_PREFIX = "aks_namespace_"

# (name, label, type, description, show_as_attribute)
CUSTOM_FIELDS = [
    (CF_PREFIX + "name", "Namespace", "STR",
     "Name of the Kubernetes namespace backing this landing zone.", True),
    (CF_PREFIX + "resource_id", "Managed Namespace ID", "STR",
     "Azure resource ID of the AKS managed namespace.", False),
    (CF_PREFIX + "uid", "Namespace UID", "STR",
     "Kubernetes UID of the namespace.", False),
    (CF_PREFIX + "cluster", "AKS Cluster", "STR",
     "Name of the AKS cluster (CloudBolt Kubernetes Cluster resource) that hosts the namespace.", True),
    (CF_PREFIX + "cluster_resource_id", "Cluster Resource ID", "INT",
     "ID of the CloudBolt Kubernetes Cluster resource that hosts the namespace.", False),
    (CF_PREFIX + "orchestrator_id", "Container Orchestrator ID", "INT",
     "ID of the Kubernetes container orchestrator used to reach the cluster.", False),
    (CF_PREFIX + "environment_id", "Environment ID", "INT",
     "ID of the CloudBolt Environment pinned to this namespace.", False),
    (CF_PREFIX + "environment_name", "Environment", "STR",
     "Name of the CloudBolt Environment pinned to this namespace.", True),
    (CF_PREFIX + "entitled_groups", "Entitled CloudBolt Groups", "TXT",
     "CloudBolt Groups allowed to order into this namespace, one per line.", True),
    (CF_PREFIX + "size_tier", "Size Tier", "STR",
     "Landing-zone size tier that sets the namespace quotas.", True),
    (CF_PREFIX + "cpu_limit", "CPU Limit (cores)", "INT",
     "Namespace quota for CPU (requests and limits), in cores.", True),
    (CF_PREFIX + "memory_limit_gb", "Memory Limit (GiB)", "INT",
     "Namespace quota for memory (requests and limits), in GiB.", True),
    (CF_PREFIX + "storage_limit_gb", "Storage Limit (GiB)", "INT",
     "Namespace quota for persistent volume claim requests, in GiB.", True),
    (CF_PREFIX + "max_pods", "Max Pods", "INT",
     "Namespace quota for the number of pods.", True),
    (CF_PREFIX + "network_policy", "Network Policy", "STR",
     "Ingress and egress policy AKS enforces on the namespace.", True),
    (CF_PREFIX + "entra_access", "Microsoft Entra Access (kubectl)", "TXT",
     "Microsoft Entra principals with an Azure RBAC role on this namespace, one per line.", True),
    (CF_PREFIX + "pipeline_identities", "Pipeline Identities", "TXT",
     "Managed identities CloudBolt created for CI pipelines deploying to this namespace (JSON).", False),
]


def ensure_custom_fields():
    """Create every custom field the blueprint persists, and keep it in sync."""
    for name, label, cf_type, description, show_as_attribute in CUSTOM_FIELDS:
        cf, created = CustomField.objects.get_or_create(
            name=name,
            defaults=dict(label=label, description=description, type=cf_type,
                          show_on_servers=False, show_as_attribute=show_as_attribute),
        )
        if created:
            continue
        changed = False
        for attr, value in (("label", label), ("description", description),
                            ("show_as_attribute", show_as_attribute)):
            if getattr(cf, attr, None) != value:
                setattr(cf, attr, value)
                changed = True
        if changed:
            cf.save()

    CustomField.objects.get_or_create(
        name=ENV_NAMESPACE_FIELD,
        defaults=dict(
            label="Kubernetes Namespace",
            description=(
                "Namespace an Environment on a Kubernetes container orchestrator is pinned to. "
                "Set by the EKS and AKS Namespace blueprints; container blueprints deploy into it."
            ),
            type="STR", show_on_servers=False, show_as_attribute=False,
        ),
    )


# ---------------------------------------------------------------------------
# Size tiers (the same catalog as the EKS Namespace blueprint)
# ---------------------------------------------------------------------------

TIERS = OrderedDict([
    ("small", dict(label="Small", cpu=4, memory_gb=16, storage_gb=100, pods=20, pvcs=10,
                   description="4 cores, 16 GiB RAM, 100 GiB storage, 20 pods")),
    ("medium", dict(label="Medium", cpu=8, memory_gb=32, storage_gb=250, pods=40, pvcs=20,
                    description="8 cores, 32 GiB RAM, 250 GiB storage, 40 pods")),
    ("large", dict(label="Large", cpu=16, memory_gb=64, storage_gb=500, pods=80, pvcs=40,
                   description="16 cores, 64 GiB RAM, 500 GiB storage, 80 pods")),
    ("xlarge", dict(label="X-Large", cpu=32, memory_gb=128, storage_gb=1000, pods=160, pvcs=80,
                    description="32 cores, 128 GiB RAM, 1 TiB storage, 160 pods")),
])


def get_tier(key):
    tier = TIERS.get(str(key or "").strip().lower())
    if not tier:
        raise ValueError(f"Unknown size tier '{key}'. Valid tiers: {', '.join(TIERS)}.")
    return tier


def tier_options(initial="small"):
    options = [(key, f"{tier['label']} - {tier['description']}") for key, tier in TIERS.items()]
    return {"options": options, "initial_value": initial, "sort": False}


def managed_quota_for_tier(tier):
    """The managed namespace defaultResourceQuota: CPU in millicores, memory in Gi.
    https://learn.microsoft.com/en-us/rest/api/aks/managed-namespaces/create-or-update#resourcequota
    """
    cpu = f"{int(tier['cpu']) * 1000}m"
    memory = f"{tier['memory_gb']}Gi"
    return {"cpuRequest": cpu, "cpuLimit": cpu, "memoryRequest": memory, "memoryLimit": memory}


def object_quota_for_tier(tier):
    """The ResourceQuota CloudBolt adds for what the managed quota does not cover.
    https://kubernetes.io/docs/concepts/policy/resource-quotas/
    """
    return {
        "requests.storage": f"{tier['storage_gb']}Gi",
        "persistentvolumeclaims": str(tier["pvcs"]),
        "pods": str(tier["pods"]),
    }


# Default container requests/limits, required alongside a compute quota: with
# requests/limits quotas in place every new pod must set them.
# https://kubernetes.io/docs/reference/kubernetes-api/policy-resources/limit-range-v1/
DEFAULT_LIMIT_RANGE_ITEM = {
    "type": "Container",
    "default": {"cpu": "500m", "memory": "512Mi"},
    "defaultRequest": {"cpu": "100m", "memory": "128Mi"},
}

# Managed namespace network policies.
# https://learn.microsoft.com/en-us/azure/aks/concepts-managed-namespaces#network-policies
NETWORK_POLICY_CHOICES = OrderedDict([
    ("AllowSameNamespace", "Allow same namespace"),
    ("AllowAll", "Allow all"),
    ("DenyAll", "Deny all"),
])


def network_policy_options(initial):
    return {"options": list(NETWORK_POLICY_CHOICES.items()), "initial_value": initial, "sort": False}


def describe_network_policy(policy):
    policy = policy or {}
    return (f"Ingress: {NETWORK_POLICY_CHOICES.get(policy.get('ingress'), policy.get('ingress') or 'n/a')}; "
            f"Egress: {NETWORK_POLICY_CHOICES.get(policy.get('egress'), policy.get('egress') or 'n/a')}")


# ---------------------------------------------------------------------------
# Small utilities
# ---------------------------------------------------------------------------

def validate_namespace_name(name):
    if not name:
        return "A namespace name is required."
    if not NAMESPACE_NAME_RE.match(name):
        return ("Namespace name must be 1-63 lowercase alphanumerics or '-', and must "
                "start and end with an alphanumeric (DNS-1123 label).")
    if name in RESERVED_NAMESPACE_NAMES or name.startswith(RESERVED_NAMESPACE_PREFIXES):
        return f"'{name}' is reserved for Kubernetes or AKS system components; choose another name."
    return None


def label_value(value):
    """A valid Kubernetes label value.
    https://kubernetes.io/docs/concepts/overview/working-with-objects/labels/#syntax-and-character-set
    """
    text = re.sub(r"[^A-Za-z0-9._-]+", "-", str(value or "")).strip("-._")
    return text[:63].rstrip("-._")


def to_date(value):
    if value is None or value == "":
        return None
    if isinstance(value, datetime.datetime):
        return value.date()
    if isinstance(value, datetime.date):
        return value
    try:
        return datetime.date.fromisoformat(str(value).strip()[:10])
    except (TypeError, ValueError):
        return None


def resource_expiration(resource):
    if resource is None:
        return None
    return to_date(resource.get_value_for_custom_field("expiration_date"))


def truthy(value):
    return str(value or "").strip().lower() in ("true", "1", "yes", "on")


# ---------------------------------------------------------------------------
# Kubernetes API client on top of the orchestrator's API wrapper
# ---------------------------------------------------------------------------

class NamespaceError(Exception):
    """Raised when the Kubernetes API refuses a landing-zone request."""


class NamespaceClient:
    """Minimal Kubernetes API client for landing-zone objects.

    The orchestrator's API wrapper (``Kubernetes.get_api_wrapper()``) holds
    the endpoint, the cluster admin credential, and CloudBolt's SSL trust
    settings; ``requests_get`` and friends add them to every call.
    """

    def __init__(self, wrapper):
        self._wrapper = wrapper
        base = wrapper.get_clusterrole_url("v1", skip_base_url_version=True)
        self._root = base[: -len("/api")] if base.endswith("/api") else base

    @classmethod
    def from_orchestrator(cls, orchestrator):
        return cls(orchestrator.get_api_wrapper())

    def _request(self, method, path, json=None, params=None, allow_404=False):
        url = f"{self._root}/{path.lstrip('/')}"
        expected = [200, 201, 202, 204] + ([404] if allow_404 else [])
        send = getattr(self._wrapper, f"requests_{method.lower()}")
        kwargs = {"expected_statuses": expected}
        if json is not None:
            kwargs["json"] = json
        if params is not None:
            kwargs["params"] = params
        try:
            response = send(url, **kwargs)
        except CloudBoltException as exc:
            raise NamespaceError(str(exc))
        if response.status_code == 404:
            return None
        if response.text and response.headers.get("Content-Type", "").startswith("application/json"):
            return response.json()
        return response.text

    def _apply(self, path, name, body):
        """Create ``body``, or replace the existing object (PUT needs its resourceVersion)."""
        existing = self._request("GET", f"{path}/{name}", allow_404=True)
        if existing is None:
            return self._request("POST", path, json=body)
        body = dict(body)
        body.setdefault("metadata", {})["resourceVersion"] = existing["metadata"]["resourceVersion"]
        return self._request("PUT", f"{path}/{name}", json=body)

    # https://kubernetes.io/docs/reference/kubernetes-api/cluster-resources/namespace-v1/
    def get_namespace(self, name):
        return self._request("GET", f"/api/v1/namespaces/{name}", allow_404=True)

    def create_namespace(self, name, labels=None, annotations=None):
        body = {"apiVersion": "v1", "kind": "Namespace",
                "metadata": {"name": name, "labels": labels or {}, "annotations": annotations or {}}}
        return self._request("POST", "/api/v1/namespaces", json=body)

    def patch_namespace_metadata(self, name, labels=None, annotations=None):
        """Merge labels/annotations (the wrapper sends a strategic merge patch).
        https://kubernetes.io/docs/tasks/manage-kubernetes-objects/update-api-object-kubectl-patch/
        """
        patch = {"metadata": {}}
        if labels is not None:
            patch["metadata"]["labels"] = labels
        if annotations is not None:
            patch["metadata"]["annotations"] = annotations
        return self._request("PATCH", f"/api/v1/namespaces/{name}", json=patch)

    def delete_namespace(self, name):
        return self._request("DELETE", f"/api/v1/namespaces/{name}", allow_404=True) is not None

    def wait_for_namespace_deleted(self, name, timeout=300, interval=5):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.get_namespace(name) is None:
                return True
            time.sleep(interval)
        return False

    def list_landing_zone_namespaces(self):
        result = self._request("GET", "/api/v1/namespaces",
                               params={"labelSelector": f"{LANDING_ZONE_LABEL}=true"})
        return (result or {}).get("items", [])

    # https://kubernetes.io/docs/reference/kubernetes-api/policy-resources/resource-quota-v1/
    def get_resource_quota(self, namespace, name=QUOTA_NAME):
        return self._request("GET", f"/api/v1/namespaces/{namespace}/resourcequotas/{name}", allow_404=True)

    def apply_resource_quota(self, namespace, hard, name=QUOTA_NAME):
        body = {"apiVersion": "v1", "kind": "ResourceQuota",
                "metadata": {"name": name, "namespace": namespace}, "spec": {"hard": hard}}
        return self._apply(f"/api/v1/namespaces/{namespace}/resourcequotas", name, body)

    def apply_limit_range(self, namespace, item=None, name=LIMIT_RANGE_NAME):
        # https://kubernetes.io/docs/reference/kubernetes-api/policy-resources/limit-range-v1/
        body = {"apiVersion": "v1", "kind": "LimitRange",
                "metadata": {"name": name, "namespace": namespace},
                "spec": {"limits": [item or DEFAULT_LIMIT_RANGE_ITEM]}}
        return self._apply(f"/api/v1/namespaces/{namespace}/limitranges", name, body)

    def count_workloads(self, namespace):
        """Pods, deployments, services, and PVCs in the namespace."""
        counts = {}
        for key, path in (
            ("pods", f"/api/v1/namespaces/{namespace}/pods"),
            ("deployments", f"/apis/apps/v1/namespaces/{namespace}/deployments"),
            ("services", f"/api/v1/namespaces/{namespace}/services"),
            ("persistentvolumeclaims", f"/api/v1/namespaces/{namespace}/persistentvolumeclaims"),
        ):
            try:
                counts[key] = len((self._request("GET", path, allow_404=True) or {}).get("items", []))
            except NamespaceError as exc:
                logger.warning("Could not count %s in %s: %s", key, namespace, exc)
                counts[key] = None
        return counts


# ---------------------------------------------------------------------------
# CloudBolt-side helpers: AKS cluster resources and orchestrators
# ---------------------------------------------------------------------------

def resolve_group(group):
    if group is None or isinstance(group, Group):
        return group
    return Group.objects.filter(name=str(group)).first()


def orchestrator_id_for_cluster(cluster_resource):
    value = cluster_resource.get_cf_values_as_dict().get(CLUSTER_ORCHESTRATOR_FIELD)
    try:
        return int(str(value).strip()) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def cluster_resources():
    """Every active AKS cluster resource with an orchestrator: [(resource, orchestrator_id)].

    The Kubernetes Cluster resource type is shared with the AWS EKS
    blueprint; only resources carrying aks_cluster_name count here.
    """
    clusters = []
    for resource in Resource.objects.filter(
        resource_type__name=CLUSTER_RESOURCE_TYPE, lifecycle="ACTIVE"
    ).order_by("name"):
        if not is_aks_cluster(resource):
            continue
        orchestrator_id = orchestrator_id_for_cluster(resource)
        if orchestrator_id is not None:
            clusters.append((resource, orchestrator_id))
    return clusters


def cluster_resources_for_group(group):
    """AKS clusters the group may order a namespace on.

    Gate: the cluster's orchestrator must back a cluster Environment the group
    can use. ``Group.get_available_environments()`` returns the environments
    explicitly entitled to the group (and its ancestors) PLUS every
    unconstrained environment, so a cluster whose per-cluster environment has
    no groups assigned is offered to everyone, and a platform team narrows
    that by assigning groups to the environment. Namespace environments also
    point at the orchestrator but are excluded, so holding one namespace on a
    cluster grants nothing beyond it. See docs/agents/rbac-and-security.md.
    """
    if group is None:
        return []
    available_ids = [env.id for env in group.get_available_environments()]
    orchestrator_ids = set(
        Environment.objects.filter(id__in=available_ids, container_orchestrator__isnull=False)
        .exclude(custom_field_options__field__name=ENV_NAMESPACE_FIELD)
        .values_list("container_orchestrator_id", flat=True)
    )
    return [(res, oid) for res, oid in cluster_resources() if oid in orchestrator_ids]


def cluster_label(cluster_resource):
    values = cluster_resource.get_cf_values_as_dict()
    details = []
    if values.get("aks_location"):
        details.append(str(values["aks_location"]))
    if values.get("kubernetes_version"):
        details.append(f"Kubernetes {values['kubernetes_version']}")
    return f"{cluster_resource.name} ({', '.join(details)})" if details else cluster_resource.name


def load_orchestrator(orchestrator_id):
    from containerorchestrators.kuberneteshandler.models import Kubernetes

    if orchestrator_id in (None, ""):
        return None
    return Kubernetes.objects.filter(id=int(orchestrator_id)).first()


def load_cluster(resource):
    """(namespace, orchestrator, cluster_name) from a namespace Resource; ValueError names what is missing."""
    if resource is None:
        raise ValueError("no Resource was supplied")
    namespace = resource.get_value_for_custom_field(CF_PREFIX + "name")
    if not namespace:
        raise ValueError(f"resource has no '{CF_PREFIX}name' value")
    orchestrator_id = resource.get_value_for_custom_field(CF_PREFIX + "orchestrator_id")
    if not orchestrator_id:
        raise ValueError(f"resource has no '{CF_PREFIX}orchestrator_id' value")
    orchestrator = load_orchestrator(orchestrator_id)
    if orchestrator is None:
        raise ValueError(f"no Kubernetes container orchestrator with id={orchestrator_id}")
    cluster_name = resource.get_value_for_custom_field(CF_PREFIX + "cluster") or orchestrator.name
    return namespace, orchestrator, cluster_name


def load_cluster_resource(resource):
    """The Kubernetes Cluster resource hosting a namespace Resource, or None."""
    parent = getattr(resource, "parent_resource", None)
    if parent is not None and getattr(parent.resource_type, "name", "") == CLUSTER_RESOURCE_TYPE:
        return parent
    cluster_id = resource.get_value_for_custom_field(CF_PREFIX + "cluster_resource_id")
    if not cluster_id:
        return None
    return Resource.objects.filter(id=int(cluster_id)).first()


def store_tier_on_resource(resource, tier_key, tier):
    resource.set_value_for_custom_field(CF_PREFIX + "size_tier", tier_key)
    resource.set_value_for_custom_field(CF_PREFIX + "cpu_limit", tier["cpu"])
    resource.set_value_for_custom_field(CF_PREFIX + "memory_limit_gb", tier["memory_gb"])
    resource.set_value_for_custom_field(CF_PREFIX + "storage_limit_gb", tier["storage_gb"])
    resource.set_value_for_custom_field(CF_PREFIX + "max_pods", tier["pods"])


NAMESPACE_BLUEPRINT_ID = "BP-dogqi827"
NAMESPACE_BLUEPRINT_NAME = "AKS Namespace"


def namespace_blueprint():
    """The AKS Namespace blueprint, by global id then by name, or None."""
    from servicecatalog.models import ServiceBlueprint

    return (
        ServiceBlueprint.objects.filter(global_id=NAMESPACE_BLUEPRINT_ID).first()
        or ServiceBlueprint.objects.filter(name__iexact=NAMESPACE_BLUEPRINT_NAME).first()
    )


def new_namespace_resource(blueprint, namespace, group, owner=None, cluster=None):
    """The Resource a cluster-side action files the namespace under (as an order would)."""
    return Resource.objects.create(
        name=namespace, blueprint=blueprint, resource_type=blueprint.resource_type,
        group=group, owner=owner, parent_resource=cluster, lifecycle="ACTIVE",
    )


def owning_group_for_namespace(annotations):
    global_id = (annotations or {}).get(ANNOTATION_GROUP_ID, "")
    if not global_id:
        return None
    return Group.objects.filter(global_id=global_id).first()


# ---------------------------------------------------------------------------
# Managed namespaces over ARM
# ---------------------------------------------------------------------------
# https://learn.microsoft.com/en-us/rest/api/aks/managed-namespaces
#   PUT|GET|DELETE {cluster}/managedNamespaces/{name}; GET {cluster}/managedNamespaces;
#   POST {cluster}/managedNamespaces/{name}/listCredential.
# properties: defaultResourceQuota, defaultNetworkPolicy {ingress, egress},
# adoptionPolicy (Never | IfIdentical | Always), deletePolicy (Keep | Delete),
# labels, annotations; provisioningState and portalFqdn are read-only.
# PATCH changes tags only, so property changes are a GET, change, PUT round trip.
# Once a namespace is managed, AKS blocks changes to it (and to the quota and
# policies it manages) through the Kubernetes API; labels and annotations are
# changed here.

_MNS_READ_ONLY = ("provisioningState", "portalFqdn")


def managed_namespace_id(cluster, namespace):
    return f"{cluster.id}/managedNamespaces/{namespace}"


def get_managed_namespace(cluster, namespace, allow_404=True):
    """GET the managed namespace, or None.
    https://learn.microsoft.com/en-us/rest/api/aks/managed-namespaces/get
    """
    return cluster.arm.get_json(managed_namespace_id(cluster, namespace), allow_404=allow_404,
                                what=f"Reading managed namespace '{namespace}'")


def list_managed_namespaces(cluster):
    """Every managed namespace on the cluster.
    https://learn.microsoft.com/en-us/rest/api/aks/managed-namespaces/list-by-managed-cluster
    """
    return cluster.arm.list_json(f"{cluster.id}/managedNamespaces",
                                 what=f"Listing managed namespaces of '{cluster.name}'")


def put_managed_namespace(cluster, namespace, location, properties, tags=None, what=""):
    """PUT the managed namespace and wait. Returns it as AKS reports it.
    https://learn.microsoft.com/en-us/rest/api/aks/managed-namespaces/create-or-update
    """
    label = what or f"Updating managed namespace '{namespace}'"
    body = {"location": location, "tags": tags or {}, "properties": properties}
    resp = cluster.arm.call("PUT", managed_namespace_id(cluster, namespace), body=body, ok=(200, 201),
                            what=label)
    cluster.arm.wait(resp, what=label)
    return get_managed_namespace(cluster, namespace, allow_404=False)


def update_managed_namespace(cluster, namespace, mutate, what=""):
    """Read the managed namespace, let ``mutate(properties)`` change it, and PUT it back."""
    current = get_managed_namespace(cluster, namespace, allow_404=False)
    props = dict(current.get("properties") or {})
    mutate(props)
    for key in _MNS_READ_ONLY:
        props.pop(key, None)
    return put_managed_namespace(cluster, namespace, current.get("location"), props,
                                 tags=current.get("tags"), what=what)


def delete_managed_namespace(cluster, namespace):
    """DELETE the managed namespace and wait; False when it was already gone.

    With deletePolicy Delete, AKS deletes the Kubernetes namespace and
    everything in it too.
    https://learn.microsoft.com/en-us/rest/api/aks/managed-namespaces/delete
    """
    label = f"Deleting managed namespace '{namespace}'"
    resp = cluster.arm.call("DELETE", managed_namespace_id(cluster, namespace), ok=(200, 202, 204),
                            allow_404=True, what=label)
    if resp is None or resp.status_code == 204:
        return False
    cluster.arm.wait(resp, what=label)
    return True


def managed_namespace_kubeconfig(cluster, namespace):
    """POST listCredential: the namespace's user kubeconfig.

    Returned only when it holds no secret (an Entra ID cluster's exec-based
    kubeconfig); otherwise ''.
    https://learn.microsoft.com/en-us/rest/api/aks/managed-namespaces/list-credential
    """
    import base64
    import yaml

    resp = cluster.arm.call("POST", f"{managed_namespace_id(cluster, namespace)}/listCredential", ok=(200,),
                            what=f"Reading the kubeconfig of '{namespace}'")
    kubeconfigs = (resp.json() or {}).get("kubeconfigs") or []
    if not kubeconfigs:
        return ""
    text = base64.b64decode(kubeconfigs[0]["value"]).decode("utf-8")
    config = yaml.safe_load(text) or {}
    for user in config.get("users") or []:
        details = user.get("user") or {}
        if any(details.get(key) for key in ("client-key-data", "client-key", "token", "password")):
            return ""
    return text


def adoptable_namespace(item):
    """A short reason why a managed namespace cannot be adopted, or ''."""
    state = (item.get("properties") or {}).get("provisioningState")
    if state and state not in ("Succeeded",):
        return f"provisioning state {state}"
    return ""


# ---------------------------------------------------------------------------
# The landing zone itself, shared by the blueprint build and the cluster action
# ---------------------------------------------------------------------------

def create_landing_zone(resource, cluster_resource, orchestrator, namespace, tier_key, owner="",
                        expires=None, ingress="AllowSameNamespace", egress="AllowAll",
                        additional_group=None):
    """Create the managed namespace and everything around it, recording it on ``resource``.

    In Azure: the managed namespace with the tier's CPU and memory quota, the
    chosen ingress and egress policies, CloudBolt's labels, and annotations
    for owner, group, expiration, and environment. On the cluster: a
    ResourceQuota for storage, claims, and pods, and a LimitRange with
    default requests and limits. In CloudBolt: the Environment pinned to the
    namespace, entitled to ``resource.group`` and ``additional_group``, with
    the tier mirrored into its quota. ``resource`` becomes a sub-resource of
    the cluster as soon as the namespace exists. Inputs are assumed
    validated. Returns (status, output_msg, error_msg).
    """
    tier = get_tier(tier_key)
    ordering_group = getattr(resource, "group", None)
    try:
        cluster = cluster_for_resource(cluster_resource)
        cluster_json = cluster.get()
    except AKSError as exc:
        return "FAILURE", "", f"Could not read AKS cluster '{cluster_resource.name}': {exc}"

    try:
        if get_managed_namespace(cluster, namespace) is not None:
            return ("FAILURE", "", f"A managed namespace named '{namespace}' already exists on "
                    f"'{cluster.name}'. Choose a different name, or import it with the blueprint's discovery.")
        client = NamespaceClient.from_orchestrator(orchestrator)
        if client.get_namespace(namespace) is not None:
            return ("FAILURE", "", f"A Kubernetes namespace named '{namespace}' already exists on "
                    f"'{cluster.name}'. Choose a different name.")
    except (AKSError, NamespaceError) as exc:
        return "FAILURE", "", f"Could not check for an existing namespace: {exc}"
    except Exception as exc:
        logger.exception("Could not connect to the cluster API")
        return "FAILURE", "", f"Could not connect to the API of cluster '{cluster.name}': {exc}"

    # ---- 1. CloudBolt Environment pinned to the namespace ---------------
    # Created first so the namespace can carry its id; removed again when
    # the namespace cannot be created.
    try:
        env, env_created = ensure_environment_for_namespace(
            orchestrator, cluster.name, namespace,
            description=f"AKS namespace '{namespace}' ({tier['label']} tier) on {cluster.name}.",
            region=cluster_json.get("location", ""),
        )
    except Exception as exc:
        logger.exception("Environment creation failed")
        return "FAILURE", "", f"The CloudBolt environment for '{namespace}' could not be created: {exc}"

    # ---- 2. Managed namespace -------------------------------------------
    labels = {LANDING_ZONE_LABEL: "true", LABEL_TIER: tier_key,
              LABEL_RESOURCE_ID: label_value(resource.global_id)}
    if ordering_group is not None:
        labels[LABEL_GROUP] = label_value(ordering_group.name)
    annotations = {ANNOTATION_OWNER: owner or "", ANNOTATION_ENVIRONMENT: str(env.global_id)}
    if ordering_group is not None:
        annotations[ANNOTATION_GROUP_ID] = str(ordering_group.global_id)
    if expires:
        annotations[ANNOTATION_EXPIRES] = expires.isoformat()
    policy = {"ingress": ingress, "egress": egress}
    properties = {
        "defaultResourceQuota": managed_quota_for_tier(tier),
        "defaultNetworkPolicy": policy,
        "adoptionPolicy": "Never",
        "deletePolicy": "Delete",
        "labels": labels,
        "annotations": annotations,
    }
    set_progress(f"Creating managed namespace '{namespace}' on '{cluster.name}' ({tier['label']} tier, "
                 f"{describe_network_policy(policy)})...")
    try:
        mns = put_managed_namespace(cluster, namespace, cluster_json["location"], properties,
                                    tags={"cloudbolt-resource": str(resource.global_id)},
                                    what=f"Creating managed namespace '{namespace}'")
    except AKSError as exc:
        logger.exception("Managed namespace creation failed")
        if env_created:
            delete_environment(env)
        return "FAILURE", "", f"AKS could not create managed namespace '{namespace}': {exc}"

    resource.name = namespace
    resource.parent_resource = cluster_resource
    resource.set_value_for_custom_field(CF_PREFIX + "name", namespace)
    resource.set_value_for_custom_field(CF_PREFIX + "resource_id", mns.get("id", ""))
    resource.set_value_for_custom_field(CF_PREFIX + "cluster", cluster.name)
    resource.set_value_for_custom_field(CF_PREFIX + "cluster_resource_id", cluster_resource.id)
    resource.set_value_for_custom_field(CF_PREFIX + "orchestrator_id", orchestrator.id)
    resource.set_value_for_custom_field(CF_PREFIX + "network_policy", describe_network_policy(policy))
    resource.set_value_for_custom_field(CF_PREFIX + "environment_id", env.id)
    resource.set_value_for_custom_field(CF_PREFIX + "environment_name", env.name)
    store_tier_on_resource(resource, tier_key, tier)
    resource.save()

    # ---- 3. What AKS does not manage: object quota and default limits ---
    warnings = []
    try:
        ns_object = client.get_namespace(namespace) or {}
        resource.set_value_for_custom_field(CF_PREFIX + "uid", (ns_object.get("metadata") or {}).get("uid", ""))
        client.apply_resource_quota(namespace, object_quota_for_tier(tier))
        client.apply_limit_range(namespace)
        set_progress("Added the storage, claim, and pod quota and the default container limits.")
    except NamespaceError as exc:
        logger.exception("Object quota/LimitRange creation failed")
        warnings.append(f"The storage and pod quota or the default container limits could not be added: {exc}")
        set_progress(f"Warning: {warnings[-1]}")

    # ---- 4. Entitle groups and mirror the quota -------------------------
    set_progress(f"{'Created' if env_created else 'Reusing'} environment '{env.name}'.")
    for group in (ordering_group, additional_group):
        if group is not None and entitle_group(env, group):
            set_progress(f"Entitled CloudBolt group '{group.name}' to environment '{env.name}'.")
    if set_environment_quota(env, tier):
        set_progress(f"Environment quota set to {tier['cpu']} cores, {tier['memory_gb']} GB memory, "
                     f"{tier['storage_gb']} GB storage.")

    # ---- 5. Orchestrator inventory --------------------------------------
    if register_namespace_record(orchestrator, namespace):
        set_progress(f"Listed '{namespace}' under orchestrator '{orchestrator.name}'.")

    resource.set_value_for_custom_field(CF_PREFIX + "entitled_groups", "\n".join(entitled_group_names(env)))
    resource.save()

    groups_text = ", ".join(entitled_group_names(env)) or "none"
    expires_text = f"; expires {expires.isoformat()}" if expires else ""
    output = (f"Managed namespace '{namespace}' is ready on '{cluster.name}' ({tier['label']} tier). CloudBolt "
              f"groups entitled to deploy into it through environment '{env.name}': {groups_text}{expires_text}.")
    if warnings:
        output += " " + " ".join(warnings)
    return "SUCCESS", output, ""


def change_tier(resource, tier_key):
    """Move the namespace to another tier: managed quota, object quota, label, resource, environment.

    Returns a list of progress messages; raises AccessError or AKSError.
    """
    tier = get_tier(tier_key)
    namespace, cluster, _ = access_context(resource)

    def mutate(props):
        props["defaultResourceQuota"] = managed_quota_for_tier(tier)
        props.setdefault("labels", {})[LABEL_TIER] = tier_key

    update_managed_namespace(cluster, namespace, mutate, what=f"Changing the quota of '{namespace}'")
    messages = [f"AKS now enforces {tier['cpu']} cores and {tier['memory_gb']} GiB on '{namespace}'."]
    try:
        orchestrator = load_orchestrator(resource.get_value_for_custom_field(CF_PREFIX + "orchestrator_id"))
        NamespaceClient.from_orchestrator(orchestrator).apply_resource_quota(namespace, object_quota_for_tier(tier))
        messages.append(f"Storage, claim, and pod quota set to {tier['storage_gb']} GiB, {tier['pvcs']} claims, "
                        f"{tier['pods']} pods.")
    except Exception as exc:
        messages.append(f"Warning: the storage and pod quota could not be updated: {exc}")
    store_tier_on_resource(resource, tier_key, tier)
    env = load_environment(resource)
    if env is not None and set_environment_quota(env, tier):
        messages.append(f"Environment '{env.name}' quota mirrored.")
    resource.save()
    return messages


def set_expiration_annotation(resource, new_date):
    """Write the lease-expires annotation through ARM (the Kubernetes API may not change it)."""
    namespace, cluster, _ = access_context(resource)

    def mutate(props):
        props.setdefault("annotations", {})[ANNOTATION_EXPIRES] = new_date.isoformat()

    update_managed_namespace(cluster, namespace, mutate, what=f"Updating the expiration of '{namespace}'")


def quota_usage(resource):
    """Hard and used values of every ResourceQuota in the namespace: {name: {resource: (hard, used)}}.
    https://kubernetes.io/docs/reference/kubernetes-api/policy-resources/resource-quota-v1/
    """
    namespace, orchestrator, _ = load_cluster(resource)
    client = NamespaceClient.from_orchestrator(orchestrator)
    result = client._request("GET", f"/api/v1/namespaces/{namespace}/resourcequotas", allow_404=True) or {}
    usage = {}
    for item in result.get("items", []):
        hard = (item.get("status") or {}).get("hard") or (item.get("spec") or {}).get("hard") or {}
        used = (item.get("status") or {}).get("used") or {}
        usage[(item.get("metadata") or {}).get("name", "")] = {k: (v, used.get(k, "0")) for k, v in hard.items()}
    return usage


# ---------------------------------------------------------------------------
# CloudBolt-side helpers: the Environment pinned to the namespace
# ---------------------------------------------------------------------------

def environment_namespace(env):
    return env.get_cfv(ENV_NAMESPACE_FIELD)


def find_environment_for_namespace(orchestrator, namespace):
    for env in Environment.objects.filter(container_orchestrator_id=orchestrator.id):
        if environment_namespace(env) == namespace:
            return env
    return None


def set_environment_namespace(env, namespace):
    """Pin ``env`` to ``namespace`` through its custom_field_options (like set_env_location)."""
    from orders.models import CustomFieldValue

    field = CustomField.objects.get(name=ENV_NAMESPACE_FIELD)
    for current in env.custom_field_options.filter(field=field):
        env.custom_field_options.remove(current)
    cfv, _ = CustomFieldValue.objects.get_or_create(field=field, value=namespace)
    env.custom_field_options.add(cfv)
    env.save()


def cluster_region(cluster, orchestrator):
    """The cluster's Azure region, for the environment-name suffix."""
    region = str(cluster.get_cf_values_as_dict().get("aks_location") or "").strip()
    if region:
        return region
    for env in Environment.objects.filter(container_orchestrator_id=orchestrator.id).order_by("name"):
        if "#" in env.name:
            return env.name.rsplit("#", 1)[1].strip()
    return ""


def environment_name_for_namespace(cluster_name, namespace, region):
    """``<cluster> / <namespace>#<region>``, made unique without touching the suffix.

    The suffix matches the per-cluster ``Resource-<cluster> Environment#<region>``
    environment; it is load-bearing for EKS token minting and kept for AKS so
    every Kubernetes environment reads the same.
    """
    suffix = f"#{region}" if region else ""
    base = f"{cluster_name} / {namespace}"
    candidate = f"{base}{suffix}"
    counter = 2
    while Environment.objects.filter(name=candidate).exists():
        candidate = f"{base} ({counter}){suffix}"
        counter += 1
    return candidate


def ensure_environment_for_namespace(orchestrator, cluster_name, namespace, description="", region=""):
    existing = find_environment_for_namespace(orchestrator, namespace)
    if existing is not None:
        return existing, False
    env = Environment.objects.create(
        name=environment_name_for_namespace(cluster_name, namespace, region),
        container_orchestrator=orchestrator, description=description or "",
    )
    set_environment_namespace(env, namespace)
    env.refresh_from_db()
    return env, True


def entitle_group(env, group):
    if env in group.environments.all():
        return False
    group.environments.add(env)
    try:
        group.add_env_to_descendants(env)
    except Exception as exc:
        logger.warning("Could not propagate env %s to descendants of %s: %s", env, group, exc)
    return True


def revoke_group(env, group):
    if env not in group.environments.all():
        return False
    group.environments.remove(env)
    try:
        group.remove_env_from_descendants(env)
    except Exception as exc:
        logger.warning("Could not remove env %s from descendants of %s: %s", env, group, exc)
    return True


def entitled_groups(env):
    """Groups directly entitled to ``env`` (read from the Group side; groups_served is deprecated)."""
    return Group.objects.filter(environments=env).exclude(name__startswith="Unassigned")


def entitled_group_names(env):
    return sorted(g.name for g in entitled_groups(env))


def set_environment_quota(env, tier):
    """Mirror the tier into the Environment's server quota (informational for containers)."""
    quota_set = getattr(env, "quota_set", None)
    if quota_set is None:
        logger.warning("Environment %s has no quota set; skipping quota mirror.", env)
        return False
    try:
        quota_set.change_limit(cpu_cnt=tier["cpu"], mem_size=tier["memory_gb"], disk_size=tier["storage_gb"])
        quota_set.save()
        return True
    except Exception as exc:
        logger.warning("Could not set quota on environment %s: %s", env, exc)
        set_progress(f"Warning: could not update CloudBolt environment quota: {exc}")
        return False


def delete_environment(env):
    for group in list(entitled_groups(env)):
        revoke_group(env, group)
    if hasattr(env, "can_be_deleted") and not env.can_be_deleted():
        return f"Environment '{env.name}' still has dependents and was left in place."
    name = env.name
    env.delete()
    return f"Deleted CloudBolt environment '{name}'."


def load_environment(resource):
    env_id = resource.get_value_for_custom_field(CF_PREFIX + "environment_id")
    if not env_id:
        return None
    return Environment.objects.filter(id=env_id).first()


def register_namespace_record(orchestrator, namespace):
    """Best-effort: list the namespace under the orchestrator's Namespaces tab."""
    try:
        orchestrator.get_or_create_namespace(namespace)
        return True
    except Exception as exc:
        logger.warning("Could not register namespace %s on %s: %s", namespace, orchestrator, exc)
        return False


def remove_namespace_record(orchestrator, namespace):
    from containerorchestrators.models import ContainerNamespace

    try:
        deleted, _ = ContainerNamespace.objects.filter(name=namespace, container_orchestrator=orchestrator).delete()
        return bool(deleted)
    except Exception as exc:
        logger.warning("Could not remove namespace record %s on %s: %s", namespace, orchestrator, exc)
        return False


def set_resource_expiration(resource, new_date):
    value = datetime.datetime.combine(new_date, datetime.time(hour=23, minute=59))
    resource.set_value_for_custom_field("expiration_date", value)


# ---------------------------------------------------------------------------
# Namespace access through Azure RBAC
# ---------------------------------------------------------------------------
# A managed namespace takes two role assignments per person, both scoped to
# the managed namespace's ARM id: the control-plane role Azure Kubernetes
# Service Namespace User (see the namespace, list its credential) and a
# data-plane role (RBAC Reader, Writer, or Admin) that AKS enforces inside it.
# The data-plane roles need Azure RBAC for Kubernetes authorization on the
# cluster, which the Azure Kubernetes Service blueprint enables.
#   https://learn.microsoft.com/en-us/azure/aks/managed-namespaces#assign-role
#   https://learn.microsoft.com/en-us/azure/aks/concepts-managed-namespaces#managed-namespaces-built-in-roles
#   https://learn.microsoft.com/en-us/azure/role-based-access-control/built-in-roles/containers
# Assignments CloudBolt makes carry ACCESS_DESCRIPTION, so revoke and
# teardown only ever delete CloudBolt's own.

class AccessError(Exception):
    """A readable problem with namespace access."""


# Access level -> data-plane built-in role (name, role definition GUID).
ACCESS_LEVELS = OrderedDict([
    ("edit", dict(label="Writer", role="Azure Kubernetes Service RBAC Writer",
                  guid="a7ffa36f-339b-4b5c-8bdf-e2c188b2c0eb",
                  description="create and change workloads")),
    ("view", dict(label="Reader", role="Azure Kubernetes Service RBAC Reader",
                  guid="7f6c6a51-bcf8-42ba-9220-52d62157d7db",
                  description="read only")),
    ("admin", dict(label="Admin", role="Azure Kubernetes Service RBAC Admin",
                   guid="3498e952-d568-435e-9b2c-8d77e338d7f7",
                   description="everything in the namespace, including roles and role bindings")),
])
# Listed with the access levels so the access list shows everyone who can use
# the namespace, but never granted or revoked here.
LISTED_ONLY_LEVELS = OrderedDict([
    ("cluster-admin", dict(label="Cluster admin", role="Azure Kubernetes Service RBAC Cluster Admin",
                           guid="b1ff04bb-8a4e-4dc4-8eb5-8693973ce19b",
                           description="everything on the cluster")),
])
NAMESPACE_USER_ROLE = dict(role="Azure Kubernetes Service Namespace User",
                           guid="c9f76ca8-b262-4b10-8ed2-09cf0948aa35")

ACCESS_DESCRIPTION = "Managed by CloudBolt"
PRINCIPAL_TYPES = ("User", "Group", "ServicePrincipal")


def get_access_level(key):
    level = ACCESS_LEVELS.get(str(key or "").strip().lower())
    if not level:
        raise ValueError(f"Unknown access level '{key}'. Valid levels: {', '.join(ACCESS_LEVELS)}.")
    return level


def access_level_options(initial="edit"):
    options = [(key, f"{level['label']} - {level['description']}") for key, level in ACCESS_LEVELS.items()]
    return {"options": options, "initial_value": initial, "sort": False}


def parse_principals(text):
    """Split free text into (object_id, principal_type) pairs.

    Each entry is a Microsoft Entra object ID, optionally prefixed with its
    type: ``Group:<id>``, ``User:<id>``, or ``ServicePrincipal:<id>``. The
    type lets Azure skip a directory lookup that fails for brand-new
    principals. Returns (principals, errors).
    """
    principals, errors, seen = [], [], set()
    for token in re.split(r"[\s,;]+", str(text or "").strip()):
        if not token:
            continue
        principal_type, _, object_id = token.rpartition(":")
        guid = guid_or_none(object_id)
        matched_type = next((t for t in PRINCIPAL_TYPES if t.lower() == principal_type.lower()), "")
        if not guid or (principal_type and not matched_type):
            errors.append(f"'{token}' is not a Microsoft Entra object ID (optionally Group:, User:, or "
                          "ServicePrincipal: followed by the ID).")
            continue
        if guid not in seen:
            seen.add(guid)
            principals.append((guid, matched_type))
    return principals, errors


def _role_guid(assignment):
    return str((assignment.get("properties") or {}).get("roleDefinitionId", "")).rsplit("/", 1)[-1].lower()


def _ours(assignment):
    return str((assignment.get("properties") or {}).get("description") or "").startswith(ACCESS_DESCRIPTION)


def _scope_of(assignment):
    return str((assignment.get("properties") or {}).get("scope") or "").rstrip("/").lower()


def _principal_of(assignment):
    return str((assignment.get("properties") or {}).get("principalId", "")).lower()


def access_context(resource):
    """(namespace, AKSCluster, cluster_json) for a namespace Resource; AccessError names what is missing."""
    namespace = resource.get_value_for_custom_field(CF_PREFIX + "name") if resource else None
    if not namespace:
        raise AccessError(f"The resource has no '{CF_PREFIX}name' value.")
    cluster_resource = load_cluster_resource(resource)
    if cluster_resource is None:
        raise AccessError("The namespace records no cluster resource.")
    try:
        cluster = cluster_for_resource(cluster_resource)
        cluster_json = cluster.get()
    except AKSError as exc:
        raise AccessError(str(exc))
    return namespace, cluster, cluster_json


def require_azure_rbac(cluster_json):
    """Raise AccessError unless the cluster authorizes Kubernetes requests with Azure RBAC.
    https://learn.microsoft.com/en-us/azure/aks/manage-azure-rbac
    """
    aad = (cluster_json.get("properties") or {}).get("aadProfile") or {}
    if not aad.get("enableAzureRBAC"):
        raise AccessError(
            f"AKS cluster '{cluster_json.get('name')}' does not use Azure RBAC for Kubernetes authorization, "
            "so namespace access cannot be granted with role assignments. Enable it with "
            "az aks update --enable-azure-rbac: https://learn.microsoft.com/en-us/azure/aks/manage-azure-rbac"
        )


def _assignments_at(cluster, scope, principal_id=None):
    """Role assignments made exactly at ``scope`` (optionally for one principal)."""
    return [
        a for a in list_role_assignments(cluster.arm, scope, at_scope_only=True)
        if _scope_of(a) == scope.lower() and (principal_id is None or _principal_of(a) == principal_id.lower())
    ]


def _assign(cluster, scope, role_guid, principal_id, principal_type, description, retry=False):
    """assign_role, retried while a just-created identity replicates through Entra ID.
    https://learn.microsoft.com/en-us/azure/role-based-access-control/troubleshooting
    """
    attempts = 8 if retry else 1
    for attempt in range(attempts):
        try:
            return assign_role(cluster.arm, scope, role_guid, principal_id, principal_type, description)
        except AKSError as exc:
            if exc.code != "PrincipalNotFound" or attempt == attempts - 1:
                raise
            logger.info("Entra ID does not see %s yet; retrying", principal_id)
            time.sleep(10)


def grant_namespace_access(cluster, principal_id, namespace, level_key, principal_type="", note="",
                           retry=False):
    """Give a principal the access level on the managed namespace. Returns a message.

    Assigns Namespace User and the level's data-plane role at the managed
    namespace, and removes CloudBolt's assignments of the other levels there,
    so a principal holds one level per namespace.
    """
    level = get_access_level(level_key)
    scope = managed_namespace_id(cluster, namespace)
    suffix = f" ({note})" if note else ""
    for assignment in _assignments_at(cluster, scope, principal_id):
        other = next((l for k, l in ACCESS_LEVELS.items() if l["guid"] == _role_guid(assignment) and k != level_key),
                     None)
        if other is not None and _ours(assignment):
            delete_role_assignment(cluster.arm, assignment["id"])
    _assign(cluster, scope, NAMESPACE_USER_ROLE["guid"], principal_id, principal_type,
            f"{ACCESS_DESCRIPTION}: Namespace User on {namespace}{suffix}", retry=retry)
    created = _assign(cluster, scope, level["guid"], principal_id, principal_type,
                      f"{ACCESS_DESCRIPTION}: {level['label']} on {namespace}{suffix}", retry=retry)
    if not created:
        return f"'{principal_id}' already had {level['label']} access to '{namespace}'."
    return f"Granted '{principal_id}' Namespace User and {level['role']} on '{namespace}'."


def revoke_namespace_access(cluster, principal_id, namespace):
    """Remove CloudBolt's role assignments for the principal on the namespace. Returns a message.

    Assignments made outside CloudBolt are reported, never deleted.
    """
    scope = managed_namespace_id(cluster, namespace)
    names = {l["guid"]: l["label"] for l in ACCESS_LEVELS.values()}
    names[NAMESPACE_USER_ROLE["guid"]] = "Namespace User"
    removed, others = [], []
    for assignment in _assignments_at(cluster, scope, principal_id):
        name = names.get(_role_guid(assignment), _role_guid(assignment))
        if _ours(assignment):
            delete_role_assignment(cluster.arm, assignment["id"])
            removed.append(name)
        else:
            others.append(name)
    message = (f"Removed {', '.join(removed)} on '{namespace}' from '{principal_id}'." if removed
               else f"'{principal_id}' held no CloudBolt-granted access to '{namespace}'.")
    if others:
        message += f" It keeps {', '.join(others)}, which was not granted by CloudBolt."
    return message


def namespace_access(cluster, namespace):
    """Who can work in the namespace through an AKS RBAC role.

    Returns [{"principal", "principal_type", "level", "label", "scope", "managed"}]
    where scope is "namespace" (assigned on the managed namespace) or
    "cluster" (the cluster or anything above it), and managed says CloudBolt
    made it. Listing at a scope without a filter returns the assignments at,
    above, and below it.
    https://learn.microsoft.com/en-us/rest/api/authorization/role-assignments/list-for-scope
    """
    scope = managed_namespace_id(cluster, namespace)
    guids = {v["guid"]: (k, v) for k, v in list(ACCESS_LEVELS.items()) + list(LISTED_ONLY_LEVELS.items())}
    rows = []
    for assignment in list_role_assignments(cluster.arm, scope, at_scope_only=False):
        hit = guids.get(_role_guid(assignment))
        if hit is None:
            continue
        key, level = hit
        props = assignment.get("properties") or {}
        at = _scope_of(assignment)
        if at != scope.lower() and not scope.lower().startswith(at + "/"):
            continue  # another namespace or a sub-scope
        rows.append({
            "principal": props.get("principalId", ""),
            "principal_type": props.get("principalType", ""),
            "level": key,
            "label": level["label"],
            "scope": "namespace" if at == scope.lower() else "cluster",
            "managed": _ours(assignment),
        })
    return sorted(rows, key=lambda r: (r["scope"] != "namespace", r["principal"], r["level"]))


def remove_all_namespace_access(cluster, namespace):
    """Teardown: drop every CloudBolt role assignment on the namespace. Returns messages."""
    scope = managed_namespace_id(cluster, namespace)
    principals = sorted({_principal_of(a) for a in _assignments_at(cluster, scope) if _ours(a)})
    return [revoke_namespace_access(cluster, principal, namespace) for principal in principals]


def render_access(rows):
    scopes = {"namespace": "", "cluster": " (whole cluster)"}
    return "\n".join(
        f"{row['label']}: {row['principal_type'] + ' ' if row['principal_type'] else ''}{row['principal']}"
        f"{scopes[row['scope']]}" for row in rows
    )


def refresh_access_attribute(resource, cluster, namespace):
    """Re-read who has access and store it on the resource. Never raises."""
    try:
        rows = namespace_access(cluster, namespace)
    except Exception as exc:
        logger.warning("Could not list access to %s: %s", namespace, exc)
        return None
    resource.set_value_for_custom_field(CF_PREFIX + "entra_access", render_access(rows))
    return rows


def grant_team_access(resource, principals, level_key):
    """Build-time grant of the level to each (object_id, type). Returns (status, output, error)."""
    if not principals:
        return "SUCCESS", "", ""
    try:
        namespace, cluster, cluster_json = access_context(resource)
        require_azure_rbac(cluster_json)
        messages = []
        for principal_id, principal_type in principals:
            messages.append(grant_namespace_access(cluster, principal_id, namespace, level_key, principal_type))
            set_progress(messages[-1])
    except (AccessError, AKSError) as exc:
        return "FAILURE", "", str(exc)
    refresh_access_attribute(resource, cluster, namespace)
    resource.save()
    return "SUCCESS", " ".join(messages), ""


# ---------------------------------------------------------------------------
# Connection details
# ---------------------------------------------------------------------------

def connection_commands(subscription_id, resource_group, cluster_name, namespace, entra=True):
    """Shell commands that connect kubectl to the namespace.

    az aks namespace get-credentials (Azure CLI 2.80.0 or later):
      https://learn.microsoft.com/en-us/cli/azure/aks/namespace#az-aks-namespace-get-credentials
    kubelogin for Microsoft Entra ID clusters:
      https://learn.microsoft.com/en-us/azure/aks/kubelogin-authentication
    """
    commands = [
        "az login",
        f"az account set --subscription {subscription_id}",
        f"az aks namespace get-credentials --name {namespace} --cluster-name {cluster_name} "
        f"--resource-group {resource_group}",
    ]
    if entra:
        commands.append("kubelogin convert-kubeconfig -l azurecli")
    commands.append(f"kubectl get pods --namespace {namespace}")
    return commands


def connection_details(resource):
    """Everything the Connection Details action and the Connect tab show.

    Returns a dict: namespace, cluster_name, cluster_resource, resource_group,
    subscription_id, tenant_id, location, fqdn, auth_mode, azure_rbac,
    network_policy, commands, kubeconfig ('' unless the namespace's
    kubeconfig holds no secret), environment_name, portal_url (the managed
    namespace), pipelines (each with a workflow snippet), access, and
    access_error. Raises AccessError when the cluster cannot be read.
    """
    from shared_modules.azure_aks import describe_authentication, portal_url

    namespace, cluster, cluster_json = access_context(resource)
    props = cluster_json.get("properties") or {}
    aad = props.get("aadProfile") or {}
    entra = bool(aad.get("managed") or aad.get("tenantID"))
    mns = None
    try:
        mns = get_managed_namespace(cluster, namespace)
    except AKSError as exc:
        logger.warning("Could not read managed namespace %s: %s", namespace, exc)
    details = {
        "namespace": namespace,
        "cluster_name": cluster.name,
        "cluster_resource": load_cluster_resource(resource),
        "resource_group": cluster.resource_group,
        "subscription_id": cluster.arm.subscription_id,
        "tenant_id": cluster.arm.tenant_id,
        "location": cluster_json.get("location", ""),
        "fqdn": props.get("fqdn") or props.get("privateFQDN") or "",
        "auth_mode": describe_authentication(props),
        "azure_rbac": bool(aad.get("enableAzureRBAC")),
        "network_policy": describe_network_policy(((mns or {}).get("properties") or {}).get("defaultNetworkPolicy")),
        "commands": connection_commands(cluster.arm.subscription_id, cluster.resource_group, cluster.name,
                                        namespace, entra=entra),
        "kubeconfig": "",
        "environment_name": resource.get_value_for_custom_field(CF_PREFIX + "environment_name") or "",
        "portal_url": portal_url(cluster.arm, managed_namespace_id(cluster, namespace)),
        "access": [],
        "access_error": "",
        "pipelines": [
            dict(item, snippet=workflow_snippet(item, cluster.arm.tenant_id, cluster.arm.subscription_id,
                                                cluster.resource_group, cluster.name, namespace))
            for item in load_pipeline_identities(resource)
        ],
    }
    if mns is not None:
        try:
            details["kubeconfig"] = managed_namespace_kubeconfig(cluster, namespace)
        except AKSError as exc:
            logger.warning("Could not read the kubeconfig of %s: %s", namespace, exc)
    try:
        require_azure_rbac(cluster_json)
        details["access"] = namespace_access(cluster, namespace)
    except AccessError as exc:
        details["access_error"] = str(exc)
    except Exception as exc:
        logger.warning("Could not list access to %s: %s", namespace, exc)
        details["access_error"] = f"Could not list role assignments: {exc}"
    return details


# ---------------------------------------------------------------------------
# Pipeline identities: a managed identity a GitHub Actions workflow signs in as
# ---------------------------------------------------------------------------
# A user-assigned managed identity in the cluster's resource group trusts
# GitHub's OIDC issuer through federated identity credentials, so the
# workflow signs in with azure/login and no secret. The identity gets the
# same two role assignments a person does on the managed namespace.
#   https://learn.microsoft.com/en-us/entra/workload-id/workload-identity-federation-create-trust-user-assigned-managed-identity
#   https://learn.microsoft.com/en-us/rest/api/managedidentity/user-assigned-identities/create-or-update
#   https://learn.microsoft.com/en-us/rest/api/managedidentity/federated-identity-credentials/create-or-update
#   https://learn.microsoft.com/en-us/entra/workload-id/workload-identity-federation-considerations
#   https://docs.github.com/en/actions/reference/security/oidc
# What this code relies on, from those pages:
#   * issuer https://token.actions.githubusercontent.com, audience
#     api://AzureADTokenExchange, at most 20 credentials per identity, and
#     credentials created one at a time (concurrent writes answer 409);
#   * a federated credential matches one exact subject (no wildcards), so a
#     branch or a GitHub environment is required;
#   * repositories created, renamed, or transferred after July 15, 2026 send
#     repo:OWNER@OWNER-ID/REPO@REPO-ID:... instead of repo:OWNER/REPO:..., so a
#     credential is created for each form.

GITHUB_OIDC_ISSUER = "https://token.actions.githubusercontent.com"
FEDERATED_AUDIENCE = "api://AzureADTokenExchange"
GITHUB_API = "https://api.github.com"
GITHUB_API_VERSION = "2026-03-10"

PIPELINE_TAG_RESOURCE = "cloudbolt-resource-id"
PIPELINE_TAG_REPOSITORY = "cloudbolt-repository"
PIPELINE_TAG_TRIGGER = "cloudbolt-trigger"

REPOSITORY_RE = re.compile(r"^([A-Za-z0-9-]{1,39})(?:@(\d+))?/([A-Za-z0-9._-]{1,100})(?:@(\d+))?$")
TRIGGER_VALUE_RE = re.compile(r"^[^\s*?]{1,255}$")


def parse_repository(text):
    """``owner/repo`` or ``owner@ID/repo@ID`` -> (owner, repo, owner_id, repo_id)."""
    match = REPOSITORY_RE.match(str(text or "").strip())
    if not match:
        raise ValueError(f"'{text}' is not a GitHub repository. Give it as owner/repo, or as "
                         "owner@OWNER-ID/repo@REPO-ID.")
    owner, owner_id, repo, repo_id = match.groups()
    if bool(owner_id) != bool(repo_id):
        raise ValueError("Give both numeric ids (owner@OWNER-ID/repo@REPO-ID) or neither.")
    return owner, repo, owner_id, repo_id


def github_repository(owner, repo):
    """(owner, repo, owner_id, repo_id) as GitHub reports them, or None.
    https://docs.github.com/en/rest/repos/repos#get-a-repository
    """
    import requests
    from utilities.helpers import get_ssl_verification

    try:
        response = requests.get(
            f"{GITHUB_API}/repos/{owner}/{repo}",
            headers={"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": GITHUB_API_VERSION},
            timeout=15, verify=get_ssl_verification(),
        )
    except Exception as exc:
        logger.warning("Could not reach the GitHub API for %s/%s: %s", owner, repo, exc)
        return None
    if response.status_code != 200:
        return None
    data = response.json()
    full_owner, _, full_repo = str(data.get("full_name") or f"{owner}/{repo}").partition("/")
    return full_owner, full_repo, str((data.get("owner") or {}).get("id") or ""), str(data.get("id") or "")


def pipeline_trigger(branch, environment):
    """(subject suffix, human text) for the branch or environment; one is required."""
    if environment:
        return f"environment:{str(environment).replace(':', '%3A')}", f"environment {environment}"
    if branch:
        return f"ref:refs/heads/{str(branch).replace(':', '%3A')}", f"branch {branch}"
    raise ValueError("Give a branch or a GitHub environment: Azure federated credentials match one exact "
                     "subject and take no wildcards.")


def pipeline_subjects(owner, repo, owner_id, repo_id, suffix):
    prefixes = [f"repo:{owner}/{repo}"]
    if owner_id and repo_id:
        prefixes.append(f"repo:{owner}@{owner_id}/{repo}@{repo_id}")
    return [f"{prefix}:{suffix}" for prefix in prefixes]


def pipeline_identity_name(cluster_name, namespace, owner, repo, suffix):
    """Stable per namespace, repository, and trigger: 3-128 letters, digits, hyphens, underscores.
    https://learn.microsoft.com/en-us/azure/azure-resource-manager/management/resource-name-rules#microsoftmanagedidentity
    """
    digest = hashlib.sha1(
        f"{cluster_name}|{namespace}|{owner.lower()}/{repo.lower()}|{suffix}".encode("utf-8")
    ).hexdigest()[:10]
    return f"cb-aks-{namespace[:60]}-{digest}"


def _identity_path(cluster, name):
    return (f"/subscriptions/{cluster.arm.subscription_id}/resourceGroups/{cluster.resource_group}"
            f"/providers/Microsoft.ManagedIdentity/userAssignedIdentities/{name}")


def load_pipeline_identities(resource):
    raw = resource.get_value_for_custom_field(CF_PREFIX + "pipeline_identities") or ""
    try:
        identities = json.loads(raw) if raw else []
    except ValueError:
        logger.warning("Unreadable pipeline identities on %s: %r", resource, raw)
        identities = []
    return [item for item in identities if isinstance(item, dict) and item.get("identity_name")]


def store_pipeline_identities(resource, identities):
    resource.set_value_for_custom_field(
        CF_PREFIX + "pipeline_identities", json.dumps(identities, sort_keys=True) if identities else ""
    )


def workflow_snippet(identity, tenant_id, subscription_id, resource_group, cluster_name, namespace):
    """A GitHub Actions job that signs in as the identity and deploys into the namespace.

    actions/checkout: https://github.com/actions/checkout
    azure/login (OIDC with client-id, tenant-id, subscription-id): https://github.com/Azure/login
    azure/use-kubelogin: https://github.com/Azure/use-kubelogin
    id-token: write is required to request the OIDC token:
    https://learn.microsoft.com/en-us/azure/developer/github/connect-from-azure-openid-connect
    """
    lines = ["permissions:", "  id-token: write", "  contents: read", "jobs:", "  deploy:",
             "    runs-on: ubuntu-latest"]
    if identity.get("environment"):
        lines.append(f"    environment: {identity['environment']}")
    lines += [
        "    steps:",
        "      - uses: actions/checkout@v7",
        "      - uses: azure/login@v3",
        "        with:",
        f"          client-id: {identity.get('client_id', '')}",
        f"          tenant-id: {tenant_id}",
        f"          subscription-id: {subscription_id}",
        "      - uses: azure/use-kubelogin@v1",
        "        with:",
        "          kubelogin-version: v0.2.21",
        f"      - run: az aks namespace get-credentials --name {namespace} --cluster-name {cluster_name} "
        f"--resource-group {resource_group}",
        "      - run: kubelogin convert-kubeconfig -l azurecli",
        f"      - run: kubectl apply --namespace {namespace} -f k8s/",
    ]
    return "\n".join(lines) + "\n"


def add_pipeline_identity(resource, repository, branch="", environment="", level_key="edit"):
    """Create (or update) the managed identity for a repository and grant it the namespace.

    Returns (identity dict, progress messages). Raises AccessError, ValueError, or AKSError.
    """
    if branch and environment:
        raise ValueError("Give a branch or an environment, not both.")
    for value in (branch, environment):
        if value and not TRIGGER_VALUE_RE.match(value):
            raise ValueError(f"'{value}' cannot be used: no spaces, * or ? in branch and environment names.")
    level = get_access_level(level_key)
    owner, repo, owner_id, repo_id = parse_repository(repository)
    suffix, trigger_text = pipeline_trigger(branch, environment)

    namespace, cluster, cluster_json = access_context(resource)
    require_azure_rbac(cluster_json)
    messages = []
    if not owner_id:
        found = github_repository(owner, repo)
        if found:
            owner, repo, owner_id, repo_id = found
        else:
            messages.append(
                f"GitHub did not return {owner}/{repo} (private, or api.github.com unreachable), so only the "
                "name-based subject is trusted. For a repository created after July 15, 2026, run this again "
                "with owner@OWNER-ID/repo@REPO-ID."
            )
    subjects = pipeline_subjects(owner, repo, owner_id, repo_id, suffix)

    name = pipeline_identity_name(cluster.name, namespace, owner, repo, suffix)
    path = _identity_path(cluster, name)
    existing = cluster.arm.get_json(path, api_version=MSI_API_VERSION, allow_404=True,
                                    what=f"Reading managed identity {name}")
    if existing is not None and (existing.get("tags") or {}).get(PIPELINE_TAG_RESOURCE) != str(resource.global_id):
        raise AccessError(f"Managed identity {name} exists and was not created for this namespace.")
    resp = cluster.arm.call(
        "PUT", path, api_version=MSI_API_VERSION, ok=(200, 201), what=f"Creating managed identity {name}",
        body={
            "location": cluster_json["location"],
            "tags": {
                PIPELINE_TAG_RESOURCE: str(resource.global_id),
                PIPELINE_TAG_REPOSITORY: f"{owner}/{repo}"[:256],
                PIPELINE_TAG_TRIGGER: trigger_text[:256],
                "cloudbolt-managed": "true",
            },
        },
    )
    msi = resp.json()
    principal_id = (msi.get("properties") or {}).get("principalId")
    client_id = (msi.get("properties") or {}).get("clientId")
    messages.append(f"{'Updated' if existing else 'Created'} managed identity {name} (client ID {client_id}).")

    # One federated credential per subject form, created one after the other.
    for subject in subjects:
        credential = "github-ids" if "@" in subject.split(":", 2)[1] else "github"
        cluster.arm.call(
            "PUT", f"{path}/federatedIdentityCredentials/{credential}", api_version=MSI_API_VERSION,
            ok=(200, 201), what=f"Trusting {subject}",
            body={"properties": {"issuer": GITHUB_OIDC_ISSUER, "subject": subject,
                                 "audiences": [FEDERATED_AUDIENCE]}},
        )
        messages.append(f"Trusted GitHub subject {subject}.")

    messages.append(grant_namespace_access(cluster, principal_id, namespace, level_key, "ServicePrincipal",
                                           note=f"pipeline {owner}/{repo} {trigger_text}", retry=True))
    identity = {
        "repository": f"{owner}/{repo}",
        "repository_ids": f"{owner_id}/{repo_id}" if owner_id else "",
        "trigger": trigger_text,
        "environment": environment or "",
        "identity_name": name,
        "identity_id": msi.get("id", path),
        "client_id": client_id,
        "principal_id": principal_id,
        "level": level_key,
        "subjects": subjects,
    }
    identities = [item for item in load_pipeline_identities(resource) if item["identity_name"] != name]
    store_pipeline_identities(resource, identities + [identity])
    refresh_access_attribute(resource, cluster, namespace)
    resource.save()
    messages.append(f"{level['label']} access for {owner}/{repo} ({trigger_text}) is ready.")
    return identity, messages


def remove_pipeline_identity(resource, identity):
    """Revoke the identity's access and delete it (its federated credentials go with it). Returns messages."""
    messages = []
    namespace = resource.get_value_for_custom_field(CF_PREFIX + "name")
    cluster_resource = load_cluster_resource(resource)
    if cluster_resource is None:
        raise AccessError("The namespace records no cluster resource, so its subscription is unknown.")
    cluster = cluster_for_resource(cluster_resource)
    if identity.get("principal_id"):
        try:
            messages.append(revoke_namespace_access(cluster, identity["principal_id"], namespace))
        except AKSError as exc:
            messages.append(f"Namespace access was not revoked: {exc}")
    path = _identity_path(cluster, identity["identity_name"])
    current = cluster.arm.get_json(path, api_version=MSI_API_VERSION, allow_404=True,
                                   what=f"Reading managed identity {identity['identity_name']}")
    if current is None:
        messages.append(f"Managed identity {identity['identity_name']} was already gone.")
    elif (current.get("tags") or {}).get(PIPELINE_TAG_RESOURCE) != str(resource.global_id):
        messages.append(f"Managed identity {identity['identity_name']} is not tagged for this namespace and "
                        "was left in place.")
    else:
        # https://learn.microsoft.com/en-us/rest/api/managedidentity/user-assigned-identities/delete
        cluster.arm.call("DELETE", path, api_version=MSI_API_VERSION, ok=(200, 204), allow_404=True,
                         what=f"Deleting managed identity {identity['identity_name']}")
        messages.append(f"Deleted managed identity {identity['identity_name']}.")
    store_pipeline_identities(resource, [
        item for item in load_pipeline_identities(resource) if item["identity_name"] != identity["identity_name"]
    ])
    refresh_access_attribute(resource, cluster, namespace)
    resource.save()
    return messages


def find_pipeline_identity(resource, repository, branch="", environment=""):
    owner, repo, _, _ = parse_repository(repository)
    suffix, _ = pipeline_trigger(branch, environment)
    wanted = f"{owner}/{repo}".lower()
    for item in load_pipeline_identities(resource):
        if item.get("repository", "").lower() == wanted and any(
            subject.endswith(":" + suffix) for subject in item.get("subjects", [])
        ):
            return item
    return None
