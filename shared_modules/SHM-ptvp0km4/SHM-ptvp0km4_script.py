"""
Shared module: azure_aks

Azure Kubernetes Service helpers for the Azure Kubernetes Service blueprint,
its day-2 actions, the AKS Namespace blueprint, and the Azure AKS Cluster UI
extension. It has four parts:

1. ``ArmClient`` -- Azure Resource Manager over HTTPS, authenticated as a
   CloudBolt Azure resource handler's service principal. Plain REST rather
   than the azure-mgmt-containerservice SDK, so the plugins do not depend on
   the SDK version an appliance ships. Long-running operations are followed
   through their Azure-AsyncOperation or Location header.

2. ``AKSCluster`` -- one managed cluster: read it, its upgrade profile, its
   agent pools (node pools), and their upgrade profiles; create, scale,
   upgrade, and delete node pools; upgrade the control plane; read the
   admin and user kubeconfigs. Every call cites the REST reference it follows
   (docs/agents/external-apis.md).

3. CloudBolt-side helpers -- the custom fields stamped on a Kubernetes
   Cluster resource and on its AKS Node Pool sub-resources, the Azure
   environments a group may order into, the Kubernetes container orchestrator
   and per-cluster Environment that let CloudBolt (and the namespace
   blueprint) reach the cluster's API server.

4. Generic ARM helpers the namespace module builds on: role assignments and
   user-assigned managed identities with federated credentials.

The Kubernetes Cluster resource type is shared with the AWS EKS blueprint;
an AKS cluster is told apart by its ``aks_cluster_name`` value.

Import from plugins as::

    from shared_modules.azure_aks import AKSCluster, AKSError, cluster_for_resource, ...

Shared-module code is cached by the running CloudBolt process: after changing
this file and syncing, restart CloudBolt for the change to take effect.
"""

import base64
import datetime
import json
import re
import time
import uuid

import requests
import yaml

from common.methods import set_progress
from infrastructure.models import CustomField, Environment
from resources.models import Resource, ResourceType
from utilities.helpers import get_ssl_verification
from utilities.logger import ThreadLogger

logger = ThreadLogger(__name__)

# ---------------------------------------------------------------------------
# API versions (stable, non-preview), one per resource provider
# ---------------------------------------------------------------------------

# https://learn.microsoft.com/en-us/rest/api/aks/managed-clusters
# https://learn.microsoft.com/en-us/rest/api/aks/agent-pools
# https://learn.microsoft.com/en-us/rest/api/aks/managed-namespaces
AKS_API_VERSION = "2026-07-01"
# https://learn.microsoft.com/en-us/rest/api/authorization/role-assignments
AUTHORIZATION_API_VERSION = "2022-04-01"
# https://learn.microsoft.com/en-us/rest/api/managedidentity/user-assigned-identities
# https://learn.microsoft.com/en-us/rest/api/managedidentity/federated-identity-credentials
MSI_API_VERSION = "2024-11-30"
# https://learn.microsoft.com/en-us/rest/api/kubernetesconfiguration/extensions/extensions/list
EXTENSIONS_API_VERSION = "2025-03-01"
# https://learn.microsoft.com/en-us/rest/api/resources/resource-groups
RESOURCES_API_VERSION = "2021-04-01"

REQUEST_TIMEOUT = 60
POLL_SECONDS = 15

# Entra ID login and ARM endpoints per Azure cloud, keyed by the handler's
# cloud_environment value. Mirrors shared_modules/azure_management_locks.
_CLOUD_ENDPOINTS = {
    "PUBLIC": ("https://login.microsoftonline.com", "https://management.azure.com",
               "https://portal.azure.com"),
    "US_GOV": ("https://login.microsoftonline.us", "https://management.usgovcloudapi.net",
               "https://portal.azure.us"),
    "CHINA": ("https://login.chinacloudapi.cn", "https://management.chinacloudapi.cn",
              "https://portal.azure.cn"),
    "GERMAN": ("https://login.microsoftonline.de", "https://management.microsoftazure.de",
               "https://portal.microsoftazure.de"),
}

# The resource types this module stamps. kubernetes_cluster is shared with the
# AWS EKS blueprint; aks_node_pool is the AKS counterpart of eks_nodegroup.
CLUSTER_RESOURCE_TYPE = "kubernetes_cluster"
NODE_POOL_RESOURCE_TYPE = "aks_node_pool"
NAMESPACE_RESOURCE_TYPE = "kubernetes_namespace"
CLUSTER_ORCHESTRATOR_FIELD = "container_orchestrator_id"

# The field that marks a Kubernetes Cluster resource as an AKS cluster.
AKS_MARKER_FIELD = "aks_cluster_name"

# Azure resource names: AKS cluster names are 1-63 characters of letters,
# digits, underscores, and hyphens, starting and ending with a letter or digit.
# https://learn.microsoft.com/en-us/azure/azure-resource-manager/management/resource-name-rules#microsoftcontainerservice
CLUSTER_NAME_RE = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9_-]{0,61}[A-Za-z0-9])?$")
# Linux node pool names: 1-12 lowercase letters and digits, starting with a letter.
# https://learn.microsoft.com/en-us/azure/aks/create-node-pools#limitations
NODE_POOL_NAME_RE = re.compile(r"^[a-z][a-z0-9]{0,11}$")
GUID_RE = re.compile(r"^[0-9a-fA-F]{8}-([0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}$")


class AKSError(Exception):
    """A readable Azure or AKS failure. ``code`` is the ARM error code when known."""

    def __init__(self, message, status_code=None, code=""):
        super().__init__(message)
        self.status_code = status_code
        self.code = code or ""


# ---------------------------------------------------------------------------
# Azure Resource Manager client
# ---------------------------------------------------------------------------

def cloud_endpoints(rh):
    """(login_base, arm_base, portal_base) for the handler's Azure cloud."""
    cloud = getattr(rh, "cloud_environment", "PUBLIC") or "PUBLIC"
    return _CLOUD_ENDPOINTS.get(cloud, _CLOUD_ENDPOINTS["PUBLIC"])


def _error_parts(resp):
    """(code, message) from an ARM or Entra error body; never includes headers."""
    try:
        body = resp.json() or {}
    except ValueError:
        body = {}
    err = body.get("error") if isinstance(body, dict) else None
    if isinstance(err, dict):
        return err.get("code") or "", err.get("message") or ""
    if err:
        return str(err), str(body.get("error_description", ""))
    return "", f"HTTP {resp.status_code}: {(resp.text or '')[:300]}"


class ArmClient:
    """ARM calls for one Azure resource handler's subscription."""

    def __init__(self, rh):
        self.rh = rh
        self.login_base, self.arm_base, self.portal_base = cloud_endpoints(rh)
        # The Azure subscription id is the handler's inherited `serviceaccount`.
        self.subscription_id = getattr(rh, "serviceaccount", None)
        if not self.subscription_id:
            raise AKSError(f"Azure handler '{rh}' has no subscription id (serviceaccount).")
        self.tenant_id = getattr(rh, "azure_tenant_id", "") or ""
        self._token = None
        self._token_expiry = 0.0

    # ---- auth --------------------------------------------------------------
    def _get_token(self):
        """A bearer token for ARM, cached until shortly before it expires.

        The handler's own service principal through the OAuth2
        client-credentials grant; when the handler has no client secret
        (certificate or managed identity), the SDK credential CloudBolt
        already built for it.
        https://learn.microsoft.com/en-us/entra/identity-platform/v2-oauth2-client-creds-grant-flow
        """
        if self._token and time.time() < self._token_expiry - 60:
            return self._token
        client_id = getattr(self.rh, "client_id", None)
        secret = getattr(self.rh, "secret", None)
        if client_id and secret and self.tenant_id:
            try:
                resp = requests.post(
                    f"{self.login_base}/{self.tenant_id}/oauth2/v2.0/token",
                    data={
                        "grant_type": "client_credentials",
                        "client_id": client_id,
                        "client_secret": secret,
                        "scope": f"{self.arm_base}/.default",
                    },
                    timeout=REQUEST_TIMEOUT,
                    verify=get_ssl_verification(),
                )
            except requests.exceptions.RequestException as exc:
                raise AKSError(f"Could not reach {self.login_base}: {exc}")
            if resp.status_code != 200:
                code, message = _error_parts(resp)
                raise AKSError(f"Token request failed: {code}: {message}", resp.status_code, code)
            body = resp.json()
            self._token = body["access_token"]
            self._token_expiry = time.time() + int(body.get("expires_in", 3000))
            return self._token
        try:
            access = self.rh.get_api_wrapper().credentials.get_token(f"{self.arm_base}/.default")
        except Exception as exc:  # noqa: BLE001 - any credential failure is a token failure
            raise AKSError(f"Could not obtain an ARM token from the handler credential: {exc}")
        self._token = access.token
        self._token_expiry = float(getattr(access, "expires_on", time.time() + 3000))
        return self._token

    # ---- transport ---------------------------------------------------------
    def request(self, method, path, body=None, params=None, api_version=AKS_API_VERSION):
        """One ARM call; returns the response whatever its status.

        ``path`` is relative to the ARM base, or an absolute URL (a nextLink or
        an async-operation URL) that already carries its api-version. One 429
        is retried after Retry-After.
        """
        absolute = path.startswith("http")
        url = path if absolute else f"{self.arm_base}{path}"
        query = dict(params or {})
        if not absolute and api_version:
            query.setdefault("api-version", api_version)
        headers = {"Authorization": f"Bearer {self._get_token()}", "Content-Type": "application/json"}
        for attempt in range(2):
            try:
                resp = requests.request(
                    method, url, headers=headers, json=body, params=query,
                    timeout=REQUEST_TIMEOUT, verify=get_ssl_verification(),
                )
            except requests.exceptions.RequestException as exc:
                raise AKSError(f"Could not reach {self.arm_base}: {exc}")
            if resp.status_code != 429 or attempt:
                return resp
            try:
                delay = min(int(resp.headers.get("Retry-After", "10")), 60)
            except ValueError:
                delay = 10
            logger.warning("ARM throttled %s %s; retrying in %ss", method, url, delay)
            time.sleep(delay)
        return resp

    def call(self, method, path, body=None, params=None, api_version=AKS_API_VERSION,
             ok=(200, 201, 202, 204), allow_404=False, what=""):
        """``request`` that raises AKSError unless the status is in ``ok``.

        Returns the response, or None for a 404 when ``allow_404``.
        """
        resp = self.request(method, path, body=body, params=params, api_version=api_version)
        if resp.status_code == 404 and allow_404:
            return None
        if resp.status_code not in ok:
            code, message = _error_parts(resp)
            prefix = f"{what} failed" if what else f"{method} {path.split('?')[0]} failed"
            raise AKSError(f"{prefix}: {code + ': ' if code else ''}{message}", resp.status_code, code)
        return resp

    def get_json(self, path, api_version=AKS_API_VERSION, params=None, allow_404=False, what=""):
        resp = self.call("GET", path, params=params, api_version=api_version,
                         ok=(200,), allow_404=allow_404, what=what)
        if resp is None:
            return None
        return resp.json() if resp.content else {}

    def list_json(self, path, api_version=AKS_API_VERSION, params=None, what=""):
        """GET a list result, following nextLink; returns the ``value`` items."""
        items, next_path, next_params = [], path, params
        while next_path:
            data = self.get_json(next_path, api_version=api_version, params=next_params, what=what)
            items.extend(data.get("value") or [])
            next_path, next_params = data.get("nextLink"), None
        return items

    def wait(self, resp, what="operation", timeout=3600, progress=True):
        """Follow a long-running operation to its end. Returns the final status text.

        Azure-AsyncOperation (preferred) answers {status: InProgress |
        Succeeded | Failed | Canceled, error}; a Location URL answers 202
        until done, then 200 or 204. A response with neither header finished
        synchronously.
        https://learn.microsoft.com/en-us/azure/azure-resource-manager/management/async-operations
        """
        async_url = resp.headers.get("Azure-AsyncOperation") or resp.headers.get("azure-asyncoperation")
        location = resp.headers.get("Location") or resp.headers.get("location")
        if not async_url and not location:
            return "Succeeded"
        started = time.time()
        deadline = started + timeout
        last_report = started
        while time.time() < deadline:
            delay = POLL_SECONDS
            try:
                delay = max(5, min(int(resp.headers.get("Retry-After", POLL_SECONDS)), 60))
            except (TypeError, ValueError):
                pass
            time.sleep(delay)
            if async_url:
                resp = self.request("GET", async_url, api_version=None)
                if resp.status_code >= 400:
                    code, message = _error_parts(resp)
                    raise AKSError(f"{what} status check failed: {code}: {message}", resp.status_code, code)
                body = resp.json() if resp.content else {}
                status = str(body.get("status") or "InProgress")
                if status.lower() in ("succeeded",):
                    return "Succeeded"
                if status.lower() in ("failed", "canceled", "cancelled"):
                    err = body.get("error") or {}
                    raise AKSError(
                        f"{what} {status.lower()}: {err.get('code', '')}: {err.get('message', '')}".strip(),
                        code=err.get("code", ""),
                    )
            else:
                resp = self.request("GET", location, api_version=None)
                if resp.status_code in (200, 201, 204):
                    return "Succeeded"
                if resp.status_code != 202:
                    code, message = _error_parts(resp)
                    raise AKSError(f"{what} failed: {code}: {message}", resp.status_code, code)
                status = "InProgress"
            if progress and time.time() - last_report >= 60:
                last_report = time.time()
                set_progress(f"{what} is {status} ({int((last_report - started) // 60)} min)", increment_tasks=1)
        raise AKSError(f"{what} did not finish within {timeout // 60} minutes.")


# ---------------------------------------------------------------------------
# Resource ids
# ---------------------------------------------------------------------------

def parse_resource_id(resource_id):
    """(subscription, resource_group, name) of an ARM resource id; missing parts are ''.

    /subscriptions/{sub}/resourceGroups/{rg}/providers/{ns}/{type}/{name}
    See docs/agents/common-patterns.md (Parsing Azure Resource IDs).
    """
    parts = str(resource_id or "").strip("/").split("/")
    lowered = [p.lower() for p in parts]
    sub = parts[lowered.index("subscriptions") + 1] if "subscriptions" in lowered else ""
    rg = parts[lowered.index("resourcegroups") + 1] if "resourcegroups" in lowered else ""
    return sub, rg, parts[-1] if parts and parts[-1] else ""


def cluster_id(subscription_id, resource_group, name):
    return (
        f"/subscriptions/{subscription_id}/resourceGroups/{resource_group}"
        f"/providers/Microsoft.ContainerService/managedClusters/{name}"
    )


# ---------------------------------------------------------------------------
# One managed cluster
# ---------------------------------------------------------------------------

# Properties ARM reports but a PUT must not send back. AKS ignores most
# read-only properties, but sending fewer keeps the round trip predictable.
# https://learn.microsoft.com/en-us/rest/api/aks/agent-pools/create-or-update
_POOL_READ_ONLY = (
    "provisioningState", "currentOrchestratorVersion", "nodeImageVersion", "eTag",
)
# https://learn.microsoft.com/en-us/rest/api/aks/managed-clusters/create-or-update
_CLUSTER_READ_ONLY = (
    "provisioningState", "powerState", "maxAgentPools", "currentKubernetesVersion",
    "fqdn", "azurePortalFQDN", "privateFQDN", "resourceUID", "eTag",
)


class AKSCluster:
    """A managed cluster, addressed by subscription (through ``arm``), resource group, and name."""

    def __init__(self, arm, resource_group, name):
        self.arm = arm
        self.resource_group = resource_group
        self.name = name
        self.id = cluster_id(arm.subscription_id, resource_group, name)

    # -- the cluster ----------------------------------------------------------
    def get(self, allow_404=False):
        """GET managedClusters/{name}.
        https://learn.microsoft.com/en-us/rest/api/aks/managed-clusters/get
        """
        return self.arm.get_json(self.id, allow_404=allow_404, what=f"Reading AKS cluster '{self.name}'")

    def upgrade_profile(self):
        """GET managedClusters/{name}/upgradeProfiles/default.

        properties.controlPlaneProfile {kubernetesVersion, upgrades[{kubernetesVersion, isPreview}]}
        and properties.agentPoolProfiles[] the same per pool.
        https://learn.microsoft.com/en-us/rest/api/aks/managed-clusters/get-upgrade-profile
        """
        return self.arm.get_json(
            f"{self.id}/upgradeProfiles/default", what=f"Reading upgrades for '{self.name}'"
        )

    def put(self, body, what=""):
        """PUT managedClusters/{name} and wait for it. Returns the cluster as AKS reports it.
        https://learn.microsoft.com/en-us/rest/api/aks/managed-clusters/create-or-update
        """
        label = what or f"Updating AKS cluster '{self.name}'"
        resp = self.arm.call("PUT", self.id, body=body, ok=(200, 201), what=label)
        self.arm.wait(resp, what=label)
        return self.get()

    def update(self, mutate, what=""):
        """Read the cluster, let ``mutate(properties)`` change it, and PUT it back.

        AKS has no PATCH for cluster properties (PATCH only updates tags), so
        changes are a GET, change, PUT round trip, as the Azure CLI does.
        https://learn.microsoft.com/en-us/rest/api/aks/managed-clusters/update-tags
        """
        current = self.get()
        body = {
            "location": current["location"],
            "tags": current.get("tags") or {},
            "sku": current.get("sku"),
            "identity": _writable_identity(current.get("identity")),
            "properties": dict(current.get("properties") or {}),
        }
        body = {k: v for k, v in body.items() if v is not None}
        # mutate sees the read-only values too (currentOrchestratorVersion, ...);
        # they are dropped before the PUT.
        mutate(body["properties"])
        for key in _CLUSTER_READ_ONLY:
            body["properties"].pop(key, None)
        for profile in body["properties"].get("agentPoolProfiles") or []:
            for key in _POOL_READ_ONLY:
                profile.pop(key, None)
        return self.put(body, what=what)

    def upgrade_control_plane(self, version, include_node_pools=False):
        """Upgrade the control plane to ``version``; node pools follow only when asked.

        The AKS API upgrades the control plane through kubernetesVersion; an
        agentPoolProfiles entry whose orchestratorVersion is left as it is
        stays on its version, which is what ``az aks upgrade
        --control-plane-only`` sends.
        https://learn.microsoft.com/en-us/azure/aks/upgrade-aks-cluster
        """
        def mutate(props):
            props["kubernetesVersion"] = version
            for profile in props.get("agentPoolProfiles") or []:
                if include_node_pools:
                    profile["orchestratorVersion"] = version
                elif not profile.get("orchestratorVersion"):
                    # Pin the pool explicitly so it is not upgraded with the control plane.
                    profile["orchestratorVersion"] = profile.get("currentOrchestratorVersion") or ""
        return self.update(mutate, what=f"Upgrading '{self.name}' to Kubernetes {version}")

    def start(self):
        """POST managedClusters/{name}/start.
        https://learn.microsoft.com/en-us/rest/api/aks/managed-clusters/start
        """
        resp = self.arm.call("POST", f"{self.id}/start", ok=(200, 202, 204), what=f"Starting '{self.name}'")
        self.arm.wait(resp, what=f"Starting '{self.name}'")

    def stop(self):
        """POST managedClusters/{name}/stop.
        https://learn.microsoft.com/en-us/rest/api/aks/managed-clusters/stop
        """
        resp = self.arm.call("POST", f"{self.id}/stop", ok=(200, 202, 204), what=f"Stopping '{self.name}'")
        self.arm.wait(resp, what=f"Stopping '{self.name}'")

    def delete(self):
        """DELETE managedClusters/{name} and wait. Returns False when it was already gone.
        https://learn.microsoft.com/en-us/rest/api/aks/managed-clusters/delete
        """
        resp = self.arm.call("DELETE", self.id, ok=(200, 202, 204), allow_404=True,
                             what=f"Deleting AKS cluster '{self.name}'")
        if resp is None or resp.status_code == 204:
            return resp is not None
        self.arm.wait(resp, what=f"Deleting AKS cluster '{self.name}'")
        return True

    # -- credentials -----------------------------------------------------------
    def _kubeconfig(self, action, params=None):
        resp = self.arm.call("POST", f"{self.id}/{action}", params=params, ok=(200,),
                             what=f"Reading credentials of '{self.name}'")
        kubeconfigs = (resp.json() or {}).get("kubeconfigs") or []
        if not kubeconfigs:
            raise AKSError(f"AKS returned no kubeconfig for '{self.name}'.")
        return base64.b64decode(kubeconfigs[0]["value"]).decode("utf-8")

    def admin_kubeconfig(self):
        """POST listClusterAdminCredential: the local admin kubeconfig (a secret).

        Fails with BadRequest when the cluster disables local accounts.
        https://learn.microsoft.com/en-us/rest/api/aks/managed-clusters/list-cluster-admin-credentials
        """
        return self._kubeconfig("listClusterAdminCredential")

    def user_kubeconfig(self, exec_format=True):
        """POST listClusterUserCredential, format=exec for Entra ID clusters.

        For a cluster with Microsoft Entra ID integration the kubeconfig holds
        no secret: the cluster CA and an exec block that runs kubelogin.
        https://learn.microsoft.com/en-us/rest/api/aks/managed-clusters/list-cluster-user-credentials
        """
        return self._kubeconfig("listClusterUserCredential", params={"format": "exec"} if exec_format else None)

    # -- node pools ------------------------------------------------------------
    def pools_path(self, pool=""):
        return f"{self.id}/agentPools" + (f"/{pool}" if pool else "")

    def list_pools(self):
        """GET agentPools: every node pool, sorted by name.
        https://learn.microsoft.com/en-us/rest/api/aks/agent-pools/list
        """
        pools = self.arm.list_json(self.pools_path(), what=f"Listing node pools of '{self.name}'")
        return sorted(pools, key=lambda p: p.get("name", ""))

    def get_pool(self, pool, allow_404=False):
        """GET agentPools/{pool}.
        https://learn.microsoft.com/en-us/rest/api/aks/agent-pools/get
        """
        return self.arm.get_json(self.pools_path(pool), allow_404=allow_404, what=f"Reading node pool '{pool}'")

    def put_pool(self, pool, properties, what=""):
        """PUT agentPools/{pool} with ``properties`` and wait. Returns the pool as AKS reports it.
        https://learn.microsoft.com/en-us/rest/api/aks/agent-pools/create-or-update
        """
        label = what or f"Updating node pool '{pool}'"
        resp = self.arm.call("PUT", self.pools_path(pool), body={"properties": properties},
                             ok=(200, 201), what=label)
        self.arm.wait(resp, what=label)
        return self.get_pool(pool)

    def update_pool(self, pool, mutate, what=""):
        """Read the pool, let ``mutate(properties)`` change it, and PUT it back.

        Agent pools have no PATCH; a PUT carries the whole pool, so the
        current properties are sent back with the change applied.
        """
        current = self.get_pool(pool)
        props = dict(current.get("properties") or {})
        mutate(props)
        for key in _POOL_READ_ONLY:
            props.pop(key, None)
        return self.put_pool(pool, props, what=what)

    def delete_pool(self, pool):
        """DELETE agentPools/{pool} and wait. Returns False when it was already gone.
        https://learn.microsoft.com/en-us/rest/api/aks/agent-pools/delete
        """
        label = f"Deleting node pool '{pool}'"
        resp = self.arm.call("DELETE", self.pools_path(pool), ok=(200, 202, 204), allow_404=True, what=label)
        if resp is None:
            return False
        if resp.status_code != 204:
            self.arm.wait(resp, what=label)
        return True

    def pool_upgrade_profile(self, pool):
        """GET agentPools/{pool}/upgradeProfiles/default.

        properties {kubernetesVersion, upgrades[{kubernetesVersion, isPreview}], latestNodeImageVersion}
        https://learn.microsoft.com/en-us/rest/api/aks/agent-pools/get-upgrade-profile
        """
        return self.arm.get_json(f"{self.pools_path(pool)}/upgradeProfiles/default",
                                 what=f"Reading upgrades for node pool '{pool}'")

    def upgrade_node_image(self, pool):
        """POST agentPools/{pool}/upgradeNodeImageVersion and wait.
        https://learn.microsoft.com/en-us/rest/api/aks/agent-pools/upgrade-node-image-version
        """
        label = f"Updating the node image of '{pool}'"
        resp = self.arm.call("POST", f"{self.pools_path(pool)}/upgradeNodeImageVersion",
                             ok=(200, 202), what=label)
        self.arm.wait(resp, what=label)
        return self.get_pool(pool)

    # -- extensions --------------------------------------------------------------
    def list_extensions(self):
        """Cluster extensions (Flux, Dapr, ...) with their versions; [] when none or unreadable.

        GET {cluster}/providers/Microsoft.KubernetesConfiguration/extensions
        https://learn.microsoft.com/en-us/rest/api/kubernetesconfiguration/extensions/extensions/list
        """
        try:
            return self.arm.list_json(
                f"{self.id}/providers/Microsoft.KubernetesConfiguration/extensions",
                api_version=EXTENSIONS_API_VERSION, what="Listing cluster extensions",
            )
        except AKSError as exc:
            logger.info("No extensions listed for %s: %s", self.name, exc)
            return []


def _writable_identity(identity):
    """The cluster identity as a PUT accepts it (principalId and tenantId are read-only)."""
    if not identity:
        return None
    result = {"type": identity.get("type")}
    if identity.get("userAssignedIdentities"):
        result["userAssignedIdentities"] = {key: {} for key in identity["userAssignedIdentities"]}
    return result


# ---------------------------------------------------------------------------
# Kubernetes versions in a region
# ---------------------------------------------------------------------------

def kubernetes_versions(arm, location):
    """Supported minor versions in ``location``, newest first.

    GET /subscriptions/{sub}/providers/Microsoft.ContainerService/locations/{location}/kubernetesVersions
    values[] {version, isDefault, isPreview, patchVersions{patch: {upgrades}}, capabilities.supportPlan}
    https://learn.microsoft.com/en-us/rest/api/aks/managed-clusters/list-kubernetes-versions
    Returns [{"version", "is_default", "is_preview", "latest_patch", "support_plans"}].
    """
    data = arm.get_json(
        f"/subscriptions/{arm.subscription_id}/providers/Microsoft.ContainerService"
        f"/locations/{location}/kubernetesVersions",
        what=f"Listing Kubernetes versions in {location}",
    )
    versions = []
    for item in data.get("values") or []:
        patches = sorted((item.get("patchVersions") or {}).keys(), key=version_key)
        versions.append({
            "version": item.get("version"),
            "is_default": bool(item.get("isDefault")),
            "is_preview": bool(item.get("isPreview")),
            "latest_patch": patches[-1] if patches else item.get("version"),
            "support_plans": list((item.get("capabilities") or {}).get("supportPlan") or []),
        })
    return sorted(versions, key=lambda v: version_key(v["version"]), reverse=True)


def version_key(version):
    """Sort key for '1.31' or '1.31.2'."""
    return tuple(int(p) if p.isdigit() else 0 for p in re.split(r"[.-]", str(version or "")))


def minor(version):
    """'1.31.2' -> '1.31'."""
    return ".".join(str(version or "").split(".")[:2])


# ---------------------------------------------------------------------------
# CloudBolt: environments, resources, and custom fields
# ---------------------------------------------------------------------------

def resolve_group(group):
    """The group kwarg arrives as a Group or as its name depending on caller."""
    from accounts.models import Group

    if group is None or isinstance(group, Group):
        return group
    return Group.objects.filter(name=str(group)).first()


def azure_environments_for_group(group):
    """Azure environments the group may order into, sorted by name.

    ``Group.get_available_environments()`` returns the environments entitled
    to the group (and its ancestors) plus every unconstrained environment;
    only those backed by an Azure resource handler are kept
    (docs/agents/rbac-and-security.md).
    """
    if group is None:
        return []
    available_ids = [env.id for env in group.get_available_environments()]
    return list(
        Environment.objects.filter(
            id__in=available_ids, resource_handler__resource_technology__slug="azure_arm"
        ).order_by("name")
    )


def handler_for_environment(env):
    """The cast Azure handler behind an Environment, or raise AKSError."""
    rh = env.resource_handler.cast() if env and env.resource_handler_id else None
    if rh is None or not hasattr(rh, "azure_tenant_id"):
        raise AKSError(f"Environment '{env}' is not backed by an Azure resource handler.")
    return rh


def environment_resource_groups(env):
    """Resource groups an Azure environment offers (its resource_group_arm options)."""
    values = env.custom_field_options.filter(field__name="resource_group_arm")
    return sorted({v.str_value for v in values if v.str_value}, key=str.lower)


def environment_node_sizes(env):
    """VM sizes an Azure environment offers (its node_size options)."""
    return sorted({str(v.value) for v in env.get_cfvs_for_custom_field("node_size") if v.value}, key=str.lower)


def is_aks_cluster(resource):
    """True for a Kubernetes Cluster resource of the Azure Kubernetes Service blueprint."""
    if resource is None or getattr(resource.resource_type, "name", "") != CLUSTER_RESOURCE_TYPE:
        return False
    return bool(resource.get_cf_values_as_dict().get(AKS_MARKER_FIELD))


def cluster_for_resource(resource):
    """An ``AKSCluster`` for a Kubernetes Cluster resource of this blueprint.

    The handler comes from the environment the cluster was ordered into
    (``env_id``) or, for a discovered cluster, from ``azure_rh_id``; the
    resource group and name from ``aks_resource_group`` and
    ``aks_cluster_name``. The handler stays server-side
    (docs/agents/rbac-and-security.md).
    """
    from resourcehandlers.azure_arm.models import AzureARMHandler

    values = resource.get_cf_values_as_dict()
    name = values.get(AKS_MARKER_FIELD)
    resource_group = values.get("aks_resource_group")
    if not name or not resource_group:
        raise AKSError(
            f"Resource '{resource.name}' records no AKS cluster (aks_cluster_name and "
            "aks_resource_group); run the Azure Kubernetes Service blueprint's discovery."
        )
    rh = None
    env_id = values.get("env_id")
    if env_id:
        env = Environment.objects.filter(id=int(str(env_id).strip())).first()
        if env is not None and env.resource_handler_id:
            rh = env.resource_handler.cast()
    if rh is None and values.get("azure_rh_id"):
        rh = AzureARMHandler.objects.filter(id=int(str(values["azure_rh_id"]).strip())).first()
    if rh is None:
        raise AKSError(f"Resource '{resource.name}' records no Azure environment or handler (env_id, azure_rh_id).")
    return AKSCluster(ArmClient(rh), resource_group, name)


def portal_url(arm, resource_id, blade="overview"):
    """Azure portal link to a resource: {portal}/#@{tenant}/resource{id}/{blade}."""
    tenant = f"@{arm.tenant_id}" if arm.tenant_id else ""
    return f"{arm.portal_base}/#{tenant}/resource{resource_id}/{blade}"


# https://learn.microsoft.com/en-us/rest/api/resources/subscriptions/get
SUBSCRIPTIONS_API_VERSION = "2022-12-01"


def subscription_display_name(arm):
    """The display name of the handler's subscription, as the Azure portal shows it.

    GET /subscriptions/{subscriptionId} -> {displayName, subscriptionId, state}
    https://learn.microsoft.com/en-us/rest/api/resources/subscriptions/get
    Read-only; raises AKSError when the handler may not read the subscription.
    """
    data = arm.get_json(f"/subscriptions/{arm.subscription_id}", api_version=SUBSCRIPTIONS_API_VERSION,
                        what="Reading the subscription")
    return (data or {}).get("displayName") or ""


# The custom fields on a Kubernetes Cluster resource of this blueprint.
# (name, label, type, description, show_as_attribute)
CLUSTER_FIELDS = [
    ("aks_cluster_name", "AKS Cluster Name", "STR", "Name of the AKS managed cluster.", True),
    ("aks_resource_group", "Resource Group", "STR", "Azure resource group of the AKS cluster.", True),
    ("aks_resource_id", "AKS Resource ID", "STR", "Azure resource ID of the AKS cluster.", False),
    ("aks_location", "Azure Region", "STR", "Azure region of the AKS cluster.", True),
    ("azure_rh_id", "Azure RH ID", "STR", "Azure resource handler that manages the cluster.", False),
    ("env_id", "Environment ID", "STR", "CloudBolt Environment the cluster was ordered into.", False),
    ("kubernetes_version", "Kubernetes Version", "STR", "Kubernetes version of the control plane.", True),
    ("aks_provisioning_state", "Provisioning State", "STR", "Last provisioning state AKS reported.", True),
    ("aks_power_state", "Power State", "STR", "Running or Stopped.", True),
    ("aks_fqdn", "API Server Address", "STR", "Fully qualified name of the Kubernetes API server.", True),
    ("aks_sku_tier", "Pricing Tier", "STR", "AKS pricing tier: Free, Standard, or Premium.", True),
    ("aks_network", "Network Configuration", "STR", "Network plugin, mode, dataplane, and policy engine.", True),
    ("aks_authentication", "Authentication", "STR", "Kubernetes authentication and authorization mode.", True),
    ("aks_node_resource_group", "Node Resource Group", "STR", "Resource group AKS manages for the nodes.", False),
    ("aks_upgrade_channel", "Upgrade Channel", "STR", "Automatic Kubernetes upgrade channel.", False),
    ("container_orchestrator_id", "Container Orchestrator ID", "INT",
     "Kubernetes container orchestrator CloudBolt uses to reach the cluster.", False),
]

# The custom fields on an AKS Node Pool sub-resource.
POOL_FIELDS = [
    ("aks_pool_name", "Node Pool", "STR", "Name of the AKS node pool.", True),
    ("aks_pool_mode", "Mode", "STR", "System or User node pool.", True),
    ("aks_pool_vm_size", "Node Size", "STR", "Azure VM size of the nodes.", True),
    ("aks_pool_os", "Operating System", "STR", "OS type and SKU of the nodes.", True),
    ("aks_pool_scale_method", "Scale Method", "STR", "Manual or Autoscale.", True),
    ("aks_pool_node_count", "Node Count", "INT", "Current number of nodes.", True),
    ("aks_pool_min_count", "Autoscale Minimum", "INT", "Fewest nodes the cluster autoscaler keeps.", True),
    ("aks_pool_max_count", "Autoscale Maximum", "INT", "Most nodes the cluster autoscaler adds.", True),
    ("aks_pool_kubernetes_version", "Kubernetes Version", "STR", "Kubernetes version of the nodes.", True),
    ("aks_pool_node_image", "Node Image Version", "STR", "Node image the nodes run.", True),
    ("aks_pool_zones", "Availability Zones", "STR", "Availability zones the nodes span.", True),
    ("aks_pool_state", "Provisioning State", "STR", "Last provisioning state AKS reported.", True),
    ("aks_pool_power_state", "Power State", "STR", "Running or Stopped.", True),
]


def _ensure_fields(definitions):
    """Create each custom field, and keep label, description, and visibility in sync."""
    for name, label, cf_type, description, show_as_attribute in definitions:
        cf, created = CustomField.objects.get_or_create(
            name=name,
            defaults=dict(label=label, description=description, type=cf_type,
                          show_on_servers=False, show_as_attribute=show_as_attribute),
        )
        if created or not name.startswith("aks_"):
            # Fields shared with other content (env_id, kubernetes_version, ...) are only created.
            continue
        changed = False
        for attr, value in (("label", label), ("description", description),
                            ("show_as_attribute", show_as_attribute)):
            if getattr(cf, attr, None) != value:
                setattr(cf, attr, value)
                changed = True
        if changed:
            cf.save()


def ensure_cluster_fields():
    _ensure_fields(CLUSTER_FIELDS)


def ensure_pool_fields():
    _ensure_fields(POOL_FIELDS)


def node_pool_resource_type():
    """The AKS Node Pool resource type, created on first use."""
    rt, _ = ResourceType.objects.get_or_create(
        name=NODE_POOL_RESOURCE_TYPE,
        defaults={"label": "AKS Node Pool", "plural_label": "AKS Node Pools", "icon": "fas fa-server"},
    )
    return rt


def describe_network(profile):
    """'Azure CNI Overlay, Cilium dataplane, Cilium network policy' from a networkProfile."""
    profile = profile or {}
    plugin = {"azure": "Azure CNI", "kubenet": "Kubenet", "none": "BYO CNI"}.get(
        str(profile.get("networkPlugin") or "").lower(), profile.get("networkPlugin") or "")
    if str(profile.get("networkPluginMode") or "").lower() == "overlay":
        plugin += " Overlay"
    parts = [plugin] if plugin else []
    if profile.get("networkDataplane"):
        parts.append(f"{str(profile['networkDataplane']).capitalize()} dataplane")
    policy = profile.get("networkPolicy")
    parts.append(f"{str(policy).capitalize()} network policy" if policy else "no network policy engine")
    return ", ".join(parts)


def describe_authentication(props):
    """'Microsoft Entra ID with Azure RBAC' (or with Kubernetes RBAC, or local accounts)."""
    aad = props.get("aadProfile") or {}
    if aad.get("managed") or aad.get("tenantID"):
        mode = "Microsoft Entra ID with Azure RBAC" if aad.get("enableAzureRBAC") else \
            "Microsoft Entra ID with Kubernetes RBAC"
    else:
        mode = "Local accounts with Kubernetes RBAC" if props.get("enableRBAC", True) else "Local accounts"
    if props.get("disableLocalAccounts"):
        mode += " (local accounts disabled)"
    return mode


def cluster_values(cluster_json):
    """The CLUSTER_FIELDS values for a managedCluster as GET returns it."""
    props = cluster_json.get("properties") or {}
    _, resource_group, _ = parse_resource_id(cluster_json.get("id"))
    return {
        "aks_cluster_name": cluster_json.get("name"),
        "aks_resource_group": resource_group,
        "aks_resource_id": cluster_json.get("id"),
        "aks_location": cluster_json.get("location"),
        "kubernetes_version": props.get("currentKubernetesVersion") or props.get("kubernetesVersion"),
        "aks_provisioning_state": props.get("provisioningState"),
        "aks_power_state": (props.get("powerState") or {}).get("code"),
        "aks_fqdn": props.get("fqdn") or props.get("privateFQDN"),
        "aks_sku_tier": (cluster_json.get("sku") or {}).get("tier"),
        "aks_network": describe_network(props.get("networkProfile")),
        "aks_authentication": describe_authentication(props),
        "aks_node_resource_group": props.get("nodeResourceGroup"),
        "aks_upgrade_channel": (props.get("autoUpgradeProfile") or {}).get("upgradeChannel") or "none",
    }


def stamp_cluster(resource, cluster_json, rh=None, env=None):
    """Write the cluster's values onto its CloudBolt resource (and save it)."""
    ensure_cluster_fields()
    for key, value in cluster_values(cluster_json).items():
        if value not in (None, ""):
            resource.set_value_for_custom_field(key, value)
    if rh is not None:
        resource.set_value_for_custom_field("azure_rh_id", str(rh.id))
    if env is not None:
        resource.set_value_for_custom_field("env_id", str(env.id))
    resource.save()


def pool_values(pool_json):
    """The POOL_FIELDS values for an agentPool as GET returns it."""
    props = pool_json.get("properties") or {}
    autoscale = bool(props.get("enableAutoScaling"))
    os_parts = [props.get("osType") or "Linux"]
    if props.get("osSKU"):
        os_parts.append(props["osSKU"])
    return {
        "aks_pool_name": pool_json.get("name"),
        "aks_pool_mode": props.get("mode"),
        "aks_pool_vm_size": props.get("vmSize"),
        "aks_pool_os": " / ".join(os_parts),
        "aks_pool_scale_method": "Autoscale" if autoscale else "Manual",
        "aks_pool_node_count": props.get("count"),
        "aks_pool_min_count": props.get("minCount") if autoscale else None,
        "aks_pool_max_count": props.get("maxCount") if autoscale else None,
        "aks_pool_kubernetes_version": props.get("currentOrchestratorVersion") or props.get("orchestratorVersion"),
        "aks_pool_node_image": props.get("nodeImageVersion"),
        "aks_pool_zones": ", ".join(str(z) for z in props.get("availabilityZones") or []) or "None",
        "aks_pool_state": props.get("provisioningState"),
        "aks_pool_power_state": (props.get("powerState") or {}).get("code"),
    }


def pool_label(pool_json):
    """'userpool (User, 3 x Standard_D4s_v5, autoscale 1-5, Succeeded)' for dropdowns."""
    values = pool_values(pool_json)
    scale = (
        f"autoscale {values['aks_pool_min_count']}-{values['aks_pool_max_count']}"
        if values["aks_pool_scale_method"] == "Autoscale" else "manual"
    )
    return (
        f"{values['aks_pool_name']} ({values['aks_pool_mode']}, {values['aks_pool_node_count']} x "
        f"{values['aks_pool_vm_size']}, {scale}, Kubernetes {values['aks_pool_kubernetes_version']}, "
        f"{values['aks_pool_state']})"
    )


def stamp_pool(sub_resource, pool_json):
    """Write the pool's values onto its sub-resource; a value AKS no longer reports is cleared."""
    for key, value in pool_values(pool_json).items():
        sub_resource.set_value_for_custom_field(key, None if value in (None, "") else value, ignore_none_value=True)
    sub_resource.save()


def pool_sub_resources(resource):
    return Resource.objects.filter(
        parent_resource=resource, resource_type__name=NODE_POOL_RESOURCE_TYPE, lifecycle="ACTIVE"
    )


def upsert_pool_sub_resource(resource, pool_json):
    """Create or refresh the AKS Node Pool sub-resource for one pool. Returns (sub, created)."""
    ensure_pool_fields()
    name = pool_json.get("name")
    sub = pool_sub_resources(resource).filter(name=name).first()
    created = sub is None
    if created:
        sub = Resource.objects.create(
            name=name,
            group=resource.group,
            owner=resource.owner,
            parent_resource=resource,
            resource_type=node_pool_resource_type(),
            blueprint=resource.blueprint,
            lifecycle="ACTIVE",
        )
    stamp_pool(sub, pool_json)
    return sub, created


def remove_pool_sub_resource(resource, pool_name):
    """Delete the sub-resource of a pool that no longer exists. Returns how many were removed."""
    # Count first: delete() also counts the rows it cascades to.
    stale = pool_sub_resources(resource).filter(name=pool_name)
    removed = stale.count()
    stale.delete()
    return removed


def sync_pool_sub_resources(resource, pools):
    """Make the AKS Node Pool sub-resources match ``pools``. Returns (created, updated, removed)."""
    names = {p.get("name") for p in pools}
    stale = pool_sub_resources(resource).exclude(name__in=names)
    removed = stale.count()
    stale.delete()
    created = updated = 0
    for pool in pools:
        _, was_created = upsert_pool_sub_resource(resource, pool)
        created += int(was_created)
        updated += int(not was_created)
    return created, updated, removed


# ---------------------------------------------------------------------------
# Kubernetes container orchestrator and per-cluster Environment
# ---------------------------------------------------------------------------

def parse_admin_kubeconfig(text):
    """(server, ca_data, cert_data, key_data, token) from an AKS admin kubeconfig.

    Data fields stay base64, which the orchestrator's certificate fields accept.
    https://kubernetes.io/docs/concepts/configuration/organize-cluster-access-kubeconfig/
    """
    config = yaml.safe_load(text) or {}
    context_name = config.get("current-context")
    contexts = {c.get("name"): c.get("context") or {} for c in config.get("contexts") or []}
    context = contexts.get(context_name) or (next(iter(contexts.values())) if contexts else {})
    clusters = {c.get("name"): c.get("cluster") or {} for c in config.get("clusters") or []}
    users = {u.get("name"): u.get("user") or {} for u in config.get("users") or []}
    cluster = clusters.get(context.get("cluster")) or (next(iter(clusters.values())) if clusters else {})
    user = users.get(context.get("user")) or (next(iter(users.values())) if users else {})
    return (
        cluster.get("server", ""),
        cluster.get("certificate-authority-data", ""),
        user.get("client-certificate-data", ""),
        user.get("client-key-data", ""),
        user.get("token", ""),
    )


def _orchestrator_credentials(cluster):
    """Kubernetes orchestrator fields from the cluster's admin kubeconfig.

    Client-certificate authentication verifies the API server against the
    cluster's own CA; a token-only kubeconfig falls back to bearer-token auth.
    """
    from urllib.parse import urlparse

    server, ca, cert, key, token = parse_admin_kubeconfig(cluster.admin_kubeconfig())
    parsed = urlparse(server)
    fields = {"ip": parsed.hostname or "", "port": parsed.port or 443, "protocol": parsed.scheme or "https",
              "serviceaccount": "clusterAdmin"}
    if cert and key:
        fields.update(auth_type="CERTIFICATE", cert_file_contents=cert, key_file_contents=key,
                      ca_file_contents=ca, servicepasswd="")
    elif token:
        fields.update(auth_type="TOKEN", servicepasswd=token, ca_file_contents=ca)
    else:
        raise AKSError(f"The admin kubeconfig of '{cluster.name}' has neither a client certificate nor a token.")
    return fields


def cluster_environment_name(cluster_name, location):
    return f"Resource-{cluster_name} Environment#{location}"


def register_orchestrator(resource, cluster, location):
    """Create (or refresh) the cluster's Kubernetes orchestrator and per-cluster Environment.

    The orchestrator lets CloudBolt, the namespace blueprint, and container
    blueprints reach the API server; the Environment on it is what groups
    are entitled to when they may order namespaces on the cluster. Returns
    (orchestrator, environment, created).
    """
    from containerorchestrators.kuberneteshandler.models import Kubernetes
    from containerorchestrators.models import ContainerOrchestratorTechnology

    fields = _orchestrator_credentials(cluster)
    orchestrator = None
    current_id = resource.get_cf_values_as_dict().get(CLUSTER_ORCHESTRATOR_FIELD)
    if current_id:
        orchestrator = Kubernetes.objects.filter(id=int(current_id)).first()
    created = orchestrator is None
    if created:
        tech = ContainerOrchestratorTechnology.objects.get(name="Kubernetes")
        orchestrator = Kubernetes.objects.create(name=cluster.name, container_technology=tech, **fields)
    else:
        for key, value in fields.items():
            setattr(orchestrator, key, value)
        orchestrator.save()
    resource.set_value_for_custom_field(CLUSTER_ORCHESTRATOR_FIELD, orchestrator.id)
    resource.save()

    env = (
        Environment.objects.filter(container_orchestrator=orchestrator, resource_handler__isnull=True,
                                   name__startswith="Resource-")
        .exclude(custom_field_options__field__name=NAMESPACE_ENV_FIELD)
        .first()
    )
    if env is None:
        env = Environment.objects.create(
            name=cluster_environment_name(cluster.name, location),
            container_orchestrator=orchestrator,
            description=f"AKS cluster {cluster.name} ({location}).",
        )
    return orchestrator, env, created


# Mirrors shared_modules/eks_namespace: the field that pins an Environment to a namespace.
NAMESPACE_ENV_FIELD = "kubernetes_namespace"


def delete_orchestrator(resource):
    """Remove the cluster's orchestrator and the per-cluster Environment(s) without a handler.

    Environment.container_orchestrator is SET_NULL, so the environments would
    otherwise be orphaned. Returns a message.
    """
    from containerorchestrators.kuberneteshandler.models import Kubernetes

    orchestrator_id = resource.get_cf_values_as_dict().get(CLUSTER_ORCHESTRATOR_FIELD)
    if not orchestrator_id:
        return "No container orchestrator was recorded."
    orchestrator = Kubernetes.objects.filter(id=int(orchestrator_id)).first()
    if orchestrator is None:
        return "The container orchestrator was already gone."
    environments = Environment.objects.filter(
        container_orchestrator=orchestrator, resource_handler__isnull=True
    )
    removed = environments.count()
    environments.delete()
    name = orchestrator.name
    orchestrator.delete()
    return f"Removed container orchestrator {name} and {removed} environment(s)."


def active_namespaces(resource):
    """Namespace landing zones hosted on the cluster (sub-resources), oldest first."""
    return Resource.objects.filter(
        parent_resource=resource, resource_type__name=NAMESPACE_RESOURCE_TYPE, lifecycle="ACTIVE"
    ).order_by("name")


# ---------------------------------------------------------------------------
# Role assignments
# ---------------------------------------------------------------------------
# https://learn.microsoft.com/en-us/rest/api/authorization/role-assignments/create
# https://learn.microsoft.com/en-us/rest/api/authorization/role-assignments/list-for-scope
# https://learn.microsoft.com/en-us/rest/api/authorization/role-assignments/delete
# Assignment names are GUIDs. They are derived from (scope, role, principal)
# so assigning twice is idempotent and the assignment can be found again.

def role_definition_id(arm, role_guid):
    return (
        f"/subscriptions/{arm.subscription_id}/providers/Microsoft.Authorization"
        f"/roleDefinitions/{role_guid}"
    )


def assignment_name(scope, role_guid, principal_id):
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"cloudbolt|{scope.lower()}|{role_guid}|{principal_id.lower()}"))


def assign_role(arm, scope, role_guid, principal_id, principal_type="", description=""):
    """Assign a role at ``scope``. Returns True when created, False when it already existed.

    ``principal_type`` (User, Group, ServicePrincipal) avoids the replication
    delay error for a principal created moments ago.
    """
    body = {"properties": {
        "roleDefinitionId": role_definition_id(arm, role_guid),
        "principalId": principal_id,
    }}
    if principal_type:
        body["properties"]["principalType"] = principal_type
    if description:
        body["properties"]["description"] = description[:1024]
    path = f"{scope}/providers/Microsoft.Authorization/roleAssignments/{assignment_name(scope, role_guid, principal_id)}"
    resp = arm.request("PUT", path, body=body, api_version=AUTHORIZATION_API_VERSION)
    if resp.status_code in (200, 201):
        return resp.status_code == 201
    code, message = _error_parts(resp)
    if code == "RoleAssignmentExists":
        return False
    raise AKSError(f"Assigning a role at {scope} failed: {code}: {message}", resp.status_code, code)


def list_role_assignments(arm, scope, at_scope_only=True):
    """Role assignments at (or above, unless ``at_scope_only``) ``scope``."""
    params = {"$filter": "atScope()"} if at_scope_only else None
    return arm.list_json(
        f"{scope}/providers/Microsoft.Authorization/roleAssignments",
        api_version=AUTHORIZATION_API_VERSION, params=params, what=f"Listing role assignments at {scope}",
    )


def delete_role_assignment(arm, assignment_id):
    """Delete a role assignment by its full id. Returns False when it was already gone."""
    resp = arm.call("DELETE", assignment_id, api_version=AUTHORIZATION_API_VERSION,
                    ok=(200, 204), allow_404=True, what="Deleting a role assignment")
    return resp is not None and resp.status_code == 200


def guid_or_none(text):
    text = str(text or "").strip()
    return text.lower() if GUID_RE.match(text) else None


def now_utc():
    """An aware UTC timestamp (CloudBolt's own clock is naive local time)."""
    return datetime.datetime.now(datetime.timezone.utc)


def to_json(value):
    return json.dumps(value, sort_keys=True)
