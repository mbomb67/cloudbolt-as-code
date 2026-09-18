"""
OpenShift Landing Zone shared module.

Shared by the "OpenShift Project Landing Zone" blueprint's build, teardown,
discovery, and day-2 plugins. It has two halves:

1. ``OpenShiftLandingZoneClient`` -- a thin REST client for the OpenShift /
   Kubernetes API, built from a CloudBolt OpenShift Virtualization resource
   handler (``resourcehandlers.ovirt.models.OVirtHandler``). It creates and
   manages the namespace, ResourceQuota, LimitRange, NetworkPolicies, and
   RoleBindings that make up a landing zone. Every call site cites the vendor
   API reference it follows (docs/agents/external-apis.md).

2. CloudBolt-side helpers -- create an Environment pinned to the namespace
   (the OpenShift Virtualization handler scopes each Environment to one
   namespace), entitle CloudBolt Groups to that Environment, and mirror the
   namespace quota into the Environment's server quota so CloudBolt enforces
   the tier at order time.

Import from plugins as::

    from shared_modules.openshift_landing_zone import (
        OpenShiftLandingZoneClient, TIERS, ensure_custom_fields, ...
    )

Shared-module code is cached by the running CloudBolt process: after changing
this file and syncing, restart CloudBolt for the change to take effect.
"""

import datetime
import re
import time
from collections import OrderedDict

import requests
import urllib3

from common.methods import set_progress
from infrastructure.models import CustomField, Environment
from utilities.logger import ThreadLogger

logger = ThreadLogger(__name__)

# ---------------------------------------------------------------------------
# Naming constants shared by every plugin in the blueprint
# ---------------------------------------------------------------------------

# Labels / annotations stamped on the namespace so discovery can find landing
# zones and so cluster admins can see who owns what from inside OpenShift.
LANDING_ZONE_LABEL = "cloudbolt.io/landing-zone"
LABEL_TIER = "cloudbolt.io/size-tier"
LABEL_GROUP = "cloudbolt.io/group"
LABEL_RESOURCE_ID = "cloudbolt.io/resource-id"
ANNOTATION_EXPIRES = "cloudbolt.io/lease-expires"
ANNOTATION_ENVIRONMENT = "cloudbolt.io/environment-id"
ANNOTATION_OWNER = "cloudbolt.io/owner"

# OpenShift project annotations. A project is a Kubernetes namespace carrying
# these annotations; they are what `oc new-project --display-name --description`
# sets. Docs: https://docs.redhat.com/en/documentation/openshift_container_platform/4.16/html/building_applications/projects
ANNOTATION_OS_DISPLAY_NAME = "openshift.io/display-name"
ANNOTATION_OS_DESCRIPTION = "openshift.io/description"
ANNOTATION_OS_REQUESTER = "openshift.io/requester"

# Names of the in-namespace objects the landing zone owns.
QUOTA_NAME = "cloudbolt-landing-zone"
LIMIT_RANGE_NAME = "cloudbolt-landing-zone"
ROLE_BINDING_PREFIX = "cloudbolt-"

# Namespace names OpenShift reserves for system projects.
RESERVED_NAMESPACE_PREFIXES = ("openshift-", "kube-")
RESERVED_NAMESPACE_NAMES = {"default", "openshift", "kube-system", "kube-public"}

# DNS-1123 label: what Kubernetes accepts as a namespace name.
# Docs: https://kubernetes.io/docs/concepts/overview/working-with-objects/names/#dns-label-names
NAMESPACE_NAME_RE = re.compile(r"^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?$")

# Prefix for every custom field this blueprint stores on its Resources.
CF_PREFIX = "openshift_project_"

# The custom fields persisted on each landing-zone Resource.
# (name, label, type, description)
# (name, label, type, description, show_as_attribute)
# show_as_attribute=True puts the value in the Attributes panel on the resource
# page; the identifiers and wiring fields stay out of it.
CUSTOM_FIELDS = [
    (CF_PREFIX + "name", "Name", "STR",
     "Name of the OpenShift project (namespace) backing this landing zone.", False),
    (CF_PREFIX + "uid", "UID", "STR",
     "Kubernetes UID of the namespace; the unique identifier used by discovery.", False),
    (CF_PREFIX + "display_name", "Display Name", "STR",
     "Human-readable display name shown in the OpenShift console.", True),
    (CF_PREFIX + "cluster", "OpenShift Cluster", "STR",
     "Name of the CloudBolt OpenShift Virtualization resource handler (cluster).", True),
    (CF_PREFIX + "rh_id", "Resource Handler ID", "INT",
     "ID of the OpenShift Virtualization resource handler that owns this project.", False),
    (CF_PREFIX + "environment_id", "Environment ID", "INT",
     "ID of the CloudBolt Environment pinned to this project's namespace.", False),
    (CF_PREFIX + "environment_name", "Environment", "STR",
     "Name of the CloudBolt Environment pinned to this project's namespace.", False),
    (CF_PREFIX + "size_tier", "Size Tier", "STR",
     "Landing-zone size tier that sets the namespace ResourceQuota.", False),
    (CF_PREFIX + "cpu_limit", "CPU Limit (cores)", "INT",
     "Namespace quota for CPU (requests and limits), in cores.", True),
    (CF_PREFIX + "memory_limit_gb", "Memory Limit (GB)", "INT",
     "Namespace quota for memory (requests and limits), in GiB.", True),
    (CF_PREFIX + "storage_limit_gb", "Storage Limit (GB)", "INT",
     "Namespace quota for persistent storage requests, in GiB.", True),
    (CF_PREFIX + "max_pods", "Max Pods", "INT",
     "Namespace quota for the number of pods.", True),
    (CF_PREFIX + "max_vms", "Max Virtual Machines", "INT",
     "Namespace quota for the number of KubeVirt VirtualMachines.", True),
    (CF_PREFIX + "entitled_groups", "Entitled CloudBolt Groups", "TXT",
     "CloudBolt Groups allowed to order into this project, one per line.", False),
    (CF_PREFIX + "team_bindings", "Team Access", "TXT",
     "OpenShift RoleBindings managed by CloudBolt, one 'role kind name' per line.", True),
    (CF_PREFIX + "network_isolation", "Network Isolation", "BOOL",
     "Whether default-deny NetworkPolicies were applied to the namespace.", True),
    (CF_PREFIX + "console_url", "Console URL", "URL",
     "Link to the project in the OpenShift web console.", True),
]


def ensure_custom_fields():
    """Create every custom field the blueprint persists, and keep it in sync.

    Idempotent. These fields are namespaced to this blueprint, so when one
    already exists its label, description, and show_as_attribute are updated to
    match the definition above -- get_or_create alone would leave a field
    created by an earlier version untouched.
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


# ---------------------------------------------------------------------------
# Size tiers
# ---------------------------------------------------------------------------

# Each tier sets the namespace ResourceQuota and is mirrored into the CloudBolt
# Environment quota (cpu_cnt / mem_size / disk_size / vm_cnt).
TIERS = OrderedDict([
    ("small", dict(label="Small", cpu=4, memory_gb=16, storage_gb=100, pods=20, pvcs=10, vms=4,
                   description="4 cores, 16 GiB RAM, 100 GiB storage, 20 pods, 4 VMs")),
    ("medium", dict(label="Medium", cpu=8, memory_gb=32, storage_gb=250, pods=40, pvcs=20, vms=8,
                    description="8 cores, 32 GiB RAM, 250 GiB storage, 40 pods, 8 VMs")),
    ("large", dict(label="Large", cpu=16, memory_gb=64, storage_gb=500, pods=80, pvcs=40, vms=16,
                   description="16 cores, 64 GiB RAM, 500 GiB storage, 80 pods, 16 VMs")),
    ("xlarge", dict(label="X-Large", cpu=32, memory_gb=128, storage_gb=1000, pods=160, pvcs=80, vms=32,
                    description="32 cores, 128 GiB RAM, 1 TiB storage, 160 pods, 32 VMs")),
])


def get_tier(key):
    """Return the tier dict for ``key`` (case-insensitive) or raise ValueError."""
    tier = TIERS.get(str(key or "").strip().lower())
    if not tier:
        valid = ", ".join(TIERS)
        raise ValueError(f"Unknown size tier '{key}'. Valid tiers: {valid}.")
    return tier


def tier_options(initial="small"):
    """Size-tier dropdown for a plugin action input.

    Action-input generators take plain (value, label) tuples; the dict-shaped
    rich options are only for Generated Parameter Options hooks and render as
    raw text here. The description is folded into the label instead.
    """
    options = [(key, f"{tier['label']} - {tier['description']}") for key, tier in TIERS.items()]
    return {"options": options, "initial_value": initial, "sort": False}


def quota_hard_for_tier(tier):
    """Translate a tier into a ResourceQuota ``spec.hard`` map.

    Docs: https://kubernetes.io/docs/concepts/policy/resource-quotas/
          Compute keys requests.cpu / limits.cpu / requests.memory / limits.memory;
          storage keys requests.storage / persistentvolumeclaims; object counts
          ``pods`` and the ``count/<resource>.<group>`` syntax for custom resources.
    """
    return {
        "requests.cpu": str(tier["cpu"]),
        "limits.cpu": str(tier["cpu"]),
        "requests.memory": f"{tier['memory_gb']}Gi",
        "limits.memory": f"{tier['memory_gb']}Gi",
        "requests.storage": f"{tier['storage_gb']}Gi",
        "persistentvolumeclaims": str(tier["pvcs"]),
        "pods": str(tier["pods"]),
        "count/virtualmachines.kubevirt.io": str(tier["vms"]),
    }


# Default container requests/limits. Required alongside a compute quota: with
# requests/limits quotas in place every new pod must set them, and the
# LimitRange supplies defaults for pods that do not.
# Docs: https://kubernetes.io/docs/concepts/policy/resource-quotas/ (LimitRange note)
#       https://kubernetes.io/docs/reference/kubernetes-api/policy-resources/limit-range-v1/
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
        return "A project name is required."
    if not NAMESPACE_NAME_RE.match(name):
        return (
            "Project name must be 1-63 lowercase alphanumerics or '-', and must "
            "start and end with an alphanumeric (DNS-1123 label)."
        )
    if name in RESERVED_NAMESPACE_NAMES or name.startswith(RESERVED_NAMESPACE_PREFIXES):
        return f"'{name}' is reserved for OpenShift system projects; choose another name."
    return None


def label_value(value):
    """Coerce ``value`` into a valid Kubernetes label value.

    Label values are at most 63 chars of [A-Za-z0-9._-] and must start/end
    alphanumeric. Docs: https://kubernetes.io/docs/concepts/overview/working-with-objects/labels/#syntax-and-character-set
    """
    text = re.sub(r"[^A-Za-z0-9._-]+", "-", str(value or "")).strip("-._")
    return text[:63].rstrip("-._")


def role_binding_name(cluster_role, subject_kind, subject_name):
    """Deterministic RoleBinding name for a subject, so grant/revoke line up."""
    slug = re.sub(r"[^a-z0-9-]+", "-", str(subject_name or "").lower()).strip("-")
    return f"{ROLE_BINDING_PREFIX}{cluster_role}-{subject_kind.lower()}-{slug}"[:253]


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
    (destination Both), so CloudBolt stores it on the Resource at order time
    and its normal expiration handling applies.
    """
    if resource is None:
        return None
    return to_date(resource.get_value_for_custom_field("expiration_date"))


def set_resource_expiration(resource, new_date):
    """Write ``new_date`` (a date) to the Resource's standard expiration_date parameter."""
    value = datetime.datetime.combine(new_date, datetime.time(hour=23, minute=59))
    resource.set_value_for_custom_field("expiration_date", value)


# ---------------------------------------------------------------------------
# OpenShift / Kubernetes REST client
# ---------------------------------------------------------------------------

class LandingZoneError(Exception):
    """Raised for non-2xx responses from the OpenShift API."""


class OpenShiftLandingZoneClient:
    """Minimal OpenShift API client for landing-zone objects.

    Built the same way this repo's working ``openshift_import`` shared module
    talks to the cluster: the OpenShift Virtualization handler's API wrapper
    client exposes the API base URL and issues an OAuth bearer token.
    """

    def __init__(self, api_url, token, verify_ssl=False):
        self.api_url = api_url.rstrip("/")
        self._session = requests.Session()
        self._session.headers.update({
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        })
        self._session.verify = verify_ssl
        if not verify_ssl:
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    @classmethod
    def from_handler(cls, handler):
        """Create a client from a cast ``OVirtHandler`` (OpenShift Virtualization)."""
        wrapper = handler.get_api_wrapper()
        client = wrapper.client

        api_url = getattr(client, "openshift_base_api_url", None)
        if not api_url:
            protocol = getattr(handler, "protocol", "https")
            port = getattr(handler, "port", 6443)
            api_url = f"{protocol}://{handler.ip}:{port}"

        token = client.get_oauth_token()
        if isinstance(token, dict):
            token = token.get("access_token", token)

        verify_ssl = bool(getattr(handler, "enable_ssl_verification", False))
        return cls(api_url=api_url, token=token, verify_ssl=verify_ssl)

    # -- transport ----------------------------------------------------------

    def _request(self, method, path, json=None, params=None, headers=None, allow_404=False):
        """Issue a request; return parsed JSON, or None on 404 when ``allow_404``."""
        url = f"{self.api_url}/{path.lstrip('/')}"
        resp = self._session.request(method, url, json=json, params=params, headers=headers)
        if resp.status_code == 404 and allow_404:
            return None
        if not resp.ok:
            try:
                detail = resp.json().get("message", resp.text[:500])
            except Exception:
                detail = resp.text[:500]
            raise LandingZoneError(f"{method.upper()} {path} failed ({resp.status_code}): {detail}")
        if resp.headers.get("Content-Type", "").startswith("application/json") and resp.text:
            return resp.json()
        return resp.text

    def _apply(self, path, name, body):
        """Create ``body`` at ``path``, or replace it if an object named ``name`` exists.

        PUT requires the current metadata.resourceVersion, so the existing object
        is read first and its resourceVersion copied onto the replacement.
        """
        existing = self._request("GET", f"{path}/{name}", allow_404=True)
        if existing is None:
            return self._request("POST", path, json=body)
        body = dict(body)
        body.setdefault("metadata", {})["resourceVersion"] = existing["metadata"]["resourceVersion"]
        return self._request("PUT", f"{path}/{name}", json=body)

    # -- namespaces ---------------------------------------------------------
    # Docs: https://kubernetes.io/docs/reference/kubernetes-api/cluster-resources/namespace-v1/
    #       POST /api/v1/namespaces; GET|PATCH|DELETE /api/v1/namespaces/{name};
    #       GET /api/v1/namespaces?labelSelector=...; PATCH accepts
    #       application/merge-patch+json; status.phase is Active or Terminating.

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
        patch = {"metadata": {}}
        if labels is not None:
            patch["metadata"]["labels"] = labels
        if annotations is not None:
            patch["metadata"]["annotations"] = annotations
        return self._request(
            "PATCH", f"/api/v1/namespaces/{name}", json=patch,
            headers={"Content-Type": "application/merge-patch+json"},
        )

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
    # Docs: https://kubernetes.io/docs/reference/kubernetes-api/policy-resources/resource-quota-v1/
    #       POST /api/v1/namespaces/{ns}/resourcequotas; GET|PUT .../resourcequotas/{name};
    #       spec.hard is a map of resource name -> quantity; status.used mirrors usage.

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
        # Docs: https://kubernetes.io/docs/reference/kubernetes-api/policy-resources/limit-range-v1/
        #       POST /api/v1/namespaces/{ns}/limitranges; spec.limits[] items carry
        #       type / default / defaultRequest / max / min.
        body = {
            "apiVersion": "v1",
            "kind": "LimitRange",
            "metadata": {"name": name, "namespace": namespace},
            "spec": {"limits": [item or DEFAULT_LIMIT_RANGE_ITEM]},
        }
        return self._apply(f"/api/v1/namespaces/{namespace}/limitranges", name, body)

    # -- network policies ---------------------------------------------------
    # Docs: https://kubernetes.io/docs/reference/kubernetes-api/policy-resources/network-policy-v1/
    #       POST /apis/networking.k8s.io/v1/namespaces/{ns}/networkpolicies
    # The four policies below are OpenShift's documented multitenant-isolation
    # set: deny by default, allow same-namespace, allow the ingress controller,
    # allow cluster monitoring. Namespace selector labels quoted from
    # openshift-docs enterprise-4.16 modules/nw-networkpolicy-about.adoc and
    # modules/nw-networkpolicy-multitenant-isolation.adoc.

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
            "name": "allow-from-openshift-ingress",
            "spec": {"podSelector": {}, "policyTypes": ["Ingress"],
                     "ingress": [{"from": [{"namespaceSelector": {"matchLabels": {
                         "policy-group.network.openshift.io/ingress": ""}}}]}]},
        },
        {
            "name": "allow-from-openshift-monitoring",
            "spec": {"podSelector": {}, "policyTypes": ["Ingress"],
                     "ingress": [{"from": [{"namespaceSelector": {"matchLabels": {
                         "network.openshift.io/policy-group": "monitoring"}}}]}]},
        },
    ]

    def apply_network_policies(self, namespace):
        """Create the isolation policy set. Returns the policy names applied."""
        applied = []
        for policy in self.NETWORK_POLICIES:
            body = {
                "apiVersion": "networking.k8s.io/v1",
                "kind": "NetworkPolicy",
                "metadata": {"name": policy["name"], "namespace": namespace},
                "spec": policy["spec"],
            }
            self._apply(
                f"/apis/networking.k8s.io/v1/namespaces/{namespace}/networkpolicies",
                policy["name"], body,
            )
            applied.append(policy["name"])
        return applied

    # -- RBAC ---------------------------------------------------------------
    # Docs: https://kubernetes.io/docs/reference/kubernetes-api/authorization-resources/role-binding-v1/
    #       POST /apis/rbac.authorization.k8s.io/v1/namespaces/{ns}/rolebindings;
    #       GET|DELETE .../rolebindings/{name}; roleRef {apiGroup, kind, name} is
    #       immutable; subjects[] kind User|Group (apiGroup rbac.authorization.k8s.io)
    #       or ServiceAccount (apiGroup "", plus namespace).
    # OpenShift's default project roles are the ClusterRoles admin, edit, view.

    PROJECT_ROLES = ("admin", "edit", "view")

    def apply_role_binding(self, namespace, cluster_role, subject_kind, subject_name):
        if cluster_role not in self.PROJECT_ROLES:
            raise ValueError(f"Role must be one of {', '.join(self.PROJECT_ROLES)}.")
        if subject_kind not in ("User", "Group", "ServiceAccount"):
            raise ValueError("Subject kind must be User, Group, or ServiceAccount.")
        subject = {"kind": subject_kind, "name": subject_name}
        if subject_kind == "ServiceAccount":
            subject["namespace"] = namespace
        else:
            subject["apiGroup"] = "rbac.authorization.k8s.io"
        name = role_binding_name(cluster_role, subject_kind, subject_name)
        body = {
            "apiVersion": "rbac.authorization.k8s.io/v1",
            "kind": "RoleBinding",
            "metadata": {"name": name, "namespace": namespace,
                         "labels": {LANDING_ZONE_LABEL: "true"}},
            "roleRef": {"apiGroup": "rbac.authorization.k8s.io", "kind": "ClusterRole",
                        "name": cluster_role},
            "subjects": [subject],
        }
        self._apply(f"/apis/rbac.authorization.k8s.io/v1/namespaces/{namespace}/rolebindings",
                    name, body)
        return name

    def delete_role_binding(self, namespace, cluster_role, subject_kind, subject_name):
        """Delete a managed RoleBinding. Returns False if it did not exist."""
        name = role_binding_name(cluster_role, subject_kind, subject_name)
        result = self._request(
            "DELETE",
            f"/apis/rbac.authorization.k8s.io/v1/namespaces/{namespace}/rolebindings/{name}",
            allow_404=True,
        )
        return result is not None

    def list_groups(self):
        """Names of the cluster's OpenShift groups (identity-provider synced or local).

        Docs: https://docs.redhat.com/en/documentation/openshift_container_platform/4.16/html/user_and_group_apis/group-user-openshift-io-v1
              GET /apis/user.openshift.io/v1/groups (cluster-scoped GroupList);
              type definition: openshift/api user/v1/types.go (Group.metadata.name, Group.users).
        """
        result = self._request("GET", "/apis/user.openshift.io/v1/groups", allow_404=True)
        names = [
            (item.get("metadata") or {}).get("name", "") for item in (result or {}).get("items", [])
        ]
        return sorted(n for n in names if n)

    def list_users(self):
        """Names of the cluster's OpenShift users.

        Docs: openshift/api user/v1/types.go -- User is cluster-scoped under
              user.openshift.io/v1 (metadata.name is the username);
              GET /apis/user.openshift.io/v1/users returns a UserList.
        """
        result = self._request("GET", "/apis/user.openshift.io/v1/users", allow_404=True)
        names = [
            (item.get("metadata") or {}).get("name", "") for item in (result or {}).get("items", [])
        ]
        return sorted(n for n in names if n)

    def list_service_accounts(self, namespace):
        """Names of the ServiceAccounts in ``namespace``.

        Docs: https://kubernetes.io/docs/reference/kubernetes-api/authentication-resources/service-account-v1/
              GET /api/v1/namespaces/{namespace}/serviceaccounts
        """
        result = self._request(
            "GET", f"/api/v1/namespaces/{namespace}/serviceaccounts", allow_404=True
        )
        names = [
            (item.get("metadata") or {}).get("name", "") for item in (result or {}).get("items", [])
        ]
        return sorted(n for n in names if n)

    def list_subjects(self, kind, namespace):
        """Candidate subject names for a RoleBinding of the given kind."""
        if kind == "Group":
            return self.list_groups()
        if kind == "User":
            return self.list_users()
        if kind == "ServiceAccount":
            return self.list_service_accounts(namespace)
        raise ValueError("Subject kind must be User, Group, or ServiceAccount.")

    def list_managed_role_bindings(self, namespace):
        """Return [(role, kind, name), ...] for RoleBindings this blueprint created."""
        result = self._request(
            "GET",
            f"/apis/rbac.authorization.k8s.io/v1/namespaces/{namespace}/rolebindings",
            params={"labelSelector": f"{LANDING_ZONE_LABEL}=true"},
            allow_404=True,
        )
        bindings = []
        for item in (result or {}).get("items", []):
            role = item.get("roleRef", {}).get("name", "")
            for subject in item.get("subjects", []) or []:
                bindings.append((role, subject.get("kind", ""), subject.get("name", "")))
        return bindings

    # -- inventory ----------------------------------------------------------

    def count_workloads(self, namespace):
        """Count pods, KubeVirt VirtualMachines, and PVCs in the namespace.

        Docs: https://kubernetes.io/docs/reference/kubernetes-api/workload-resources/pod-v1/
              GET /api/v1/namespaces/{ns}/pods
              https://kubernetes.io/docs/reference/kubernetes-api/config-and-storage-resources/persistent-volume-claim-v1/
              GET /api/v1/namespaces/{ns}/persistentvolumeclaims
              https://kubevirt.io/api-reference/main/operations.html
              GET /apis/kubevirt.io/v1/namespaces/{ns}/virtualmachines
        """
        counts = {}
        for key, path in (
            ("pods", f"/api/v1/namespaces/{namespace}/pods"),
            ("persistentvolumeclaims", f"/api/v1/namespaces/{namespace}/persistentvolumeclaims"),
            ("virtualmachines", f"/apis/kubevirt.io/v1/namespaces/{namespace}/virtualmachines"),
        ):
            try:
                result = self._request("GET", path, allow_404=True)
                counts[key] = len((result or {}).get("items", []))
            except LandingZoneError as exc:
                logger.warning("Could not count %s in %s: %s", key, namespace, exc)
                counts[key] = None
        return counts

    def get_console_url(self):
        """Best-effort OpenShift web console URL from the ``console`` Route.

        Same Route lookup this repo's openshift_import module uses to find the
        CDI upload proxy: GET /apis/route.openshift.io/v1/namespaces/{ns}/routes/{name}
        and read spec.host. Returns '' if the route cannot be read.
        """
        try:
            route = self._request(
                "GET", "/apis/route.openshift.io/v1/namespaces/openshift-console/routes/console",
                allow_404=True,
            )
        except LandingZoneError as exc:
            logger.warning("Could not read console route: %s", exc)
            return ""
        host = ((route or {}).get("spec") or {}).get("host")
        return f"https://{host}" if host else ""

    def project_console_url(self, namespace):
        base = self.get_console_url()
        return f"{base}/k8s/cluster/projects/{namespace}" if base else ""


# ---------------------------------------------------------------------------
# CloudBolt-side helpers: Environment, Group entitlement, quota mirroring
# ---------------------------------------------------------------------------

def find_environment_for_namespace(rh, namespace):
    """Return the Environment on ``rh`` pinned to ``namespace``, or None.

    The OpenShift Virtualization handler stores the namespace as the
    environment's location (``OVirtHandler.get_env_location``).
    """
    for env in Environment.objects.filter(resource_handler_id=rh.id):
        try:
            location = rh.get_env_location(env)
        except Exception:
            location = getattr(env, "ovirt_namespace", None)
        if location == namespace:
            return env
    return None


def _create_environment(rh, namespace, env_name):
    """Create the Environment through the handler's own factory, with a plain fallback.

    ``create_location_specific_env`` is what the handler details page uses when
    an admin adds an environment for a location; if a handler override rejects
    the call, create the bare Environment and let the OpenShift Virtualization
    override of ``make_env_location_specific`` pin it to the namespace.
    """
    try:
        return rh.create_location_specific_env(namespace, env_name=env_name)
    except Exception as exc:
        logger.warning("create_location_specific_env failed (%s); creating environment directly.", exc)
    env = Environment.objects.create(name=env_name, resource_handler_id=rh.id)
    rh.make_env_location_specific(namespace, env)
    env.refresh_from_db()
    return env


def ensure_environment_for_namespace(rh, namespace, env_name="", description="", source_env=None):
    """Create (or reuse) the CloudBolt Environment pinned to ``namespace``.

    Uses the resource handler's own location-specific environment factory
    (``ResourceHandler.create_location_specific_env``) so the environment is
    wired exactly as one created from the handler's details page, then confirms
    the location stuck via ``get_env_location``. When ``source_env`` (the
    environment the order was placed against) is given, its OS builds are made
    available on the new environment so VM blueprints are orderable into the
    project right away. Returns (env, created).
    """
    existing = find_environment_for_namespace(rh, namespace)
    if existing is not None:
        return existing, False

    env_name = env_name or rh.get_default_env_name(namespace)
    env = _create_environment(rh, namespace, env_name)

    if rh.get_env_location(env) != namespace:
        # Belt and braces: the OVirt override sets the namespace on the env.
        rh.make_env_location_specific(namespace, env)
        env.refresh_from_db()

    if description:
        env.description = description
        env.save()

    # Best-effort: pull handler parameter options (storage classes etc.) and the
    # namespace's networks onto the new environment so VM orders work immediately.
    for step, func in (("parameters", rh.import_parameters_for_env), ("networks", rh.sync_subnets)):
        try:
            func(env)
        except Exception as exc:  # non-fatal: admins can import from the env page
            logger.warning("Could not import %s for environment %s: %s", step, env, exc)

    if source_env is not None:
        copy_os_builds(source_env, env)

    return env, True


def copy_os_builds(source_env, env):
    """Make every OS build offered on ``source_env`` available on ``env``.

    OS builds are attached to environments through ``OSBuild.environments``;
    a freshly created environment has none, so VM blueprints would show no
    images until an admin adds them. Returns the number of builds copied.
    """
    from externalcontent.models import OSBuild

    copied = 0
    try:
        for os_build in OSBuild.objects.filter(environments=source_env):
            if not os_build.environments.filter(id=env.id).exists():
                os_build.environments.add(env)
                copied += 1
    except Exception as exc:  # non-fatal: admins can add OS builds on the env page
        logger.warning("Could not copy OS builds from %s to %s: %s", source_env, env, exc)
    if copied:
        set_progress(f"Made {copied} OS build(s) from '{source_env.name}' available on '{env.name}'.")
    return copied


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

    Read from the Group side: ``Environment.groups_served`` is a separate
    relation and does not reflect ``group.environments.add(env)``.
    """
    from accounts.models import Group

    return Group.objects.filter(environments=env).exclude(name__startswith="Unassigned")


def entitled_group_names(env):
    """Names of the Groups directly entitled to ``env`` (CloudBolt's Unassigned excluded)."""
    return sorted(g.name for g in entitled_groups(env))


def set_environment_quota(env, tier):
    """Mirror the tier into the Environment's server quota so CloudBolt enforces it.

    ``Environment.quota_set`` is a ServerQuotaSet with cpu_cnt (cores), mem_size
    (GB), disk_size (GB), and vm_cnt quotas; QuotaSet.change_limit takes one
    keyword per quota. Returns True on success.
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
            vm_cnt=tier["vms"],
        )
        quota_set.save()
        return True
    except Exception as exc:
        # Lowering a limit below current usage raises; report and keep going.
        logger.warning("Could not set quota on environment %s: %s", env, exc)
        set_progress(f"Warning: could not update CloudBolt environment quota: {exc}")
        return False


def store_tier_on_resource(resource, tier_key, tier):
    """Write the tier and its limits onto the Resource's custom fields."""
    resource.set_value_for_custom_field(CF_PREFIX + "size_tier", tier_key)
    resource.set_value_for_custom_field(CF_PREFIX + "cpu_limit", tier["cpu"])
    resource.set_value_for_custom_field(CF_PREFIX + "memory_limit_gb", tier["memory_gb"])
    resource.set_value_for_custom_field(CF_PREFIX + "storage_limit_gb", tier["storage_gb"])
    resource.set_value_for_custom_field(CF_PREFIX + "max_pods", tier["pods"])
    resource.set_value_for_custom_field(CF_PREFIX + "max_vms", tier["vms"])


def render_bindings(bindings):
    """Render [(role, kind, name), ...] as the one-per-line block stored on the Resource."""
    return "\n".join(f"{role} {kind} {name}" for role, kind, name in sorted(set(bindings)))


def load_handler(resource):
    """Return (namespace, OVirtHandler) from a landing-zone Resource's custom fields.

    Raises ValueError naming the missing piece so callers can report precisely.
    """
    if resource is None:
        raise ValueError("no Resource was supplied")
    namespace = resource.get_value_for_custom_field(CF_PREFIX + "name")
    if not namespace:
        raise ValueError(f"resource has no '{CF_PREFIX}name' value")
    rh_id = resource.get_value_for_custom_field(CF_PREFIX + "rh_id")
    if not rh_id:
        raise ValueError(f"resource has no '{CF_PREFIX}rh_id' value")

    from resourcehandlers.ovirt.models import OVirtHandler

    try:
        return namespace, OVirtHandler.objects.get(id=rh_id)
    except OVirtHandler.DoesNotExist:
        raise ValueError(f"no OpenShift Virtualization handler with id={rh_id}")


def load_environment(resource):
    """Return the Environment recorded on the Resource, or None."""
    env_id = resource.get_value_for_custom_field(CF_PREFIX + "environment_id")
    if not env_id:
        return None
    return Environment.objects.filter(id=env_id).first()
