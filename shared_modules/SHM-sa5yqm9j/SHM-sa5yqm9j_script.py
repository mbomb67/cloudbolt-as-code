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

3. EKS access and connection helpers -- namespace-scoped access entries
   that give IAM roles and users kubectl access to a landing zone (build
   input, Manage Namespace Access, teardown), pipeline identities (an IAM
   role a GitHub Actions workflow assumes through OIDC, created and deleted
   by Manage Pipeline Identity and teardown), and the kubeconfig and
   commands the Namespace Connection Details action and the extension's
   Connect tab show. These call the EKS and IAM APIs with the AWS handler
   behind the cluster resource.

This is the EKS twin of the OpenShift Project Landing Zone module
(shared_modules/SHM-qmiweowv).

Import from plugins as::

    from shared_modules.eks_namespace import NamespaceClient, TIERS, ...

Shared-module code is cached by the running CloudBolt process: after changing
this file and syncing, restart CloudBolt for the change to take effect.
"""

import datetime
import fnmatch
import json
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
    (CF_PREFIX + "iam_access", "IAM Access (kubectl)", "TXT",
     "IAM roles and users with an EKS access policy covering this namespace, one per line.", True),
    (CF_PREFIX + "pipeline_identities", "Pipeline Identities", "TXT",
     "IAM roles CloudBolt created for CI pipelines deploying to this namespace (JSON).", False),
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


# ---------------------------------------------------------------------------
# Expiration
# ---------------------------------------------------------------------------

def set_resource_expiration(resource, new_date):
    """Write ``new_date`` (a date) to the Resource's standard expiration_date parameter."""
    value = datetime.datetime.combine(new_date, datetime.time(hour=23, minute=59))
    resource.set_value_for_custom_field("expiration_date", value)


# ---------------------------------------------------------------------------
# The cluster behind a namespace Resource, as AWS sees it
# ---------------------------------------------------------------------------

def load_cluster_resource(resource):
    """The Kubernetes Cluster resource hosting a namespace Resource, or None.

    The parent link is authoritative; the stored cluster_resource_id covers
    namespaces recorded before the link existed.
    """
    parent = getattr(resource, "parent_resource", None)
    if parent is not None and getattr(parent.resource_type, "name", "") == CLUSTER_RESOURCE_TYPE:
        return parent
    cluster_id = resource.get_value_for_custom_field(CF_PREFIX + "cluster_resource_id")
    if not cluster_id:
        return None
    return Resource.objects.filter(id=int(cluster_id)).first()


def eks_cluster_name(cluster):
    """The EKS cluster name the AWS EKS blueprint stores on its resource."""
    return cluster.get_cf_values_as_dict().get("eks_cluster_name") or cluster.name


def cluster_aws(cluster):
    """(AWS handler, region) for an EKS cluster resource.

    Same lookup as the AWS EKS blueprint's own day-2 plugins: the environment
    the cluster was ordered into (env_id), else the handler and region its
    build and discovery store (aws_rh_id, aws_region). The handler stays
    server-side; users only ever pick environments
    (docs/agents/rbac-and-security.md).
    """
    from resourcehandlers.aws.models import AWSHandler

    values = cluster.get_cf_values_as_dict()
    env_id = values.get("env_id")
    if env_id:
        env = Environment.objects.filter(id=int(str(env_id).strip())).first()
        if env is not None and env.resource_handler_id and env.aws_region:
            return env.resource_handler.cast(), env.aws_region
    rh_id, region = values.get("aws_rh_id"), values.get("aws_region")
    if rh_id and region:
        rh = AWSHandler.objects.filter(id=int(str(rh_id).strip())).first()
        if rh is not None:
            return rh, str(region).strip()
    raise AccessEntryError(
        f"Cluster resource '{cluster.name}' records no AWS environment or handler "
        "(env_id, or aws_rh_id and aws_region)."
    )


def eks_client(cluster):
    """boto3 EKS client for the cluster's account and region, plus the region."""
    rh, region = cluster_aws(cluster)
    return rh.get_boto3_client(region, "eks"), region


# ---------------------------------------------------------------------------
# EKS access entries: kubectl access for IAM principals, scoped to a namespace
# ---------------------------------------------------------------------------
# Amazon EKS authorizes an IAM principal through an access entry on the
# cluster plus access policies associated with that entry, each with its own
# scope (the whole cluster, or a list of namespaces).
#   https://docs.aws.amazon.com/eks/latest/userguide/access-entries.html
#   https://docs.aws.amazon.com/eks/latest/userguide/creating-access-entries.html
#   https://docs.aws.amazon.com/eks/latest/userguide/access-policies.html
# What this code relies on, from those pages:
#   * an IAM principal can be in only one access entry per cluster;
#   * each access policy can be associated to an entry once; running
#     AssociateAccessPolicy again for the same policy replaces its scope,
#     which is how a namespace is added to or removed from the list;
#   * access entries need the cluster's authentication mode to be API or
#     API_AND_CONFIG_MAP;
#   * a role ARN may include a path (IAM Identity Center roles keep theirs);
#     STS session (assumed-role) ARNs and service-linked roles are refused.
# So a principal holding several landing zones on one cluster has ONE entry
# whose Edit (or View, or Admin) association lists all of those namespaces,
# and every grant or revoke rewrites that list. Two jobs changing the same
# principal at the same moment can race; the later write wins.

class AccessEntryError(Exception):
    """A readable problem with EKS access entries."""


# Access level -> EKS access policy. The policies' Kubernetes permissions:
# https://docs.aws.amazon.com/eks/latest/userguide/access-policy-permissions.html
ACCESS_LEVELS = OrderedDict([
    ("edit", dict(label="Edit", policy="AmazonEKSEditPolicy",
                  description="create and change workloads")),
    ("view", dict(label="View", policy="AmazonEKSViewPolicy",
                  description="read only")),
    ("admin", dict(label="Admin", policy="AmazonEKSAdminPolicy",
                   description="everything in the namespace, including roles and role bindings")),
])

# Listed with the access levels so the access list shows everyone who can
# use the namespace, but never granted or revoked here: EKS cluster creators
# get it cluster-wide, and it can only be scoped to the whole cluster.
LISTED_ONLY_LEVELS = OrderedDict([
    ("cluster-admin", dict(label="Cluster admin", policy="AmazonEKSClusterAdminPolicy",
                           description="everything on the cluster")),
])

# Access entries CloudBolt creates carry this tag, so revoke and teardown
# delete only entries CloudBolt made, and only once nothing else uses them.
ACCESS_ENTRY_TAG_KEY = "cloudbolt.io/managed-by"
ACCESS_ENTRY_TAG_VALUE = "CloudBolt"

# IAM role or user ARN, any partition; a role path is allowed.
# https://docs.aws.amazon.com/IAM/latest/UserGuide/reference-arns.html
IAM_PRINCIPAL_RE = re.compile(
    r"^arn:(aws|aws-cn|aws-us-gov|aws-iso[a-z-]*):iam::\d{12}:(role|user)/[\w+=,.@/-]+$"
)
STS_ASSUMED_ROLE_RE = re.compile(r"^arn:[^:]+:sts::(\d{12}):assumed-role/([^/]+)/")


def get_access_level(key):
    """Return the access-level dict for ``key`` or raise ValueError."""
    level = ACCESS_LEVELS.get(str(key or "").strip().lower())
    if not level:
        raise ValueError(f"Unknown access level '{key}'. Valid levels: {', '.join(ACCESS_LEVELS)}.")
    return level


def access_level_options(initial="edit"):
    """Access-level dropdown for a plugin action input."""
    options = [(key, f"{level['label']} - {level['description']}") for key, level in ACCESS_LEVELS.items()]
    return {"options": options, "initial_value": initial, "sort": False}


def policy_arn(level_key, partition="aws"):
    """ARN of the EKS access policy behind an access level (or a listed-only one).

    Format from ListAccessPolicies: arn:<partition>:eks::aws:cluster-access-policy/<name>
    https://docs.aws.amazon.com/eks/latest/userguide/access-policies.html
    """
    level = LISTED_ONLY_LEVELS.get(level_key) or get_access_level(level_key)
    return f"arn:{partition}:eks::aws:cluster-access-policy/{level['policy']}"


def validate_principal_arn(arn):
    """Return None if ``arn`` can be given an access entry, else a message."""
    sts = STS_ASSUMED_ROLE_RE.match(arn or "")
    if sts:
        return (
            f"'{arn}' is a temporary STS session, which EKS access entries cannot use. "
            f"Give the IAM role's ARN instead: aws iam get-role --role-name {sts.group(2)} "
            "--query Role.Arn --output text"
        )
    if not IAM_PRINCIPAL_RE.match(arn or ""):
        return f"'{arn}' is not an IAM role or user ARN (arn:aws:iam::<account>:role/<name>)."
    if ":role/aws-service-role/" in arn:
        return f"'{arn}' is a service-linked role, which EKS access entries do not support."
    return None


def parse_principal_arns(text):
    """Split free text (newlines, commas, or spaces) into unique ARNs.

    Returns (arns, errors); ``errors`` holds one message per invalid ARN.
    """
    arns, errors = [], []
    for token in re.split(r"[\s,]+", str(text or "").strip()):
        if not token or token in arns:
            continue
        error = validate_principal_arn(token)
        if error:
            errors.append(error)
        else:
            arns.append(token)
    return arns, errors


def _error_code(exc):
    response = getattr(exc, "response", None) or {}
    return (response.get("Error") or {}).get("Code", "")


def aws_message(exc):
    """A botocore ClientError as 'Code: message', anything else as str()."""
    response = getattr(exc, "response", None) or {}
    error = response.get("Error") or {}
    if error:
        return f"{error.get('Code', 'Error')}: {error.get('Message', exc)}"
    return str(exc)


def describe_eks_cluster(eks, cluster_name):
    """DescribeCluster's ``cluster`` object.

    https://docs.aws.amazon.com/eks/latest/APIReference/API_DescribeCluster.html
    https://docs.aws.amazon.com/eks/latest/APIReference/API_Cluster.html
    (endpoint, certificateAuthority.data, accessConfig, arn)
    """
    try:
        return eks.describe_cluster(name=cluster_name)["cluster"]
    except Exception as exc:
        raise AccessEntryError(f"Could not read EKS cluster '{cluster_name}': {aws_message(exc)}")


def require_access_entries(cluster_info):
    """Raise AccessEntryError when the cluster only uses the aws-auth ConfigMap.

    accessConfig.authenticationMode is API, API_AND_CONFIG_MAP, or CONFIG_MAP.
    https://docs.aws.amazon.com/eks/latest/userguide/setting-up-access-entries.html
    """
    mode = (cluster_info.get("accessConfig") or {}).get("authenticationMode", "")
    if mode == "CONFIG_MAP":
        raise AccessEntryError(
            f"EKS cluster '{cluster_info.get('name')}' authenticates through the aws-auth "
            "ConfigMap only. Switch its authentication mode to API_AND_CONFIG_MAP to use access "
            "entries: https://docs.aws.amazon.com/eks/latest/userguide/setting-up-access-entries.html"
        )


def partition_of(cluster_info):
    """The AWS partition from the cluster ARN (arn:<partition>:eks:...)."""
    parts = str(cluster_info.get("arn") or "").split(":")
    return parts[1] if len(parts) > 1 and parts[1] else "aws"


def _describe_entry(eks, cluster_name, principal):
    """DescribeAccessEntry's ``accessEntry``, or None when the principal has none.

    https://docs.aws.amazon.com/eks/latest/APIReference/API_DescribeAccessEntry.html
    """
    try:
        return eks.describe_access_entry(clusterName=cluster_name, principalArn=principal)["accessEntry"]
    except Exception as exc:
        if _error_code(exc) == "ResourceNotFoundException":
            return None
        raise


def _associations(eks, cluster_name, principal):
    """{policyArn: accessScope} for the principal's entry, following nextToken.

    https://docs.aws.amazon.com/eks/latest/APIReference/API_ListAssociatedAccessPolicies.html
    """
    result, token = {}, None
    while True:
        params = {"clusterName": cluster_name, "principalArn": principal}
        if token:
            params["nextToken"] = token
        page = eks.list_associated_access_policies(**params)
        for item in page.get("associatedAccessPolicies", []):
            result[item["policyArn"]] = item.get("accessScope") or {}
        token = page.get("nextToken")
        if not token:
            return result


def _principals_with_policy(eks, cluster_name, policy):
    """Principals whose entries have ``policy`` associated, following nextToken.

    https://docs.aws.amazon.com/eks/latest/APIReference/API_ListAccessEntries.html
    (associatedPolicyArn returns only the entries associated to that policy)
    """
    principals, token = [], None
    while True:
        params = {"clusterName": cluster_name, "associatedPolicyArn": policy}
        if token:
            params["nextToken"] = token
        page = eks.list_access_entries(**params)
        principals.extend(page.get("accessEntries", []))
        token = page.get("nextToken")
        if not token:
            return principals


def _associate(eks, cluster_name, principal, policy, namespaces):
    """Associate ``policy`` scoped to ``namespaces``, replacing any earlier scope.

    https://docs.aws.amazon.com/eks/latest/APIReference/API_AssociateAccessPolicy.html
    Access entries are eventually consistent, so a just-created entry can
    briefly answer ResourceNotFoundException; retry a few times.
    https://docs.aws.amazon.com/eks/latest/userguide/creating-access-entries.html
    """
    for attempt in range(5):
        try:
            return eks.associate_access_policy(
                clusterName=cluster_name,
                principalArn=principal,
                policyArn=policy,
                accessScope={"type": "namespace", "namespaces": sorted(set(namespaces))},
            )
        except Exception as exc:
            if _error_code(exc) != "ResourceNotFoundException" or attempt == 4:
                raise
            time.sleep(3)


def _drop_namespace(eks, cluster_name, principal, policy, scope, namespace):
    """Remove ``namespace`` from a namespace-scoped association. Returns True if it was there.

    The association is rewritten without the namespace, or disassociated when
    it was the last one.
    https://docs.aws.amazon.com/eks/latest/APIReference/API_DisassociateAccessPolicy.html
    """
    if scope.get("type") != "namespace" or namespace not in (scope.get("namespaces") or []):
        return False
    remaining = [ns for ns in scope.get("namespaces") or [] if ns != namespace]
    if remaining:
        _associate(eks, cluster_name, principal, policy, remaining)
    else:
        eks.disassociate_access_policy(clusterName=cluster_name, principalArn=principal, policyArn=policy)
    return True


def _delete_entry_if_unused(eks, cluster_name, principal):
    """Delete the principal's entry when CloudBolt created it and nothing uses it any more.

    https://docs.aws.amazon.com/eks/latest/APIReference/API_DeleteAccessEntry.html
    Returns True when the entry was deleted.
    """
    entry = _describe_entry(eks, cluster_name, principal)
    if entry is None:
        return False
    if (entry.get("tags") or {}).get(ACCESS_ENTRY_TAG_KEY) != ACCESS_ENTRY_TAG_VALUE:
        return False
    if entry.get("kubernetesGroups") or _associations(eks, cluster_name, principal):
        return False
    eks.delete_access_entry(clusterName=cluster_name, principalArn=principal)
    return True


def grant_namespace_access(eks, cluster_info, principal, namespace, level_key):
    """Give ``principal`` the access level on ``namespace``. Returns a message.

    Creates a STANDARD access entry (tagged as CloudBolt's) when the
    principal has none, adds the namespace to the level's policy
    association, and takes the namespace out of the other levels'
    namespace-scoped associations so the principal holds one level per
    namespace. A cluster-wide association of the same policy already covers
    the namespace and is left alone; cluster-wide associations are never
    rewritten.
    https://docs.aws.amazon.com/eks/latest/APIReference/API_CreateAccessEntry.html
    """
    cluster_name = cluster_info["name"]
    level = get_access_level(level_key)
    partition = partition_of(cluster_info)
    target = policy_arn(level_key, partition)

    entry = _describe_entry(eks, cluster_name, principal)
    if entry is None:
        try:
            eks.create_access_entry(
                clusterName=cluster_name,
                principalArn=principal,
                type="STANDARD",
                tags={ACCESS_ENTRY_TAG_KEY: ACCESS_ENTRY_TAG_VALUE},
            )
        except Exception as exc:
            if _error_code(exc) != "ResourceInUseException":
                raise
    elif entry.get("type", "STANDARD") != "STANDARD":
        raise AccessEntryError(
            f"'{principal}' has a {entry.get('type')} access entry on '{cluster_name}'; "
            "access policies can only be associated to STANDARD entries."
        )

    associations = _associations(eks, cluster_name, principal)
    for other_key in ACCESS_LEVELS:
        other = policy_arn(other_key, partition)
        if other != target and other in associations:
            _drop_namespace(eks, cluster_name, principal, other, associations[other], namespace)

    scope = associations.get(target)
    if scope and scope.get("type") == "cluster":
        return f"'{principal}' already has {level['label']} access to every namespace on '{cluster_name}'."
    current = (scope or {}).get("namespaces") or []
    if namespace in current:
        return f"'{principal}' already had {level['label']} access to '{namespace}'."
    _associate(eks, cluster_name, principal, target, current + [namespace])
    return f"Granted '{principal}' {level['label']} access to namespace '{namespace}'."


def revoke_namespace_access(eks, cluster_info, principal, namespace):
    """Take ``namespace`` out of every level association the principal holds. Returns a message.

    Deletes the access entry when CloudBolt created it and it no longer
    grants anything. Cluster-wide associations and wildcard namespace
    patterns are left alone and reported, since they were not granted here.
    """
    cluster_name = cluster_info["name"]
    partition = partition_of(cluster_info)
    if _describe_entry(eks, cluster_name, principal) is None:
        return f"'{principal}' has no access entry on '{cluster_name}'."

    removed, remaining = [], []
    associations = _associations(eks, cluster_name, principal)
    for key, level in ACCESS_LEVELS.items():
        policy = policy_arn(key, partition)
        scope = associations.get(policy)
        if not scope:
            continue
        if _drop_namespace(eks, cluster_name, principal, policy, scope, namespace):
            removed.append(level["label"])
        elif scope.get("type") == "cluster":
            remaining.append(f"{level['label']} on every namespace")
        elif any(fnmatch.fnmatchcase(namespace, ns) for ns in scope.get("namespaces") or []):
            remaining.append(f"{level['label']} through a namespace pattern")

    if not removed:
        message = f"'{principal}' held no namespace-scoped access to '{namespace}'."
    else:
        message = f"Removed {', '.join(removed)} access to '{namespace}' from '{principal}'."
    if remaining:
        message += f" It still has {'; '.join(remaining)}, which was not granted here."
    if removed and _delete_entry_if_unused(eks, cluster_name, principal):
        message += " Its CloudBolt-created access entry had no other use and was deleted."
    return message


def namespace_access(eks, cluster_info, namespace):
    """Who can use ``namespace`` through the CloudBolt access levels or as cluster admin.

    Returns [{"principal", "level", "label", "scope"}] sorted by principal,
    where scope is "namespace" (granted on this namespace), "pattern"
    (a wildcard namespace that matches it), or "cluster" (every namespace).
    """
    cluster_name = cluster_info["name"]
    partition = partition_of(cluster_info)
    rows = []
    for key, level in list(ACCESS_LEVELS.items()) + list(LISTED_ONLY_LEVELS.items()):
        policy = policy_arn(key, partition)
        for principal in _principals_with_policy(eks, cluster_name, policy):
            scope = _associations(eks, cluster_name, principal).get(policy) or {}
            namespaces = scope.get("namespaces") or []
            if scope.get("type") == "cluster":
                where = "cluster"
            elif namespace in namespaces:
                where = "namespace"
            elif any(fnmatch.fnmatchcase(namespace, ns) for ns in namespaces):
                where = "pattern"
            else:
                continue
            rows.append({"principal": principal, "level": key, "label": level["label"], "scope": where})
    return sorted(rows, key=lambda row: (row["principal"], row["level"]))


def remove_all_namespace_access(eks, cluster_info, namespace):
    """Teardown: drop ``namespace`` from every principal's level associations.

    Without this, a namespace created later with the same name would hand
    the old principals access again. Returns one message per principal changed.
    """
    principals = sorted({
        row["principal"] for row in namespace_access(eks, cluster_info, namespace)
        if row["scope"] == "namespace" and row["level"] in ACCESS_LEVELS
    })
    return [revoke_namespace_access(eks, cluster_info, principal, namespace) for principal in principals]


def render_access(rows):
    """Access rows as text for the resource's access attribute, one per line."""
    scopes = {"namespace": "", "pattern": " (namespace pattern)", "cluster": " (every namespace)"}
    return "\n".join(f"{row['label']}: {row['principal']}{scopes[row['scope']]}" for row in rows)


def refresh_access_attribute(resource, eks, cluster_info, namespace):
    """Re-read who has access and store it on the resource. Never raises."""
    try:
        rows = namespace_access(eks, cluster_info, namespace)
    except Exception as exc:
        logger.warning("Could not list access to %s: %s", namespace, exc)
        return None
    resource.set_value_for_custom_field(CF_PREFIX + "iam_access", render_access(rows))
    return rows


def access_context(resource):
    """(namespace, eks client, cluster_info) for a namespace Resource.

    Raises AccessEntryError naming what is missing, or when the cluster only
    authenticates through the aws-auth ConfigMap.
    """
    namespace = resource.get_value_for_custom_field(CF_PREFIX + "name") if resource else None
    if not namespace:
        raise AccessEntryError(f"The resource has no '{CF_PREFIX}name' value.")
    cluster = load_cluster_resource(resource)
    if cluster is None:
        raise AccessEntryError("The namespace records no cluster resource.")
    eks, _ = eks_client(cluster)
    cluster_info = describe_eks_cluster(eks, eks_cluster_name(cluster))
    require_access_entries(cluster_info)
    return namespace, eks, cluster_info


def grant_team_access(resource, principals, level_key):
    """Build-time grant: give each principal the level on the new namespace.

    Returns a plugin-style (status, output_msg, error_msg) tuple.
    """
    if not principals:
        return "SUCCESS", "", ""
    try:
        namespace, eks, cluster_info = access_context(resource)
        messages = []
        for principal in principals:
            messages.append(grant_namespace_access(eks, cluster_info, principal, namespace, level_key))
            set_progress(messages[-1])
    except AccessEntryError as exc:
        return "FAILURE", "", str(exc)
    except Exception as exc:
        logger.exception("Granting IAM access failed")
        return "FAILURE", "", f"Could not grant IAM access: {aws_message(exc)}"
    refresh_access_attribute(resource, eks, cluster_info, namespace)
    resource.save()
    return "SUCCESS", " ".join(messages), ""


# ---------------------------------------------------------------------------
# Connection details: how a team member points kubectl at the namespace
# ---------------------------------------------------------------------------

def _yaml_str(value):
    """A YAML scalar for ``value``: JSON string syntax is valid YAML."""
    return json.dumps(str(value))


def render_kubeconfig(cluster_info, region, namespace):
    """A kubeconfig for the namespace, equivalent to update-kubeconfig plus set-context.

    Holds no secret: the cluster endpoint and CA, and an exec block that runs
    ``aws eks get-token`` with the caller's own AWS credentials, which work
    only for an IAM principal with an access entry. Useful where the caller
    lacks eks:DescribeCluster, which update-kubeconfig needs.
      exec plugin: https://kubernetes.io/docs/reference/access-authn-authz/authentication/#client-go-credential-plugins
      get-token (ExecCredential, client.authentication.k8s.io/v1beta1):
        https://docs.aws.amazon.com/cli/latest/reference/eks/get-token.html
      context namespace: https://kubernetes.io/docs/concepts/configuration/organize-cluster-access-kubeconfig/
    """
    name = cluster_info["name"]
    arn = cluster_info.get("arn") or name
    context = f"{name}-{namespace}"
    ca = (cluster_info.get("certificateAuthority") or {}).get("data", "")
    lines = [
        "apiVersion: v1",
        "kind: Config",
        "clusters:",
        f"- name: {_yaml_str(arn)}",
        "  cluster:",
        f"    server: {_yaml_str(cluster_info.get('endpoint', ''))}",
        f"    certificate-authority-data: {_yaml_str(ca)}",
        "contexts:",
        f"- name: {_yaml_str(context)}",
        "  context:",
        f"    cluster: {_yaml_str(arn)}",
        f"    user: {_yaml_str(arn)}",
        f"    namespace: {_yaml_str(namespace)}",
        f"current-context: {_yaml_str(context)}",
        "users:",
        f"- name: {_yaml_str(arn)}",
        "  user:",
        "    exec:",
        "      apiVersion: client.authentication.k8s.io/v1beta1",
        "      command: aws",
        "      args:",
    ]
    for arg in ("--region", region, "eks", "get-token", "--cluster-name", name, "--output", "json"):
        lines.append(f"      - {_yaml_str(arg)}")
    return "\n".join(lines) + "\n"


def connection_commands(cluster_name, region, namespace):
    """The shell commands that connect kubectl to the namespace.

    update-kubeconfig: https://docs.aws.amazon.com/cli/latest/reference/eks/update-kubeconfig.html
    (--alias names the context; it defaults to the cluster ARN)
    set-context: https://kubernetes.io/docs/reference/kubectl/generated/kubectl_config/kubectl_config_set-context/
    """
    context = f"{cluster_name}-{namespace}"
    return [
        f"aws eks update-kubeconfig --region {region} --name {cluster_name} --alias {context}",
        f"kubectl config set-context {context} --namespace {namespace}",
        f"kubectl get pods --namespace {namespace}",
    ]


def connection_details(resource):
    """Everything the Connection Details action and the namespace tab show.

    Returns a dict: namespace, cluster_name, cluster_resource, region,
    endpoint, auth_mode, commands, kubeconfig, environment_name, pipelines
    (recorded pipeline identities, each with a workflow snippet), access (rows
    from namespace_access), and access_error (a message when the access list
    could not be read; the rest is still filled in). Raises AccessEntryError
    when the cluster itself cannot be read.
    """
    namespace = resource.get_value_for_custom_field(CF_PREFIX + "name")
    if not namespace:
        raise AccessEntryError(f"The resource has no '{CF_PREFIX}name' value.")
    cluster = load_cluster_resource(resource)
    if cluster is None:
        raise AccessEntryError("The namespace records no cluster resource.")
    eks, region = eks_client(cluster)
    cluster_info = describe_eks_cluster(eks, eks_cluster_name(cluster))
    name = cluster_info["name"]
    details = {
        "namespace": namespace,
        "cluster_name": name,
        "cluster_resource": cluster,
        "region": region,
        "endpoint": cluster_info.get("endpoint", ""),
        "auth_mode": (cluster_info.get("accessConfig") or {}).get("authenticationMode", ""),
        "commands": connection_commands(name, region, namespace),
        "kubeconfig": render_kubeconfig(cluster_info, region, namespace),
        "environment_name": resource.get_value_for_custom_field(CF_PREFIX + "environment_name") or "",
        "access": [],
        "access_error": "",
        "pipelines": [
            dict(item, snippet=workflow_snippet(
                item["role_arn"], region, name, namespace, item.get("environment", "")
            ))
            for item in load_pipeline_identities(resource)
        ],
    }
    try:
        require_access_entries(cluster_info)
        details["access"] = namespace_access(eks, cluster_info, namespace)
    except AccessEntryError as exc:
        details["access_error"] = str(exc)
    except Exception as exc:
        logger.warning("Could not list access to %s: %s", namespace, exc)
        details["access_error"] = f"Could not list IAM access: {aws_message(exc)}"
    return details


# ---------------------------------------------------------------------------
# Pipeline identities: an IAM role a GitHub Actions workflow assumes through
# OIDC, with kubectl access to the namespace
# ---------------------------------------------------------------------------
# A team's people bring their own IAM roles; a CI pipeline usually has none
# yet, so CloudBolt creates one per repository (and optional branch or
# environment), owns it, and deletes it on remove and teardown.
#   GitHub side: https://docs.github.com/en/actions/security-for-github-actions/security-hardening-your-deployments/configuring-openid-connect-in-amazon-web-services
#   Subject claims: https://docs.github.com/en/actions/reference/security/oidc
#   AWS side: https://docs.aws.amazon.com/IAM/latest/UserGuide/id_roles_create_for-idp_oidc.html#idp_oidc_Create_GitHub
# What this code relies on, from those pages:
#   * the IAM OIDC provider URL is https://token.actions.githubusercontent.com
#     and the audience sts.amazonaws.com (what configure-aws-credentials asks for);
#   * IAM refuses a trust policy for that provider without a
#     token.actions.githubusercontent.com:sub condition, or with one that is
#     only a wildcard;
#   * sub is repo:OWNER/REPO:ref:refs/heads/BRANCH,
#     repo:OWNER/REPO:environment:ENVIRONMENT, and so on; colons inside a
#     name become %3A;
#   * repositories created, renamed, or transferred after July 15, 2026 (or
#     opted in) send the immutable form repo:OWNER@OWNER-ID/REPO@REPO-ID:...
#     instead. GitHub documents the immutable prefix with a branch example;
#     this code assumes environments take the same prefix.
# The trust policy therefore lists both forms, the immutable one pinned to the
# numeric ids, so it keeps working whichever form the repository sends.

GITHUB_OIDC_HOST = "token.actions.githubusercontent.com"
GITHUB_OIDC_URL = "https://" + GITHUB_OIDC_HOST
GITHUB_OIDC_AUDIENCE = "sts.amazonaws.com"
GITHUB_API = "https://api.github.com"
# https://docs.github.com/en/rest/about-the-rest-api/api-versions
GITHUB_API_VERSION = "2026-03-10"

PIPELINE_ROLE_PATH = "/cloudbolt/eks-namespace/"
PIPELINE_TAG_RESOURCE = "cloudbolt.io/resource-id"
PIPELINE_TAG_REPOSITORY = "cloudbolt.io/repository"
PIPELINE_TAG_TRIGGER = "cloudbolt.io/trigger"
PIPELINE_POLICY_NAME = "eks-describe-cluster"

# GitHub owner names: alphanumerics and hyphens; repository names add . and _.
# Either part may carry its numeric id after @ (the immutable subject form).
REPOSITORY_RE = re.compile(r"^([A-Za-z0-9-]{1,39})(?:@(\d+))?/([A-Za-z0-9._-]{1,100})(?:@(\d+))?$")
# Branch and environment names as they appear after refs/heads/ or
# environment: in the subject; IAM condition values must not contain the
# policy wildcards * and ?.
TRIGGER_VALUE_RE = re.compile(r"^[^\s*?]{1,255}$")


def parse_repository(text):
    """``owner/repo`` or ``owner@ID/repo@ID`` -> (owner, repo, owner_id, repo_id).

    Raises ValueError with a readable message. The ids are None when not given.
    """
    match = REPOSITORY_RE.match(str(text or "").strip())
    if not match:
        raise ValueError(
            f"'{text}' is not a GitHub repository. Give it as owner/repo, or as "
            "owner@OWNER-ID/repo@REPO-ID."
        )
    owner, owner_id, repo, repo_id = match.groups()
    if bool(owner_id) != bool(repo_id):
        raise ValueError("Give both numeric ids (owner@OWNER-ID/repo@REPO-ID) or neither.")
    return owner, repo, owner_id, repo_id


def github_repository(owner, repo):
    """(owner, repo, owner_id, repo_id) as GitHub reports them, or None.

    GET /repos/{owner}/{repo}; id and owner.id are the numeric ids the
    immutable subject uses, full_name the canonical names. A private
    repository, or an appliance without access to api.github.com, returns
    None and the caller falls back to the name-based subject.
    https://docs.github.com/en/rest/repos/repos#get-a-repository
    """
    import requests
    from utilities.helpers import get_ssl_verification

    try:
        response = requests.get(
            f"{GITHUB_API}/repos/{owner}/{repo}",
            headers={"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": GITHUB_API_VERSION},
            timeout=15,
            verify=get_ssl_verification(),
        )
    except Exception as exc:
        logger.warning("Could not reach the GitHub API for %s/%s: %s", owner, repo, exc)
        return None
    if response.status_code != 200:
        logger.info("GitHub API returned %s for %s/%s", response.status_code, owner, repo)
        return None
    data = response.json()
    full_owner, _, full_repo = str(data.get("full_name") or f"{owner}/{repo}").partition("/")
    return full_owner, full_repo, str((data.get("owner") or {}).get("id") or ""), str(data.get("id") or "")


def _subject_value(name):
    """A branch or environment name as GitHub writes it into the subject."""
    return str(name).replace(":", "%3A")


def pipeline_trigger(branch, environment):
    """(subject suffix, human text, tag text) for the optional branch or environment."""
    if environment:
        return f"environment:{_subject_value(environment)}", f"environment {environment}", f"environment {environment}"
    if branch:
        return f"ref:refs/heads/{_subject_value(branch)}", f"branch {branch}", f"branch {branch}"
    return "*", "any branch, tag, or environment", "any"


def pipeline_subjects(owner, repo, owner_id, repo_id, suffix):
    """The sub values to trust: name-based, plus the immutable form when the ids are known."""
    prefixes = [f"repo:{owner}/{repo}"]
    if owner_id and repo_id:
        prefixes.append(f"repo:{owner}@{owner_id}/{repo}@{repo_id}")
    return [f"{prefix}:{suffix}" for prefix in prefixes]


def pipeline_trust_policy(provider_arn, subjects):
    """Role trust policy for GitHub Actions OIDC, per the AWS GitHub example.

    Exact subjects use StringEquals; the any-ref form (suffix *) uses
    StringLike, which IAM accepts because the value is not only a wildcard.
    https://docs.aws.amazon.com/IAM/latest/UserGuide/id_roles_create_for-idp_oidc.html#idp_oidc_Create_GitHub
    """
    condition = {"StringEquals": {f"{GITHUB_OIDC_HOST}:aud": GITHUB_OIDC_AUDIENCE}}
    if any(subject.endswith(":*") for subject in subjects):
        condition["StringLike"] = {f"{GITHUB_OIDC_HOST}:sub": subjects}
    else:
        condition["StringEquals"][f"{GITHUB_OIDC_HOST}:sub"] = subjects
    return {
        "Version": "2012-10-17",
        "Statement": [{
            "Effect": "Allow",
            "Principal": {"Federated": provider_arn},
            "Action": "sts:AssumeRoleWithWebIdentity",
            "Condition": condition,
        }],
    }


def pipeline_role_name(cluster_name, namespace, owner, repo, suffix):
    """A stable role name per namespace, repository, and trigger (64 characters at most).

    Role names allow [\\w+=,.@-]; the hash keeps them unique per cluster too.
    https://docs.aws.amazon.com/IAM/latest/APIReference/API_CreateRole.html
    """
    import hashlib

    digest = hashlib.sha1(
        f"{cluster_name}|{namespace}|{owner.lower()}/{repo.lower()}|{suffix}".encode("utf-8")
    ).hexdigest()[:10]
    return f"cb-eks-{namespace[:40]}-{digest}"


def _tag_value(value):
    """An IAM tag value: letters, digits, spaces and _.:/=+-@ only, 256 characters at most.

    https://docs.aws.amazon.com/IAM/latest/UserGuide/id_tags.html
    """
    return re.sub(r"[^\w .:/=+\-@]", "-", str(value))[:256]


def ensure_github_oidc_provider(iam):
    """ARN of the account's GitHub Actions OIDC provider, creating it when missing.

    Returns (arn, created). An existing provider is used as it is; one that
    lacks the sts.amazonaws.com audience is refused rather than changed,
    since other roles trust it too. The thumbprint is optional and IAM
    fetches it when omitted. Providers are never deleted here.
      https://docs.aws.amazon.com/IAM/latest/APIReference/API_ListOpenIDConnectProviders.html
      https://docs.aws.amazon.com/IAM/latest/APIReference/API_GetOpenIDConnectProvider.html
      https://docs.aws.amazon.com/IAM/latest/APIReference/API_CreateOpenIDConnectProvider.html
    """
    for item in iam.list_open_id_connect_providers().get("OpenIDConnectProviderList", []):
        arn = item.get("Arn", "")
        if not arn.endswith(f"oidc-provider/{GITHUB_OIDC_HOST}"):
            continue
        provider = iam.get_open_id_connect_provider(OpenIDConnectProviderArn=arn)
        if GITHUB_OIDC_AUDIENCE not in provider.get("ClientIDList", []):
            raise AccessEntryError(
                f"The account's GitHub OIDC provider ({arn}) does not list the audience "
                f"{GITHUB_OIDC_AUDIENCE}. Add it to the provider, then run this again."
            )
        return arn, False
    response = iam.create_open_id_connect_provider(
        Url=GITHUB_OIDC_URL,
        ClientIDList=[GITHUB_OIDC_AUDIENCE],
        Tags=[{"Key": ACCESS_ENTRY_TAG_KEY, "Value": ACCESS_ENTRY_TAG_VALUE}],
    )
    return response["OpenIDConnectProviderArn"], True


def load_pipeline_identities(resource):
    """The pipeline identities recorded on the namespace Resource, as a list of dicts."""
    raw = resource.get_value_for_custom_field(CF_PREFIX + "pipeline_identities") or ""
    try:
        identities = json.loads(raw) if raw else []
    except ValueError:
        logger.warning("Unreadable pipeline identities on %s: %r", resource, raw)
        identities = []
    return [item for item in identities if isinstance(item, dict) and item.get("role_name")]


def store_pipeline_identities(resource, identities):
    resource.set_value_for_custom_field(
        CF_PREFIX + "pipeline_identities", json.dumps(identities, sort_keys=True) if identities else ""
    )


def workflow_snippet(role_arn, region, cluster_name, namespace, environment=""):
    """A GitHub Actions job that assumes the role and deploys into the namespace.

    configure-aws-credentials usage (v6): https://github.com/aws-actions/configure-aws-credentials
    checkout usage (v7): https://github.com/actions/checkout
    id-token: write is required to request the OIDC token:
    https://docs.github.com/en/actions/security-for-github-actions/security-hardening-your-deployments/configuring-openid-connect-in-amazon-web-services
    """
    lines = [
        "permissions:",
        "  id-token: write",
        "  contents: read",
        "jobs:",
        "  deploy:",
        "    runs-on: ubuntu-latest",
    ]
    if environment:
        lines.append(f"    environment: {environment}")
    lines += [
        "    steps:",
        "      - uses: actions/checkout@v7",
        "      - uses: aws-actions/configure-aws-credentials@v6",
        "        with:",
        f"          role-to-assume: {role_arn}",
        f"          aws-region: {region}",
        f"      - run: aws eks update-kubeconfig --region {region} --name {cluster_name}",
        f"      - run: kubectl apply --namespace {namespace} -f k8s/",
    ]
    return "\n".join(lines) + "\n"


def _grant_with_retry(eks, cluster_info, principal, namespace, level_key, attempts=8, delay=5):
    """grant_namespace_access, retried while a just-created role propagates.

    IAM is eventually consistent, so EKS can briefly reject a brand-new role
    as an invalid principal.
    https://docs.aws.amazon.com/IAM/latest/UserGuide/troubleshoot_general.html#troubleshoot_general_eventual-consistency
    """
    for attempt in range(attempts):
        try:
            return grant_namespace_access(eks, cluster_info, principal, namespace, level_key)
        except Exception as exc:
            if _error_code(exc) != "InvalidParameterException" or attempt == attempts - 1:
                raise
            logger.info("EKS does not see %s yet (%s); retrying", principal, aws_message(exc))
            time.sleep(delay)


def add_pipeline_identity(resource, repository, branch="", environment="", level_key="edit"):
    """Create (or update) the pipeline role for a repository and grant it the namespace.

    Returns (identity dict, list of progress messages). Raises AccessEntryError
    or ValueError with a readable message; AWS errors propagate.
      https://docs.aws.amazon.com/IAM/latest/APIReference/API_CreateRole.html
      https://docs.aws.amazon.com/IAM/latest/APIReference/API_UpdateAssumeRolePolicy.html
      https://docs.aws.amazon.com/IAM/latest/APIReference/API_PutRolePolicy.html
    """
    if branch and environment:
        raise ValueError("Give a branch or an environment, not both.")
    for value in (branch, environment):
        if value and not TRIGGER_VALUE_RE.match(value):
            raise ValueError(f"'{value}' cannot be used: no spaces, * or ? in branch and environment names.")
    level = get_access_level(level_key)
    owner, repo, owner_id, repo_id = parse_repository(repository)

    namespace, eks, cluster_info = access_context(resource)
    cluster = load_cluster_resource(resource)
    rh, region = cluster_aws(cluster)
    iam = rh.get_boto3_client(region, "iam")
    messages = []

    if not owner_id:
        found = github_repository(owner, repo)
        if found:
            owner, repo, owner_id, repo_id = found
        else:
            messages.append(
                f"GitHub did not return {owner}/{repo} (private, or api.github.com unreachable), so only "
                "the name-based subject is trusted. For a repository created after July 15, 2026, run this "
                "again with owner@OWNER-ID/repo@REPO-ID."
            )
    suffix, trigger_text, trigger_tag = pipeline_trigger(branch, environment)
    subjects = pipeline_subjects(owner, repo, owner_id, repo_id, suffix)

    provider_arn, created_provider = ensure_github_oidc_provider(iam)
    if created_provider:
        messages.append(f"Created the account's GitHub Actions OIDC provider {provider_arn}.")

    role_name = pipeline_role_name(cluster_info["name"], namespace, owner, repo, suffix)
    trust = json.dumps(pipeline_trust_policy(provider_arn, subjects))
    try:
        existing = iam.get_role(RoleName=role_name)["Role"]
    except Exception as exc:
        if _error_code(exc) != "NoSuchEntity":
            raise
        existing = None
    if existing is not None:
        tags = {t["Key"]: t["Value"] for t in iam.list_role_tags(RoleName=role_name).get("Tags", [])}
        if tags.get(PIPELINE_TAG_RESOURCE) != str(resource.global_id):
            raise AccessEntryError(f"IAM role {role_name} exists and was not created for this namespace.")
        iam.update_assume_role_policy(RoleName=role_name, PolicyDocument=trust)
        role_arn = existing["Arn"]
        messages.append(f"Updated the trust policy of {role_arn}.")
    else:
        role_arn = iam.create_role(
            Path=PIPELINE_ROLE_PATH,
            RoleName=role_name,
            AssumeRolePolicyDocument=trust,
            Description=_tag_value(
                f"GitHub Actions for {owner}/{repo} ({trigger_text}) deploying to EKS namespace "
                f"{namespace} on {cluster_info['name']}. Managed by CloudBolt."
            )[:1000],
            Tags=[
                {"Key": ACCESS_ENTRY_TAG_KEY, "Value": ACCESS_ENTRY_TAG_VALUE},
                {"Key": PIPELINE_TAG_RESOURCE, "Value": str(resource.global_id)},
                {"Key": PIPELINE_TAG_REPOSITORY, "Value": _tag_value(f"{owner}/{repo}")},
                {"Key": PIPELINE_TAG_TRIGGER, "Value": _tag_value(trigger_tag)},
            ],
        )["Role"]["Arn"]
        messages.append(f"Created IAM role {role_arn} trusting {', '.join(subjects)}.")

    # update-kubeconfig in the workflow needs eks:DescribeCluster on this cluster;
    # get-token needs no IAM permission. The role gets nothing else in AWS.
    # https://docs.aws.amazon.com/eks/latest/userguide/create-kubeconfig.html
    iam.put_role_policy(
        RoleName=role_name,
        PolicyName=PIPELINE_POLICY_NAME,
        PolicyDocument=json.dumps({
            "Version": "2012-10-17",
            "Statement": [{"Effect": "Allow", "Action": "eks:DescribeCluster", "Resource": cluster_info["arn"]}],
        }),
    )

    messages.append(_grant_with_retry(eks, cluster_info, role_arn, namespace, level_key))

    identity = {
        "repository": f"{owner}/{repo}",
        "repository_ids": f"{owner_id}/{repo_id}" if owner_id else "",
        "trigger": trigger_text,
        "environment": environment or "",
        "role_name": role_name,
        "role_arn": role_arn,
        "level": level_key,
        "subjects": subjects,
    }
    identities = [item for item in load_pipeline_identities(resource) if item["role_name"] != role_name]
    store_pipeline_identities(resource, identities + [identity])
    refresh_access_attribute(resource, eks, cluster_info, namespace)
    resource.save()
    messages.append(f"{level['label']} access for {owner}/{repo} ({trigger_text}) is ready.")
    return identity, messages


def delete_pipeline_role(iam, role_name, resource_global_id):
    """Delete a CloudBolt pipeline role: inline policies, attached policies, then the role.

    Only a role tagged with this namespace's resource id is deleted. Returns a message.
      https://docs.aws.amazon.com/IAM/latest/APIReference/API_DeleteRole.html (what must go first)
      https://docs.aws.amazon.com/IAM/latest/APIReference/API_DeleteRolePolicy.html
      https://docs.aws.amazon.com/IAM/latest/APIReference/API_DetachRolePolicy.html
      https://docs.aws.amazon.com/IAM/latest/APIReference/API_RemoveRoleFromInstanceProfile.html
    """
    try:
        tags = {t["Key"]: t["Value"] for t in iam.list_role_tags(RoleName=role_name).get("Tags", [])}
    except Exception as exc:
        if _error_code(exc) == "NoSuchEntity":
            return f"IAM role {role_name} was already gone."
        raise
    if tags.get(PIPELINE_TAG_RESOURCE) != str(resource_global_id):
        return f"IAM role {role_name} is not tagged for this namespace and was left in place."
    for name in iam.list_role_policies(RoleName=role_name).get("PolicyNames", []):
        iam.delete_role_policy(RoleName=role_name, PolicyName=name)
    for policy in iam.list_attached_role_policies(RoleName=role_name).get("AttachedPolicies", []):
        iam.detach_role_policy(RoleName=role_name, PolicyArn=policy["PolicyArn"])
    for profile in iam.list_instance_profiles_for_role(RoleName=role_name).get("InstanceProfiles", []):
        iam.remove_role_from_instance_profile(
            InstanceProfileName=profile["InstanceProfileName"], RoleName=role_name
        )
    iam.delete_role(RoleName=role_name)
    return f"Deleted IAM role {role_name}."


def remove_pipeline_identity(resource, identity):
    """Revoke a pipeline role's namespace access, then delete the role. Returns messages.

    The access is revoked first because EKS keeps an access entry after its
    IAM principal is deleted:
    https://docs.aws.amazon.com/eks/latest/userguide/creating-access-entries.html
    When the cluster cannot be read (already deleted), the role is deleted anyway.
    """
    messages = []
    namespace = resource.get_value_for_custom_field(CF_PREFIX + "name")
    try:
        _, eks, cluster_info = access_context(resource)
        messages.append(revoke_namespace_access(eks, cluster_info, identity["role_arn"], namespace))
    except AccessEntryError as exc:
        eks = cluster_info = None
        messages.append(f"Namespace access was not revoked: {exc}")
    cluster = load_cluster_resource(resource)
    if cluster is None:
        raise AccessEntryError("The namespace records no cluster resource, so its AWS account is unknown.")
    rh, region = cluster_aws(cluster)
    iam = rh.get_boto3_client(region, "iam")
    messages.append(delete_pipeline_role(iam, identity["role_name"], resource.global_id))
    store_pipeline_identities(
        resource, [item for item in load_pipeline_identities(resource) if item["role_name"] != identity["role_name"]]
    )
    if eks is not None:
        refresh_access_attribute(resource, eks, cluster_info, namespace)
    resource.save()
    return messages


def find_pipeline_identity(resource, repository, branch="", environment=""):
    """The recorded identity for a repository and trigger, or None."""
    owner, repo, _, _ = parse_repository(repository)
    suffix, _, _ = pipeline_trigger(branch, environment)
    wanted = f"{owner}/{repo}".lower()
    for item in load_pipeline_identities(resource):
        same_repo = item.get("repository", "").lower() == wanted
        if same_repo and any(subject.endswith(":" + suffix) for subject in item.get("subjects", [])):
            return item
    return None
