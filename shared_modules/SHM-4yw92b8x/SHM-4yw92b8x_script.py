"""
Shared module: gcp_gke

Google Kubernetes Engine helpers for the Google Kubernetes Engine blueprint,
its day-2 actions, the GKE Namespace blueprint, and the Google GKE Cluster UI
extension. It has four parts:

1. ``GKEApi`` -- the GKE (container v1) and Compute Engine APIs, authenticated
   as a CloudBolt GCP resource handler. The clients come from the handler's
   own ``gcloud_wrapper.discover()``, which CloudBolt provides so content can
   build further Google API clients that honour the appliance's proxy and SSL
   settings. Long-running operations are followed through
   projects.locations.operations.get.

2. ``GKECluster`` -- one Standard cluster: read it, its server config and
   upgrade information, its node pools; create, resize, autoscale, upgrade,
   and delete node pools; upgrade the control plane; delete the cluster.
   Every call cites the REST reference it follows (docs/agents/external-apis.md).

3. ``KubeClient`` -- the cluster's Kubernetes API over HTTPS, verified against
   the cluster's own CA (masterAuth.clusterCaCertificate) rather than the
   appliance trust store. The build and discovery use it with the handler's
   Google access token to create the ``cloudbolt-admin`` ServiceAccount whose
   token the Kubernetes container orchestrator holds; everything else uses it
   with that token.

4. CloudBolt-side helpers -- the custom fields stamped on a Kubernetes
   Cluster resource and on its GKE Node Pool sub-resources, the GCP
   environments a group may order into, the container orchestrator and
   per-cluster Environment, and the CloudBolt groups entitled to that
   Environment (who may order namespaces on the cluster).

The Kubernetes Cluster resource type is shared with the AWS EKS and Azure
Kubernetes Service blueprints; a GKE cluster is told apart by its
``gke_cluster_name`` value.

Import from plugins as::

    from shared_modules.gcp_gke import GKECluster, GKEError, cluster_for_resource, ...

Shared-module code is cached by the running CloudBolt process: after changing
this file and syncing, restart CloudBolt (the job engine and the web server)
for the change to take effect.
"""

import base64
import datetime
import json
import os
import re
import tempfile
import time

import requests

from common.methods import set_progress
from infrastructure.models import CustomField, Environment
from resources.models import Resource, ResourceType
from utilities.logger import ThreadLogger

logger = ThreadLogger(__name__)

REQUEST_TIMEOUT = 60
POLL_SECONDS = 15
BUSY_WAIT_SECONDS = 1800
# What GKE answers while another operation holds the cluster or node pool.
# https://cloud.google.com/kubernetes-engine/docs/troubleshooting/concurrent-operations
BUSY_RE = re.compile(r"incompatible operation|currently being created, deleted, updated or repaired|"
                     r"is currently \w+ing cluster", re.IGNORECASE)

CLOUD_PLATFORM_SCOPE = "https://www.googleapis.com/auth/cloud-platform"
USERINFO_EMAIL_SCOPE = "https://www.googleapis.com/auth/userinfo.email"

# The resource types this module stamps. kubernetes_cluster is shared with the
# EKS and AKS blueprints; gke_node_pool is the GKE counterpart of aks_node_pool.
CLUSTER_RESOURCE_TYPE = "kubernetes_cluster"
NODE_POOL_RESOURCE_TYPE = "gke_node_pool"
NAMESPACE_RESOURCE_TYPE = "kubernetes_namespace"
CLUSTER_ORCHESTRATOR_FIELD = "container_orchestrator_id"
# Mirrors shared_modules/eks_namespace: the field that pins an Environment to a namespace.
NAMESPACE_ENV_FIELD = "kubernetes_namespace"

# The field that marks a Kubernetes Cluster resource as a GKE cluster.
GKE_MARKER_FIELD = "gke_cluster_name"

# Cluster names: up to 40 characters of lowercase letters, digits, and hyphens,
# starting with a letter and ending with a letter or digit.
# https://cloud.google.com/kubernetes-engine/docs/reference/rest/v1/projects.locations.clusters#Cluster
CLUSTER_NAME_RE = re.compile(r"^[a-z]([-a-z0-9]{0,38}[a-z0-9])?$")
# GKE documents no node pool name rule; this content applies the cluster rule,
# which also keeps a name valid as a Kubernetes label value.
NODE_POOL_NAME_RE = re.compile(r"^[a-z]([-a-z0-9]{0,38}[a-z0-9])?$")

# The node pool the build creates.
DEFAULT_POOL_NAME = "default-pool"

# Release channels. A cluster on a channel is upgraded automatically, control
# plane and nodes; Extended keeps a minor version up to 24 months.
# https://cloud.google.com/kubernetes-engine/docs/concepts/release-channels
RELEASE_CHANNELS = [
    ("REGULAR", "Regular (recommended)"),
    ("STABLE", "Stable"),
    ("RAPID", "Rapid"),
    ("EXTENDED", "Extended"),
]
RELEASE_CHANNEL_LABELS = {"REGULAR": "Regular", "STABLE": "Stable", "RAPID": "Rapid", "EXTENDED": "Extended",
                          "UNSPECIFIED": "No channel"}

# The ServiceAccount, its token Secret, and the ClusterRoleBinding that give
# CloudBolt's container orchestrator cluster-admin on a GKE cluster.
ADMIN_SERVICE_ACCOUNT = "cloudbolt-admin"
ADMIN_TOKEN_SECRET = "cloudbolt-admin-token"
ADMIN_BINDING = "cloudbolt-admin"
ADMIN_NAMESPACE = "kube-system"
# The ClusterRoleBinding that gives the order form's Cluster Admin Principals cluster-admin.
CLUSTER_ADMINS_BINDING = "cloudbolt-cluster-admins"
MANAGED_BY_LABEL = "app.kubernetes.io/managed-by"
MANAGED_BY_VALUE = "cloudbolt"

EMAIL_RE = re.compile(r"^[^@\s:]+@[^@\s:]+\.[^@\s:]+$")


class GKEError(Exception):
    """A readable Google Cloud or Kubernetes failure. ``status_code`` is the HTTP status when known."""

    def __init__(self, message, status_code=None, reason=""):
        super().__init__(message)
        self.status_code = status_code
        self.reason = reason or ""


# ---------------------------------------------------------------------------
# Google API clients
# ---------------------------------------------------------------------------

def _http_error_parts(exc):
    """(status, reason, message) from a googleapiclient HttpError; never includes headers."""
    status = getattr(getattr(exc, "resp", None), "status", None)
    reason, message = "", ""
    try:
        body = json.loads((getattr(exc, "content", b"") or b"").decode("utf-8"))
        err = body.get("error") if isinstance(body, dict) else None
        if isinstance(err, dict):
            reason = err.get("status") or ""
            message = err.get("message") or ""
    except (ValueError, AttributeError):
        pass
    if not message:
        message = str(exc)[:300]
    try:
        status = int(status) if status is not None else None
    except (TypeError, ValueError):
        status = None
    return status, reason, message


class GKEApi:
    """GKE and Compute Engine calls as one GCP resource handler.

    The handler's ``gcloud_wrapper`` holds the handler's OAuth or service
    account credentials (cloud-platform scope), refreshed on creation, and an
    httplib2 client that applies the appliance's proxy and SSL settings;
    ``discover()`` builds further API clients on it.
    """

    def __init__(self, rh):
        self.rh = rh
        try:
            self.wrapper = rh.gcloud_wrapper
            # https://cloud.google.com/kubernetes-engine/docs/reference/rest
            self.container = self.wrapper.discover("container", "v1")
            # https://cloud.google.com/compute/docs/reference/rest/v1
            self.compute = self.wrapper.compute
        except Exception as exc:  # noqa: BLE001 - any credential or discovery failure
            raise GKEError(f"Could not connect GCP handler '{rh}' to Google Cloud: {exc}")

    # ---- transport ---------------------------------------------------------
    def call(self, request, what="", allow_404=False, wait_if_busy=False):
        """Execute a googleapiclient request; raise GKEError, or return None on 404 when ``allow_404``.

        ``num_retries`` retries 429 and 5xx responses with backoff.
        https://googleapis.github.io/google-api-python-client/docs/epy/googleapiclient.http.HttpRequest-class.html#execute
        GKE runs one operation at the cluster level, or one per node pool, at
        a time, its own automatic upgrades and repairs included; a change
        made meanwhile is refused. With ``wait_if_busy`` such a refusal is
        retried every 30 seconds for up to BUSY_WAIT_SECONDS.
        https://cloud.google.com/kubernetes-engine/docs/troubleshooting/concurrent-operations
        """
        from googleapiclient.errors import HttpError

        deadline = time.time() + BUSY_WAIT_SECONDS
        while True:
            try:
                return request.execute(num_retries=3)
            except HttpError as exc:
                status, reason, message = _http_error_parts(exc)
                if status == 404 and allow_404:
                    return None
                if wait_if_busy and BUSY_RE.search(message) and time.time() < deadline:
                    set_progress(f"{what or 'The change'} waits for another GKE operation: {message[:200]}")
                    time.sleep(30)
                    continue
                prefix = f"{what} failed" if what else "Google Cloud request failed"
                raise GKEError(f"{prefix}: {message}", status, reason)
            except Exception as exc:  # noqa: BLE001 - transport errors from httplib2
                raise GKEError(f"{what or 'Google Cloud request'} failed: {exc}")

    def access_token(self):
        """A Google access token for the handler's identity, for the cluster's Kubernetes API.

        GKE accepts Google user and service account tokens as bearer tokens
        and authorizes them with the identity's IAM permissions
        (roles/container.admin covers every Kubernetes object and binding a
        ClusterRole). A token without the userinfo.email scope identifies a
        service account by its numeric unique ID instead of its email, so a
        service-account handler gets a token with both scopes; a handler
        signed in with user OAuth credentials keeps the scopes it was granted.
        https://cloud.google.com/kubernetes-engine/docs/how-to/api-server-authentication
        https://cloud.google.com/kubernetes-engine/docs/how-to/role-based-access-control
        https://cloud.google.com/kubernetes-engine/docs/how-to/iam
        """
        from google_auth_httplib2 import Request

        try:
            authorized = self.wrapper.authorized_http
            creds = authorized.credentials
            if hasattr(creds, "with_scopes") and getattr(creds, "service_account_email", None):
                creds = creds.with_scopes([CLOUD_PLATFORM_SCOPE, USERINFO_EMAIL_SCOPE])
                creds.refresh(Request(authorized.http))
                return creds.token
            if not getattr(creds, "valid", False):
                creds.refresh(Request(authorized.http))
            return creds.token
        except Exception as exc:  # noqa: BLE001 - google.auth refresh and transport errors
            raise GKEError(f"Could not get a Google access token for handler '{self.rh}': {exc}")

    def project_number(self, project):
        """The numeric project number: cloudresourcemanager v1 projects.get -> projectNumber.
        https://cloud.google.com/resource-manager/reference/rest/v1/projects/get
        """
        data = self.call(self.wrapper.cloud_resource_manager.projects().get(projectId=project),
                         what=f"Reading project {project}") or {}
        return str(data.get("projectNumber") or "")

    # ---- operations ----------------------------------------------------------
    def wait(self, operation, project, location, what="operation", timeout=3600, progress=True):
        """Follow a GKE operation until it is DONE; raise GKEError when it ends with an error.

        projects.locations.operations.get returns {name, status: PENDING |
        RUNNING | DONE | ABORTING, error {code, message}, statusMessage,
        operationType, progress}.
        https://cloud.google.com/kubernetes-engine/docs/reference/rest/v1/projects.locations.operations/get
        https://cloud.google.com/kubernetes-engine/docs/reference/rest/v1/projects.locations.operations#Operation
        """
        if not operation:
            return operation
        op_name = str(operation.get("name") or "")
        full_name = op_name if op_name.startswith("projects/") else \
            f"projects/{project}/locations/{location}/operations/{op_name}"
        started = time.time()
        deadline = started + timeout
        last_report = started
        current = operation
        while True:
            status = str(current.get("status") or "")
            if status == "DONE":
                err = current.get("error") or {}
                if err:
                    raise GKEError(f"{what} failed: {err.get('message') or current.get('statusMessage') or err}")
                return current
            if time.time() >= deadline:
                raise GKEError(f"{what} did not finish within {timeout // 60} minutes (operation {op_name}).")
            time.sleep(POLL_SECONDS)
            current = self.call(
                self.container.projects().locations().operations().get(name=full_name),
                what=f"Checking {what}",
            )
            if progress and time.time() - last_report >= 60:
                last_report = time.time()
                set_progress(f"{what} is {current.get('status', 'RUNNING')} "
                             f"({int((last_report - started) // 60)} min)", increment_tasks=1)

    # ---- locations -------------------------------------------------------------
    def server_config(self, project, location):
        """projects.locations.getServerConfig: versions and release channels offered in a location.

        {defaultClusterVersion, validMasterVersions[], validNodeVersions[],
         channels[{channel, defaultVersion, validVersions[], upgradeTargetVersion}]}
        https://cloud.google.com/kubernetes-engine/docs/reference/rest/v1/projects.locations/getServerConfig
        https://cloud.google.com/kubernetes-engine/docs/reference/rest/v1/ServerConfig
        """
        return self.call(
            self.container.projects().locations().getServerConfig(name=f"projects/{project}/locations/{location}"),
            what=f"Reading the GKE versions offered in {location}",
        ) or {}

    def list_clusters(self, project):
        """Every cluster in the project, all locations: clusters.list with location '-'.

        Returns (clusters, missing_zones).
        https://cloud.google.com/kubernetes-engine/docs/reference/rest/v1/projects.locations.clusters/list
        """
        data = self.call(
            self.container.projects().locations().clusters().list(parent=f"projects/{project}/locations/-"),
            what=f"Listing GKE clusters in project {project}",
        ) or {}
        return data.get("clusters") or [], data.get("missingZones") or []

    def usable_subnetworks(self, project, region=None):
        """Subnetworks the handler may use in a project, Shared VPC subnets included.

        subnetworks.listUsable returns UsableSubnetwork {subnetwork, network,
        ipCidrRange, secondaryIpRanges[]} with full URLs.
        https://cloud.google.com/compute/docs/reference/rest/v1/subnetworks/listUsable
        """
        items, request = [], self.compute.subnetworks().listUsable(project=project)
        while request is not None:
            page = self.call(request, what=f"Listing usable subnetworks in {project}") or {}
            items.extend(page.get("items") or [])
            request = self.compute.subnetworks().listUsable_next(previous_request=request, previous_response=page)
        if region:
            items = [i for i in items if f"/regions/{region}/" in str(i.get("subnetwork", ""))]
        return sorted(items, key=lambda i: str(i.get("subnetwork", "")))

    def secondary_ranges(self, project, subnetwork):
        """The secondary ranges of a subnetwork as GKE sees them, with their status.

        projects.aggregated.usableSubnetworks.list, filtered to the network's
        project, returns {subnetwork, network, ipCidrRange,
        secondaryIpRanges[{rangeName, ipCidrRange, status}]}; status is UNUSED,
        IN_USE_SERVICE, IN_USE_SHAREABLE_POD, or IN_USE_MANAGED_POD.
        https://cloud.google.com/kubernetes-engine/docs/reference/rest/v1/projects.aggregated.usableSubnetworks/list
        """
        wanted = relative_name(subnetwork)
        network_project = subnetwork_parts(wanted)[0]
        usable = self.container.projects().aggregated().usableSubnetworks()
        request = usable.list(parent=f"projects/{project}", filter=f"networkProjectId={network_project}")
        while request is not None:
            page = self.call(request, what=f"Listing the subnetworks GKE may use in {network_project}") or {}
            for item in page.get("subnetworks") or []:
                if relative_name(item.get("subnetwork")) == wanted:
                    return item.get("secondaryIpRanges") or []
            request = usable.list_next(previous_request=request, previous_response=page)
        return []

    def region_zones(self, project, region):
        """Zones of a region that are UP, sorted: regions.get -> zones[] (URLs).
        https://cloud.google.com/compute/docs/reference/rest/v1/regions/get
        """
        data = self.call(self.compute.regions().get(project=project, region=region),
                         what=f"Reading region {region}") or {}
        zones = sorted(str(z).rsplit("/", 1)[-1] for z in data.get("zones") or [])
        return zones


# ---------------------------------------------------------------------------
# One Standard cluster
# ---------------------------------------------------------------------------

class GKECluster:
    """A GKE cluster, addressed by project, location (region or zone), and name."""

    def __init__(self, api, project, location, name):
        self.api = api
        self.project = project
        self.location = location
        self.name = name
        self.path = f"projects/{project}/locations/{location}/clusters/{name}"

    @property
    def _clusters(self):
        return self.api.container.projects().locations().clusters()

    def _wait(self, operation, what):
        return self.api.wait(operation, self.project, self.location, what=what)

    # -- the cluster ----------------------------------------------------------
    def get(self, allow_404=False):
        """clusters.get.
        https://cloud.google.com/kubernetes-engine/docs/reference/rest/v1/projects.locations.clusters/get
        """
        return self.api.call(self._clusters.get(name=self.path), what=f"Reading GKE cluster '{self.name}'",
                             allow_404=allow_404)

    def create(self, cluster_body):
        """clusters.create {cluster} and wait. Returns the cluster as GKE reports it.
        https://cloud.google.com/kubernetes-engine/docs/reference/rest/v1/projects.locations.clusters/create
        """
        what = f"Creating GKE cluster '{self.name}'"
        operation = self.api.call(
            self._clusters.create(parent=f"projects/{self.project}/locations/{self.location}",
                                  body={"cluster": cluster_body}),
            what=what, wait_if_busy=True,
        )
        self._wait(operation, what)
        return self.get()

    def update(self, update, what=""):
        """clusters.update {update: ClusterUpdate} and wait. Returns the cluster.
        https://cloud.google.com/kubernetes-engine/docs/reference/rest/v1/projects.locations.clusters/update
        """
        label = what or f"Updating GKE cluster '{self.name}'"
        operation = self.api.call(self._clusters.update(name=self.path, body={"update": update}), what=label,
                                  wait_if_busy=True)
        self._wait(operation, label)
        return self.get()

    def upgrade_control_plane(self, version):
        """Upgrade the control plane: ClusterUpdate.desiredMasterVersion ('-' = default version).
        https://cloud.google.com/kubernetes-engine/docs/reference/rest/v1/ClusterUpdate
        https://cloud.google.com/kubernetes-engine/docs/how-to/upgrading-a-cluster
        """
        return self.update({"desiredMasterVersion": version},
                           what=f"Upgrading the control plane of '{self.name}' to {version}")

    def delete(self):
        """clusters.delete and wait. Returns False when it was already gone.
        https://cloud.google.com/kubernetes-engine/docs/reference/rest/v1/projects.locations.clusters/delete
        """
        what = f"Deleting GKE cluster '{self.name}'"
        operation = self.api.call(self._clusters.delete(name=self.path), what=what, allow_404=True,
                                  wait_if_busy=True)
        if operation is None:
            return False
        self._wait(operation, what)
        return True

    def upgrade_info(self, version=None):
        """clusters.fetchClusterUpgradeInfo, or None when GKE does not answer.

        ClusterUpgradeInfo {autoUpgradeStatus[], pausedReason[],
        upgradeDetails[], endOfStandardSupportTimestamp,
        endOfExtendedSupportTimestamp, minorTargetVersion, patchTargetVersion}
        https://cloud.google.com/kubernetes-engine/docs/reference/rest/v1/projects.locations.clusters/fetchClusterUpgradeInfo
        """
        try:
            kwargs = {"name": self.path}
            if version:
                kwargs["version"] = version
            return self.api.call(self._clusters.fetchClusterUpgradeInfo(**kwargs),
                                 what=f"Reading upgrade information of '{self.name}'")
        except (GKEError, AttributeError) as exc:
            logger.info("No upgrade information for %s: %s", self.name, exc)
            return None

    # -- node pools ------------------------------------------------------------
    @property
    def _pools(self):
        return self._clusters.nodePools()

    def pool_path(self, pool):
        return f"{self.path}/nodePools/{pool}"

    def list_pools(self):
        """nodePools.list: every node pool, sorted by name.
        https://cloud.google.com/kubernetes-engine/docs/reference/rest/v1/projects.locations.clusters.nodePools/list
        """
        data = self.api.call(self._pools.list(parent=self.path), what=f"Listing node pools of '{self.name}'") or {}
        return sorted(data.get("nodePools") or [], key=lambda p: p.get("name", ""))

    def get_pool(self, pool, allow_404=False):
        """nodePools.get.
        https://cloud.google.com/kubernetes-engine/docs/reference/rest/v1/projects.locations.clusters.nodePools/get
        """
        return self.api.call(self._pools.get(name=self.pool_path(pool)), what=f"Reading node pool '{pool}'",
                             allow_404=allow_404)

    def create_pool(self, pool_body):
        """nodePools.create {nodePool} and wait. Returns the pool.
        https://cloud.google.com/kubernetes-engine/docs/reference/rest/v1/projects.locations.clusters.nodePools/create
        """
        what = f"Creating node pool '{pool_body.get('name')}'"
        operation = self.api.call(self._pools.create(parent=self.path, body={"nodePool": pool_body}), what=what,
                                  wait_if_busy=True)
        self._wait(operation, what)
        return self.get_pool(pool_body["name"])

    def set_pool_size(self, pool, node_count):
        """nodePools.setSize {nodeCount} (nodes per zone of the pool) and wait.
        https://cloud.google.com/kubernetes-engine/docs/reference/rest/v1/projects.locations.clusters.nodePools/setSize
        """
        what = f"Resizing node pool '{pool}' to {node_count} node(s) per zone"
        operation = self.api.call(self._pools.setSize(name=self.pool_path(pool), body={"nodeCount": node_count}),
                                  what=what, wait_if_busy=True)
        self._wait(operation, what)
        return self.get_pool(pool)

    def set_pool_autoscaling(self, pool, enabled, min_count=None, max_count=None):
        """nodePools.setAutoscaling {autoscaling: NodePoolAutoscaling} and wait.

        minNodeCount and maxNodeCount count nodes per zone.
        https://cloud.google.com/kubernetes-engine/docs/reference/rest/v1/projects.locations.clusters.nodePools/setAutoscaling
        https://cloud.google.com/kubernetes-engine/docs/reference/rest/v1/projects.locations.clusters.nodePools#NodePoolAutoscaling
        """
        autoscaling = {"enabled": bool(enabled)}
        if enabled:
            autoscaling.update(minNodeCount=int(min_count), maxNodeCount=int(max_count))
        what = (f"Setting autoscaling of node pool '{pool}' to {min_count}-{max_count} node(s) per zone"
                if enabled else f"Turning off autoscaling of node pool '{pool}'")
        operation = self.api.call(
            self._pools.setAutoscaling(name=self.pool_path(pool), body={"autoscaling": autoscaling}), what=what,
            wait_if_busy=True)
        self._wait(operation, what)
        return self.get_pool(pool)

    def upgrade_pool(self, pool, version="-"):
        """nodePools.update {nodeVersion, imageType} and wait; '-' moves the nodes to the control plane's version.

        The request names the image type too; the pool's current one is sent back unchanged.
        https://cloud.google.com/kubernetes-engine/docs/reference/rest/v1/projects.locations.clusters.nodePools/update
        https://cloud.google.com/kubernetes-engine/docs/how-to/upgrading-a-cluster#upgrading-nodes
        """
        current = self.get_pool(pool)
        body = {"nodeVersion": version}
        image_type = (current.get("config") or {}).get("imageType")
        if image_type:
            body["imageType"] = image_type
        what = f"Upgrading node pool '{pool}' to {'the control plane version' if version == '-' else version}"
        operation = self.api.call(self._pools.update(name=self.pool_path(pool), body=body), what=what,
                                  wait_if_busy=True)
        self._wait(operation, what)
        return self.get_pool(pool)

    def delete_pool(self, pool):
        """nodePools.delete and wait. Returns False when it was already gone.
        https://cloud.google.com/kubernetes-engine/docs/reference/rest/v1/projects.locations.clusters.nodePools/delete
        """
        what = f"Deleting node pool '{pool}'"
        operation = self.api.call(self._pools.delete(name=self.pool_path(pool)), what=what, allow_404=True,
                                  wait_if_busy=True)
        if operation is None:
            return False
        self._wait(operation, what)
        return True

    def pool_sizes(self, pools):
        """{pool name: nodes across its zones} from the pools' managed instance groups.

        A node pool's initialNodeCount is only its size at creation; the
        current size is the targetSize of the managed instance group behind
        each of its zones (nodePools[].instanceGroupUrls). A pool whose groups
        cannot be read is left out.
        https://cloud.google.com/kubernetes-engine/docs/reference/rest/v1/projects.locations.clusters.nodePools#NodePool
        https://cloud.google.com/compute/docs/reference/rest/v1/instanceGroupManagers/get
        """
        sizes = {}
        for pool in pools:
            total = 0
            try:
                for url in pool.get("instanceGroupUrls") or []:
                    match = re.search(r"projects/([^/]+)/zones/([^/]+)/instanceGroup(?:Manager)?s/([^/]+)", url)
                    if not match:
                        raise GKEError(f"Unrecognised instance group URL {url}")
                    project, zone, name = match.groups()
                    group = self.api.call(
                        self.api.compute.instanceGroupManagers().get(project=project, zone=zone,
                                                                     instanceGroupManager=name),
                        what=f"Reading instance group {name}",
                    ) or {}
                    total += int(group.get("targetSize") or 0)
            except (GKEError, ValueError) as exc:
                logger.info("No size for node pool %s: %s", pool.get("name"), exc)
                continue
            sizes[pool.get("name")] = total
        return sizes

    def pool_upgrade_info(self, pool):
        """nodePools.fetchNodePoolUpgradeInfo, or None.
        https://cloud.google.com/kubernetes-engine/docs/reference/rest/v1/projects.locations.clusters.nodePools/fetchNodePoolUpgradeInfo
        """
        try:
            return self.api.call(self._pools.fetchNodePoolUpgradeInfo(name=self.pool_path(pool)),
                                 what=f"Reading upgrade information of node pool '{pool}'")
        except (GKEError, AttributeError) as exc:
            logger.info("No upgrade information for pool %s: %s", pool, exc)
            return None


# ---------------------------------------------------------------------------
# Request bodies
# ---------------------------------------------------------------------------

# Node defaults: Container-Optimized OS with containerd, balanced persistent
# disks, the cloud-platform scope (IAM decides what the node service account
# may do), Shielded VM features, and the GKE metadata server so pods use
# Workload Identity Federation for GKE.
#   https://cloud.google.com/kubernetes-engine/docs/reference/rest/v1/NodeConfig
#   https://cloud.google.com/kubernetes-engine/docs/concepts/node-images
#   https://cloud.google.com/kubernetes-engine/docs/how-to/workload-identity
DEFAULT_IMAGE_TYPE = "COS_CONTAINERD"
DEFAULT_DISK_TYPE = "pd-balanced"
DEFAULT_DISK_SIZE_GB = 100


def node_pool_body(name, machine_type, node_count, max_count=0, min_count=None, spot=False, labels=None,
                   version=None):
    """A NodePool for clusters.create or nodePools.create.

    node_count is per zone (initialNodeCount). When max_count is greater,
    the cluster autoscaler keeps the pool between min_count (default
    node_count) and max_count nodes per zone. Auto-upgrade and auto-repair are
    on: a cluster on a release channel requires auto-upgrade.
      https://cloud.google.com/kubernetes-engine/docs/reference/rest/v1/projects.locations.clusters.nodePools#NodePool
      https://cloud.google.com/kubernetes-engine/docs/reference/rest/v1/projects.locations.clusters.nodePools#NodePoolAutoscaling
      https://cloud.google.com/kubernetes-engine/docs/reference/rest/v1/NodeManagement
      https://cloud.google.com/kubernetes-engine/docs/concepts/release-channels
    """
    config = {
        "machineType": machine_type,
        "imageType": DEFAULT_IMAGE_TYPE,
        "diskType": DEFAULT_DISK_TYPE,
        "diskSizeGb": DEFAULT_DISK_SIZE_GB,
        "oauthScopes": [CLOUD_PLATFORM_SCOPE],
        "shieldedInstanceConfig": {"enableSecureBoot": True, "enableIntegrityMonitoring": True},
        "workloadMetadataConfig": {"mode": "GKE_METADATA"},
    }
    if spot:
        # https://cloud.google.com/kubernetes-engine/docs/how-to/spot-vms
        config["spot"] = True
    if labels:
        config["resourceLabels"] = labels
    body = {
        "name": name,
        "initialNodeCount": int(node_count),
        "config": config,
        "management": {"autoUpgrade": True, "autoRepair": True},
    }
    if max_count and int(max_count) > int(node_count):
        body["autoscaling"] = {
            "enabled": True,
            "minNodeCount": int(node_count if min_count is None else min_count),
            "maxNodeCount": int(max_count),
        }
    if version:
        body["version"] = version
    return body


def cluster_body(name, project, location, subnetwork, network, pool, channel="REGULAR", version="",
                 private_nodes=False, security_group="", labels=None, description="", pod_range="",
                 service_range=""):
    """The Cluster for clusters.create: a Standard cluster with GKE's recommended hardening.

      * release channel (automatic control plane and node upgrades)
      * VPC-native (alias IP) networking on the chosen subnetwork, secondary
        ranges created by GKE
      * GKE Dataplane V2, which enforces Kubernetes NetworkPolicy
      * Workload Identity Federation for GKE (PROJECT.svc.id.goog)
      * Shielded GKE nodes; no legacy ABAC; no client certificate
      * optional private nodes and Google Groups for RBAC
    https://cloud.google.com/kubernetes-engine/docs/reference/rest/v1/projects.locations.clusters#Cluster
    https://cloud.google.com/kubernetes-engine/docs/how-to/hardening-your-cluster
    https://cloud.google.com/kubernetes-engine/docs/how-to/alias-ips
    https://cloud.google.com/kubernetes-engine/docs/concepts/dataplane-v2
    https://cloud.google.com/kubernetes-engine/docs/how-to/google-groups-rbac

    ``pod_range`` and ``service_range`` name existing secondary ranges, which a
    Shared VPC subnetwork needs (pick_secondary_ranges); without them GKE
    creates the ranges.

    ``subnetwork`` and ``network`` may be URLs or relative names. The API
    takes the network and subnetwork by name; a Shared VPC subnetwork in
    another project is passed by its projects/HOST/... path, as Google's
    Shared VPC guide does.
    https://cloud.google.com/kubernetes-engine/docs/how-to/cluster-shared-vpc
    """
    subnet_project, _, subnet_name = subnetwork_parts(subnetwork)
    if subnet_project == project:
        network, subnetwork = _short(network), subnet_name
    else:
        network, subnetwork = relative_name(network), relative_name(subnetwork)
    body = {
        "name": name,
        "description": description[:2000] if description else "",
        "resourceLabels": labels or {},
        "nodePools": [pool],
        "releaseChannel": {"channel": channel},
        "network": network,
        "subnetwork": subnetwork,
        "ipAllocationPolicy": {"useIpAliases": True},
        "networkConfig": {"datapathProvider": "ADVANCED_DATAPATH"},
        "workloadIdentityConfig": {"workloadPool": f"{project}.svc.id.goog"},
        "shieldedNodes": {"enabled": True},
        "legacyAbac": {"enabled": False},
        "masterAuth": {"clientCertificateConfig": {"issueClientCertificate": False}},
    }
    if version:
        body["initialClusterVersion"] = version
    if pod_range and service_range:
        body["ipAllocationPolicy"].update(clusterSecondaryRangeName=pod_range,
                                          servicesSecondaryRangeName=service_range)
    if private_nodes:
        # networkConfig.defaultEnablePrivateNodes replaces the deprecated
        # privateClusterConfig.enablePrivateNodes; it needs VPC-native networking.
        # https://cloud.google.com/kubernetes-engine/docs/how-to/latest/network-isolation
        body["networkConfig"]["defaultEnablePrivateNodes"] = True
    if security_group:
        body["authenticatorGroupsConfig"] = {"enabled": True, "securityGroup": security_group}
    return body


def pick_secondary_ranges(ranges):
    """(pod range name, service range name) for a Shared VPC cluster, or raise GKEError.

    A cluster in a Shared VPC subnetwork must name existing secondary ranges
    (user-managed); GKE cannot create them in the host project. The Services
    range must be unused or a services range other clusters in the subnetwork
    share; the Pods range unused or a shareable Pods range. Ranges GKE created
    for another cluster cannot be used.
    https://cloud.google.com/kubernetes-engine/docs/how-to/cluster-shared-vpc
    https://cloud.google.com/kubernetes-engine/docs/how-to/alias-ips
    """
    by_status = {}
    for item in ranges:
        by_status.setdefault(item.get("status"), []).append(item.get("rangeName"))
    unused = list(by_status.get("UNUSED", []))
    service = (by_status.get("IN_USE_SERVICE") or [None])[0] or (unused.pop(0) if unused else None)
    pods = (unused.pop(0) if unused else None) or (by_status.get("IN_USE_SHAREABLE_POD") or [None])[0]
    if not service or not pods:
        raise GKEError(
            "The Shared VPC subnetwork has no free secondary ranges for Pods and Services. Ask the host "
            "project's network administrator for two secondary ranges on it (GKE uses existing ranges in a "
            "Shared VPC)."
        )
    return pods, service


def subnetwork_parts(subnetwork_url):
    """(project, region, name) from a subnetwork URL or relative name."""
    match = re.search(r"projects/([^/]+)/regions/([^/]+)/subnetworks/([^/]+)", str(subnetwork_url or ""))
    if not match:
        raise GKEError(f"'{subnetwork_url}' is not a subnetwork.")
    return match.groups()


def relative_name(url):
    """'https://www.googleapis.com/compute/v1/projects/p/...' -> 'projects/p/...'."""
    text = str(url or "")
    index = text.find("projects/")
    return text[index:] if index >= 0 else text


# ---------------------------------------------------------------------------
# Versions
# ---------------------------------------------------------------------------

def version_key(version):
    """Sort key for '1.33', '1.33.4', or '1.33.4-gke.1245000'."""
    return tuple(int(p) if p.isdigit() else 0 for p in re.split(r"[.-]|gke", str(version or "")) if p != "")


def minor(version):
    """'1.33.4-gke.1245000' -> '1.33'."""
    return ".".join(str(version or "").split(".")[:2])


def channel_versions(server_config, channel):
    """(default_version, valid_versions newest first, upgrade_target) for a release channel."""
    for item in server_config.get("channels") or []:
        if item.get("channel") == channel:
            versions = sorted(item.get("validVersions") or [], key=version_key, reverse=True)
            return item.get("defaultVersion") or "", versions, item.get("upgradeTargetVersion") or ""
    return "", [], ""


def master_upgrade_targets(server_config, cluster_json):
    """Control-plane versions newer than the cluster's, newest first.

    On a release channel GKE accepts the channel's valid versions; without
    one, validMasterVersions. Kubernetes moves one minor version at a time,
    so later minors are dropped.
    https://cloud.google.com/kubernetes-engine/docs/how-to/upgrading-a-cluster#upgrading_the_cluster
    https://kubernetes.io/releases/version-skew-policy/
    """
    current = cluster_json.get("currentMasterVersion") or ""
    channel = (cluster_json.get("releaseChannel") or {}).get("channel") or ""
    if channel and channel != "UNSPECIFIED":
        _, candidates, _ = channel_versions(server_config, channel)
    else:
        candidates = sorted(server_config.get("validMasterVersions") or [], key=version_key, reverse=True)
    current_minor = version_key(minor(current))
    targets = []
    for version in candidates:
        if version_key(version) <= version_key(current):
            continue
        version_minor = version_key(minor(version))
        if len(version_minor) == 2 and len(current_minor) == 2 and version_minor[1] > current_minor[1] + 1:
            continue
        targets.append(version)
    return targets


# ---------------------------------------------------------------------------
# CloudBolt: environments, resources, and custom fields
# ---------------------------------------------------------------------------

def resolve_group(group):
    """The group kwarg arrives as a Group or as its name depending on caller."""
    from accounts.models import Group

    if group is None or isinstance(group, Group):
        return group
    return Group.objects.filter(name=str(group)).first()


def gcp_environments_for_group(group):
    """GCP environments the group may order into, sorted by name.

    ``Group.get_available_environments()`` returns the environments entitled
    to the group (and its ancestors) plus every unconstrained environment;
    only those backed by a GCP resource handler are kept
    (docs/agents/rbac-and-security.md).
    """
    if group is None:
        return []
    available_ids = [env.id for env in group.get_available_environments()]
    return list(
        Environment.objects.filter(
            id__in=available_ids, resource_handler__resource_technology__slug="gcp"
        ).order_by("name")
    )


def handler_for_environment(env):
    """The cast GCP handler behind an Environment, or raise GKEError."""
    rh = env.resource_handler.cast() if env and env.resource_handler_id else None
    if rh is None or not hasattr(rh, "gcloud_wrapper"):
        raise GKEError(f"Environment '{env}' is not backed by a GCP resource handler.")
    return rh


def project_for_environment(env, rh=None):
    """The Google Cloud project ID of a GCP environment (its gcp_project), or raise GKEError.

    A GCP environment is project-scoped: GCPHandler.get_env_project reads
    the environment's gcp_project value.
    """
    rh = rh or handler_for_environment(env)
    project = rh.get_env_project(env)
    if project is None or not getattr(project, "gcp_id", ""):
        raise GKEError(f"Environment '{env}' has no Google Cloud project.")
    return project.gcp_id


def environment_zones(env):
    """Zones a GCP environment offers (its gcp_zone options), sorted."""
    return sorted({str(v.value) for v in env.get_cfvs_for_custom_field("gcp_zone") if v.value})


def environment_regions(env):
    """Regions of the environment's zones: 'us-central1-a' -> 'us-central1'."""
    return sorted({zone.rsplit("-", 1)[0] for zone in environment_zones(env) if "-" in zone})


def environment_machine_types(env):
    """Machine types a GCP environment offers (its gcp_node_size options), sorted."""
    return sorted({str(v.value) for v in env.get_cfvs_for_custom_field("gcp_node_size") if v.value},
                  key=str.lower)


# Node machine types offered on order forms: x86 general-purpose and
# compute-optimized families in their standard, highmem, and highcpu shapes.
# Accelerator (A2, A3, G2, TPU), memory-optimized (M, X), bare-metal, local-SSD,
# Arm (C4A, N4A, T2A, which need arm64 images), and shared-core types are left out.
# https://cloud.google.com/compute/docs/machine-resource
NODE_MACHINE_FAMILIES = ("e2", "n2", "n2d", "n4", "c3", "c3d", "c4", "c4d", "n1", "c2", "c2d")
NODE_MACHINE_SHAPES = ("standard", "highmem", "highcpu")
NODE_MACHINE_TYPE_RE = re.compile(r"^([a-z0-9]+)-(standard|highmem|highcpu)-(\d+)$")


def node_machine_types(env):
    """The environment's machine types that suit GKE nodes, by family, shape, then vCPUs."""
    found = []
    for name in environment_machine_types(env):
        match = NODE_MACHINE_TYPE_RE.match(name)
        if match and match.group(1) in NODE_MACHINE_FAMILIES:
            family, shape, vcpus = match.groups()
            found.append(((NODE_MACHINE_FAMILIES.index(family), NODE_MACHINE_SHAPES.index(shape), int(vcpus)), name))
    return [name for _, name in sorted(found)]


def is_gke_cluster(resource):
    """True for a Kubernetes Cluster resource of the Google Kubernetes Engine blueprint."""
    if resource is None or getattr(resource.resource_type, "name", "") != CLUSTER_RESOURCE_TYPE:
        return False
    return bool(resource.get_cf_values_as_dict().get(GKE_MARKER_FIELD))


def handler_for_resource(resource):
    """The GCP handler that manages a cluster resource: from env_id, else gcp_rh_id."""
    from resourcehandlers.gcp.models import GCPHandler

    values = resource.get_cf_values_as_dict()
    env_id = values.get("env_id")
    if env_id:
        try:
            env = Environment.objects.filter(id=int(str(env_id).strip())).first()
        except (TypeError, ValueError):
            env = None
        if env is not None and env.resource_handler_id:
            rh = env.resource_handler.cast()
            if hasattr(rh, "gcloud_wrapper"):
                return rh
    if values.get("gcp_rh_id"):
        rh = GCPHandler.objects.filter(id=int(str(values["gcp_rh_id"]).strip())).first()
        if rh is not None:
            return rh
    raise GKEError(f"Resource '{resource.name}' records no GCP environment or handler (env_id, gcp_rh_id).")


def cluster_for_resource(resource):
    """A ``GKECluster`` for a Kubernetes Cluster resource of this blueprint.

    The handler comes from the environment the cluster was ordered into
    (``env_id``) or, for a discovered cluster, from ``gcp_rh_id``; project,
    location, and name from the gke_* values. The handler stays server-side
    (docs/agents/rbac-and-security.md).
    """
    values = resource.get_cf_values_as_dict()
    name = values.get(GKE_MARKER_FIELD)
    project = values.get("gke_project_id")
    location = values.get("gke_location")
    if not name or not project or not location:
        raise GKEError(
            f"Resource '{resource.name}' records no GKE cluster (gke_cluster_name, gke_project_id, "
            "gke_location); run the Google Kubernetes Engine blueprint's discovery."
        )
    return GKECluster(GKEApi(handler_for_resource(resource)), project, location, name)


def console_url(project, location, name, page="details"):
    """Google Cloud console link to a cluster page (details, nodes, ...).

    The console's own URL format; Google documents only the cluster list
    (https://console.cloud.google.com/kubernetes/list).
    """
    return (f"https://console.cloud.google.com/kubernetes/clusters/details/{location}/{name}/{page}"
            f"?project={project}")


def node_pool_console_url(project, location, cluster, pool):
    return (f"https://console.cloud.google.com/kubernetes/nodepool/{location}/{cluster}/{pool}"
            f"?project={project}")


def resource_label(value):
    """A GCP resource label value: lowercase letters, digits, '_' and '-', 63 characters at most.
    https://cloud.google.com/resource-manager/docs/labels-overview#requirements
    """
    return re.sub(r"[^a-z0-9_-]+", "-", str(value or "").lower())[:63].strip("-")


# The custom fields on a Kubernetes Cluster resource of this blueprint.
# (name, label, type, description, show_as_attribute)
CLUSTER_FIELDS = [
    ("gke_cluster_name", "GKE Cluster Name", "STR", "Name of the GKE cluster.", True),
    ("gke_project_id", "Google Cloud Project", "STR", "Google Cloud project ID of the GKE cluster.", True),
    ("gke_location", "Location", "STR", "Region (regional cluster) or zone (zonal cluster) of the cluster.", True),
    ("gke_cluster_id", "GKE Cluster ID", "STR",
     "Unique ID GKE assigns to the cluster; a node pool record holds <cluster ID>/nodePools/<pool name>.", False),
    ("gke_self_link", "GKE Self Link", "STR", "Server-defined URL of the cluster.", False),
    ("gcp_rh_id", "GCP RH ID", "STR", "GCP resource handler that manages the cluster.", False),
    ("env_id", "Environment ID", "STR", "CloudBolt Environment the cluster was ordered into.", False),
    ("kubernetes_version", "Kubernetes Version", "STR", "Kubernetes version of the control plane.", True),
    ("gke_status", "Status", "STR", "Last cluster status GKE reported.", True),
    ("gke_endpoint", "API Server Address", "STR", "IP address of the cluster's Kubernetes API server.", True),
    ("gke_release_channel", "Release Channel", "STR", "Release channel that upgrades the cluster.", True),
    ("gke_network", "Network", "STR", "VPC network and subnetwork of the cluster.", True),
    ("gke_dataplane", "Dataplane", "STR", "Dataplane and network policy enforcement.", True),
    ("gke_workload_pool", "Workload Identity Pool", "STR", "Workload Identity Federation for GKE pool.", False),
    ("gke_private_nodes", "Private Nodes", "STR", "Whether nodes have only internal IP addresses.", True),
    ("container_orchestrator_id", "Container Orchestrator ID", "INT",
     "Kubernetes container orchestrator CloudBolt uses to reach the cluster.", False),
]

# The custom fields on a GKE Node Pool sub-resource.
POOL_FIELDS = [
    ("gke_pool_name", "Node Pool", "STR", "Name of the GKE node pool.", True),
    ("gke_pool_machine_type", "Machine Type", "STR", "Compute Engine machine type of the nodes.", True),
    ("gke_pool_image_type", "Image Type", "STR", "Node image type.", True),
    ("gke_pool_scale_method", "Scale Method", "STR",
     "Manual, Autoscale (limits per zone), or Autoscale (whole pool) (limits for the whole pool).", True),
    ("gke_pool_node_count", "Nodes per Zone", "INT", "Nodes in each zone of the pool (manual size).", True),
    ("gke_pool_total_nodes", "Nodes", "INT", "Nodes in the pool across all its zones.", True),
    ("gke_pool_min_count", "Autoscale Minimum", "INT",
     "Fewest nodes the autoscaler keeps, per zone unless the scale method says whole pool.", True),
    ("gke_pool_max_count", "Autoscale Maximum", "INT",
     "Most nodes the autoscaler adds, per zone unless the scale method says whole pool.", True),
    ("gke_pool_version", "Kubernetes Version", "STR", "Kubernetes version of the nodes.", True),
    ("gke_pool_zones", "Zones", "STR", "Zones the nodes run in.", True),
    ("gke_pool_status", "Status", "STR", "Last node pool status GKE reported.", True),
    ("gke_pool_spot", "Spot VMs", "STR", "Whether the nodes are Spot VMs.", True),
    ("gke_pool_auto_upgrade", "Auto-upgrade", "STR", "Whether GKE upgrades the nodes automatically.", True),
]


def _ensure_fields(definitions):
    """Create each custom field, and keep label, description, and visibility in sync."""
    for name, label, cf_type, description, show_as_attribute in definitions:
        cf, created = CustomField.objects.get_or_create(
            name=name,
            defaults=dict(label=label, description=description, type=cf_type,
                          show_on_servers=False, show_as_attribute=show_as_attribute),
        )
        if created or not name.startswith("gke_"):
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
    """The GKE Node Pool resource type, created on first use."""
    rt, _ = ResourceType.objects.get_or_create(
        name=NODE_POOL_RESOURCE_TYPE,
        defaults={"label": "GKE Node Pool", "plural_label": "GKE Node Pools", "icon": "fas fa-server"},
    )
    return rt


def _short(url):
    """The last path segment of a Google resource URL or relative name."""
    return str(url or "").rstrip("/").rsplit("/", 1)[-1]


def describe_network(cluster_json):
    """'VPC default / subnet default (VPC-native)' from a cluster."""
    network = cluster_json.get("network") or _short((cluster_json.get("networkConfig") or {}).get("network"))
    subnet = cluster_json.get("subnetwork") or _short((cluster_json.get("networkConfig") or {}).get("subnetwork"))
    text = f"VPC {network or 'default'}"
    if subnet:
        text += f" / subnet {subnet}"
    if (cluster_json.get("ipAllocationPolicy") or {}).get("useIpAliases"):
        text += " (VPC-native)"
    return text


def describe_dataplane(cluster_json):
    """'Dataplane V2 (network policy built in)' or the legacy dataplane and its policy addon."""
    provider = (cluster_json.get("networkConfig") or {}).get("datapathProvider") or ""
    if provider == "ADVANCED_DATAPATH":
        return "Dataplane V2 (enforces network policy)"
    policy = cluster_json.get("networkPolicy") or {}
    if policy.get("enabled"):
        return f"Legacy dataplane with {str(policy.get('provider') or 'Calico').capitalize()} network policy"
    return "Legacy dataplane, network policy not enforced"


def control_plane_endpoints(cluster_json):
    """(public IP, private IP, DNS endpoint) of the control plane; '' where GKE reports none.

    controlPlaneEndpointsConfig replaces the deprecated
    privateClusterConfig.publicEndpoint/privateEndpoint, which are read as a fallback.
    https://cloud.google.com/kubernetes-engine/docs/reference/rest/v1/projects.locations.clusters#Cluster
    """
    config = cluster_json.get("controlPlaneEndpointsConfig") or {}
    ip = config.get("ipEndpointsConfig") or {}
    legacy = cluster_json.get("privateClusterConfig") or {}
    return (ip.get("publicEndpoint") or legacy.get("publicEndpoint") or "",
            ip.get("privateEndpoint") or legacy.get("privateEndpoint") or "",
            (config.get("dnsEndpointConfig") or {}).get("endpoint") or "")


def private_nodes(cluster_json):
    """True when the cluster's nodes have only internal IP addresses."""
    network_config = cluster_json.get("networkConfig") or {}
    if network_config.get("defaultEnablePrivateNodes") is not None:
        return bool(network_config.get("defaultEnablePrivateNodes"))
    return bool((cluster_json.get("privateClusterConfig") or {}).get("enablePrivateNodes"))


def cluster_values(cluster_json, project):
    """The CLUSTER_FIELDS values for a cluster as clusters.get returns it.
    https://cloud.google.com/kubernetes-engine/docs/reference/rest/v1/projects.locations.clusters#Cluster
    """
    channel = (cluster_json.get("releaseChannel") or {}).get("channel") or "UNSPECIFIED"
    return {
        "gke_cluster_name": cluster_json.get("name"),
        "gke_project_id": project,
        "gke_location": cluster_json.get("location"),
        "gke_cluster_id": cluster_json.get("id"),
        "gke_self_link": cluster_json.get("selfLink"),
        "kubernetes_version": cluster_json.get("currentMasterVersion"),
        "gke_status": cluster_json.get("status"),
        "gke_endpoint": cluster_json.get("endpoint"),
        "gke_release_channel": RELEASE_CHANNEL_LABELS.get(channel, channel),
        "gke_network": describe_network(cluster_json),
        "gke_dataplane": describe_dataplane(cluster_json),
        "gke_workload_pool": (cluster_json.get("workloadIdentityConfig") or {}).get("workloadPool"),
        "gke_private_nodes": "Yes" if private_nodes(cluster_json) else "No",
    }


def stamp_cluster(resource, cluster_json, project, rh=None, env=None):
    """Write the cluster's values onto its CloudBolt resource (and save it)."""
    ensure_cluster_fields()
    for key, value in cluster_values(cluster_json, project).items():
        if value not in (None, ""):
            resource.set_value_for_custom_field(key, value)
    if rh is not None:
        resource.set_value_for_custom_field("gcp_rh_id", str(rh.id))
    if env is not None:
        resource.set_value_for_custom_field("env_id", str(env.id))
    resource.save()


def pool_zones(pool_json, cluster_json=None):
    """The zones a node pool runs in (its locations, else the cluster's)."""
    zones = pool_json.get("locations") or (cluster_json or {}).get("locations") or []
    return [str(z) for z in zones]


def pool_values(pool_json, cluster_json=None, total_nodes=None):
    """The POOL_FIELDS values for a node pool as nodePools.get returns it.

    ``total_nodes`` is the pool's current size (GKECluster.pool_sizes); without
    it the manual size falls back to initialNodeCount, the size at creation.
    https://cloud.google.com/kubernetes-engine/docs/reference/rest/v1/projects.locations.clusters.nodePools#NodePool
    """
    config = pool_json.get("config") or {}
    autoscaling = pool_json.get("autoscaling") or {}
    autoscale = bool(autoscaling.get("enabled"))
    total_limits = autoscale_uses_totals(autoscaling)
    zones = pool_zones(pool_json, cluster_json)
    per_zone = pool_json.get("initialNodeCount")
    if total_nodes is not None and zones:
        per_zone = int(round(total_nodes / len(zones)))
    values = {
        "gke_pool_name": pool_json.get("name"),
        "gke_pool_machine_type": config.get("machineType"),
        "gke_pool_image_type": config.get("imageType"),
        "gke_pool_scale_method": (("Autoscale (whole pool)" if total_limits else "Autoscale")
                                  if autoscale else "Manual"),
        "gke_pool_node_count": per_zone if not autoscale else None,
        "gke_pool_total_nodes": total_nodes,
        "gke_pool_min_count": (autoscaling.get("totalMinNodeCount", 0) if total_limits
                               else autoscaling.get("minNodeCount", 0)) if autoscale else None,
        "gke_pool_max_count": (autoscaling.get("totalMaxNodeCount") if total_limits
                               else autoscaling.get("maxNodeCount")) if autoscale else None,
        "gke_pool_version": pool_json.get("version"),
        "gke_pool_zones": ", ".join(zones) or "None",
        "gke_pool_status": pool_json.get("status"),
        "gke_pool_spot": "Yes" if (config.get("spot") or config.get("preemptible")) else "No",
        "gke_pool_auto_upgrade": "Yes" if (pool_json.get("management") or {}).get("autoUpgrade") else "No",
    }
    if (cluster_json or {}).get("id"):
        values["gke_cluster_id"] = pool_identifier(cluster_json["id"], pool_json.get("name"))
    return values


def pool_identifier(cluster_id, pool_name):
    """The identifier a node pool record carries under gke_cluster_id, which discovery matches on.

    GKE gives node pools no ID of their own; the cluster ID and the pool name
    are unique together, and a rebuilt cluster gets a new cluster ID.
    """
    return f"{cluster_id}/nodePools/{pool_name}"


def pool_label(pool_json, cluster_json=None, total_nodes=None):
    """'default-pool (e2-standard-4, autoscale 1-3 per zone, 1.33.4-gke.1245000, RUNNING)' for dropdowns."""
    values = pool_values(pool_json, cluster_json, total_nodes)
    method = values["gke_pool_scale_method"]
    if method == "Autoscale":
        scale = f"autoscale {values['gke_pool_min_count']}-{values['gke_pool_max_count']} per zone"
    elif method.startswith("Autoscale"):
        scale = f"autoscale {values['gke_pool_min_count']}-{values['gke_pool_max_count']} in total"
    else:
        scale = f"{values['gke_pool_node_count']} per zone"
    return (f"{values['gke_pool_name']} ({values['gke_pool_machine_type']}, {scale}, "
            f"{values['gke_pool_version']}, {values['gke_pool_status']})")


def autoscale_uses_totals(autoscaling):
    """True when a pool's autoscaling limits count the whole pool (totalMin/MaxNodeCount), not each zone.
    https://cloud.google.com/kubernetes-engine/docs/reference/rest/v1/projects.locations.clusters.nodePools#NodePoolAutoscaling
    """
    autoscaling = autoscaling or {}
    return autoscaling.get("totalMinNodeCount") is not None or autoscaling.get("totalMaxNodeCount") is not None


def stamp_pool(sub_resource, pool_json, cluster_json=None, total_nodes=None):
    """Write the pool's values onto its sub-resource; a value GKE no longer reports is cleared."""
    for key, value in pool_values(pool_json, cluster_json, total_nodes).items():
        sub_resource.set_value_for_custom_field(key, None if value in (None, "") else value, ignore_none_value=True)
    sub_resource.save()


def pool_sub_resources(resource):
    return Resource.objects.filter(
        parent_resource=resource, resource_type__name=NODE_POOL_RESOURCE_TYPE, lifecycle="ACTIVE"
    )


def upsert_pool_sub_resource(resource, pool_json, cluster_json=None, total_nodes=None):
    """Create or refresh the GKE Node Pool sub-resource for one pool. Returns (sub, created)."""
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
    stamp_pool(sub, pool_json, cluster_json, total_nodes)
    return sub, created


def remove_pool_sub_resource(resource, pool_name):
    """Delete the sub-resource of a pool that no longer exists. Returns how many were removed."""
    # Count first: delete() also counts the rows it cascades to.
    stale = pool_sub_resources(resource).filter(name=pool_name)
    removed = stale.count()
    stale.delete()
    return removed


def sync_pool_sub_resources(resource, pools, cluster_json=None, sizes=None):
    """Make the GKE Node Pool sub-resources match ``pools``. Returns (created, updated, removed).

    ``sizes`` is GKECluster.pool_sizes(pools), the current node totals.
    """
    sizes = sizes or {}
    names = {p.get("name") for p in pools}
    stale = pool_sub_resources(resource).exclude(name__in=names)
    removed = stale.count()
    stale.delete()
    created = updated = 0
    for pool in pools:
        _, was_created = upsert_pool_sub_resource(resource, pool, cluster_json, sizes.get(pool.get("name")))
        created += int(was_created)
        updated += int(not was_created)
    return created, updated, removed


# ---------------------------------------------------------------------------
# Kubernetes API, verified against the cluster's own CA
# ---------------------------------------------------------------------------

def _pem(ca_data):
    """PEM text from base64 PEM (as GKE and kubeconfigs carry it) or PEM text."""
    text = str(ca_data or "").strip()
    if not text:
        return ""
    if "BEGIN CERTIFICATE" in text:
        return text + "\n"
    try:
        return base64.b64decode(text).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        raise GKEError("The cluster CA certificate is neither PEM nor base64-encoded PEM.")


class KubeClient:
    """A minimal Kubernetes API client for one GKE cluster.

    TLS is verified against the cluster's CA certificate, which GKE reports
    as masterAuth.clusterCaCertificate (base64 PEM), so the appliance trust
    store does not need the cluster's private CA. The bearer token is either
    the GCP handler's Google access token (bootstrap) or the cloudbolt-admin
    ServiceAccount token the orchestrator holds.
    https://cloud.google.com/kubernetes-engine/docs/how-to/api-server-authentication
    https://kubernetes.io/docs/reference/using-api/api-concepts/
    """

    def __init__(self, server, ca_data, token):
        if not server:
            raise GKEError("The cluster has no API server address yet.")
        self.server = server if server.startswith("http") else f"https://{server}"
        self.server = self.server.rstrip("/")
        self._token = token
        self.ca_data = ca_data
        pem = _pem(ca_data)
        self._ca_path = None
        if pem:
            handle, self._ca_path = tempfile.mkstemp(suffix=".pem", prefix="gke-ca-")
            with os.fdopen(handle, "w") as stream:
                stream.write(pem)

    def __del__(self):
        self.close()

    def close(self):
        if getattr(self, "_ca_path", None) and os.path.exists(self._ca_path):
            try:
                os.remove(self._ca_path)
            except OSError:
                pass
        self._ca_path = None

    @classmethod
    def for_cluster(cls, cluster_json, token):
        """A client for a cluster as clusters.get returns it, with ``token``."""
        return cls(cluster_json.get("endpoint", ""),
                   (cluster_json.get("masterAuth") or {}).get("clusterCaCertificate", ""), token)

    @classmethod
    def from_orchestrator(cls, orchestrator):
        """A client with the credentials the Kubernetes container orchestrator holds."""
        port = int(orchestrator.port or 443)
        server = f"{orchestrator.protocol or 'https'}://{orchestrator.ip}" + ("" if port == 443 else f":{port}")
        token = getattr(orchestrator, "servicepasswd", "") or ""
        if not token:
            raise GKEError(f"Container orchestrator '{orchestrator.name}' holds no bearer token.")
        return cls(server, getattr(orchestrator, "ca_file_contents", "") or "", token)

    def request(self, method, path, json_body=None, params=None, allow_404=False, ok=(200, 201, 202),
                content_type=None):
        """One Kubernetes API call; returns the parsed JSON (or None on 404 when ``allow_404``)."""
        from common.methods import get_proxies

        url = f"{self.server}/{path.lstrip('/')}"
        headers = {"Authorization": f"Bearer {self._token}", "Accept": "application/json"}
        if content_type:
            headers["Content-Type"] = content_type
        try:
            proxies = get_proxies(url)
        except Exception:  # noqa: BLE001 - a proxy misconfiguration should not hide the real call
            proxies = {}
        try:
            data = None
            if json_body is not None and content_type:
                data = json.dumps(json_body)
                json_body = None
            resp = requests.request(
                method, url, headers=headers, json=json_body, data=data, params=params,
                timeout=REQUEST_TIMEOUT, verify=self._ca_path or True, proxies=proxies,
            )
        except requests.exceptions.SSLError as exc:
            raise GKEError(f"TLS verification of {self.server} against the cluster CA failed: {exc}")
        except requests.exceptions.RequestException as exc:
            raise GKEError(f"Could not reach the Kubernetes API at {self.server}: {exc}")
        if resp.status_code == 404 and allow_404:
            return None
        if resp.status_code not in ok:
            message = ""
            try:
                message = (resp.json() or {}).get("message", "")
            except ValueError:
                pass
            raise GKEError(f"{method} {path.split('?')[0]} returned HTTP {resp.status_code}: "
                           f"{message or (resp.text or '')[:300]}", resp.status_code)
        if not resp.content:
            return {}
        try:
            return resp.json()
        except ValueError:
            return {"text": resp.text}

    def get(self, path, params=None, allow_404=False):
        return self.request("GET", path, params=params, allow_404=allow_404)

    def apply(self, collection_path, name, body):
        """Create ``body``, or replace the existing object (a PUT needs its resourceVersion)."""
        existing = self.request("GET", f"{collection_path}/{name}", allow_404=True)
        if existing is None:
            return self.request("POST", collection_path, json_body=body)
        body = json.loads(json.dumps(body))
        body.setdefault("metadata", {})["resourceVersion"] = (existing.get("metadata") or {}).get("resourceVersion")
        return self.request("PUT", f"{collection_path}/{name}", json_body=body)

    def merge_patch(self, path, patch):
        """application/merge-patch+json: maps merge key by key; a null value removes a key.
        https://kubernetes.io/docs/tasks/manage-kubernetes-objects/update-api-object-kubectl-patch/
        """
        return self.request("PATCH", path, json_body=patch, content_type="application/merge-patch+json")

    def delete(self, path):
        """Delete an object. Returns False when it was already gone."""
        return self.request("DELETE", path, allow_404=True, ok=(200, 202)) is not None


# ---------------------------------------------------------------------------
# Kubernetes container orchestrator and per-cluster Environment
# ---------------------------------------------------------------------------

def _labels():
    return {MANAGED_BY_LABEL: MANAGED_BY_VALUE}


def bootstrap_admin_token(kube, timeout=60):
    """Create (or reuse) the cloudbolt-admin ServiceAccount, its cluster-admin binding, and token.

    The orchestrator needs a credential that does not expire: a Secret of
    type kubernetes.io/service-account-token annotated with the
    ServiceAccount's name, which the token controller fills with a token.
      https://kubernetes.io/docs/concepts/configuration/secret/#serviceaccount-token-secrets
      https://kubernetes.io/docs/tasks/configure-pod-container/configure-service-account/#manually-create-an-api-token-for-a-serviceaccount
      https://kubernetes.io/docs/reference/access-authn-authz/rbac/#user-facing-roles (cluster-admin)
    ``kube`` must authenticate as an identity allowed to create a
    cluster-admin ClusterRoleBinding (roles/container.admin).

    The Secret is never listed in the ServiceAccount's secrets, so the
    legacy-token cleaner does not treat it as auto-generated. Completing a
    GKE credential rotation (which GKE also starts on its own before the
    cluster CA expires) invalidates the token: a token the API server
    refuses is replaced by deleting and re-creating the Secret.
      https://kubernetes.io/docs/reference/access-authn-authz/service-accounts-admin/
      https://cloud.google.com/kubernetes-engine/docs/how-to/credential-rotation
    Returns the token text.
    """
    token = _issue_admin_token(kube, timeout)
    probe = KubeClient(kube.server, kube.ca_data, token)
    try:
        probe.get("/api")
        return token
    except GKEError as exc:
        if exc.status_code != 401:
            raise
    finally:
        probe.close()
    kube.delete(f"/api/v1/namespaces/{ADMIN_NAMESPACE}/secrets/{ADMIN_TOKEN_SECRET}")
    return _issue_admin_token(kube, timeout)


def _issue_admin_token(kube, timeout):
    ns = ADMIN_NAMESPACE
    kube.apply(f"/api/v1/namespaces/{ns}/serviceaccounts", ADMIN_SERVICE_ACCOUNT, {
        "apiVersion": "v1", "kind": "ServiceAccount",
        "metadata": {"name": ADMIN_SERVICE_ACCOUNT, "namespace": ns, "labels": _labels()},
    })
    kube.apply("/apis/rbac.authorization.k8s.io/v1/clusterrolebindings", ADMIN_BINDING, {
        "apiVersion": "rbac.authorization.k8s.io/v1", "kind": "ClusterRoleBinding",
        "metadata": {"name": ADMIN_BINDING, "labels": _labels()},
        "roleRef": {"apiGroup": "rbac.authorization.k8s.io", "kind": "ClusterRole", "name": "cluster-admin"},
        "subjects": [{"kind": "ServiceAccount", "name": ADMIN_SERVICE_ACCOUNT, "namespace": ns}],
    })
    secret_path = f"/api/v1/namespaces/{ns}/secrets"
    if kube.get(f"{secret_path}/{ADMIN_TOKEN_SECRET}", allow_404=True) is None:
        kube.request("POST", secret_path, json_body={
            "apiVersion": "v1", "kind": "Secret", "type": "kubernetes.io/service-account-token",
            "metadata": {"name": ADMIN_TOKEN_SECRET, "namespace": ns, "labels": _labels(),
                         "annotations": {"kubernetes.io/service-account.name": ADMIN_SERVICE_ACCOUNT}},
        })
    deadline = time.time() + timeout
    while True:
        secret = kube.get(f"{secret_path}/{ADMIN_TOKEN_SECRET}") or {}
        token = (secret.get("data") or {}).get("token")
        if token:
            return base64.b64decode(token).decode("utf-8")
        if time.time() >= deadline:
            raise GKEError(f"Kubernetes did not issue a token for {ns}/{ADMIN_TOKEN_SECRET} "
                           f"within {timeout} seconds.")
        time.sleep(3)


def parse_rbac_principals(text):
    """RBAC subjects from 'user:alice@example.com', 'group:team@example.com', or a bare email.

    A Google user or Google service account is a User subject named by its
    email; a Google Group (Google Groups for RBAC) is a Group subject.
      https://cloud.google.com/kubernetes-engine/docs/how-to/role-based-access-control#rolebinding
      https://cloud.google.com/kubernetes-engine/docs/how-to/google-groups-rbac
    Returns (subjects, bad_tokens).
    """
    subjects, bad = [], []
    for token in re.split(r"[\s,;]+", str(text or "").strip()):
        if not token:
            continue
        kind, value = "user", token
        if ":" in token:
            kind, value = token.split(":", 1)
            kind = kind.lower()
        if kind not in ("user", "serviceaccount", "group") or not EMAIL_RE.match(value):
            bad.append(token)
            continue
        subjects.append({"kind": "Group" if kind == "group" else "User", "name": value,
                         "apiGroup": "rbac.authorization.k8s.io"})
    return subjects, bad


def apply_cluster_admins(kube, subjects):
    """Give ``subjects`` cluster-admin through the cloudbolt-cluster-admins ClusterRoleBinding."""
    if not subjects:
        return None
    return kube.apply("/apis/rbac.authorization.k8s.io/v1/clusterrolebindings", CLUSTER_ADMINS_BINDING, {
        "apiVersion": "rbac.authorization.k8s.io/v1", "kind": "ClusterRoleBinding",
        "metadata": {"name": CLUSTER_ADMINS_BINDING, "labels": _labels()},
        "roleRef": {"apiGroup": "rbac.authorization.k8s.io", "kind": "ClusterRole", "name": "cluster-admin"},
        "subjects": subjects,
    })


def cluster_environment_name(cluster_name, location):
    return f"Resource-{cluster_name} Environment#{location}"


def _cluster_environment(orchestrator):
    """The per-cluster Environment on the orchestrator (not a namespace's), or None.

    Filter on the foreign key itself: Environment's custom-field attribute
    manager turns ``container_orchestrator_id=`` into a custom-field lookup
    that matches nothing.
    """
    return (
        Environment.objects.filter(container_orchestrator=orchestrator, resource_handler__isnull=True,
                                   name__startswith="Resource-")
        .exclude(custom_field_options__field__name=NAMESPACE_ENV_FIELD)
        .order_by("id")
        .first()
    )


def register_orchestrator(resource, cluster, cluster_json, bootstrap=None):
    """Create (or refresh) the cluster's Kubernetes orchestrator and per-cluster Environment.

    The orchestrator authenticates with the cloudbolt-admin ServiceAccount
    token (TOKEN auth) and carries the cluster CA. Google prefers
    short-lived IAM credentials for automation, but CloudBolt's Kubernetes
    orchestrator only refreshes tokens for EKS and otherwise holds a static
    token or certificate, and GKE no longer issues client certificates by
    default; discovery and List GKE Node Pools call this again, which
    replaces a token a credential rotation invalidated.
    https://cloud.google.com/kubernetes-engine/docs/how-to/hardening-your-cluster
    ``bootstrap`` is a
    KubeClient that may create that ServiceAccount (the handler's Google
    token); one is built from ``cluster`` when omitted. Returns
    (orchestrator, environment, created).
    """
    current_id = resource.get_cf_values_as_dict().get(CLUSTER_ORCHESTRATOR_FIELD)
    orchestrator, env, created = ensure_orchestrator(cluster.api, cluster_json, cluster.project,
                                                     current_id=current_id, bootstrap=bootstrap)
    resource.set_value_for_custom_field(CLUSTER_ORCHESTRATOR_FIELD, orchestrator.id)
    resource.save()
    return orchestrator, env, created


def ensure_orchestrator(api, cluster_json, project, current_id=None, bootstrap=None):
    """Create or refresh the orchestrator and per-cluster Environment without touching a resource.

    Reuses the orchestrator ``current_id`` names, else one registered for the
    cluster's endpoint. Discovery calls this directly, since the sync creates
    the resource after the plugin returns. Returns (orchestrator, environment, created).
    """
    from containerorchestrators.kuberneteshandler.models import Kubernetes
    from containerorchestrators.models import ContainerOrchestratorTechnology

    name = cluster_json.get("name", "")
    location = cluster_json.get("location", "")
    # The IP endpoint: GKE's DNS endpoint refuses ServiceAccount tokens unless
    # enableK8sTokensViaDns is set.
    # https://cloud.google.com/kubernetes-engine/docs/how-to/latest/network-isolation
    endpoint = cluster_json.get("endpoint") or ""
    ca = (cluster_json.get("masterAuth") or {}).get("clusterCaCertificate", "")
    if not endpoint:
        raise GKEError(f"GKE reports no IP endpoint for the control plane of '{name}'.")
    kube = bootstrap or KubeClient.for_cluster(cluster_json, api.access_token())
    try:
        token = bootstrap_admin_token(kube)
    finally:
        if bootstrap is None:
            kube.close()
    fields = {"ip": endpoint, "port": 443, "protocol": "https", "auth_type": "TOKEN",
              "serviceaccount": ADMIN_SERVICE_ACCOUNT, "servicepasswd": token, "ca_file_contents": ca}

    orchestrator = None
    if current_id not in (None, ""):
        try:
            orchestrator = Kubernetes.objects.filter(id=int(str(current_id).strip())).first()
        except (TypeError, ValueError):
            orchestrator = None
    if orchestrator is None:
        orchestrator = Kubernetes.objects.filter(ip=endpoint).first()
    created = orchestrator is None
    if created:
        tech = ContainerOrchestratorTechnology.objects.get(name="Kubernetes")
        orchestrator = Kubernetes.objects.create(name=name, container_technology=tech, **fields)
    else:
        for key, value in fields.items():
            setattr(orchestrator, key, value)
        orchestrator.save()

    env = _cluster_environment(orchestrator)
    if env is None:
        env = Environment.objects.create(
            name=cluster_environment_name(name, location),
            container_orchestrator=orchestrator,
            description=f"GKE cluster {name} ({project}, {location}).",
        )
    return orchestrator, env, created


def delete_orchestrator(resource):
    """Remove the cluster's orchestrator and the Environment(s) on it without a handler.

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
    environments = Environment.objects.filter(container_orchestrator=orchestrator, resource_handler__isnull=True)
    removed = environments.count()
    environments.delete()
    name = orchestrator.name
    orchestrator.delete()
    return f"Removed container orchestrator {name} and {removed} environment(s)."


def orchestrator_for_resource(resource):
    """The Kubernetes orchestrator recorded on a cluster resource, or None."""
    from containerorchestrators.kuberneteshandler.models import Kubernetes

    value = resource.get_cf_values_as_dict().get(CLUSTER_ORCHESTRATOR_FIELD)
    try:
        return Kubernetes.objects.filter(id=int(str(value).strip())).first() if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def kube_for_resource(resource):
    """A KubeClient for a cluster resource, with its orchestrator's credentials; raise GKEError."""
    orchestrator = orchestrator_for_resource(resource)
    if orchestrator is None:
        raise GKEError(
            f"Cluster '{resource.name}' has no container orchestrator; run the Google Kubernetes Engine "
            "blueprint's discovery to register it."
        )
    return KubeClient.from_orchestrator(orchestrator)


# ---------------------------------------------------------------------------
# CloudBolt groups entitled to the per-cluster Environment
# ---------------------------------------------------------------------------
# A group may order GKE namespaces on a cluster when the cluster's Environment
# is in Group.get_available_environments(): entitled to the group or an
# ancestor, or unconstrained (no groups at all). With the global preference
# that restricts new environments on, CloudBolt puts each new Environment in
# the Unassigned group, a placeholder that entitles no real group. Changes
# follow CloudBolt's own Edit groups dialog for an environment: the group set
# changes, descendant groups inherit (or lose) the environment, and the
# environment's history records the change. Same rules as shared_modules/azure_aks.

GROUP_ID_RE = re.compile(r"GRP-[A-Za-z0-9]+")

UNCONSTRAINED = "unconstrained"
RESTRICTED = "restricted"
ENTITLED = "entitled"


def cluster_environment(resource):
    """The per-cluster Environment of a cluster resource, or raise GKEError."""
    orchestrator = orchestrator_for_resource(resource)
    env = _cluster_environment(orchestrator) if orchestrator is not None else None
    if env is None:
        raise GKEError(
            f"Cluster '{resource.name}' has no CloudBolt environment: CloudBolt never registered it as a "
            "container orchestrator. Run the Google Kubernetes Engine blueprint's discovery."
        )
    return env


def assignable_groups():
    """Every group a cluster may be entitled to (all but the Unassigned placeholders), by name."""
    from accounts.models import Group

    return list(Group.without_unassigned.order_by("name"))


def group_ids_in(text):
    """Group global IDs in a rendered input, in order, without repeats.

    A multi-select input renders as a Python list ("['GRP-...', ...]"); the
    IDs are matched, never evaluated.
    """
    return list(dict.fromkeys(GROUP_ID_RE.findall(str(text or ""))))


def groups_for_ids(group_ids):
    """(groups, unknown): the assignable groups with these global IDs, and the IDs that match none."""
    from accounts.models import Group

    found = {g.global_id: g for g in Group.without_unassigned.filter(global_id__in=group_ids)}
    return [found[i] for i in group_ids if i in found], [i for i in group_ids if i not in found]


def environment_groups(env):
    """(state, groups) for an Environment: UNCONSTRAINED when it has no groups,
    RESTRICTED when it has only Unassigned placeholders, else ENTITLED with
    its real groups by name."""
    groups = list(env.group_set.order_by("name"))
    real = [g for g in groups if not g.is_unassigned]
    if real:
        return ENTITLED, real
    return (RESTRICTED if groups else UNCONSTRAINED), []


def _grant(env, group):
    env.group_set.add(group)
    try:
        group.add_env_to_descendants(env)
    except Exception as exc:
        logger.warning("Could not propagate env %s to descendants of %s: %s", env, group, exc)


def _revoke(env, group):
    env.group_set.remove(group)
    try:
        group.remove_env_from_descendants(env)
    except Exception as exc:
        logger.warning("Could not remove env %s from descendants of %s: %s", env, group, exc)


def _escaped(names):
    """Names for a history message, HTML-escaped as CloudBolt's own Edit groups dialog does."""
    from django.utils.html import escape

    return ", ".join(escape(name) for name in names)


def _record(env, message, profile=None, job=None):
    """Add an environment history event; message holds names already escaped."""
    try:
        env.add_event("INFO", message, profile=profile, job=job)
    except Exception as exc:
        logger.warning("Could not record a history event on environment %s: %s", env, exc)


def add_environment_groups(env, groups, profile=None, job=None):
    """Entitle groups to the Environment, dropping any Unassigned placeholder.
    Returns the names of the groups added."""
    current = set(env.group_set.all())
    added = [g for g in groups if g not in current]
    if not added:
        return []
    for group in current:
        if group.is_unassigned:
            _revoke(env, group)
    for group in added:
        _grant(env, group)
    names = [g.name for g in added]
    _record(env, f"Entitled group(s) {_escaped(names)} to environment '{_escaped([env.name])}'.", profile, job)
    return names


def remove_environment_groups(env, groups, profile=None, job=None):
    """Remove groups from the Environment. When no real group is left, the
    Environment becomes unconstrained (any Unassigned placeholder goes too).
    Returns the names of the groups removed."""
    current = set(env.group_set.all())
    removed = [g for g in groups if g in current]
    if not removed:
        return []
    for group in removed:
        _revoke(env, group)
    if environment_groups(env)[0] == RESTRICTED:
        for group in list(env.group_set.all()):
            _revoke(env, group)
    names = [g.name for g in removed]
    message = f"Removed group(s) {_escaped(names)} from environment '{_escaped([env.name])}'."
    if environment_groups(env)[0] == UNCONSTRAINED:
        message += " No groups remain, so every group may use it."
    _record(env, message, profile, job)
    return names


def allow_all_groups(env, profile=None, job=None):
    """Remove every group, Unassigned included, so every group may use the
    Environment. Returns the names of the groups removed."""
    groups = list(env.group_set.order_by("name"))
    for group in groups:
        _revoke(env, group)
    names = [g.name for g in groups]
    if names:
        _record(env, f"Removed every group ({_escaped(names)}) from environment '{_escaped([env.name])}'; "
                     "every group may use it.", profile, job)
    return names


def active_namespaces(resource):
    """Namespace landing zones hosted on the cluster (sub-resources), by name."""
    return Resource.objects.filter(
        parent_resource=resource, resource_type__name=NAMESPACE_RESOURCE_TYPE, lifecycle="ACTIVE"
    ).order_by("name")


# ---------------------------------------------------------------------------
# Small utilities
# ---------------------------------------------------------------------------

def text_input(value):
    """A rendered input as plain text; an input left empty renders as 'None'."""
    text = str(value or "").strip()
    return "" if text == "None" else text


def int_input(value, default):
    try:
        return int(text_input(value))
    except (TypeError, ValueError):
        return default


def truthy(value):
    """Interpret a rendered BOOL plugin input."""
    return text_input(value).lower() in ("true", "1", "yes", "on")


def now_utc():
    """An aware UTC timestamp (CloudBolt's own clock is naive local time)."""
    return datetime.datetime.now(datetime.timezone.utc)
