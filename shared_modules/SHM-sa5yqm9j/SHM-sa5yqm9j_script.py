"""
EKS Namespace shared module.

Shared by the "AWS EKS Namespace" blueprint's build, teardown, and discovery
plugins. It has two halves:

1. ``NamespaceClient`` -- a thin Kubernetes API client built on the API
   wrapper of the Kubernetes container orchestrator the AWS EKS blueprint
   registers for each cluster. The wrapper mints the cluster's bearer token
   from the AWS handler credentials and applies CloudBolt's SSL trust
   settings; this client only builds the requests. It creates and manages
   the namespace, ResourceQuota, LimitRange, and NetworkPolicies that make
   up a namespace landing zone. Every call site cites the API reference it
   follows (docs/agents/external-apis.md).

2. CloudBolt-side helpers -- the size-tier catalog, the custom fields stored
   on each namespace Resource, the RBAC-gated list of clusters a group may
   order a namespace on, and the Environment pinned to the namespace: one
   Environment per landing zone that points at the cluster's orchestrator,
   carries the namespace as its ``kubernetes_namespace`` value, is entitled
   to the ordering Group, and mirrors the tier into its quota. Container
   blueprints ordered into that Environment read the namespace from it.

This is the EKS twin of the OpenShift Project Landing Zone module
(shared_modules/SHM-qmiweowv).

Import from plugins as::

    from shared_modules.eks_namespace import NamespaceClient, TIERS, ...

Shared-module code is cached by the running CloudBolt process: after changing
this file and syncing, restart CloudBolt for the change to take effect.
"""

import datetime
import re
import time
from collections import OrderedDict

from accounts.models import Group
from common.methods import set_progress
from infrastructure.models import CustomField, Environment
from resources.models import Resource
from utilities.exceptions import CloudBoltException
from utilities.logger import ThreadLogger

logger = ThreadLogger(__name__)

# ---------------------------------------------------------------------------
# Naming constants shared by every plugin in the blueprint
# ---------------------------------------------------------------------------

# Labels / annotations stamped on the namespace so discovery can find landing
# zones and so cluster admins can see who owns what from kubectl.
LANDING_ZONE_LABEL = "cloudbolt.io/landing-zone"
LABEL_TIER = "cloudbolt.io/size-tier"
LABEL_GROUP = "cloudbolt.io/group"
LABEL_RESOURCE_ID = "cloudbolt.io/resource-id"
ANNOTATION_EXPIRES = "cloudbolt.io/lease-expires"
ANNOTATION_OWNER = "cloudbolt.io/owner"
ANNOTATION_ENVIRONMENT = "cloudbolt.io/environment-id"
# The owning CloudBolt Group's global id (labels cannot hold spaces, so the
# LABEL_GROUP value is only human-readable); discovery maps it back to the Group.
ANNOTATION_GROUP_ID = "cloudbolt.io/group-id"

# Names of the in-namespace objects the landing zone owns.
QUOTA_NAME = "cloudbolt-landing-zone"
LIMIT_RANGE_NAME = "cloudbolt-landing-zone"

# Namespaces Kubernetes reserves for itself.
# https://kubernetes.io/docs/concepts/overview/working-with-objects/namespaces/#initial-namespaces
RESERVED_NAMESPACE_PREFIXES = ("kube-",)
RESERVED_NAMESPACE_NAMES = {"default", "kube-system", "kube-public", "kube-node-lease"}

# DNS-1123 label: what Kubernetes accepts as a namespace name.
# https://kubernetes.io/docs/concepts/overview/working-with-objects/names/#dns-label-names
NAMESPACE_NAME_RE = re.compile(r"^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?$")

# The resource type the AWS EKS blueprint gives its clusters, and the custom
# field on those resources that names the Kubernetes orchestrator.
CLUSTER_RESOURCE_TYPE = "kubernetes_cluster"
CLUSTER_ORCHESTRATOR_FIELD = "container_orchestrator_id"

# The custom field that pins an Environment to a namespace. It is stored in
# the Environment's custom_field_options, the same way resource handlers
# store a location on a location-specific environment, so plugins read it as
# ``env.kubernetes_namespace`` or ``env.get_cfv("kubernetes_namespace")``.
ENV_NAMESPACE_FIELD = "kubernetes_namespace"

# Prefix for every custom field this blueprint stores on its Resources.
CF_PREFIX = "eks_namespace_"

# The custom fields persisted on each namespace Resource.
# (name, label, type, description, show_as_attribute)
# show_as_attribute=True puts the value in the Attributes panel on the resource
# page; the identifiers and wiring fields stay out of it.
CUSTOM_FIELDS = [
    (CF_PREFIX + "name", "Namespace", "STR",
     "Name of the Kubernetes namespace backing this landing zone.", True),
    (CF_PREFIX + "uid", "Namespace UID", "STR",
     "Kubernetes UID of the namespace; the unique identifier used by discovery.", False),
    (CF_PREFIX + "cluster", "EKS Cluster", "STR",
     "Name of the EKS cluster (CloudBolt Kubernetes Cluster resource) that hosts the namespace.", True),
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
     "Landing-zone size tier that sets the namespace ResourceQuota.", True),
    (CF_PREFIX + "cpu_limit", "CPU Limit (cores)", "INT",
     "Namespace quota for CPU (requests and limits), in cores.", True),
    (CF_PREFIX + "memory_limit_gb", "Memory Limit (GiB)", "INT",
     "Namespace quota for memory (requests and limits), in GiB.", True),
    (CF_PREFIX + "storage_limit_gb", "Storage Limit (GiB)", "INT",
     "Namespace quota for persistent volume claim requests, in GiB.", True),
    (CF_PREFIX + "max_pods", "Max Pods", "INT",
     "Namespace quota for the number of pods.", True),
    (CF_PREFIX + "network_isolation", "Network Isolation", "BOOL",
     "Whether the default-deny NetworkPolicy set was applied to the namespace.", True),
]


def ensure_custom_fields():
    """Create every custom field the blueprint persists, and keep it in sync.

    Idempotent. The eks_namespace_* fields are namespaced to this blueprint,
    so when one already exists its label, description, and show_as_attribute
    are updated to match the definition above. The environment-side
    kubernetes_namespace field is only created, never rewritten.
    """
    for name, label, cf_type, description, show_as_attribute in CUSTOM_FIELDS:
        cf, created = CustomField.objects.get_or_create(
            name=name,
            defaults=dict(
                label=label,
                description=description,
                type=cf_type,
                show_on_servers=False,
                show_as_attribute=show_as_attribute,
            ),
        )
        if created:
            continue
        changed = False
        for attr, value in (
            ("label", label),
            ("description", description),
            ("show_as_attribute", show_as_attribute),
        ):
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
                "Set by the AWS EKS Namespace blueprint; container blueprints deploy into it."
            ),
            type="STR",
            show_on_servers=False,
            show_as_attribute=False,
        ),
    )


# ---------------------------------------------------------------------------
# Size tiers
# ---------------------------------------------------------------------------

# Each tier sets the namespace ResourceQuota, which the cluster's API server
# enforces on every pod and persistent volume claim in the namespace, and is
# mirrored into the pinned Environment's quota (cpu_cnt / mem_size /
# disk_size) so CloudBolt shows the same numbers.
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
    """Return the tier dict for ``key`` (case-insensitive) or raise ValueError."""
    tier = TIERS.get(str(key or "").strip().lower())
    if not tier:
        valid = ", ".join(TIERS)
        raise ValueError(f"Unknown size tier '{key}'. Valid tiers: {valid}.")
    return tier


def tier_options(initial="small"):
    """Size-tier dropdown for a plugin action input (plain value/label tuples)."""
    options = [(key, f"{tier['label']} - {tier['description']}") for key, tier in TIERS.items()]
    return {"options": options, "initial_value": initial, "sort": False}


def quota_hard_for_tier(tier):
    """Translate a tier into a ResourceQuota ``spec.hard`` map.

    https://kubernetes.io/docs/concepts/policy/resource-quotas/
    Compute keys requests.cpu / limits.cpu / requests.memory / limits.memory;
    storage keys requests.storage / persistentvolumeclaims; object count
    ``pods``.
    """
    return {
        "requests.cpu": str(tier["cpu"]),
        "limits.cpu": str(tier["cpu"]),
        "requests.memory": f"{tier['memory_gb']}Gi",
        "limits.memory": f"{tier['memory_gb']}Gi",
        "requests.storage": f"{tier['storage_gb']}Gi",
        "persistentvolumeclaims": str(tier["pvcs"]),
        "pods": str(tier["pods"]),
    }


# Default container requests/limits. Required alongside a compute quota: with
# requests/limits quotas in place every new pod must set them, and the
# LimitRange supplies defaults for pods that do not.
# https://kubernetes.io/docs/concepts/policy/resource-quotas/ (LimitRange note)
# https://kubernetes.io/docs/reference/kubernetes-api/policy-resources/limit-range-v1/
DEFAULT_LIMIT_RANGE_ITEM = {
    "type": "Container",
    "default": {"cpu": "500m", "memory": "512Mi"},
    "defaultRequest": {"cpu": "100m", "memory": "128Mi"},
}


# ---------------------------------------------------------------------------
# Small utilities
# ---------------------------------------------------------------------------

def validate_namespace_name(name):
    """Return None if ``name`` is a valid, non-reserved namespace name, else a message."""
    if not name:
        return "A namespace name is required."
    if not NAMESPACE_NAME_RE.match(name):
        return (
            "Namespace name must be 1-63 lowercase alphanumerics or '-', and must "
            "start and end with an alphanumeric (DNS-1123 label)."
        )
    if name in RESERVED_NAMESPACE_NAMES or name.startswith(RESERVED_NAMESPACE_PREFIXES):
        return f"'{name}' is reserved for Kubernetes system namespaces; choose another name."
    return None


def label_value(value):
    """Coerce ``value`` into a valid Kubernetes label value.

    Label values are at most 63 chars of [A-Za-z0-9._-] and must start/end
    alphanumeric. https://kubernetes.io/docs/concepts/overview/working-with-objects/labels/#syntax-and-character-set
    """
    text = re.sub(r"[^A-Za-z0-9._-]+", "-", str(value or "")).strip("-._")
    return text[:63].rstrip("-._")


def to_date(value):
    """Coerce a datetime, date, or 'YYYY-MM-DD[...]' string to a date, else None."""
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
    """The Resource's CloudBolt expiration_date as a date, or None when unset.

    The blueprint declares the standard ``expiration_date`` parameter
    (destination Both), so CloudBolt stores it on the Resource at order time.
    """
    if resource is None:
        return None
    return to_date(resource.get_value_for_custom_field("expiration_date"))


def truthy(value):
    """Interpret a rendered BOOL plugin input."""
    return str(value or "").strip().lower() in ("true", "1", "yes", "on")


# ---------------------------------------------------------------------------
# Kubernetes API client on top of the orchestrator's API wrapper
# ---------------------------------------------------------------------------

class NamespaceError(Exception):
    """Raised when the Kubernetes API refuses a landing-zone request."""


class NamespaceClient:
    """Minimal Kubernetes API client for landing-zone objects.

    The orchestrator's API wrapper (``Kubernetes.get_api_wrapper()``) holds
    the endpoint, the bearer token (minted from the AWS handler for EKS
    clusters registered with auto-rotate), and CloudBolt's SSL trust
    settings; ``requests_get`` and friends add all three to every call.
    """

    def __init__(self, wrapper):
        self._wrapper = wrapper
        # get_clusterrole_url("v1", skip version) is "<scheme>://<host>[:port][/prefix]/api";
        # strip the trailing "/api" to get the root both API groups hang off.
        base = wrapper.get_clusterrole_url("v1", skip_base_url_version=True)
        self._root = base[: -len("/api")] if base.endswith("/api") else base

    @classmethod
    def from_orchestrator(cls, orchestrator):
        """Create a client from a cast ``Kubernetes`` container orchestrator."""
        return cls(orchestrator.get_api_wrapper())

    # -- transport ----------------------------------------------------------

    def _request(self, method, path, json=None, params=None, allow_404=False):
        """Issue a request; return parsed JSON, or None on 404 when ``allow_404``.

        The wrapper raises CloudBoltException for any status outside
        ``expected_statuses``; that is turned into NamespaceError so callers
        can report it without a traceback.
        """
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
        """Create ``body`` at ``path``, or replace it if an object named ``name`` exists.

        PUT requires the current metadata.resourceVersion, so the existing
        object is read first and its resourceVersion copied onto the replacement.
        """
        existing = self._request("GET", f"{path}/{name}", allow_404=True)
        if existing is None:
            return self._request("POST", path, json=body)
        body = dict(body)
        body.setdefault("metadata", {})["resourceVersion"] = existing["metadata"]["resourceVersion"]
        return self._request("PUT", f"{path}/{name}", json=body)

    # -- namespaces ---------------------------------------------------------
    # https://kubernetes.io/docs/reference/kubernetes-api/cluster-resources/namespace-v1/
    # POST /api/v1/namespaces; GET|PATCH|DELETE /api/v1/namespaces/{name};
    # GET /api/v1/namespaces?labelSelector=...; status.phase is Active or
    # Terminating.

    def get_namespace(self, name):
        return self._request("GET", f"/api/v1/namespaces/{name}", allow_404=True)

    def create_namespace(self, name, labels=None, annotations=None):
        body = {
            "apiVersion": "v1",
            "kind": "Namespace",
            "metadata": {"name": name, "labels": labels or {}, "annotations": annotations or {}},
        }
        return self._request("POST", "/api/v1/namespaces", json=body)

    def patch_namespace_metadata(self, name, labels=None, annotations=None):
        """Merge labels/annotations onto the namespace.

        The wrapper's requests_patch sends application/strategic-merge-patch+json,
        which merges metadata maps key by key.
        https://kubernetes.io/docs/tasks/manage-kubernetes-objects/update-api-object-kubectl-patch/
        """
        patch = {"metadata": {}}
        if labels is not None:
            patch["metadata"]["labels"] = labels
        if annotations is not None:
            patch["metadata"]["annotations"] = annotations
        return self._request("PATCH", f"/api/v1/namespaces/{name}", json=patch)

    def delete_namespace(self, name):
        """Delete the namespace. Returns False if it was already gone."""
        result = self._request("DELETE", f"/api/v1/namespaces/{name}", allow_404=True)
        return result is not None

    def wait_for_namespace_deleted(self, name, timeout=300, interval=5):
        """Poll until the namespace returns 404. Returns True when gone."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.get_namespace(name) is None:
                return True
            time.sleep(interval)
        return False

    def list_landing_zone_namespaces(self):
        """Namespaces created by this blueprint, selected by the landing-zone label."""
        result = self._request(
            "GET", "/api/v1/namespaces", params={"labelSelector": f"{LANDING_ZONE_LABEL}=true"}
        )
        return (result or {}).get("items", [])

    # -- quota & limits -----------------------------------------------------
    # https://kubernetes.io/docs/reference/kubernetes-api/policy-resources/resource-quota-v1/
    # POST /api/v1/namespaces/{ns}/resourcequotas; GET|PUT .../resourcequotas/{name};
    # spec.hard is a map of resource name -> quantity; status.used mirrors usage.

    def get_resource_quota(self, namespace, name=QUOTA_NAME):
        return self._request(
            "GET", f"/api/v1/namespaces/{namespace}/resourcequotas/{name}", allow_404=True
        )

    def apply_resource_quota(self, namespace, hard, name=QUOTA_NAME):
        body = {
            "apiVersion": "v1",
            "kind": "ResourceQuota",
            "metadata": {"name": name, "namespace": namespace},
            "spec": {"hard": hard},
        }
        return self._apply(f"/api/v1/namespaces/{namespace}/resourcequotas", name, body)

    def apply_limit_range(self, namespace, item=None, name=LIMIT_RANGE_NAME):
        # https://kubernetes.io/docs/reference/kubernetes-api/policy-resources/limit-range-v1/
        # POST /api/v1/namespaces/{ns}/limitranges; spec.limits[] items carry
        # type / default / defaultRequest / max / min.
        body = {
            "apiVersion": "v1",
            "kind": "LimitRange",
            "metadata": {"name": name, "namespace": namespace},
            "spec": {"limits": [item or DEFAULT_LIMIT_RANGE_ITEM]},
        }
        return self._apply(f"/api/v1/namespaces/{namespace}/limitranges", name, body)

    # -- network policies ---------------------------------------------------
    # https://kubernetes.io/docs/reference/kubernetes-api/policy-resources/network-policy-v1/
    # POST /apis/networking.k8s.io/v1/namespaces/{ns}/networkpolicies
    # Deny all ingress by default, then allow pods in the same namespace and
    # pods in kube-system (CoreDNS, metrics-server and other cluster
    # services). Namespaces carry the immutable kubernetes.io/metadata.name
    # label, which is what the kube-system selector matches:
    # https://kubernetes.io/docs/concepts/overview/working-with-objects/namespaces/#automatic-labelling
    # On EKS these policies are enforced only when the Amazon VPC CNI network
    # policy feature is on (or another policy engine is installed):
    # https://docs.aws.amazon.com/eks/latest/userguide/cni-network-policy.html

    NETWORK_POLICIES = [
        {
            "name": "deny-by-default",
            "spec": {"podSelector": {}, "policyTypes": ["Ingress"], "ingress": []},
        },
        {
            "name": "allow-same-namespace",
            "spec": {"podSelector": {}, "policyTypes": ["Ingress"],
                     "ingress": [{"from": [{"podSelector": {}}]}]},
        },
        {
            "name": "allow-from-kube-system",
            "spec": {"podSelector": {}, "policyTypes": ["Ingress"],
                     "ingress": [{"from": [{"namespaceSelector": {"matchLabels": {
                         "kubernetes.io/metadata.name": "kube-system"}}}]}]},
        },
    ]

    def apply_network_policies(self, namespace):
        """Create the isolation policy set. Returns the policy names applied."""
        applied = []
        for policy in self.NETWORK_POLICIES:
            body = {
                "apiVersion": "networking.k8s.io/v1",
                "kind": "NetworkPolicy",
                "metadata": {"name": policy["name"], "namespace": namespace,
                             "labels": {LANDING_ZONE_LABEL: "true"}},
                "spec": policy["spec"],
            }
            self._apply(
                f"/apis/networking.k8s.io/v1/namespaces/{namespace}/networkpolicies",
                policy["name"], body,
            )
            applied.append(policy["name"])
        return applied

    def list_managed_network_policies(self, namespace):
        """Names of the NetworkPolicies this blueprint created in the namespace."""
        result = self._request(
            "GET", f"/apis/networking.k8s.io/v1/namespaces/{namespace}/networkpolicies",
            params={"labelSelector": f"{LANDING_ZONE_LABEL}=true"}, allow_404=True,
        )
        return sorted(
            (item.get("metadata") or {}).get("name", "") for item in (result or {}).get("items", [])
        )

    # -- inventory ----------------------------------------------------------

    def count_workloads(self, namespace):
        """Count pods, deployments, services, and PVCs in the namespace.

        https://kubernetes.io/docs/reference/kubernetes-api/workload-resources/pod-v1/
        https://kubernetes.io/docs/reference/kubernetes-api/workload-resources/deployment-v1/
        https://kubernetes.io/docs/reference/kubernetes-api/service-resources/service-v1/
        https://kubernetes.io/docs/reference/kubernetes-api/config-and-storage-resources/persistent-volume-claim-v1/
        """
        counts = {}
        for key, path in (
            ("pods", f"/api/v1/namespaces/{namespace}/pods"),
            ("deployments", f"/apis/apps/v1/namespaces/{namespace}/deployments"),
            ("services", f"/api/v1/namespaces/{namespace}/services"),
            ("persistentvolumeclaims", f"/api/v1/namespaces/{namespace}/persistentvolumeclaims"),
        ):
            try:
                result = self._request("GET", path, allow_404=True)
                counts[key] = len((result or {}).get("items", []))
            except NamespaceError as exc:
                logger.warning("Could not count %s in %s: %s", key, namespace, exc)
                counts[key] = None
        return counts


# ---------------------------------------------------------------------------
# CloudBolt-side helpers: cluster resources and orchestrators
# ---------------------------------------------------------------------------

def resolve_group(group):
    """The group kwarg arrives as a Group or as its name depending on caller."""
    if group is None or isinstance(group, Group):
        return group
    return Group.objects.filter(name=str(group)).first()


def orchestrator_id_for_cluster(cluster_resource):
    """The Kubernetes orchestrator id stored on a Kubernetes Cluster resource, or None."""
    value = cluster_resource.get_cf_values_as_dict().get(CLUSTER_ORCHESTRATOR_FIELD)
    try:
        return int(str(value).strip()) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def cluster_resources():
    """Every active Kubernetes Cluster resource that has an orchestrator, with its id.

    Returns [(resource, orchestrator_id), ...] sorted by resource name.
    """
    clusters = []
    for resource in Resource.objects.filter(
        resource_type__name=CLUSTER_RESOURCE_TYPE, lifecycle="ACTIVE"
    ).order_by("name"):
        orchestrator_id = orchestrator_id_for_cluster(resource)
        if orchestrator_id is not None:
            clusters.append((resource, orchestrator_id))
    return clusters


def cluster_resources_for_group(group):
    """Clusters the group may order a namespace on, as [(resource, orchestrator_id), ...].

    Gate: the cluster's orchestrator must back a cluster Environment the
    group can use. ``Group.get_available_environments()`` returns the
    environments explicitly entitled to the group (and its ancestors) PLUS
    every unconstrained environment, so a cluster whose per-cluster
    environment has no groups assigned is offered to everyone, and a
    platform team narrows that by assigning groups to the environment. An
    appliance that restricts new environments (GlobalPreferences
    restrict_new_environments) puts every new environment in the Unassigned
    group instead, so there the cluster is offered to nobody until a group
    is assigned.
    Namespace environments created by this blueprint also point at the
    orchestrator but are excluded, so holding one namespace on a cluster
    grants nothing beyond it. See docs/agents/rbac-and-security.md.
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
    """Dropdown label: cluster name with region and Kubernetes version when known."""
    values = cluster_resource.get_cf_values_as_dict()
    details = []
    if values.get("aws_region"):
        details.append(str(values["aws_region"]))
    if values.get("kubernetes_version"):
        details.append(f"Kubernetes {values['kubernetes_version']}")
    return f"{cluster_resource.name} ({', '.join(details)})" if details else cluster_resource.name


def load_orchestrator(orchestrator_id):
    """Return the Kubernetes orchestrator with ``orchestrator_id``, or None."""
    from containerorchestrators.kuberneteshandler.models import Kubernetes

    if orchestrator_id in (None, ""):
        return None
    return Kubernetes.objects.filter(id=int(orchestrator_id)).first()


def load_cluster(resource):
    """Return (namespace, orchestrator, cluster_name) from a namespace Resource's fields.

    Raises ValueError naming the missing piece so callers can report precisely.
    """
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


def store_tier_on_resource(resource, tier_key, tier):
    """Write the tier and its limits onto the Resource's custom fields."""
    resource.set_value_for_custom_field(CF_PREFIX + "size_tier", tier_key)
    resource.set_value_for_custom_field(CF_PREFIX + "cpu_limit", tier["cpu"])
    resource.set_value_for_custom_field(CF_PREFIX + "memory_limit_gb", tier["memory_gb"])
    resource.set_value_for_custom_field(CF_PREFIX + "storage_limit_gb", tier["storage_gb"])
    resource.set_value_for_custom_field(CF_PREFIX + "max_pods", tier["pods"])


# The blueprint whose resources represent namespace landing zones, so the
# cluster-side Create action can file its namespaces under the same
# blueprint as ordered ones (same resource type, day-2 actions, teardown).
NAMESPACE_BLUEPRINT_ID = "BP-e4c0q58z"
NAMESPACE_BLUEPRINT_NAME = "AWS EKS Namespace"


def namespace_blueprint():
    """The AWS EKS Namespace blueprint, by global id then by name, or None."""
    from servicecatalog.models import ServiceBlueprint

    return (
        ServiceBlueprint.objects.filter(global_id=NAMESPACE_BLUEPRINT_ID).first()
        or ServiceBlueprint.objects.filter(name__iexact=NAMESPACE_BLUEPRINT_NAME).first()
    )


def new_namespace_resource(blueprint, namespace, group, owner=None, cluster=None):
    """Create the Resource record a cluster-side action files the namespace under.

    Mirrors what an order creates: the blueprint's resource type, lifecycle
    ACTIVE, owned by ``group`` (the team) and ``owner`` (the admin running the
    action), and a sub-resource of ``cluster`` so the cluster page lists it.
    """
    return Resource.objects.create(
        name=namespace,
        blueprint=blueprint,
        resource_type=blueprint.resource_type,
        group=group,
        owner=owner,
        parent_resource=cluster,
        lifecycle="ACTIVE",
    )


def owning_group_for_namespace(annotations):
    """The CloudBolt Group a landing-zone namespace records in its annotations, or None."""
    global_id = (annotations or {}).get(ANNOTATION_GROUP_ID, "")
    if not global_id:
        return None
    return Group.objects.filter(global_id=global_id).first()


# ---------------------------------------------------------------------------
# The landing zone itself, shared by the blueprint build and the cluster action
# ---------------------------------------------------------------------------

def create_landing_zone(resource, cluster, orchestrator, namespace, tier_key, owner="",
                        expires=None, network_isolation=True, additional_group=None):
    """Create the namespace and everything around it, recording it on ``resource``.

    On the cluster: namespace, ResourceQuota, LimitRange, and (when
    ``network_isolation``) the default-deny NetworkPolicy set. In CloudBolt:
    the Environment pinned to the namespace, entitled to ``resource.group``
    and ``additional_group``, with the tier mirrored into its quota, and the
    namespace listed under the orchestrator. Inputs are assumed validated.
    The namespace identity is written to ``resource`` as soon as the
    namespace exists, so teardown can clean up after a later failure, and
    ``resource`` becomes a sub-resource of ``cluster``: the cluster page lists
    its namespaces, Delete Cluster refuses while any remain, and a cluster
    deleted anyway takes them along instead of orphaning them.

    Returns a plugin-style (status, output_msg, error_msg) tuple.
    """
    tier = get_tier(tier_key)
    ordering_group = getattr(resource, "group", None)

    try:
        client = NamespaceClient.from_orchestrator(orchestrator)
    except Exception as exc:
        logger.exception("Could not connect to the cluster API")
        return "FAILURE", "", f"Could not connect to the API of cluster '{cluster.name}': {exc}"

    # ---- Refuse to adopt an existing namespace --------------------------
    try:
        if client.get_namespace(namespace) is not None:
            return (
                "FAILURE",
                "",
                f"A namespace named '{namespace}' already exists on '{cluster.name}'. Choose "
                "a different name, or import it with the blueprint's discovery plugin.",
            )
    except NamespaceError as exc:
        return "FAILURE", "", f"Could not check for an existing namespace: {exc}"

    # ---- 1. Namespace ---------------------------------------------------
    labels = {LANDING_ZONE_LABEL: "true", LABEL_TIER: tier_key}
    if ordering_group is not None:
        labels[LABEL_GROUP] = label_value(ordering_group.name)
    labels[LABEL_RESOURCE_ID] = label_value(resource.global_id)

    annotations = {ANNOTATION_OWNER: owner or ""}
    if ordering_group is not None:
        annotations[ANNOTATION_GROUP_ID] = str(ordering_group.global_id)
    if expires:
        annotations[ANNOTATION_EXPIRES] = expires.isoformat()

    set_progress(f"Creating namespace '{namespace}' on '{cluster.name}'...")
    try:
        created_ns = client.create_namespace(namespace, labels=labels, annotations=annotations)
    except NamespaceError as exc:
        logger.exception("Namespace creation failed")
        return "FAILURE", "", f"Failed to create namespace '{namespace}': {exc}"

    resource.name = namespace
    resource.parent_resource = cluster
    resource.set_value_for_custom_field(CF_PREFIX + "name", namespace)
    resource.set_value_for_custom_field(
        CF_PREFIX + "uid", (created_ns.get("metadata") or {}).get("uid", "")
    )
    resource.set_value_for_custom_field(CF_PREFIX + "cluster", cluster.name)
    resource.set_value_for_custom_field(CF_PREFIX + "cluster_resource_id", cluster.id)
    resource.set_value_for_custom_field(CF_PREFIX + "orchestrator_id", orchestrator.id)
    resource.set_value_for_custom_field(CF_PREFIX + "network_isolation", network_isolation)
    store_tier_on_resource(resource, tier_key, tier)
    resource.save()

    # ---- 2. ResourceQuota + 3. LimitRange -------------------------------
    set_progress(f"Applying '{tier['label']}' quota: {tier['description']}...")
    try:
        client.apply_resource_quota(namespace, quota_hard_for_tier(tier))
        client.apply_limit_range(namespace)
    except NamespaceError as exc:
        logger.exception("Quota/LimitRange creation failed")
        return "FAILURE", "", f"Namespace created but quota could not be applied: {exc}"

    # ---- 4. NetworkPolicies ---------------------------------------------
    if network_isolation:
        set_progress("Applying default-deny NetworkPolicies...")
        try:
            applied = client.apply_network_policies(namespace)
            set_progress(f"Applied NetworkPolicies: {', '.join(applied)}.")
        except NamespaceError as exc:
            logger.exception("NetworkPolicy creation failed")
            return "FAILURE", "", f"Namespace created but NetworkPolicies could not be applied: {exc}"
    else:
        set_progress("Cross-namespace ingress allowed; no NetworkPolicies applied.")

    # ---- 5. CloudBolt Environment pinned to the namespace ---------------
    set_progress("Creating a CloudBolt environment pinned to the namespace...")
    try:
        env, created = ensure_environment_for_namespace(
            orchestrator,
            cluster.name,
            namespace,
            description=f"EKS namespace '{namespace}' ({tier['label']} tier) on {cluster.name}.",
            region=cluster_region(cluster, orchestrator),
        )
    except Exception as exc:
        logger.exception("Environment creation failed")
        return (
            "FAILURE",
            "",
            f"Namespace '{namespace}' was created on the cluster, but the CloudBolt "
            f"environment could not be created: {exc}",
        )
    set_progress(f"{'Created' if created else 'Reusing'} environment '{env.name}'.")

    # ---- 6. Entitle groups ----------------------------------------------
    for group in (ordering_group, additional_group):
        if group is None:
            continue
        if entitle_group(env, group):
            set_progress(f"Entitled CloudBolt group '{group.name}' to environment '{env.name}'.")

    # ---- 7. Mirror quota into the Environment ---------------------------
    if set_environment_quota(env, tier):
        set_progress(
            f"Environment quota set to {tier['cpu']} cores, {tier['memory_gb']} GB memory, "
            f"{tier['storage_gb']} GB storage."
        )

    # Point the namespace back at its CloudBolt environment.
    try:
        client.patch_namespace_metadata(
            namespace, annotations={ANNOTATION_ENVIRONMENT: str(env.global_id)}
        )
    except NamespaceError as exc:
        logger.warning("Could not annotate namespace with environment id: %s", exc)

    # ---- 8. Orchestrator inventory --------------------------------------
    if register_namespace_record(orchestrator, namespace):
        set_progress(f"Listed '{namespace}' under orchestrator '{orchestrator.name}'.")

    # ---- Persist the rest -----------------------------------------------
    resource.set_value_for_custom_field(CF_PREFIX + "environment_id", env.id)
    resource.set_value_for_custom_field(CF_PREFIX + "environment_name", env.name)
    resource.set_value_for_custom_field(
        CF_PREFIX + "entitled_groups", "\n".join(entitled_group_names(env))
    )
    resource.save()
    set_progress("Stored landing-zone metadata on the resource.")

    groups_text = ", ".join(entitled_group_names(env)) or "none"
    expires_text = f"; expires {expires.isoformat()}" if expires else ""
    return (
        "SUCCESS",
        f"Namespace '{namespace}' is ready on '{cluster.name}' ({tier['label']} tier). "
        f"CloudBolt groups entitled to deploy into it through environment '{env.name}': "
        f"{groups_text}{expires_text}.",
        "",
    )


# ---------------------------------------------------------------------------
# CloudBolt-side helpers: the Environment pinned to the namespace
# ---------------------------------------------------------------------------

def environment_namespace(env):
    """The namespace an Environment is pinned to, or None."""
    return env.get_cfv(ENV_NAMESPACE_FIELD)


def find_environment_for_namespace(orchestrator, namespace):
    """Return the Environment on ``orchestrator`` pinned to ``namespace``, or None."""
    for env in Environment.objects.filter(container_orchestrator_id=orchestrator.id):
        if environment_namespace(env) == namespace:
            return env
    return None


def set_environment_namespace(env, namespace):
    """Pin ``env`` to ``namespace`` through its custom_field_options.

    Mirrors ResourceHandler.set_env_location: one CustomFieldValue for the
    kubernetes_namespace field, replacing any earlier value.
    """
    from orders.models import CustomFieldValue

    field = CustomField.objects.get(name=ENV_NAMESPACE_FIELD)
    for current in env.custom_field_options.filter(field=field):
        env.custom_field_options.remove(current)
    cfv, _ = CustomFieldValue.objects.get_or_create(field=field, value=namespace)
    env.custom_field_options.add(cfv)
    env.save()


def cluster_region(cluster, orchestrator):
    """The AWS region of the cluster, for the environment-name suffix.

    From the cluster resource's aws_region, else from the region suffix of an
    environment already on the orchestrator (the per-cluster one is named
    ``Resource-<cluster> Environment#<region>``), else ''.
    """
    region = str(cluster.get_cf_values_as_dict().get("aws_region") or "").strip()
    if region:
        return region
    for env in Environment.objects.filter(container_orchestrator_id=orchestrator.id).order_by("name"):
        if "#" in env.name:
            return env.name.rsplit("#", 1)[1].strip()
    return ""


def environment_name_for_namespace(cluster_name, namespace, region):
    """``<cluster> / <namespace>#<region>``, made unique without touching the suffix.

    The ``#<region>`` suffix is load-bearing: when a Kubernetes orchestrator
    has no resource handler of its own, CloudBolt's EKS token minting
    (``TechnologyWrapper.get_api_refresh_token``) takes the region from the
    orchestrator's first environment's name after ``#``. The per-cluster
    environment the AWS EKS blueprint creates follows that convention, and
    namespace environments sort before it, so they must too.
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
    """Create (or reuse) the CloudBolt Environment pinned to ``namespace``.

    The Environment points at the cluster's Kubernetes orchestrator, like
    the per-cluster environment the AWS EKS blueprint creates, carries the
    namespace as its kubernetes_namespace value, and keeps the ``#<region>``
    name suffix the orchestrator's token minting relies on. Returns
    (env, created).
    """
    existing = find_environment_for_namespace(orchestrator, namespace)
    if existing is not None:
        return existing, False

    env = Environment.objects.create(
        name=environment_name_for_namespace(cluster_name, namespace, region),
        container_orchestrator=orchestrator,
        description=description or "",
    )
    set_environment_namespace(env, namespace)
    env.refresh_from_db()
    return env, True


def entitle_group(env, group):
    """Make ``env`` orderable by ``group`` (and its descendant groups)."""
    if env in group.environments.all():
        return False
    group.environments.add(env)
    try:
        group.add_env_to_descendants(env)
    except Exception as exc:
        logger.warning("Could not propagate env %s to descendants of %s: %s", env, group, exc)
    return True


def revoke_group(env, group):
    """Remove ``env`` from ``group`` (and its descendants)."""
    if env not in group.environments.all():
        return False
    group.environments.remove(env)
    try:
        group.remove_env_from_descendants(env)
    except Exception as exc:
        logger.warning("Could not remove env %s from descendants of %s: %s", env, group, exc)
    return True


def entitled_groups(env):
    """Groups directly entitled to ``env`` via Group.environments.

    Read from the Group side: ``Environment.groups_served`` is a separate,
    deprecated relation and does not reflect ``group.environments.add(env)``.
    """
    return Group.objects.filter(environments=env).exclude(name__startswith="Unassigned")


def entitled_group_names(env):
    """Names of the Groups directly entitled to ``env`` (CloudBolt's Unassigned excluded)."""
    return sorted(g.name for g in entitled_groups(env))


def set_environment_quota(env, tier):
    """Mirror the tier into the Environment's quota so CloudBolt shows the same limits.

    ``Environment.quota_set`` is a ServerQuotaSet with cpu_cnt (cores),
    mem_size (GB), disk_size (GB), and vm_cnt quotas; QuotaSet.change_limit
    takes one keyword per quota. Containers do not count against a server
    quota, so this is informational for container orders and only binds VM
    orders, which a Kubernetes environment never receives. Returns True on
    success.
    """
    quota_set = getattr(env, "quota_set", None)
    if quota_set is None:
        logger.warning("Environment %s has no quota set; skipping quota mirror.", env)
        return False
    try:
        quota_set.change_limit(
            cpu_cnt=tier["cpu"],
            mem_size=tier["memory_gb"],
            disk_size=tier["storage_gb"],
        )
        quota_set.save()
        return True
    except Exception as exc:
        logger.warning("Could not set quota on environment %s: %s", env, exc)
        set_progress(f"Warning: could not update CloudBolt environment quota: {exc}")
        return False


def delete_environment(env):
    """Un-entitle every group and delete the environment. Returns a message."""
    for group in list(entitled_groups(env)):
        revoke_group(env, group)
    if hasattr(env, "can_be_deleted") and not env.can_be_deleted():
        return f"Environment '{env.name}' still has dependents and was left in place."
    name = env.name
    env.delete()
    return f"Deleted CloudBolt environment '{name}'."


def load_environment(resource):
    """Return the Environment recorded on the Resource, or None."""
    env_id = resource.get_value_for_custom_field(CF_PREFIX + "environment_id")
    if not env_id:
        return None
    return Environment.objects.filter(id=env_id).first()


# ---------------------------------------------------------------------------
# CloudBolt-side helpers: the orchestrator's namespace inventory
# ---------------------------------------------------------------------------

def register_namespace_record(orchestrator, namespace):
    """Best-effort: list the namespace under the orchestrator's Namespaces tab.

    ``ContainerOrchestrator.get_or_create_namespace`` reads the namespace from
    the cluster and stores a KubernetesNamespace record, the same thing the
    orchestrator's import does. Failure is logged, never raised.
    """
    try:
        orchestrator.get_or_create_namespace(namespace)
        return True
    except Exception as exc:
        logger.warning("Could not register namespace %s on %s: %s", namespace, orchestrator, exc)
        return False


def remove_namespace_record(orchestrator, namespace):
    """Best-effort: drop the orchestrator's inventory record for the namespace."""
    from containerorchestrators.models import ContainerNamespace

    try:
        deleted, _ = ContainerNamespace.objects.filter(
            name=namespace, container_orchestrator=orchestrator
        ).delete()
        return bool(deleted)
    except Exception as exc:
        logger.warning("Could not remove namespace record %s on %s: %s", namespace, orchestrator, exc)
        return False
