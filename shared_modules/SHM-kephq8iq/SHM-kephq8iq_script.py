"""
GKE Namespace shared module.

Shared by the "GKE Namespace" blueprint's build, teardown, discovery, and
day-2 plugins, the Google Kubernetes Engine blueprint's Create GKE Namespace
action, and the Google GKE Cluster UI extension.

A GKE landing zone is a plain Kubernetes namespace that CloudBolt governs, as
on EKS: GKE has no managed-namespace resource like AKS. The module has four
parts:

1. ``NamespaceClient`` -- the namespace, its ResourceQuota, LimitRange,
   NetworkPolicies, and RoleBindings over the cluster's Kubernetes API. It
   uses the credentials of the Kubernetes container orchestrator the Google
   Kubernetes Engine blueprint registers (the cloudbolt-admin ServiceAccount
   token) through ``gcp_gke.KubeClient``, which verifies TLS against the
   cluster's own CA. GKE Dataplane V2, which the blueprint enables, enforces
   the NetworkPolicies.

2. CloudBolt-side helpers -- the size-tier catalog, the custom fields stored
   on each namespace Resource, the RBAC-gated list of GKE clusters a group
   may order a namespace on, and the Environment pinned to the namespace: one
   Environment per landing zone that points at the cluster's orchestrator,
   carries the namespace as its ``kubernetes_namespace`` value, is entitled
   to the ordering Group, and mirrors the tier into its quota.

3. Namespace access through Kubernetes RBAC -- Google users, Google service
   accounts, and Google Groups get a RoleBinding in the namespace to the
   built-in admin, edit, or view ClusterRole.

4. Pipeline identities and connection details -- a GitHub Actions workflow
   that deploys into the namespace through Workload Identity Federation (no
   stored key), and the commands and kubeconfig the Connection Details action
   and the extension's Connect tab show.

This is the GKE twin of the EKS Namespace module (shared_modules/SHM-sa5yqm9j)
and the AKS Namespace module.

Import from plugins as::

    from shared_modules.gke_namespace import create_landing_zone, TIERS, ...

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
from shared_modules.gcp_gke import (
    GKEError,
    KubeClient,
    cluster_for_resource,
    is_gke_cluster,
)
from utilities.logger import ThreadLogger

logger = ThreadLogger(__name__)

# ---------------------------------------------------------------------------
# Naming constants shared by every plugin in the blueprint
# ---------------------------------------------------------------------------

# The same keys as the EKS and AKS Namespace blueprints, so a landing zone
# reads the same on every cloud.
LANDING_ZONE_LABEL = "cloudbolt.io/landing-zone"
LABEL_TIER = "cloudbolt.io/size-tier"
LABEL_GROUP = "cloudbolt.io/group"
LABEL_RESOURCE_ID = "cloudbolt.io/resource-id"
ANNOTATION_EXPIRES = "cloudbolt.io/lease-expires"
ANNOTATION_OWNER = "cloudbolt.io/owner"
ANNOTATION_ENVIRONMENT = "cloudbolt.io/environment-id"
ANNOTATION_GROUP_ID = "cloudbolt.io/group-id"
ANNOTATION_NETWORK_POLICY = "cloudbolt.io/network-policy"
# Labels on the objects CloudBolt creates inside the namespace.
MANAGED_BY_LABEL = "app.kubernetes.io/managed-by"
MANAGED_BY_VALUE = "cloudbolt"
POLICY_DIRECTION_LABEL = "cloudbolt.io/policy-direction"
ANNOTATION_PRINCIPAL = "cloudbolt.io/principal"

QUOTA_NAME = "cloudbolt-landing-zone"
LIMIT_RANGE_NAME = "cloudbolt-landing-zone"

# Namespaces Kubernetes and GKE reserve for their own components.
# https://kubernetes.io/docs/concepts/overview/working-with-objects/namespaces/#initial-namespaces
# GKE runs its managed components in kube-system and in gke-* and gmp-*
# namespaces (Google Cloud Managed Service for Prometheus uses gmp-system and
# gmp-public), and Config Sync, Policy Controller, and Cloud Service Mesh add
# their own.
RESERVED_NAMESPACE_PREFIXES = ("kube-", "gke-", "gmp-")
RESERVED_NAMESPACE_NAMES = {"default", "kube-system", "kube-public", "kube-node-lease",
                            "config-management-system", "config-management-monitoring",
                            "gatekeeper-system", "istio-system", "cnrm-system", "resource-group-system"}

# DNS-1123 label: what Kubernetes accepts as a namespace name.
# https://kubernetes.io/docs/concepts/overview/working-with-objects/names/#dns-label-names
NAMESPACE_NAME_RE = re.compile(r"^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?$")

CLUSTER_RESOURCE_TYPE = "kubernetes_cluster"
CLUSTER_ORCHESTRATOR_FIELD = "container_orchestrator_id"
ENV_NAMESPACE_FIELD = "kubernetes_namespace"

CF_PREFIX = "gke_namespace_"

# (name, label, type, description, show_as_attribute)
CUSTOM_FIELDS = [
    (CF_PREFIX + "name", "Namespace", "STR",
     "Name of the Kubernetes namespace backing this landing zone.", True),
    (CF_PREFIX + "uid", "Namespace UID", "STR",
     "Kubernetes UID of the namespace; the unique identifier used by discovery.", False),
    (CF_PREFIX + "cluster", "GKE Cluster", "STR",
     "Name of the GKE cluster (CloudBolt Kubernetes Cluster resource) that hosts the namespace.", True),
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
    (CF_PREFIX + "network_policy", "Network Policy", "STR",
     "Ingress and egress policy the namespace's NetworkPolicies enforce.", True),
    (CF_PREFIX + "google_access", "Google Access (kubectl)", "TXT",
     "Google users, groups, and service accounts with a RoleBinding in this namespace, one per line.", True),
    (CF_PREFIX + "pipeline_identities", "Pipeline Identities", "TXT",
     "Workload Identity Federation providers CloudBolt created for CI pipelines deploying to this "
     "namespace (JSON).", False),
]


def ensure_custom_fields():
    """Create every custom field the blueprint persists, and keep it in sync.

    The gke_namespace_* fields belong to this blueprint, so their label,
    description, and show_as_attribute are kept as defined above; the
    environment-side kubernetes_namespace field is only created.
    """
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
    ensure_environment_field()


def ensure_environment_field():
    """Create the Environment-side kubernetes_namespace field when missing (never rewritten).

    Discovery calls only this: the discovery sync creates the resource
    parameters itself, but it cannot pin an Environment to a namespace.
    """
    CustomField.objects.get_or_create(
        name=ENV_NAMESPACE_FIELD,
        defaults=dict(
            label="Kubernetes Namespace",
            description=(
                "Namespace an Environment on a Kubernetes container orchestrator is pinned to. "
                "Set by the EKS, AKS, and GKE Namespace blueprints; container blueprints deploy into it."
            ),
            type="STR", show_on_servers=False, show_as_attribute=False,
        ),
    )


# ---------------------------------------------------------------------------
# Size tiers (the same catalog as the EKS and AKS Namespace blueprints)
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
    """Return the tier dict for ``key`` (case-insensitive) or raise ValueError."""
    tier = TIERS.get(str(key or "").strip().lower())
    if not tier:
        raise ValueError(f"Unknown size tier '{key}'. Valid tiers: {', '.join(TIERS)}.")
    return tier


def tier_options(initial="small"):
    """Size-tier dropdown for a plugin action input."""
    options = [(key, f"{tier['label']} - {tier['description']}") for key, tier in TIERS.items()]
    return {"options": options, "initial_value": initial, "sort": False}


def quota_hard_for_tier(tier):
    """Translate a tier into a ResourceQuota ``spec.hard`` map.
    https://kubernetes.io/docs/concepts/policy/resource-quotas/
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


def tier_for_quota(hard):
    """The tier whose CPU and memory match a ResourceQuota's hard limits, or None."""
    for key, tier in TIERS.items():
        if (str(hard.get("limits.cpu") or hard.get("requests.cpu") or "") == str(tier["cpu"])
                and str(hard.get("limits.memory") or hard.get("requests.memory") or "") == f"{tier['memory_gb']}Gi"):
            return key
    return None


# Default container requests/limits. Required alongside a compute quota: with
# requests/limits quotas in place every new pod must set them, and the
# LimitRange supplies defaults for pods that do not.
# https://kubernetes.io/docs/concepts/policy/resource-quotas/
# https://kubernetes.io/docs/reference/kubernetes-api/policy-resources/limit-range-v1/
DEFAULT_LIMIT_RANGE_ITEM = {
    "type": "Container",
    "default": {"cpu": "500m", "memory": "512Mi"},
    "defaultRequest": {"cpu": "100m", "memory": "128Mi"},
}


# ---------------------------------------------------------------------------
# Network policy choices (the AKS managed-namespace choices, as NetworkPolicies)
# ---------------------------------------------------------------------------
# https://kubernetes.io/docs/concepts/services-networking/network-policies/
# https://cloud.google.com/kubernetes-engine/docs/concepts/dataplane-v2
# Restricted egress still allows DNS lookups to kube-dns in kube-system, or
# pods could not resolve any name. Namespaces carry the immutable
# kubernetes.io/metadata.name label, which is what the kube-system selector matches:
# https://kubernetes.io/docs/concepts/overview/working-with-objects/namespaces/#automatic-labelling

NETWORK_POLICY_CHOICES = OrderedDict([
    ("AllowSameNamespace", "Allow same namespace"),
    ("AllowAll", "Allow all"),
    ("DenyAll", "Deny all"),
])

_KUBE_SYSTEM = {"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "kube-system"}}}
_DNS_EGRESS = {"to": [_KUBE_SYSTEM], "ports": [{"protocol": "UDP", "port": 53}, {"protocol": "TCP", "port": 53}]}


def network_policy_options(initial):
    return {"options": list(NETWORK_POLICY_CHOICES.items()), "initial_value": initial, "sort": False}


def describe_network_policy(ingress, egress):
    return (f"Ingress: {NETWORK_POLICY_CHOICES.get(ingress, ingress or 'n/a')}; "
            f"Egress: {NETWORK_POLICY_CHOICES.get(egress, egress or 'n/a')}")


def parse_network_policy(text):
    """(ingress, egress) from describe_network_policy's text or the namespace annotation, else ('', '')."""
    by_label = {label: key for key, label in NETWORK_POLICY_CHOICES.items()}
    found = dict(re.findall(r"(Ingress|Egress):\s*([^;]+)", str(text or "")))
    ingress = found.get("Ingress", "").strip()
    egress = found.get("Egress", "").strip()
    return by_label.get(ingress, ingress if ingress in NETWORK_POLICY_CHOICES else ""), \
        by_label.get(egress, egress if egress in NETWORK_POLICY_CHOICES else "")


def network_policies_for(ingress, egress):
    """The NetworkPolicy specs that implement an ingress and an egress choice, as [(name, direction, spec)]."""
    policies = []
    if ingress in ("AllowSameNamespace", "DenyAll"):
        policies.append(("cloudbolt-deny-ingress", "ingress",
                         {"podSelector": {}, "policyTypes": ["Ingress"], "ingress": []}))
    if ingress == "AllowSameNamespace":
        policies.append(("cloudbolt-allow-ingress-same-namespace", "ingress",
                         {"podSelector": {}, "policyTypes": ["Ingress"],
                          "ingress": [{"from": [{"podSelector": {}}]}]}))
        policies.append(("cloudbolt-allow-ingress-kube-system", "ingress",
                         {"podSelector": {}, "policyTypes": ["Ingress"], "ingress": [{"from": [_KUBE_SYSTEM]}]}))
    if egress in ("AllowSameNamespace", "DenyAll"):
        policies.append(("cloudbolt-deny-egress", "egress",
                         {"podSelector": {}, "policyTypes": ["Egress"], "egress": [_DNS_EGRESS]}))
    if egress == "AllowSameNamespace":
        policies.append(("cloudbolt-allow-egress-same-namespace", "egress",
                         {"podSelector": {}, "policyTypes": ["Egress"], "egress": [{"to": [{"podSelector": {}}]}]}))
    return policies


# ---------------------------------------------------------------------------
# Small utilities
# ---------------------------------------------------------------------------

def validate_namespace_name(name):
    """Return None if ``name`` is a valid, non-reserved namespace name, else a message."""
    if not name:
        return "A namespace name is required."
    if not NAMESPACE_NAME_RE.match(name):
        return ("Namespace name must be 1-63 lowercase alphanumerics or '-', and must "
                "start and end with an alphanumeric (DNS-1123 label).")
    if name in RESERVED_NAMESPACE_NAMES or name.startswith(RESERVED_NAMESPACE_PREFIXES):
        return f"'{name}' is reserved for Kubernetes or GKE system components; choose another name."
    return None


def label_value(value):
    """A valid Kubernetes label value.
    https://kubernetes.io/docs/concepts/overview/working-with-objects/labels/#syntax-and-character-set
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
    """The Resource's CloudBolt expiration_date as a date, or None when unset."""
    if resource is None:
        return None
    return to_date(resource.get_value_for_custom_field("expiration_date"))


def truthy(value):
    return str(value or "").strip().lower() in ("true", "1", "yes", "on")


def _managed_labels(extra=None):
    labels = {MANAGED_BY_LABEL: MANAGED_BY_VALUE, LANDING_ZONE_LABEL: "true"}
    labels.update(extra or {})
    return labels


# ---------------------------------------------------------------------------
# Kubernetes API client for landing-zone objects
# ---------------------------------------------------------------------------

class NamespaceError(Exception):
    """Raised when the Kubernetes API refuses a landing-zone request."""


RBAC = "/apis/rbac.authorization.k8s.io/v1"
NETWORKING = "/apis/networking.k8s.io/v1"


class NamespaceClient:
    """Landing-zone objects over a cluster's Kubernetes API (gcp_gke.KubeClient)."""

    def __init__(self, kube):
        self._kube = kube

    @classmethod
    def from_orchestrator(cls, orchestrator):
        """A client with the cloudbolt-admin token the cluster's orchestrator holds."""
        try:
            return cls(KubeClient.from_orchestrator(orchestrator))
        except GKEError as exc:
            raise NamespaceError(str(exc))

    def _call(self, method, *args, **kwargs):
        try:
            return getattr(self._kube, method)(*args, **kwargs)
        except GKEError as exc:
            raise NamespaceError(str(exc))

    def get(self, path, params=None, allow_404=True):
        return self._call("get", path, params=params, allow_404=allow_404)

    def _apply(self, path, name, body):
        return self._call("apply", path, name, body)

    def _items(self, path, selector=None):
        params = {"labelSelector": selector} if selector else None
        return (self.get(path, params=params) or {}).get("items") or []

    # -- namespaces ---------------------------------------------------------
    # https://kubernetes.io/docs/reference/kubernetes-api/cluster-resources/namespace-v1/
    def get_namespace(self, name):
        return self.get(f"/api/v1/namespaces/{name}")

    def create_namespace(self, name, labels=None, annotations=None):
        body = {"apiVersion": "v1", "kind": "Namespace",
                "metadata": {"name": name, "labels": labels or {}, "annotations": annotations or {}}}
        return self._call("request", "POST", "/api/v1/namespaces", json_body=body)

    def patch_namespace_metadata(self, name, labels=None, annotations=None):
        """Merge labels/annotations onto the namespace (JSON merge patch)."""
        patch = {"metadata": {}}
        if labels is not None:
            patch["metadata"]["labels"] = labels
        if annotations is not None:
            patch["metadata"]["annotations"] = annotations
        return self._call("merge_patch", f"/api/v1/namespaces/{name}", patch)

    def delete_namespace(self, name):
        """Delete the namespace and everything in it. Returns False if it was already gone."""
        return self._call("delete", f"/api/v1/namespaces/{name}")

    def wait_for_namespace_deleted(self, name, timeout=300, interval=5):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.get_namespace(name) is None:
                return True
            time.sleep(interval)
        return False

    def list_landing_zone_namespaces(self):
        """Namespaces created by this blueprint, selected by the landing-zone label."""
        return self._items("/api/v1/namespaces", f"{LANDING_ZONE_LABEL}=true")

    # -- quota & limits -----------------------------------------------------
    # https://kubernetes.io/docs/reference/kubernetes-api/policy-resources/resource-quota-v1/
    def get_resource_quota(self, namespace, name=QUOTA_NAME):
        return self.get(f"/api/v1/namespaces/{namespace}/resourcequotas/{name}")

    def list_resource_quotas(self, namespace):
        return self._items(f"/api/v1/namespaces/{namespace}/resourcequotas")

    def apply_resource_quota(self, namespace, hard, name=QUOTA_NAME):
        body = {"apiVersion": "v1", "kind": "ResourceQuota",
                "metadata": {"name": name, "namespace": namespace, "labels": _managed_labels()},
                "spec": {"hard": hard}}
        return self._apply(f"/api/v1/namespaces/{namespace}/resourcequotas", name, body)

    def apply_limit_range(self, namespace, item=None, name=LIMIT_RANGE_NAME):
        # https://kubernetes.io/docs/reference/kubernetes-api/policy-resources/limit-range-v1/
        body = {"apiVersion": "v1", "kind": "LimitRange",
                "metadata": {"name": name, "namespace": namespace, "labels": _managed_labels()},
                "spec": {"limits": [item or DEFAULT_LIMIT_RANGE_ITEM]}}
        return self._apply(f"/api/v1/namespaces/{namespace}/limitranges", name, body)

    # -- network policies ---------------------------------------------------
    # https://kubernetes.io/docs/reference/kubernetes-api/policy-resources/network-policy-v1/
    def list_managed_network_policies(self, namespace):
        return self._items(f"{NETWORKING}/namespaces/{namespace}/networkpolicies",
                           f"{MANAGED_BY_LABEL}={MANAGED_BY_VALUE}")

    def apply_network_policies(self, namespace, ingress, egress):
        """Make CloudBolt's NetworkPolicies match the choices; others are left alone. Returns names applied."""
        wanted = network_policies_for(ingress, egress)
        names = {name for name, _, _ in wanted}
        path = f"{NETWORKING}/namespaces/{namespace}/networkpolicies"
        for item in self.list_managed_network_policies(namespace):
            name = (item.get("metadata") or {}).get("name")
            if name and name not in names:
                self._call("delete", f"{path}/{name}")
        for name, direction, spec in wanted:
            self._apply(path, name, {
                "apiVersion": "networking.k8s.io/v1", "kind": "NetworkPolicy",
                "metadata": {"name": name, "namespace": namespace,
                             "labels": _managed_labels({POLICY_DIRECTION_LABEL: direction})},
                "spec": spec,
            })
        return sorted(names)

    # -- role bindings ------------------------------------------------------
    # https://kubernetes.io/docs/reference/access-authn-authz/rbac/
    def list_role_bindings(self, namespace):
        return self._items(f"{RBAC}/namespaces/{namespace}/rolebindings")

    def list_cluster_role_bindings(self):
        return self._items(f"{RBAC}/clusterrolebindings")

    def apply_role_binding(self, namespace, name, cluster_role, subject, principal):
        return self._apply(f"{RBAC}/namespaces/{namespace}/rolebindings", name, {
            "apiVersion": "rbac.authorization.k8s.io/v1", "kind": "RoleBinding",
            "metadata": {"name": name, "namespace": namespace, "labels": _managed_labels(),
                         "annotations": {ANNOTATION_PRINCIPAL: principal}},
            "roleRef": {"apiGroup": "rbac.authorization.k8s.io", "kind": "ClusterRole", "name": cluster_role},
            "subjects": [subject],
        })

    def delete_role_binding(self, namespace, name):
        return self._call("delete", f"{RBAC}/namespaces/{namespace}/rolebindings/{name}")

    # -- inventory ----------------------------------------------------------
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
                counts[key] = len(self._items(path))
            except NamespaceError as exc:
                logger.warning("Could not count %s in %s: %s", key, namespace, exc)
                counts[key] = None
        return counts


# ---------------------------------------------------------------------------
# CloudBolt-side helpers: GKE cluster resources and orchestrators
# ---------------------------------------------------------------------------

def resolve_group(group):
    """The group kwarg arrives as a Group or as its name depending on caller."""
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
    """Every active GKE cluster resource with an orchestrator: [(resource, orchestrator_id)].

    The Kubernetes Cluster resource type is shared with the EKS and AKS
    blueprints; only resources carrying gke_cluster_name count here.
    """
    clusters = []
    for resource in Resource.objects.filter(resource_type__name=CLUSTER_RESOURCE_TYPE,
                                            lifecycle="ACTIVE").order_by("name"):
        if not is_gke_cluster(resource):
            continue
        orchestrator_id = orchestrator_id_for_cluster(resource)
        if orchestrator_id is not None:
            clusters.append((resource, orchestrator_id))
    return clusters


def cluster_resources_for_group(group):
    """GKE clusters the group may order a namespace on.

    Gate: the cluster's orchestrator must back a cluster Environment the group
    can use. ``Group.get_available_environments()`` returns the environments
    explicitly entitled to the group (and its ancestors) PLUS every
    unconstrained environment, so a cluster whose per-cluster environment has
    no groups assigned is offered to everyone, and the cluster's owners
    narrow that on its Groups tab. Namespace environments also point at the
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
    """Dropdown label: cluster name with location and Kubernetes version when known."""
    values = cluster_resource.get_cf_values_as_dict()
    details = []
    if values.get("gke_location"):
        details.append(str(values["gke_location"]))
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
    """The Kubernetes Cluster resource hosting a namespace Resource, or None.

    The parent link is authoritative; the stored cluster_resource_id covers
    a namespace whose parent was lost.
    """
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


# The blueprint whose resources represent namespace landing zones, so the
# cluster-side Create action files its namespaces under the same blueprint as
# ordered ones (same resource type, day-2 actions, teardown).
NAMESPACE_BLUEPRINT_ID = "BP-c9ca1i3z"
NAMESPACE_BLUEPRINT_NAME = "GKE Namespace"


def namespace_blueprint():
    """The GKE Namespace blueprint, by global id then by name, or None."""
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
    """The CloudBolt Group a landing-zone namespace records in its annotations, or None."""
    global_id = (annotations or {}).get(ANNOTATION_GROUP_ID, "")
    if not global_id:
        return None
    return Group.objects.filter(global_id=global_id).first()


# ---------------------------------------------------------------------------
# The landing zone itself, shared by the blueprint build and the cluster action
# ---------------------------------------------------------------------------

def create_landing_zone(resource, cluster_resource, orchestrator, namespace, tier_key, owner="",
                        expires=None, ingress="AllowSameNamespace", egress="AllowAll",
                        additional_group=None):
    """Create the namespace and everything around it, recording it on ``resource``.

    On the cluster: the namespace with CloudBolt's labels and annotations,
    ResourceQuota, LimitRange, and the NetworkPolicies for the ingress and
    egress choices. In CloudBolt: the Environment pinned to the namespace,
    entitled to ``resource.group`` and ``additional_group``, with the tier
    mirrored into its quota. ``resource`` becomes a sub-resource of the
    cluster as soon as the namespace exists, so teardown can clean up after a
    later failure, the cluster page lists it, and the cluster's teardown
    refuses while it remains. Inputs are assumed validated.
    Returns (status, output_msg, error_msg).
    """
    tier = get_tier(tier_key)
    ordering_group = getattr(resource, "group", None)
    cluster_name = cluster_resource.get_cf_values_as_dict().get("gke_cluster_name") or cluster_resource.name

    try:
        client = NamespaceClient.from_orchestrator(orchestrator)
        if client.get_namespace(namespace) is not None:
            return ("FAILURE", "", f"A namespace named '{namespace}' already exists on '{cluster_name}'. Choose "
                    "a different name, or import it with the GKE Namespace blueprint's discovery.")
    except NamespaceError as exc:
        return "FAILURE", "", f"Could not reach the API of cluster '{cluster_name}': {exc}"

    # ---- 1. CloudBolt Environment pinned to the namespace ---------------
    # Created first so the namespace can carry its id; removed again when the
    # namespace cannot be created.
    try:
        env, env_created = ensure_environment_for_namespace(
            orchestrator, cluster_name, namespace,
            description=f"GKE namespace '{namespace}' ({tier['label']} tier) on {cluster_name}.",
            region=cluster_region(cluster_resource, orchestrator),
        )
    except Exception as exc:
        logger.exception("Environment creation failed")
        return "FAILURE", "", f"The CloudBolt environment for '{namespace}' could not be created: {exc}"

    # ---- 2. Namespace ---------------------------------------------------
    labels = {LANDING_ZONE_LABEL: "true", LABEL_TIER: tier_key, LABEL_RESOURCE_ID: label_value(resource.global_id)}
    if ordering_group is not None:
        labels[LABEL_GROUP] = label_value(ordering_group.name)
    policy_text = describe_network_policy(ingress, egress)
    annotations = {ANNOTATION_OWNER: owner or "", ANNOTATION_ENVIRONMENT: str(env.global_id),
                   ANNOTATION_NETWORK_POLICY: policy_text}
    if ordering_group is not None:
        annotations[ANNOTATION_GROUP_ID] = str(ordering_group.global_id)
    if expires:
        annotations[ANNOTATION_EXPIRES] = expires.isoformat()

    set_progress(f"Creating namespace '{namespace}' on '{cluster_name}' ({tier['label']} tier, {policy_text})...")
    try:
        created_ns = client.create_namespace(namespace, labels=labels, annotations=annotations) or {}
    except NamespaceError as exc:
        logger.exception("Namespace creation failed")
        if env_created:
            delete_environment(env)
        return "FAILURE", "", f"Failed to create namespace '{namespace}': {exc}"

    resource.name = namespace
    resource.parent_resource = cluster_resource
    resource.set_value_for_custom_field(CF_PREFIX + "name", namespace)
    resource.set_value_for_custom_field(CF_PREFIX + "uid", (created_ns.get("metadata") or {}).get("uid", ""))
    resource.set_value_for_custom_field(CF_PREFIX + "cluster", cluster_name)
    resource.set_value_for_custom_field(CF_PREFIX + "cluster_resource_id", cluster_resource.id)
    resource.set_value_for_custom_field(CF_PREFIX + "orchestrator_id", orchestrator.id)
    resource.set_value_for_custom_field(CF_PREFIX + "network_policy", policy_text)
    resource.set_value_for_custom_field(CF_PREFIX + "environment_id", env.id)
    resource.set_value_for_custom_field(CF_PREFIX + "environment_name", env.name)
    store_tier_on_resource(resource, tier_key, tier)
    resource.save()

    # ---- 3. Quota, default limits, and network policies ------------------
    set_progress(f"Applying '{tier['label']}' quota: {tier['description']}...")
    try:
        client.apply_resource_quota(namespace, quota_hard_for_tier(tier))
        client.apply_limit_range(namespace)
    except NamespaceError as exc:
        logger.exception("Quota/LimitRange creation failed")
        return "FAILURE", "", f"Namespace created but quota could not be applied: {exc}"
    try:
        applied = client.apply_network_policies(namespace, ingress, egress)
        set_progress(f"Applied NetworkPolicies: {', '.join(applied) or 'none (allow all)'}.")
    except NamespaceError as exc:
        logger.exception("NetworkPolicy creation failed")
        return "FAILURE", "", f"Namespace created but NetworkPolicies could not be applied: {exc}"

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
    return ("SUCCESS", f"Namespace '{namespace}' is ready on '{cluster_name}' ({tier['label']} tier). CloudBolt "
            f"groups entitled to deploy into it through environment '{env.name}': {groups_text}{expires_text}.", "")


def change_tier(resource, tier_key):
    """Move the namespace to another tier: quota, tier label, resource, and environment quota.

    Returns a list of progress messages; raises ValueError or NamespaceError.
    """
    tier = get_tier(tier_key)
    namespace, orchestrator, _ = load_cluster(resource)
    client = NamespaceClient.from_orchestrator(orchestrator)
    client.apply_resource_quota(namespace, quota_hard_for_tier(tier))
    client.patch_namespace_metadata(namespace, labels={LABEL_TIER: tier_key})
    messages = [f"Kubernetes now enforces {tier['description']} on '{namespace}'."]
    store_tier_on_resource(resource, tier_key, tier)
    env = load_environment(resource)
    if env is not None and set_environment_quota(env, tier):
        messages.append(f"Environment '{env.name}' quota mirrored.")
    resource.save()
    return messages


def set_expiration_annotation(resource, new_date):
    """Write the lease-expires annotation on the namespace."""
    namespace, orchestrator, _ = load_cluster(resource)
    NamespaceClient.from_orchestrator(orchestrator).patch_namespace_metadata(
        namespace, annotations={ANNOTATION_EXPIRES: new_date.isoformat()})


def quota_usage(resource):
    """Hard and used values of every ResourceQuota in the namespace: {name: {resource: (hard, used)}}.

    GKE may add its own quota object to namespaces; it is returned too, under its own name.
    https://kubernetes.io/docs/reference/kubernetes-api/policy-resources/resource-quota-v1/
    """
    namespace, orchestrator, _ = load_cluster(resource)
    usage = {}
    for item in NamespaceClient.from_orchestrator(orchestrator).list_resource_quotas(namespace):
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
    # Filter on the foreign key itself: Environment's manager reads a keyword that
    # matches a custom field name (container_orchestrator_id is one) as a
    # parameter lookup, so container_orchestrator_id=... matches nothing.
    for env in Environment.objects.filter(container_orchestrator=orchestrator).order_by("id"):
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
    """The cluster's location (region or zone), for the environment-name suffix."""
    location = str(cluster.get_cf_values_as_dict().get("gke_location") or "").strip()
    if location:
        return location
    for env in Environment.objects.filter(container_orchestrator=orchestrator).order_by("name"):
        if "#" in env.name:
            return env.name.rsplit("#", 1)[1].strip()
    return ""


def environment_name_for_namespace(cluster_name, namespace, region):
    """``<cluster> / <namespace>#<location>``, made unique without touching the suffix.

    The suffix matches the per-cluster ``Resource-<cluster> Environment#<location>``
    environment; it is load-bearing for EKS token minting and kept here so
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
    """Create (or reuse) the Environment pinned to ``namespace``. Returns (env, created)."""
    ensure_environment_field()
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
    """Un-entitle every group and delete the environment. Returns a message."""
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
    """Best-effort: list the namespace under the orchestrator's Namespaces tab.

    ``get_or_create_namespace`` reads the namespace through CloudBolt's own
    Kubernetes wrapper, which verifies TLS with the appliance trust store; on
    an appliance that verifies certificates without the cluster CA in Admin >
    SSL Certificates it fails, and only the inventory record is missing.
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
        deleted, _ = ContainerNamespace.objects.filter(name=namespace, container_orchestrator=orchestrator).delete()
        return bool(deleted)
    except Exception as exc:
        logger.warning("Could not remove namespace record %s on %s: %s", namespace, orchestrator, exc)
        return False


def set_resource_expiration(resource, new_date):
    """Write ``new_date`` (a date) to the Resource's standard expiration_date parameter."""
    value = datetime.datetime.combine(new_date, datetime.time(hour=23, minute=59))
    resource.set_value_for_custom_field("expiration_date", value)


# ---------------------------------------------------------------------------
# Namespace access through Kubernetes RBAC
# ---------------------------------------------------------------------------
# GKE authenticates people with their Google identity and authorizes a
# request when either IAM or Kubernetes RBAC allows it. CloudBolt grants
# namespace access with a RoleBinding to one of Kubernetes' built-in
# user-facing ClusterRoles, so it holds in that namespace only:
#   https://cloud.google.com/kubernetes-engine/docs/how-to/role-based-access-control
#   https://kubernetes.io/docs/reference/access-authn-authz/rbac/#user-facing-roles
# Subjects: a Google account or Google service account is a User named by its
# email; a Google Group is a Group named by its email and works only when the
# cluster has Google Groups for RBAC (the blueprint's RBAC Security Group).
#   https://cloud.google.com/kubernetes-engine/docs/how-to/google-groups-rbac
# IAM roles on the project (Kubernetes Engine Developer, Admin, ...) grant
# access to every namespace and cannot be narrowed by a RoleBinding; they are
# not listed here. To fetch credentials with gcloud, a person also needs
# container.clusters.get on the project (Kubernetes Engine Cluster Viewer).

class AccessError(Exception):
    """A readable problem with namespace access."""


ACCESS_LEVELS = OrderedDict([
    ("edit", dict(label="Edit", cluster_role="edit", description="create and change workloads")),
    ("view", dict(label="View", cluster_role="view", description="read only")),
    ("admin", dict(label="Admin", cluster_role="admin",
                   description="everything in the namespace, including roles and role bindings")),
])

# Listed with the access levels so the access list shows everyone who can use
# the namespace through RBAC, but never granted or revoked here.
LISTED_ONLY_LEVELS = OrderedDict([
    ("cluster-admin", dict(label="Cluster admin", cluster_role="cluster-admin", description="everything on the cluster")),
])

PRINCIPAL_KINDS = OrderedDict([("user", "User"), ("serviceAccount", "Service account"), ("group", "Group")])
EMAIL_RE = re.compile(r"^[^@\s:]+@[^@\s:]+\.[^@\s:]+$")


def get_access_level(key):
    level = ACCESS_LEVELS.get(str(key or "").strip().lower())
    if not level:
        raise ValueError(f"Unknown access level '{key}'. Valid levels: {', '.join(ACCESS_LEVELS)}.")
    return level


def access_level_options(initial="edit"):
    options = [(key, f"{level['label']} - {level['description']}") for key, level in ACCESS_LEVELS.items()]
    return {"options": options, "initial_value": initial, "sort": False}


def parse_principal(token):
    """'user:alice@example.com', 'group:team@example.com', 'serviceAccount:ci@p.iam.gserviceaccount.com',
    or a bare email -> (canonical principal, RBAC subject); raises ValueError.

    A bare email ending in .gserviceaccount.com is a service account; any
    other bare email is a user.
    """
    text = str(token or "").strip()
    kind, value = "", text
    if ":" in text:
        kind, value = text.split(":", 1)
    kind = {"user": "user", "group": "group", "serviceaccount": "serviceAccount"}.get(kind.lower(), None) \
        if kind else ("serviceAccount" if value.lower().endswith(".gserviceaccount.com") else "user")
    if kind is None or not EMAIL_RE.match(value):
        raise ValueError(f"'{text}' is not a Google principal: give user:EMAIL, group:EMAIL, "
                         "serviceAccount:EMAIL, or an email address.")
    # RBAC matches subject names exactly; one spelling keeps a grant and a
    # later revoke of the same address pointing at the same RoleBinding.
    value = value.lower()
    subject = {"kind": "Group" if kind == "group" else "User", "name": value,
               "apiGroup": "rbac.authorization.k8s.io"}
    return f"{kind}:{value}", subject


def parse_principals(text):
    """Split free text (newlines, commas, or spaces) into unique principals. Returns (principals, errors)."""
    principals, errors = [], []
    for token in re.split(r"[\s,;]+", str(text or "").strip()):
        if not token:
            continue
        try:
            principal, _ = parse_principal(token)
        except ValueError as exc:
            errors.append(str(exc))
            continue
        if principal not in principals:
            principals.append(principal)
    return principals, errors


def binding_name(principal, level_key):
    """A stable RoleBinding name per principal and level."""
    digest = hashlib.sha1(principal.lower().encode("utf-8")).hexdigest()[:12]
    return f"cloudbolt-{level_key}-{digest}"


def _subject_principal(subject):
    """The canonical principal text for an RBAC subject."""
    kind = subject.get("kind")
    name = subject.get("name", "")
    if kind == "Group":
        return f"group:{name}"
    if kind == "ServiceAccount":
        return f"kubernetes:{subject.get('namespace', '')}/{name}"
    if name.lower().endswith(".gserviceaccount.com"):
        return f"serviceAccount:{name}"
    return f"user:{name}"


def _level_for_role(role_name):
    for key, level in list(ACCESS_LEVELS.items()) + list(LISTED_ONLY_LEVELS.items()):
        if level["cluster_role"] == role_name:
            return key, level["label"]
    return "", role_name


def _is_ours(item):
    return ((item.get("metadata") or {}).get("labels") or {}).get(MANAGED_BY_LABEL) == MANAGED_BY_VALUE


def grant_namespace_access(client, principal, namespace, level_key):
    """Give ``principal`` the level in ``namespace`` (one level per principal). Returns a message."""
    level = get_access_level(level_key)
    principal, subject = parse_principal(principal)
    target = binding_name(principal, level_key)
    for item in client.list_role_bindings(namespace):
        name = (item.get("metadata") or {}).get("name", "")
        if _is_ours(item) and name != target and \
                ((item.get("metadata") or {}).get("annotations") or {}).get(ANNOTATION_PRINCIPAL) == principal:
            client.delete_role_binding(namespace, name)
    existed = client.get(f"{RBAC}/namespaces/{namespace}/rolebindings/{target}") is not None
    client.apply_role_binding(namespace, target, level["cluster_role"], subject, principal)
    if existed:
        return f"'{principal}' already had {level['label']} access to '{namespace}'."
    return f"Granted '{principal}' {level['label']} access to namespace '{namespace}'."


def revoke_namespace_access(client, principal, namespace):
    """Delete CloudBolt's RoleBindings for ``principal`` in ``namespace``. Returns a message.

    RoleBindings CloudBolt did not create are reported, never deleted.
    """
    principal, _ = parse_principal(principal)
    removed, others = [], []
    for item in client.list_role_bindings(namespace):
        meta = item.get("metadata") or {}
        subjects = [_subject_principal(s) for s in item.get("subjects") or []]
        if _is_ours(item) and (meta.get("annotations") or {}).get(ANNOTATION_PRINCIPAL) == principal:
            client.delete_role_binding(namespace, meta.get("name"))
            removed.append(_level_for_role((item.get("roleRef") or {}).get("name"))[1])
        elif principal in subjects:
            others.append(meta.get("name"))
    if removed:
        message = f"Removed {', '.join(removed)} access to '{namespace}' from '{principal}'."
    else:
        message = f"'{principal}' held no CloudBolt access to '{namespace}'."
    if others:
        message += f" RoleBinding(s) {', '.join(others)} also name it and were not created by CloudBolt."
    return message


def namespace_access(client, namespace):
    """Who can work in the namespace through Kubernetes RBAC.

    Returns [{"principal", "kind", "level", "label", "scope", "managed", "binding"}]:
    scope "namespace" for a RoleBinding in the namespace, "cluster" for a
    ClusterRoleBinding to cluster-admin.
    """
    rows = []
    for item in client.list_role_bindings(namespace):
        role = (item.get("roleRef") or {}).get("name", "")
        key, label = _level_for_role(role)
        for subject in item.get("subjects") or []:
            principal = _subject_principal(subject)
            rows.append({"principal": principal, "kind": principal.split(":", 1)[0], "level": key, "label": label,
                         "scope": "namespace", "managed": _is_ours(item),
                         "binding": (item.get("metadata") or {}).get("name", "")})
    for item in client.list_cluster_role_bindings():
        if (item.get("roleRef") or {}).get("name") != "cluster-admin":
            continue
        for subject in item.get("subjects") or []:
            if subject.get("kind") not in ("User", "Group"):
                continue
            if subject.get("name", "").startswith("system:"):
                continue
            principal = _subject_principal(subject)
            rows.append({"principal": principal, "kind": principal.split(":", 1)[0], "level": "cluster-admin",
                         "label": "Cluster admin", "scope": "cluster", "managed": _is_ours(item),
                         "binding": (item.get("metadata") or {}).get("name", "")})
    return sorted(rows, key=lambda r: (r["scope"] != "namespace", r["principal"], r["level"]))


def remove_all_namespace_access(client, namespace):
    """Teardown: delete every CloudBolt RoleBinding in the namespace. Returns messages."""
    principals = sorted({
        ((item.get("metadata") or {}).get("annotations") or {}).get(ANNOTATION_PRINCIPAL, "")
        for item in client.list_role_bindings(namespace) if _is_ours(item)
    } - {""})
    return [revoke_namespace_access(client, principal, namespace) for principal in principals]


def render_access(rows):
    scopes = {"namespace": "", "cluster": " (whole cluster)"}
    return "\n".join(f"{row['label']}: {row['principal']}{scopes[row['scope']]}" for row in rows)


def refresh_access_attribute(resource, client, namespace):
    """Re-read who has access and store it on the resource. Never raises."""
    try:
        rows = namespace_access(client, namespace)
    except Exception as exc:
        logger.warning("Could not list access to %s: %s", namespace, exc)
        return None
    resource.set_value_for_custom_field(CF_PREFIX + "google_access", render_access(rows))
    return rows


def access_context(resource):
    """(namespace, NamespaceClient) for a namespace Resource; raises AccessError naming what is missing."""
    try:
        namespace, orchestrator, _ = load_cluster(resource)
        return namespace, NamespaceClient.from_orchestrator(orchestrator)
    except (ValueError, NamespaceError) as exc:
        raise AccessError(f"Cannot reach the namespace's cluster: {exc}")


def grant_team_access(resource, principals, level_key):
    """Build-time grant of the level to each principal. Returns (status, output, error)."""
    if not principals:
        return "SUCCESS", "", ""
    try:
        namespace, client = access_context(resource)
        messages = []
        for principal in principals:
            messages.append(grant_namespace_access(client, principal, namespace, level_key))
            set_progress(messages[-1])
    except (AccessError, NamespaceError, ValueError) as exc:
        return "FAILURE", "", str(exc)
    refresh_access_attribute(resource, client, namespace)
    resource.save()
    return "SUCCESS", " ".join(messages), ""


# ---------------------------------------------------------------------------
# Connection details
# ---------------------------------------------------------------------------

def _yaml_str(value):
    """A YAML scalar for ``value``: JSON string syntax is valid YAML."""
    return json.dumps(str(value))


def kubeconfig_names(project, location, cluster_name):
    """The cluster and user name gcloud writes into a kubeconfig: gke_PROJECT_LOCATION_CLUSTER."""
    return f"gke_{project}_{location}_{cluster_name}"


def render_kubeconfig(cluster_json, project, namespace):
    """A kubeconfig for the namespace, equivalent to get-credentials plus set-context.

    Holds no secret: the endpoint, the cluster CA, and an exec block that runs
    gke-gcloud-auth-plugin with the person's own gcloud credentials. Useful
    where the person lacks container.clusters.get, which get-credentials needs.
      exec plugin: https://kubernetes.io/docs/reference/access-authn-authz/authentication/#client-go-credential-plugins
      gke-gcloud-auth-plugin: https://cloud.google.com/kubernetes-engine/docs/how-to/cluster-access-for-kubectl#install_plugin
    """
    name = kubeconfig_names(project, cluster_json.get("location", ""), cluster_json.get("name", ""))
    context = f"{name}_{namespace}"
    ca = (cluster_json.get("masterAuth") or {}).get("clusterCaCertificate", "")
    lines = [
        "apiVersion: v1",
        "kind: Config",
        "clusters:",
        f"- name: {_yaml_str(name)}",
        "  cluster:",
        f"    server: {_yaml_str('https://' + str(cluster_json.get('endpoint', '')))}",
        f"    certificate-authority-data: {_yaml_str(ca)}",
        "contexts:",
        f"- name: {_yaml_str(context)}",
        "  context:",
        f"    cluster: {_yaml_str(name)}",
        f"    user: {_yaml_str(name)}",
        f"    namespace: {_yaml_str(namespace)}",
        f"current-context: {_yaml_str(context)}",
        "users:",
        f"- name: {_yaml_str(name)}",
        "  user:",
        "    exec:",
        "      apiVersion: client.authentication.k8s.io/v1beta1",
        "      command: gke-gcloud-auth-plugin",
        "      installHint: Install gke-gcloud-auth-plugin for use with kubectl by following",
        "        https://cloud.google.com/kubernetes-engine/docs/how-to/cluster-access-for-kubectl#install_plugin",
        "      provideClusterInfo: true",
        "      interactiveMode: IfAvailable",
    ]
    return "\n".join(lines) + "\n"


def connection_commands(project, location, cluster_name, namespace):
    """The shell commands that connect kubectl to the namespace.

    gcloud container clusters get-credentials:
      https://cloud.google.com/sdk/gcloud/reference/container/clusters/get-credentials
    set-context: https://kubernetes.io/docs/reference/kubectl/generated/kubectl_config/kubectl_config_set-context/
    """
    return [
        "gcloud components install gke-gcloud-auth-plugin",
        f"gcloud container clusters get-credentials {cluster_name} --location {location} --project {project}",
        f"kubectl config set-context --current --namespace {namespace}",
        f"kubectl get pods --namespace {namespace}",
    ]


def cluster_context(resource):
    """(namespace, cluster resource, GKECluster, cluster JSON) for a namespace Resource; raises AccessError."""
    namespace = resource.get_value_for_custom_field(CF_PREFIX + "name") if resource else None
    if not namespace:
        raise AccessError(f"The resource has no '{CF_PREFIX}name' value.")
    cluster_resource = load_cluster_resource(resource)
    if cluster_resource is None:
        raise AccessError("The namespace records no cluster resource.")
    try:
        cluster = cluster_for_resource(cluster_resource)
        cluster_json = cluster.get()
    except GKEError as exc:
        raise AccessError(f"Could not read GKE cluster '{cluster_resource.name}': {exc}")
    return namespace, cluster_resource, cluster, cluster_json


def connection_details(resource):
    """Everything the Connection Details action and the Connect tab show.

    Returns a dict: namespace, cluster_name, cluster_resource, project,
    location, endpoint, private_endpoint, dns_endpoint, auth_mode, google_groups (the RBAC
    security group or ''), network_policy, commands, kubeconfig,
    environment_name, console_url, pipelines (each with a workflow snippet),
    access, and access_error. Raises AccessError when the cluster cannot be read.
    """
    from shared_modules.gcp_gke import console_url, control_plane_endpoints

    namespace, cluster_resource, cluster, cluster_json = cluster_context(resource)
    groups_config = cluster_json.get("authenticatorGroupsConfig") or {}
    _, private_endpoint, dns_endpoint = control_plane_endpoints(cluster_json)
    details = {
        "namespace": namespace,
        "cluster_name": cluster.name,
        "cluster_resource": cluster_resource,
        "project": cluster.project,
        "location": cluster.location,
        "endpoint": cluster_json.get("endpoint", ""),
        "private_endpoint": private_endpoint,
        "dns_endpoint": dns_endpoint,
        "auth_mode": "Google identities (IAM and Kubernetes RBAC)",
        "google_groups": groups_config.get("securityGroup", "") if groups_config.get("enabled") else "",
        "network_policy": resource.get_value_for_custom_field(CF_PREFIX + "network_policy") or "",
        "commands": connection_commands(cluster.project, cluster.location, cluster.name, namespace),
        "kubeconfig": render_kubeconfig(cluster_json, cluster.project, namespace),
        "environment_name": resource.get_value_for_custom_field(CF_PREFIX + "environment_name") or "",
        "console_url": console_url(cluster.project, cluster.location, cluster.name),
        "access": [],
        "access_error": "",
        "pipelines": [
            dict(item, snippet=workflow_snippet(
                item, cluster.project, cluster.location, cluster.name, namespace,
                endpoint=cluster_json.get("endpoint", ""),
                ca=(cluster_json.get("masterAuth") or {}).get("clusterCaCertificate", ""),
            ))
            for item in load_pipeline_identities(resource)
        ],
    }
    try:
        _, client = access_context(resource)
        details["access"] = namespace_access(client, namespace)
    except (AccessError, NamespaceError) as exc:
        details["access_error"] = str(exc)
    except Exception as exc:
        logger.warning("Could not list access to %s: %s", namespace, exc)
        details["access_error"] = f"Could not list RoleBindings: {exc}"
    return details


# ---------------------------------------------------------------------------
# Pipeline identities: GitHub Actions through Workload Identity Federation
# ---------------------------------------------------------------------------
# A GitHub Actions workflow signs in to Google Cloud without a stored key and
# deploys into the namespace. Google documents Kubernetes RBAC subjects for
# Google accounts, IAM service accounts, Google Groups, and workforce pool
# identities, but not for workload identity pool principals, so the workflow
# impersonates an IAM service account that CloudBolt creates for it and the
# RoleBinding names that service account:
#   https://cloud.google.com/iam/docs/workload-identity-federation-with-deployment-pipelines
#   https://cloud.google.com/kubernetes-engine/docs/how-to/role-based-access-control
#   https://cloud.google.com/kubernetes-engine/docs/how-to/api-server-authentication
#   https://github.com/google-github-actions/auth
#   https://docs.github.com/en/actions/reference/security/oidc
# What this code relies on, from those pages:
#   * one workload identity pool (CLOUDBOLT_POOL_ID, created on first use and
#     never deleted) holds an OIDC provider per pipeline with issuer
#     https://token.actions.githubusercontent.com; GitHub is multi-tenant, so a
#     provider needs an attribute condition, here the exact subjects trusted;
#     google.subject (at most 127 bytes) is mapped from the token's sub;
#   * a provider with no allowed audiences accepts the provider's own resource
#     name as audience, which is what google-github-actions/auth requests;
#   * deleted pools and providers are soft-deleted for 30 days and their IDs
#     cannot be reused meanwhile, but they can be undeleted, so adding a
#     removed pipeline again undeletes its provider;
#   * the federated principal gets roles/iam.workloadIdentityUser on the service
#     account, and generating an access token in the workflow also needs the
#     service account to hold roles/iam.serviceAccountTokenCreator on itself;
#   * a Google token without the userinfo.email scope identifies a service
#     account by its numeric ID, so the workflow asks for that scope and the
#     RoleBinding names the email;
#   * sub is repo:OWNER/REPO:ref:refs/heads/BRANCH or
#     repo:OWNER/REPO:environment:ENVIRONMENT; repositories created, renamed, or
#     transferred after July 15, 2026 send repo:OWNER@OWNER-ID/REPO@REPO-ID:...,
#     so both forms are trusted when the ids are known.
# The handler's identity needs roles/iam.workloadIdentityPoolAdmin and
# roles/iam.serviceAccountAdmin on the project, and the IAM, Security Token
# Service, and IAM Service Account Credentials APIs must be enabled.

GITHUB_OIDC_ISSUER = "https://token.actions.githubusercontent.com"
GITHUB_API = "https://api.github.com"
# https://docs.github.com/en/rest/about-the-rest-api/api-versions
GITHUB_API_VERSION = "2026-03-10"

CLOUDBOLT_POOL_ID = "cloudbolt-github"
WORKLOAD_IDENTITY_USER = "roles/iam.workloadIdentityUser"
TOKEN_CREATOR = "roles/iam.serviceAccountTokenCreator"
MAX_SUBJECT_BYTES = 127
POLL_SECONDS = 3

REPOSITORY_RE = re.compile(r"^([A-Za-z0-9-]{1,39})(?:@(\d+))?/([A-Za-z0-9._-]{1,100})(?:@(\d+))?$")
# Branch and environment names as GitHub writes them into the subject.
TRIGGER_VALUE_RE = re.compile(r"^[^\s*?]{1,255}$")


def parse_repository(text):
    """``owner/repo`` or ``owner@ID/repo@ID`` -> (owner, repo, owner_id, repo_id); raises ValueError."""
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
    if branch and environment:
        raise ValueError("Give a branch or an environment, not both.")
    for value in (branch, environment):
        if value and not TRIGGER_VALUE_RE.match(value):
            raise ValueError(f"'{value}' cannot be used: no spaces, * or ? in branch and environment names.")
    if environment:
        return f"environment:{str(environment).replace(':', '%3A')}", f"environment {environment}"
    if branch:
        return f"ref:refs/heads/{str(branch).replace(':', '%3A')}", f"branch {branch}"
    raise ValueError("Give a branch or a GitHub environment: each pipeline identity trusts one exact subject.")


def pipeline_subjects(owner, repo, owner_id, repo_id, suffix):
    prefixes = [f"repo:{owner}/{repo}"]
    if owner_id and repo_id:
        prefixes.append(f"repo:{owner}@{owner_id}/{repo}@{repo_id}")
    return [f"{prefix}:{suffix}" for prefix in prefixes]


def pipeline_ids(resource, owner, repo, suffix):
    """(provider ID, service account ID), stable per namespace resource, repository, and trigger.

    Provider IDs are 4-32 of [a-z0-9-] and may not start with gcp-; service
    account IDs are 6-30 characters matching [a-z]([-a-z0-9]*[a-z0-9]).
    https://cloud.google.com/iam/docs/reference/rest/v1/projects.locations.workloadIdentityPools.providers/create
    https://cloud.google.com/iam/docs/reference/rest/v1/projects.serviceAccounts/create
    """
    digest = hashlib.sha1(
        f"{resource.global_id}|{owner.lower()}/{repo.lower()}|{suffix}".encode("utf-8")
    ).hexdigest()[:16]
    return f"cb-gh-{digest}", f"cb-gh-{digest}"


def load_pipeline_identities(resource):
    raw = resource.get_value_for_custom_field(CF_PREFIX + "pipeline_identities") or ""
    try:
        identities = json.loads(raw) if raw else []
    except ValueError:
        logger.warning("Unreadable pipeline identities on %s: %r", resource, raw)
        identities = []
    return [item for item in identities if isinstance(item, dict) and item.get("service_account")]


def store_pipeline_identities(resource, identities):
    resource.set_value_for_custom_field(
        CF_PREFIX + "pipeline_identities", json.dumps(identities, sort_keys=True) if identities else ""
    )


def workflow_snippet(identity, project, location, cluster_name, namespace, endpoint="", ca=""):
    """A GitHub Actions job that signs in through the provider and deploys into the namespace.

    google-github-actions/auth v3 with workload_identity_provider and
    service_account, generating an access token (token_format access_token)
    with the userinfo.email scope; kubectl then talks to the cluster's
    endpoint, verified against the cluster CA, so the workflow needs no IAM
    role on the project (get-credentials would need container.clusters.get).
    https://github.com/google-github-actions/auth
    https://docs.github.com/en/actions/how-tos/secure-your-work/security-harden-deployments/oidc-in-google-cloud-platform
    """
    endpoint = endpoint or identity.get("endpoint", "")
    ca = ca or identity.get("ca", "")
    lines = ["permissions:", "  contents: read", "  id-token: write", "jobs:", "  deploy:",
             "    runs-on: ubuntu-latest"]
    if identity.get("environment"):
        lines.append(f"    environment: {identity['environment']}")
    lines += [
        "    steps:",
        "      - uses: actions/checkout@v7",
        "      - id: auth",
        "        uses: google-github-actions/auth@v3",
        "        with:",
        f"          workload_identity_provider: {identity.get('provider', '')}",
        f"          service_account: {identity.get('service_account', '')}",
        "          token_format: access_token",
        "          access_token_scopes: https://www.googleapis.com/auth/cloud-platform,"
        "https://www.googleapis.com/auth/userinfo.email",
        f"      - name: Deploy to {namespace} on {cluster_name} ({location}, {project})",
        "        env:",
        "          GKE_TOKEN: ${{ steps.auth.outputs.access_token }}",
        f"          GKE_CA: {ca}",
        "        run: |",
        '          echo "$GKE_CA" | base64 -d > "$RUNNER_TEMP/gke-ca.crt"',
        f'          kubectl --server https://{endpoint} --certificate-authority "$RUNNER_TEMP/gke-ca.crt" '
        f'--token "$GKE_TOKEN" --namespace {namespace} apply -f k8s/',
    ]
    return "\n".join(lines) + "\n"


def _iam_wait_active(api, getter, name, what, timeout=120):
    """Poll a pool or provider until its state is ACTIVE (its create or undelete finished)."""
    deadline = time.time() + timeout
    while True:
        item = api.call(getter.get(name=name), what=f"Reading {what}", allow_404=True)
        if item and item.get("state") == "ACTIVE":
            return item
        if time.time() >= deadline:
            raise GKEError(f"{what} did not become active within {timeout} seconds.")
        time.sleep(POLL_SECONDS)


def ensure_pool(api, project):
    """The CloudBolt workload identity pool of a project, created (or undeleted) when needed.
    https://cloud.google.com/iam/docs/reference/rest/v1/projects.locations.workloadIdentityPools/create
    https://cloud.google.com/iam/docs/reference/rest/v1/projects.locations.workloadIdentityPools/undelete
    Returns the pool's resource name.
    """
    pools = api.wrapper.iam.projects().locations().workloadIdentityPools()
    name = f"projects/{project}/locations/global/workloadIdentityPools/{CLOUDBOLT_POOL_ID}"
    pool = api.call(pools.get(name=name), what=f"Reading workload identity pool {CLOUDBOLT_POOL_ID}",
                    allow_404=True)
    if pool is None:
        api.call(pools.create(
            parent=f"projects/{project}/locations/global", workloadIdentityPoolId=CLOUDBOLT_POOL_ID,
            body={"displayName": "CloudBolt GitHub Actions",
                  "description": "GitHub Actions pipelines that deploy to GKE namespaces. Managed by CloudBolt."},
        ), what=f"Creating workload identity pool {CLOUDBOLT_POOL_ID}")
        set_progress(f"Created workload identity pool {CLOUDBOLT_POOL_ID} in {project}.")
    elif pool.get("state") == "DELETED":
        api.call(pools.undelete(name=name, body={}), what=f"Undeleting workload identity pool {CLOUDBOLT_POOL_ID}")
    _iam_wait_active(api, pools, name, f"workload identity pool {CLOUDBOLT_POOL_ID}")
    return name


def _owned_by(item, resource):
    return f"({resource.global_id})" in str((item or {}).get("description") or "")


def ensure_provider(api, pool_name, provider_id, subjects, description, resource):
    """Create, undelete, or update the pipeline's OIDC provider; returns its resource name.

    The attribute condition admits only the trusted subjects.
    https://cloud.google.com/iam/docs/reference/rest/v1/projects.locations.workloadIdentityPools.providers
    """
    providers = api.wrapper.iam.projects().locations().workloadIdentityPools().providers()
    name = f"{pool_name}/providers/{provider_id}"
    body = {
        "displayName": provider_id,
        "description": description[:256],
        "attributeMapping": {
            "google.subject": "assertion.sub",
            "attribute.repository": "assertion.repository",
            "attribute.repository_id": "assertion.repository_id",
        },
        "attributeCondition": f"assertion.sub in [{', '.join(json.dumps(s) for s in subjects)}]",
        "oidc": {"issuerUri": GITHUB_OIDC_ISSUER},
    }
    current = api.call(providers.get(name=name), what=f"Reading provider {provider_id}", allow_404=True)
    if current is None:
        api.call(providers.create(parent=pool_name, workloadIdentityPoolProviderId=provider_id, body=body),
                 what=f"Creating provider {provider_id}")
    else:
        if not _owned_by(current, resource):
            raise AccessError(f"Workload identity provider {provider_id} exists and was not created for this "
                              "namespace.")
        if current.get("state") == "DELETED":
            api.call(providers.undelete(name=name, body={}), what=f"Undeleting provider {provider_id}")
            _iam_wait_active(api, providers, name, f"provider {provider_id}")
        api.call(providers.patch(name=name, updateMask="description,attributeMapping,attributeCondition",
                                 body=body), what=f"Updating provider {provider_id}")
    _iam_wait_active(api, providers, name, f"provider {provider_id}")
    return name


def ensure_service_account(api, project, account_id, display_name, description, resource):
    """Create (or reuse) the pipeline's IAM service account; returns its ServiceAccount resource.
    https://cloud.google.com/iam/docs/reference/rest/v1/projects.serviceAccounts/create
    """
    accounts = api.wrapper.iam.projects().serviceAccounts()
    email = f"{account_id}@{project}.iam.gserviceaccount.com"
    current = api.call(accounts.get(name=f"projects/{project}/serviceAccounts/{email}"),
                       what=f"Reading service account {email}", allow_404=True)
    if current is not None:
        if not _owned_by(current, resource):
            raise AccessError(f"Service account {email} exists and was not created for this namespace.")
        return current
    return api.call(accounts.create(name=f"projects/{project}", body={
        "accountId": account_id,
        "serviceAccount": {"displayName": display_name[:100], "description": description[:256]},
    }), what=f"Creating service account {email}")


def set_service_account_bindings(api, account_name, email, principals):
    """Let the federated principals impersonate the account, and the account mint its own tokens.

    The account is CloudBolt's own, so these two role bindings are rewritten
    in a get-modify-set cycle that carries the etag.
    https://cloud.google.com/iam/docs/reference/rest/v1/projects.serviceAccounts/setIamPolicy
    """
    accounts = api.wrapper.iam.projects().serviceAccounts()
    # A service account created moments ago can briefly be unknown to its own
    # policy calls, so a 400 or 404 is retried for half a minute.
    for attempt in range(6):
        try:
            policy = api.call(accounts.getIamPolicy(resource=account_name),
                              what=f"Reading the IAM policy of {email}") or {}
            bindings = [b for b in policy.get("bindings") or []
                        if b.get("role") not in (WORKLOAD_IDENTITY_USER, TOKEN_CREATOR)]
            bindings.append({"role": WORKLOAD_IDENTITY_USER, "members": sorted(principals)})
            bindings.append({"role": TOKEN_CREATOR, "members": [f"serviceAccount:{email}"]})
            policy["bindings"] = bindings
            api.call(accounts.setIamPolicy(resource=account_name, body={"policy": policy}),
                     what=f"Setting the IAM policy of {email}")
            return
        except GKEError as exc:
            if exc.status_code not in (400, 404) or attempt == 5:
                raise
            logger.info("IAM does not see %s yet (%s); retrying", email, exc)
            time.sleep(5)


def add_pipeline_identity(resource, repository, branch="", environment="", level_key="edit"):
    """Create (or update) a pipeline identity for a repository and grant it the namespace.

    Returns (identity dict, progress messages). Raises AccessError, ValueError, or GKEError.
    """
    level = get_access_level(level_key)
    suffix, trigger_text = pipeline_trigger(branch, environment)
    owner, repo, owner_id, repo_id = parse_repository(repository)
    namespace, _, cluster, cluster_json = cluster_context(resource)
    _, client = access_context(resource)
    api, project = cluster.api, cluster.project
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
    for subject in subjects:
        if len(subject.encode("utf-8")) > MAX_SUBJECT_BYTES:
            raise ValueError(f"GitHub's subject '{subject}' is longer than the {MAX_SUBJECT_BYTES} bytes Google "
                             "accepts as a subject; use a shorter branch or environment name.")

    project_number = api.project_number(project)
    provider_id, account_id = pipeline_ids(resource, owner, repo, suffix)
    marker = (f"GitHub Actions {owner}/{repo} ({trigger_text}) deploying to GKE namespace {namespace} on "
              f"{cluster.name}. Managed by CloudBolt ({resource.global_id})")

    pool_name = ensure_pool(api, project)
    provider_name = ensure_provider(api, pool_name, provider_id, subjects, marker, resource)
    messages.append(f"Workload identity provider {provider_id} trusts {', '.join(subjects)}.")

    account = ensure_service_account(api, project, account_id, f"CloudBolt pipeline {owner}/{repo}"[:100],
                                     marker, resource)
    email = account.get("email") or f"{account_id}@{project}.iam.gserviceaccount.com"
    principals = [f"principal://iam.googleapis.com/projects/{project_number}/locations/global/"
                  f"workloadIdentityPools/{CLOUDBOLT_POOL_ID}/subject/{subject}" for subject in subjects]
    set_service_account_bindings(api, account.get("name") or f"projects/{project}/serviceAccounts/{email}",
                                 email, principals)
    messages.append(f"Service account {email} may be impersonated by those subjects.")

    messages.append(grant_namespace_access(client, f"serviceAccount:{email}", namespace, level_key))
    identity = {
        "repository": f"{owner}/{repo}",
        "repository_ids": f"{owner_id}/{repo_id}" if owner_id else "",
        "trigger": trigger_text,
        "environment": environment or "",
        "level": level_key,
        "subjects": subjects,
        "principal": f"serviceAccount:{email}",
        "service_account": email,
        "provider": provider_name.replace(f"projects/{project}/", f"projects/{project_number}/", 1),
        "provider_id": provider_id,
        "project": project,
        "endpoint": cluster_json.get("endpoint", ""),
        "ca": (cluster_json.get("masterAuth") or {}).get("clusterCaCertificate", ""),
    }
    identities = [item for item in load_pipeline_identities(resource) if item["service_account"] != email]
    store_pipeline_identities(resource, identities + [identity])
    refresh_access_attribute(resource, client, namespace)
    resource.save()
    messages.append(f"{level['label']} access for {owner}/{repo} ({trigger_text}) is ready.")
    return identity, messages


def remove_pipeline_identity(resource, identity):
    """Revoke the identity's namespace access, then delete its provider and service account.

    Only a provider or service account whose description names this
    namespace resource is deleted. Deleting a provider does not revoke tokens
    it already issued; they expire within the hour. Returns messages.
    https://cloud.google.com/iam/docs/reference/rest/v1/projects.locations.workloadIdentityPools.providers/delete
    https://cloud.google.com/iam/docs/reference/rest/v1/projects.serviceAccounts/delete
    """
    messages = []
    namespace = resource.get_value_for_custom_field(CF_PREFIX + "name")
    try:
        _, client = access_context(resource)
        messages.append(revoke_namespace_access(client, identity["principal"], namespace))
    except (AccessError, NamespaceError) as exc:
        messages.append(f"Namespace access was not revoked: {exc}")
    cluster_resource = load_cluster_resource(resource)
    if cluster_resource is None:
        raise AccessError("The namespace records no cluster resource, so its Google Cloud project is unknown.")
    api = cluster_for_resource(cluster_resource).api
    project = identity.get("project") or cluster_resource.get_cf_values_as_dict().get("gke_project_id")

    providers = api.wrapper.iam.projects().locations().workloadIdentityPools().providers()
    provider_name = (f"projects/{project}/locations/global/workloadIdentityPools/{CLOUDBOLT_POOL_ID}"
                     f"/providers/{identity.get('provider_id')}")
    provider = api.call(providers.get(name=provider_name), what="Reading the provider", allow_404=True)
    if provider is None or provider.get("state") == "DELETED":
        messages.append(f"Provider {identity.get('provider_id')} was already gone.")
    elif not _owned_by(provider, resource):
        messages.append(f"Provider {identity.get('provider_id')} is not this namespace's and was left in place.")
    else:
        api.call(providers.delete(name=provider_name), what=f"Deleting provider {identity.get('provider_id')}")
        messages.append(f"Deleted provider {identity.get('provider_id')}.")

    accounts = api.wrapper.iam.projects().serviceAccounts()
    account_name = f"projects/{project}/serviceAccounts/{identity['service_account']}"
    account = api.call(accounts.get(name=account_name), what="Reading the service account", allow_404=True)
    if account is None:
        messages.append(f"Service account {identity['service_account']} was already gone.")
    elif not _owned_by(account, resource):
        messages.append(f"Service account {identity['service_account']} is not this namespace's and was left "
                        "in place.")
    else:
        api.call(accounts.delete(name=account_name), what=f"Deleting service account {identity['service_account']}")
        messages.append(f"Deleted service account {identity['service_account']}.")

    store_pipeline_identities(resource, [
        item for item in load_pipeline_identities(resource) if item["service_account"] != identity["service_account"]
    ])
    resource.save()
    return messages


def remove_all_pipeline_identities(resource):
    """Teardown: remove every pipeline identity recorded on the namespace. Returns messages."""
    messages = []
    for identity in load_pipeline_identities(resource):
        messages.extend(remove_pipeline_identity(resource, identity))
    return messages


def find_pipeline_identity(resource, repository, branch="", environment=""):
    """The recorded identity for a repository and trigger, or None."""
    owner, repo, _, _ = parse_repository(repository)
    suffix, _ = pipeline_trigger(branch, environment)
    wanted = f"{owner}/{repo}".lower()
    for item in load_pipeline_identities(resource):
        if item.get("repository", "").lower() == wanted and any(
            subject.endswith(":" + suffix) for subject in item.get("subjects", [])
        ):
            return item
    return None
