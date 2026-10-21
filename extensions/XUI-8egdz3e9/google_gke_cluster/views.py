"""
Google GKE Cluster XUI

Adds four tabs to the Kubernetes Cluster resources of the Google Kubernetes
Engine blueprint (BP-g8g8aecx) -- any resource carrying a gke_cluster_name
custom field. The AWS EKS and Azure AKS extensions gate on eks_cluster_name
and aks_cluster_name, so they never show on the same resource.

- Cluster: the status, mode, location type, release channel, and version
  support in the header; the console's Cluster basics (location, node zones,
  release channel, versions, total size, created, endpoints); Networking and
  Security cards; an Upgrades card with the control-plane versions GKE offers
  and an Upgrade button (Upgrade GKE Version); the cluster's features and
  add-ons; the jobs running on the resource; and Open in Google Cloud console.
- Node pools: every node pool with status, scale method and autoscale range
  per zone, the Kubernetes nodes it runs, machine and image type, version,
  zones, Spot, and auto-upgrade and auto-repair, with Scale, Upgrade, and
  Delete buttons, plus the Kubernetes nodes with their status, pool, version,
  zone, and address. Add node pool and Refresh (List GKE Node Pools) in the
  toolbar.
- Workloads: nodes with readiness, pressure conditions, CPU and memory
  requests against allocatable capacity and pod counts, plus namespaces with
  pod, deployment and service counts, each namespace landing zone linked to
  its CloudBolt resource, and a Create namespace button (Create GKE Namespace).
- Groups: the CloudBolt groups entitled to the cluster's environment, which
  may order namespaces on the cluster, shown as CloudBolt's own environment
  Groups tab does (no groups is Unconstrained; only the Unassigned
  placeholder is Unassigned), with Add groups, Remove, and Allow all groups
  (Manage GKE Cluster Groups).

Namespace landing zones of the GKE Namespace blueprint (kubernetes_namespace
resources carrying gke_namespace_name) get two tabs, built by the
gke_namespace shared module (SHM-kephq8iq). Each landing zone is a plain
Kubernetes namespace with a quota, default limits, NetworkPolicies, and
RoleBindings CloudBolt manages.

- Connect: where the namespace lives, the Google users, groups, and service
  accounts a RoleBinding in it names (and the cluster admins), the GitHub
  Actions pipeline identities with a workflow each, the gcloud and kubectl
  commands, and a kubeconfig that holds no secret; Manage access (Manage GKE
  Namespace Access) and Pipeline identity (Manage GKE Namespace Pipeline
  Identity) buttons.
- Workloads: a read-only inventory of the namespace: usage of CloudBolt's
  quota (and any other quota object in it), Deployments, StatefulSets and
  DaemonSets, pods, services and ingresses, persistent volume claims, and
  recent warning events; Request quota change (Request GKE Namespace Quota
  Change). No logs, Secrets or ConfigMaps, and no buttons that change the
  cluster.

Every button starts one of the blueprints' resource actions as a CloudBolt
job, so approvals, permissions and job history apply. Buttons appear only
when the action is synced, applies to the resource's blueprint and the
viewer may run it (resource_action_permitted), and the job runs as the viewer.

Google Cloud is read through the gcp_gke shared module (SHM-4yw92b8x) as the
GCP handler of the environment the cluster was ordered into (env_id), or of
the handler a discovered cluster records (gcp_rh_id); the handler is never
shown. Kubernetes is read with the cloudbolt-admin token of the container
orchestrator the blueprint registers (container_orchestrator_id), through
gcp_gke.KubeClient, which verifies TLS against the cluster's own CA. The
modules are imported inside each view, so a CloudBolt without them shows an
alert instead of failing to load the extension. Both run under a service
identity, not the viewer's, so every endpoint re-checks the viewer's
CloudBolt permission on the resource. Panels load asynchronously so a slow
Google Cloud or cluster API never stalls the page.

Vendor API references (each call site cites its own page):
    https://cloud.google.com/kubernetes-engine/docs/reference/rest/v1/projects.locations.clusters#Cluster
    https://cloud.google.com/kubernetes-engine/docs/reference/rest/v1/projects.locations.clusters/fetchClusterUpgradeInfo
    https://cloud.google.com/kubernetes-engine/docs/reference/rest/v1/projects.locations.clusters.nodePools#NodePool
    https://cloud.google.com/kubernetes-engine/docs/reference/rest/v1/projects.locations/getServerConfig
    https://cloud.google.com/kubernetes-engine/docs/concepts/release-channels
    https://cloud.google.com/kubernetes-engine/versioning
    https://cloud.google.com/kubernetes-engine/docs/concepts/node-images
    https://kubernetes.io/docs/reference/kubernetes-api/
"""

import importlib
import re
from datetime import datetime, timezone

from django.contrib import messages
from django.db.models import Q
from django.http import HttpResponseRedirect
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.utils.html import format_html, format_html_join
from django.utils.safestring import mark_safe

from cbhooks.models import ResourceAction
from cbhooks.services.parsing import get_hook_action_input_dict
from containerorchestrators.kuberneteshandler.models import Kubernetes
from extensions.views import tab_extension, TabExtensionDelegate
from infrastructure.models import Environment
from jobs.models import Job
from resources.models import Resource
from utilities.decorators import dialog_view
from utilities.logger import ThreadLogger
from utilities.permissions import resource_action_permitted

from xui.google_gke_cluster.forms import AddClusterGroupsForm, ConfirmForm, ScaleNodePoolForm

logger = ThreadLogger(__name__)

TEMPLATES = "google_gke_cluster/templates/"

# The gcp_gke (SHM-4yw92b8x) and gke_namespace (SHM-kephq8iq) shared
# modules, imported lazily by _gke() and _gke_namespace().
SHARED_MODULE = "shared_modules.gcp_gke"
NAMESPACE_MODULE = "shared_modules.gke_namespace"

# Viewing the tabs needs VIEW_PERMISSION on the resource. Every button maps
# to a resource action and is shown only when resource_action_permitted
# allows that action for the viewer.
VIEW_PERMISSION = "resource.view"

# Resource actions of the Google Kubernetes Engine blueprint, matched by the
# action's label or its plugin's name.
ACTION_UPGRADE_VERSION = "Upgrade GKE Version"
ACTION_ADD_POOL = "Add GKE Node Pool"
ACTION_DELETE_POOL = "Delete GKE Node Pool"
ACTION_LIST_POOLS = "List GKE Node Pools"
ACTION_SCALE_POOL = "Scale GKE Node Pool"
ACTION_UPGRADE_POOL = "Upgrade GKE Node Pool"
ACTION_CREATE_NAMESPACE = "Create GKE Namespace"
ACTION_MANAGE_GROUPS = "Manage GKE Cluster Groups"
# ...and of the GKE Namespace blueprint.
ACTION_MANAGE_NAMESPACE_ACCESS = "Manage GKE Namespace Access"
ACTION_MANAGE_PIPELINE_IDENTITY = "Manage GKE Namespace Pipeline Identity"
ACTION_REQUEST_QUOTA_CHANGE = "Request GKE Namespace Quota Change"

# Custom fields that mark the resources the tabs belong on.
CLUSTER_MARKER_FIELD = "gke_cluster_name"
NAMESPACE_MARKER_FIELD = "gke_namespace_name"
NAMESPACE_RESOURCE_TYPE = "kubernetes_namespace"

# Node pool names: up to 40 lowercase letters, digits, and hyphens, starting
# with a letter and ending with a letter or digit (gcp_gke.NODE_POOL_NAME_RE).
# https://cloud.google.com/kubernetes-engine/docs/reference/rest/v1/projects.locations.clusters.nodePools#NodePool
POOL_NAME_RE = re.compile(r"^[a-z]([-a-z0-9]{0,38}[a-z0-9])?$")

# Cluster.status and NodePool.status, with the words the console uses.
# https://cloud.google.com/kubernetes-engine/docs/reference/rest/v1/projects.locations.clusters#Status
# https://cloud.google.com/kubernetes-engine/docs/reference/rest/v1/projects.locations.clusters.nodePools#Status
STATUS_TEXT = {
    "PROVISIONING": "Provisioning",
    "RUNNING": "Running",
    "RUNNING_WITH_ERROR": "Running with error",
    "RECONCILING": "Reconciling",
    "STOPPING": "Stopping",
    "ERROR": "Error",
    "DEGRADED": "Degraded",
}

# NodeConfig.imageType, named as the console's Image type field names them.
# https://cloud.google.com/kubernetes-engine/docs/concepts/node-images
IMAGE_TYPES = {
    "COS_CONTAINERD": "Container-Optimized OS with containerd",
    "UBUNTU_CONTAINERD": "Ubuntu with containerd",
    "WINDOWS_LTSC_CONTAINERD": "Windows LTSC with containerd",
    "WINDOWS_SAC_CONTAINERD": "Windows SAC with containerd",
}

# BinaryAuthorization.evaluationMode.
# https://cloud.google.com/kubernetes-engine/docs/reference/rest/v1/projects.locations.clusters#EvaluationMode
BINAUTHZ_MODES = {
    "DISABLED": "Disabled",
    "PROJECT_SINGLETON_POLICY_ENFORCE": "Enforced (project policy)",
    "POLICY_BINDINGS": "Platform policies",
    "POLICY_BINDINGS_AND_PROJECT_SINGLETON_POLICY_ENFORCE": "Platform policies and project policy",
}

# NetworkConfig.gatewayApiConfig.channel.
# https://cloud.google.com/kubernetes-engine/docs/reference/rest/v1/NetworkConfig#Channel
GATEWAY_CHANNELS = {
    "CHANNEL_DISABLED": "",
    "CHANNEL_EXPERIMENTAL": "Experimental channel",
    "CHANNEL_STANDARD": "Standard channel",
}

# Kubernetes node conditions that signal pressure when their status is True.
PRESSURE_CONDITIONS = ("MemoryPressure", "DiskPressure", "PIDPressure", "NetworkUnavailable")

# Node labels GKE and Kubernetes apply.
# https://cloud.google.com/kubernetes-engine/docs/how-to/creating-managing-labels
# https://kubernetes.io/docs/reference/labels-annotations-taints/
LABEL_POOL = ("cloud.google.com/gke-nodepool",)
LABEL_INSTANCE_TYPE = ("node.kubernetes.io/instance-type", "beta.kubernetes.io/instance-type")
LABEL_ZONE = ("topology.kubernetes.io/zone", "failure-domain.beta.kubernetes.io/zone")

# Standard support ending within this many days shows as a warning.
SUPPORT_WARNING_DAYS = 90


class PanelError(Exception):
    """A readable problem the panel shows instead of a traceback."""


# ---------------------------------------------------------------------------
# Shared module, resource and permission helpers
# ---------------------------------------------------------------------------
def _gke():
    """The gcp_gke shared module, or PanelError when it is not synced."""
    try:
        return importlib.import_module(SHARED_MODULE)
    except ImportError:
        raise PanelError("The gcp_gke shared module (SHM-4yw92b8x) is not on this CloudBolt.")


def _gke_namespace():
    """The gke_namespace shared module, or PanelError when it (or gcp_gke) is not synced."""
    try:
        return importlib.import_module(NAMESPACE_MODULE)
    except ImportError:
        raise PanelError(
            "The gke_namespace shared module (SHM-kephq8iq), or the gcp_gke module it needs "
            "(SHM-4yw92b8x), is not on this CloudBolt."
        )


def _is_gke_error(exc):
    try:
        return isinstance(exc, _gke().GKEError)
    except PanelError:
        return False


def _attr(resource, name):
    """Value of a custom field on the resource (exposed as an attribute), or None."""
    try:
        value = getattr(resource, name)
    except AttributeError:
        return None
    if value in ("", None):
        return None
    return value


def _has_permission(profile, permission, resource):
    if getattr(profile, "is_cbadmin", False):
        return True
    try:
        return bool(profile.has_permission(permission, resource))
    except Exception:
        logger.exception("Permission check '%s' failed for resource %s", permission, resource.id)
        return False


def _can_view(profile, resource):
    return _has_permission(profile, VIEW_PERMISSION, resource)


def _action_for(profile, resource, name):
    """The enabled resource action labelled ``name`` (or whose plugin is named
    ``name``), when it applies to the resource's blueprint and the viewer may
    run it on this resource (the checks the resource page's action sidebar
    applies), else None."""
    try:
        actions = ResourceAction.objects.filter(enabled=True).filter(Q(label=name) | Q(hook__name=name))
        for action in actions.distinct():
            blueprint_ids = list(action.blueprints.values_list("id", flat=True))
            if blueprint_ids and resource.blueprint_id not in blueprint_ids:
                continue
            if not resource_action_permitted(profile, resource, action):
                continue
            return action
    except Exception:
        logger.exception("Could not resolve the '%s' action for resource %s", name, resource.id)
    return None


def _action_url(profile, resource, name):
    """CloudBolt's own run dialog for the action, or '' when unavailable."""
    action = _action_for(profile, resource, name)
    if action is None:
        return ""
    return reverse("run_resource_action", args=[resource.id, action.id])


def _cluster_name(resource):
    return _attr(resource, CLUSTER_MARKER_FIELD) or resource.name


def _orchestrator(resource):
    """The Kubernetes container orchestrator the blueprint registered, or None."""
    orchestrator_id = _attr(resource, "container_orchestrator_id")
    if not orchestrator_id:
        return None
    try:
        return Kubernetes.objects.filter(id=int(str(orchestrator_id).strip())).first()
    except (TypeError, ValueError):
        return None


def _environment_name(resource):
    env_id = _attr(resource, "env_id")
    if not env_id:
        return ""
    try:
        env = Environment.objects.filter(id=int(str(env_id).strip())).first()
    except (TypeError, ValueError):
        return ""
    return env.name if env else ""


def _is_zone(location):
    """'us-central1-a' is a zone (zonal cluster); 'us-central1' a region (regional cluster)."""
    return str(location or "").count("-") >= 2


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------
def _pill(text, style, icon, spin=False):
    return {"text": text, "style": style, "icon": icon, "spin": spin}


def _status_pill(status):
    """A cluster or node pool status as a pill."""
    status = str(status or "")
    text = STATUS_TEXT.get(status, status.replace("_", " ").capitalize() or "Unknown")
    if status == "RUNNING":
        return _pill(text, "ok", "bi-check-circle-fill")
    if status in ("ERROR",):
        return _pill(text, "bad", "bi-x-circle-fill")
    if status in ("DEGRADED", "RUNNING_WITH_ERROR"):
        return _pill(text, "warn", "bi-exclamation-triangle-fill")
    if status in ("PROVISIONING", "RECONCILING", "STOPPING"):
        return _pill(text, "live", "bi-arrow-repeat", spin=True)
    return _pill(text, "muted", "bi-question-circle")


def _enabled(value):
    return "Enabled" if value else "Disabled"


def _humanize(value):
    return str(value or "").replace("_", " ").capitalize()


def _parse_time(text):
    """An aware datetime from an RFC 3339 timestamp (Google returns up to nanoseconds), or None."""
    text = str(text or "").strip()
    if not text:
        return None
    text = re.sub(r"(\.\d{6})\d+", r"\1", text).replace("Z", "+00:00")
    try:
        value = datetime.fromisoformat(text)
    except ValueError:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _date_text(when):
    return when.strftime("%Y-%m-%d") if when else ""


def _age(iso_timestamp):
    """Compact age of a Kubernetes object from its creationTimestamp."""
    if not iso_timestamp:
        return ""
    try:
        created = datetime.strptime(iso_timestamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return ""
    delta = datetime.now(timezone.utc) - created
    if delta.days >= 1:
        return "{}d".format(delta.days)
    hours = delta.seconds // 3600
    if hours:
        return "{}h".format(hours)
    return "{}m".format(delta.seconds // 60)


def _label(labels, keys):
    for key in keys:
        if labels.get(key):
            return labels[key]
    return ""


def _channel_text(channel):
    """'Regular channel', 'Extended channel', or 'No channel'."""
    channel = str(channel or "UNSPECIFIED")
    if channel == "UNSPECIFIED":
        return "No channel (static version)"
    try:
        label = _gke().RELEASE_CHANNEL_LABELS.get(channel, _humanize(channel))
    except PanelError:
        label = _humanize(channel)
    return "{} channel".format(label.split(" (")[0])


def _image_type(image_type):
    value = str(image_type or "")
    return IMAGE_TYPES.get(value.upper(), value.lower())


def _active_jobs(resource):
    """Jobs still running against this resource (resource actions link their
    jobs to the resource through the hook parameters and the resource's job
    history)."""
    candidates = []
    try:
        candidates += list(
            Job.objects.filter(
                status__in=Job.ACTIVE_STATUSES,
                job_parameters__hookparameters__resources=resource,
            ).order_by("-id")[:10]
        )
    except Exception:
        logger.exception("Could not list active action jobs for resource %s", resource.id)
    try:
        candidates += list(resource.jobs.filter(status__in=Job.ACTIVE_STATUSES).order_by("-id")[:10])
    except Exception:
        logger.exception("Could not list the job history of resource %s", resource.id)

    rows, seen = [], set()
    for job in candidates:
        if job.id in seen:
            continue
        seen.add(job.id)
        label = ""
        try:
            params = job.job_parameters.cast()
            action = getattr(params, "resource_action", None)
            hook = getattr(params, "hook", None)
            label = getattr(action, "label", "") or getattr(hook, "name", "") or ""
        except Exception:
            label = ""
        try:
            status = job.get_status_display()
        except Exception:
            status = job.status
        rows.append({"id": job.id, "status": status, "url": job.get_absolute_url(), "label": label})
    return rows


# ---------------------------------------------------------------------------
# Cluster tab: cluster properties as the Google Cloud console groups them
# ---------------------------------------------------------------------------
def _version_support(info):
    """Where the control plane's minor version sits in GKE's support window,
    from fetchClusterUpgradeInfo: standard support until
    endOfStandardSupportTimestamp, then extended support (Extended channel)
    until endOfExtendedSupportTimestamp, then end of support.
    https://cloud.google.com/kubernetes-engine/docs/reference/rest/v1/projects.locations.clusters/fetchClusterUpgradeInfo
    https://cloud.google.com/kubernetes-engine/versioning#lifecycle"""
    if not info:
        return None
    standard = _parse_time(info.get("endOfStandardSupportTimestamp"))
    extended = _parse_time(info.get("endOfExtendedSupportTimestamp"))
    now = datetime.now(timezone.utc)
    if standard and now < standard:
        if (standard - now).days <= SUPPORT_WARNING_DAYS:
            return {"pill": _pill("Standard support ends soon", "warn", "bi-shield-exclamation"),
                    "hint": "Standard support ends {}".format(_date_text(standard))}
        return {"pill": _pill("Standard support", "ok", "bi-shield-check"),
                "hint": "Until {}".format(_date_text(standard))}
    if extended and now < extended:
        return {"pill": _pill("Extended support", "warn", "bi-shield-exclamation"),
                "hint": "Standard support ended {}; extended support until {}".format(
                    _date_text(standard) or "earlier", _date_text(extended))}
    if standard or extended:
        return {"pill": _pill("End of support", "bad", "bi-shield-x"),
                "hint": "Support ended {}".format(_date_text(extended or standard))}
    return None


def _auto_upgrade(info):
    """Auto-upgrade status, pause reasons, and next targets from fetchClusterUpgradeInfo."""
    if not info:
        return {}
    status = [_humanize(s) for s in info.get("autoUpgradeStatus") or [] if s and not s.endswith("UNSPECIFIED")]
    paused = [_humanize(r) for r in info.get("pausedReason") or [] if r and not r.endswith("UNSPECIFIED")]
    return {
        "status": ", ".join(status),
        "paused": ", ".join(paused),
        "patch_target": info.get("patchTargetVersion") or "",
        "minor_target": info.get("minorTargetVersion") or "",
    }


def _feature(name, enabled, detail=""):
    return {"name": name, "enabled": bool(enabled), "detail": detail if enabled else ""}


def _features(data):
    """Add-ons and features from the cluster, with the console's names.
    https://cloud.google.com/kubernetes-engine/docs/reference/rest/v1/projects.locations.clusters#AddonsConfig
    https://cloud.google.com/kubernetes-engine/docs/reference/rest/v1/NetworkConfig
    https://cloud.google.com/kubernetes-engine/docs/reference/rest/v1/projects.locations.clusters#MonitoringConfig"""
    addons = data.get("addonsConfig") or {}

    def off(key):
        return bool((addons.get(key) or {}).get("disabled"))

    def on(key):
        return bool((addons.get(key) or {}).get("enabled"))

    gateway = ((data.get("networkConfig") or {}).get("gatewayApiConfig") or {}).get("channel") or ""
    logging_service = str(data.get("loggingService") or "")
    monitoring_service = str(data.get("monitoringService") or "")
    logging_parts = (((data.get("loggingConfig") or {}).get("componentConfig") or {}).get("enableComponents") or [])
    monitoring = data.get("monitoringConfig") or {}
    return [
        _feature("HTTP load balancing", not off("httpLoadBalancing")),
        _feature("Horizontal Pod Autoscaling", not off("horizontalPodAutoscaling")),
        _feature("Compute Engine persistent disk CSI driver", on("gcePersistentDiskCsiDriverConfig")),
        _feature("Filestore CSI driver", on("gcpFilestoreCsiDriverConfig")),
        _feature("Cloud Storage FUSE CSI driver", on("gcsFuseCsiDriverConfig")),
        _feature("NodeLocal DNSCache", on("dnsCacheConfig")),
        _feature("Config Connector", on("configConnectorConfig")),
        _feature("Backup for GKE", on("gkeBackupAgentConfig")),
        _feature("Gateway API", gateway and gateway != "CHANNEL_DISABLED",
                 GATEWAY_CHANNELS.get(gateway, _humanize(gateway))),
        _feature("Cloud Logging", logging_service and logging_service != "none",
                 ", ".join(_humanize(p) for p in logging_parts)),
        _feature("Cloud Monitoring", monitoring_service and monitoring_service != "none"),
        _feature("Managed Service for Prometheus",
                 (monitoring.get("managedPrometheusConfig") or {}).get("enabled")),
    ]


def _cluster_context(resource, profile):
    gke = _gke()
    cluster = gke.cluster_for_resource(resource)
    warnings = []

    # https://cloud.google.com/kubernetes-engine/docs/reference/rest/v1/projects.locations.clusters/get
    data = cluster.get()
    location = data.get("location") or cluster.location
    current = data.get("currentMasterVersion") or ""
    pools = data.get("nodePools") or []
    channel = (data.get("releaseChannel") or {}).get("channel") or "UNSPECIFIED"
    private_config = data.get("privateClusterConfig") or {}
    endpoints_config = data.get("controlPlaneEndpointsConfig") or {}

    info = cluster.upgrade_info()
    if info is None:
        warnings.append("GKE returned no upgrade information for this cluster, so version support is not shown.")
    try:
        # https://cloud.google.com/kubernetes-engine/docs/reference/rest/v1/projects.locations/getServerConfig
        targets = gke.master_upgrade_targets(cluster.api.server_config(cluster.project, location), data)
    except gke.GKEError as exc:
        warnings.append("Available versions could not be read: {}".format(exc))
        targets = []
    behind = sorted(
        pool.get("name") or ""
        for pool in pools
        if current and pool.get("version") and gke.version_key(pool.get("version")) < gke.version_key(current)
    )
    upgrade_url = _action_url(profile, resource, ACTION_UPGRADE_VERSION)

    ip_policy = data.get("ipAllocationPolicy") or {}
    authorized = data.get("masterAuthorizedNetworksConfig") or {}
    groups_config = data.get("authenticatorGroupsConfig") or {}
    binauthz = data.get("binaryAuthorization") or {}
    binauthz_mode = binauthz.get("evaluationMode") or ("PROJECT_SINGLETON_POLICY_ENFORCE"
                                                       if binauthz.get("enabled") else "DISABLED")
    encryption = data.get("databaseEncryption") or {}
    conditions = [c.get("message") or c.get("canonicalCode") or c.get("code") or ""
                  for c in data.get("conditions") or []]

    return {
        "cluster": {
            "status": _status_pill(data.get("status")),
            "status_message": data.get("statusMessage") or "",
            "conditions": [c for c in conditions if c],
            "mode": "Autopilot" if (data.get("autopilot") or {}).get("enabled") else "Standard",
            "location_type": "Zonal" if _is_zone(location) else "Regional",
            "location": location,
            "zones": [str(z) for z in data.get("locations") or []],
            "project": cluster.project,
            "version": current,
            "node_version": data.get("currentNodeVersion") or "",
            "channel": _channel_text(channel),
            "total_nodes": data.get("currentNodeCount"),
            "created": _date_text(_parse_time(data.get("createTime"))),
            "endpoint": data.get("endpoint") or "",
            "private_endpoint": private_config.get("privateEndpoint") or "",
            "dns_endpoint": (endpoints_config.get("dnsEndpointConfig") or {}).get("endpoint") or "",
            "pool_count": len(pools),
            "description": data.get("description") or "",
        },
        "support": _version_support(info),
        "upgrades": {
            "targets": targets,
            "available": bool(targets),
            "latest": targets[0] if targets else "",
            "on_channel": channel != "UNSPECIFIED",
            "auto": _auto_upgrade(info),
            "pools_behind": behind,
            "url": upgrade_url if targets else "",
        },
        "networking": {
            "network": data.get("network") or "",
            "subnetwork": data.get("subnetwork") or "",
            "vpc_native": bool(ip_policy.get("useIpAliases")),
            "pod_range": ip_policy.get("clusterIpv4CidrBlock") or "",
            "service_range": ip_policy.get("servicesIpv4CidrBlock") or "",
            "dataplane": gke.describe_dataplane(data),
            "private_nodes": gke.private_nodes(data),
            "authorized_enabled": bool(authorized.get("enabled")),
            "authorized": [
                {"name": block.get("displayName") or "", "cidr": block.get("cidrBlock") or ""}
                for block in authorized.get("cidrBlocks") or []
            ],
        },
        "security": {
            "workload_pool": (data.get("workloadIdentityConfig") or {}).get("workloadPool") or "",
            "shielded_nodes": _enabled((data.get("shieldedNodes") or {}).get("enabled")),
            "google_groups": groups_config.get("securityGroup") if groups_config.get("enabled") else "",
            "legacy_abac": _enabled((data.get("legacyAbac") or {}).get("enabled")),
            "binary_authorization": BINAUTHZ_MODES.get(binauthz_mode, _humanize(binauthz_mode)),
            "secrets_encryption": "Enabled" if encryption.get("state") == "ENCRYPTED" else "Disabled",
            "secrets_key": (encryption.get("keyName") or "") if encryption.get("state") == "ENCRYPTED" else "",
            "client_certificate": _enabled(
                ((data.get("masterAuth") or {}).get("clientCertificateConfig") or {}).get("issueClientCertificate")
            ),
        },
        "features": _features(data),
        "jobs": _active_jobs(resource),
        "warnings": warnings,
    }


# ---------------------------------------------------------------------------
# Kubernetes reads (with the orchestrator's token, verified against the cluster CA)
# ---------------------------------------------------------------------------
def _kube(resource):
    """A gcp_gke.KubeClient with the cluster orchestrator's credentials, or PanelError."""
    gke = _gke()
    try:
        return gke.kube_for_resource(resource)
    except gke.GKEError as exc:
        raise PanelError(str(exc))


def _kube_items(kube, path):
    """The items of a Kubernetes list call."""
    return (kube.get(path) or {}).get("items", []) or []


def _ready(node):
    conditions = {c.get("type"): c for c in (node.get("status") or {}).get("conditions", []) or []}
    return conditions.get("Ready", {}).get("status")


def _node_rows(nodes):
    """The Nodes table under the node pools.
    https://kubernetes.io/docs/reference/kubernetes-api/cluster-resources/node-v1/"""
    rows = []
    for node in nodes:
        metadata, status = node.get("metadata") or {}, node.get("status") or {}
        labels = metadata.get("labels") or {}
        ready = _ready(node)
        cordoned = bool((node.get("spec") or {}).get("unschedulable"))
        if ready == "True" and cordoned:
            pill = _pill("Ready, cordoned", "warn", "bi-pause-circle-fill")
        elif ready == "True":
            pill = _pill("Ready", "ok", "bi-check-circle-fill")
        elif ready == "False":
            pill = _pill("NotReady", "bad", "bi-x-circle-fill")
        else:
            pill = _pill("Unknown", "bad", "bi-question-circle")
        internal_ip = next(
            (a.get("address") for a in status.get("addresses") or [] if a.get("type") == "InternalIP"), ""
        )
        node_info = status.get("nodeInfo") or {}
        rows.append({
            "name": metadata.get("name", ""),
            "status": pill,
            "cordoned": cordoned and ready != "True",
            "pool": _label(labels, LABEL_POOL),
            "kubelet": node_info.get("kubeletVersion", ""),
            "os_image": node_info.get("osImage", ""),
            "zone": _label(labels, LABEL_ZONE),
            "internal_ip": internal_ip or "",
            "age": _age(metadata.get("creationTimestamp")),
        })
    rows.sort(key=lambda r: (r["pool"], r["name"]))
    return rows


def _nodes_by_pool(nodes):
    """({pool: total nodes}, {pool: Ready nodes}) from the cluster's Kubernetes nodes."""
    total, ready = {}, {}
    for node in nodes:
        pool = _label((node.get("metadata") or {}).get("labels") or {}, LABEL_POOL)
        total[pool] = total.get(pool, 0) + 1
        if _ready(node) == "True":
            ready[pool] = ready.get(pool, 0) + 1
    return total, ready


# ---------------------------------------------------------------------------
# Node pools tab
# ---------------------------------------------------------------------------
def _node_pools_context(resource, profile):
    gke = _gke()
    cluster = gke.cluster_for_resource(resource)
    warnings = []

    # https://cloud.google.com/kubernetes-engine/docs/reference/rest/v1/projects.locations.clusters/get
    data = cluster.get()
    master = data.get("currentMasterVersion") or ""
    location = data.get("location") or cluster.location
    # https://cloud.google.com/kubernetes-engine/docs/reference/rest/v1/projects.locations.clusters.nodePools/list
    pools = cluster.list_pools()
    tracked = set(gke.pool_sub_resources(resource).values_list("name", flat=True))

    nodes, nodes_error = [], ""
    try:
        kube = _kube(resource)
        try:
            # GET /api/v1/nodes
            nodes = _kube_items(kube, "/api/v1/nodes")
        finally:
            kube.close()
    except PanelError as exc:
        nodes_error = str(exc)
    except Exception as exc:
        logger.warning("Kubernetes node read failed for resource %s: %s", resource.id, exc)
        nodes_error = "The cluster's nodes could not be read: {}".format(exc)
    total_by_pool, ready_by_pool = _nodes_by_pool(nodes)

    can_scale = _action_for(profile, resource, ACTION_SCALE_POOL) is not None
    # CloudBolt's own dialogs: each action's dropdown lists every node pool,
    # so one URL serves every row.
    upgrade_url = _action_url(profile, resource, ACTION_UPGRADE_POOL)
    delete_url = _action_url(profile, resource, ACTION_DELETE_POOL)

    rows = []
    for pool in pools:
        name = pool.get("name") or ""
        config = pool.get("config") or {}
        autoscaling = pool.get("autoscaling") or {}
        management = pool.get("management") or {}
        autoscale = bool(autoscaling.get("enabled"))
        version = pool.get("version") or ""
        behind = bool(version and master and gke.version_key(version) < gke.version_key(master))
        disk = ""
        if config.get("diskSizeGb"):
            disk = "{} GB {}".format(config.get("diskSizeGb"), config.get("diskType") or "").strip()
        rows.append({
            "name": name,
            "status": _status_pill(pool.get("status")),
            "status_message": pool.get("statusMessage") or "",
            "scale_method": "Autoscale" if autoscale else "Manual",
            "min": autoscaling.get("minNodeCount", 0) if autoscale else None,
            "max": autoscaling.get("maxNodeCount") if autoscale else None,
            "total_min": autoscaling.get("totalMinNodeCount") if autoscale else None,
            "total_max": autoscaling.get("totalMaxNodeCount") if autoscale else None,
            "initial": pool.get("initialNodeCount"),
            "nodes": total_by_pool.get(name, 0) if not nodes_error else None,
            "ready": ready_by_pool.get(name, 0) if not nodes_error else None,
            "machine_type": config.get("machineType") or "",
            "image_type": _image_type(config.get("imageType")),
            "disk": disk,
            "version": version,
            "behind": behind,
            "zones": gke.pool_zones(pool, data),
            "spot": bool(config.get("spot") or config.get("preemptible")),
            "auto_upgrade": bool(management.get("autoUpgrade")),
            "auto_repair": bool(management.get("autoRepair")),
            "tracked": name in tracked,
            "console_url": gke.node_pool_console_url(cluster.project, location, cluster.name, name),
            "scale_url": (
                reverse("google_gke_scale_node_pool", args=[resource.id, name])
                if can_scale and POOL_NAME_RE.match(name) else ""
            ),
            "upgrade_url": upgrade_url if behind else "",
            "delete_url": delete_url if len(pools) > 1 else "",
        })
    return {
        "rows": rows,
        "kubernetes_version": master,
        "nodes": _node_rows(nodes),
        "nodes_error": nodes_error,
        "warnings": warnings,
    }


# ---------------------------------------------------------------------------
# Workloads tab
# ---------------------------------------------------------------------------
CPU_SUFFIXES = {"n": 1e-9, "u": 1e-6, "m": 1e-3}
MEMORY_SUFFIXES = {
    "Ki": 1024, "Mi": 1024 ** 2, "Gi": 1024 ** 3, "Ti": 1024 ** 4, "Pi": 1024 ** 5, "Ei": 1024 ** 6,
    "k": 1000, "M": 1000 ** 2, "G": 1000 ** 3, "T": 1000 ** 4, "P": 1000 ** 5, "E": 1000 ** 6,
}
QUANTITY_RE = re.compile(r"^([0-9]*\.?[0-9]+(?:[eE][+-]?[0-9]+)?)([A-Za-z]*)$")


def _cpu_cores(quantity):
    """Kubernetes CPU quantity (500m, 2, 1500000n) as cores."""
    match = QUANTITY_RE.match(str(quantity or "").strip())
    if not match:
        return 0.0
    number, suffix = match.groups()
    return float(number) * CPU_SUFFIXES.get(suffix, 1.0)


def _memory_bytes(quantity):
    """Kubernetes memory quantity (128Mi, 2Gi, 1000000, 1e9) as bytes."""
    match = QUANTITY_RE.match(str(quantity or "").strip())
    if not match:
        return 0.0
    number, suffix = match.groups()
    return float(number) * MEMORY_SUFFIXES.get(suffix, 1)


def _cores_text(cores):
    return "{:.2f}".format(cores).rstrip("0").rstrip(".") or "0"


def _memory_text(size):
    if size >= 1024 ** 3:
        return "{:.1f} GiB".format(size / 1024 ** 3)
    return "{:.0f} MiB".format(size / 1024 ** 2)


def _meter(used, total, used_text, total_text):
    percent = int(round(100.0 * used / total)) if total else 0
    style = "ok" if percent < 75 else "warn" if percent < 90 else "bad"
    return {
        "requests": used_text,
        "allocatable": total_text,
        "percent": max(0, min(100, percent)),
        "style": style,
    }


def _workloads_context(resource, profile):
    gke = _gke()
    orchestrator = _orchestrator(resource)
    kube = _kube(resource)
    try:
        # Cluster-wide core and apps/v1 lists.
        # https://kubernetes.io/docs/reference/kubernetes-api/cluster-resources/node-v1/
        # https://kubernetes.io/docs/reference/kubernetes-api/workload-resources/pod-v1/
        # https://kubernetes.io/docs/reference/kubernetes-api/cluster-resources/namespace-v1/
        # https://kubernetes.io/docs/reference/kubernetes-api/service-resources/service-v1/
        # https://kubernetes.io/docs/reference/kubernetes-api/workload-resources/deployment-v1/
        nodes = _kube_items(kube, "/api/v1/nodes")
        pods = _kube_items(kube, "/api/v1/pods")
        namespaces = _kube_items(kube, "/api/v1/namespaces")
        services = _kube_items(kube, "/api/v1/services")
        deployments = _kube_items(kube, "/apis/apps/v1/deployments")
    except Exception as exc:
        logger.warning("Kubernetes API read failed for resource %s: %s", resource.id, exc)
        raise PanelError("The cluster API could not be read{}: {}".format(
            " with container orchestrator {}'s credentials".format(orchestrator.name) if orchestrator else "", exc))
    finally:
        kube.close()

    # Requests per node from the pods scheduled on it that still hold capacity
    # (Succeeded and Failed pods release theirs).
    per_node = {}
    pods_by_namespace = {}
    waiting_by_namespace = {}
    scheduled_pods = 0
    for pod in pods:
        namespace = (pod.get("metadata") or {}).get("namespace", "")
        phase = (pod.get("status") or {}).get("phase", "")
        pods_by_namespace[namespace] = pods_by_namespace.get(namespace, 0) + 1
        if phase not in ("Running", "Succeeded"):
            waiting_by_namespace[namespace] = waiting_by_namespace.get(namespace, 0) + 1
        if phase in ("Succeeded", "Failed"):
            continue
        node_name = (pod.get("spec") or {}).get("nodeName")
        if not node_name:
            continue
        scheduled_pods += 1
        usage = per_node.setdefault(node_name, {"cpu": 0.0, "memory": 0.0, "pods": 0})
        usage["pods"] += 1
        for container in (pod.get("spec") or {}).get("containers", []) or []:
            requests = (container.get("resources") or {}).get("requests") or {}
            usage["cpu"] += _cpu_cores(requests.get("cpu"))
            usage["memory"] += _memory_bytes(requests.get("memory"))

    node_rows = []
    totals = {"cpu_requests": 0.0, "cpu_allocatable": 0.0, "memory_requests": 0.0, "memory_allocatable": 0.0}
    not_ready = 0
    pressured = 0
    for node in nodes:
        metadata = node.get("metadata") or {}
        status = node.get("status") or {}
        labels = metadata.get("labels") or {}
        name = metadata.get("name", "")
        conditions = {c.get("type"): c for c in status.get("conditions", []) or []}
        ready = conditions.get("Ready", {})
        if ready.get("status") == "True":
            ready_pill = _pill("Ready", "ok", "bi-check-circle-fill")
        elif ready.get("status") == "False":
            ready_pill = _pill("NotReady", "bad", "bi-x-circle-fill")
            not_ready += 1
        else:
            ready_pill = _pill("Unknown", "bad", "bi-question-circle")
            not_ready += 1
        pressure = [
            {"type": kind, "message": conditions[kind].get("message") or kind}
            for kind in PRESSURE_CONDITIONS
            if conditions.get(kind, {}).get("status") == "True"
        ]
        if pressure:
            pressured += 1
        allocatable = status.get("allocatable") or {}
        cpu_total = _cpu_cores(allocatable.get("cpu"))
        memory_total = _memory_bytes(allocatable.get("memory"))
        usage = per_node.get(name, {"cpu": 0.0, "memory": 0.0, "pods": 0})
        totals["cpu_requests"] += usage["cpu"]
        totals["cpu_allocatable"] += cpu_total
        totals["memory_requests"] += usage["memory"]
        totals["memory_allocatable"] += memory_total
        node_rows.append({
            "name": name,
            "pool": _label(labels, LABEL_POOL),
            "instance_type": _label(labels, LABEL_INSTANCE_TYPE),
            "zone": _label(labels, LABEL_ZONE),
            "age": _age(metadata.get("creationTimestamp")),
            "ready": ready_pill,
            "conditions": pressure,
            "unschedulable": bool((node.get("spec") or {}).get("unschedulable")),
            "cpu": _meter(usage["cpu"], cpu_total, _cores_text(usage["cpu"]), _cores_text(cpu_total)),
            "memory": _meter(usage["memory"], memory_total, _memory_text(usage["memory"]), _memory_text(memory_total)),
            "pods": usage["pods"],
            "kubelet": (status.get("nodeInfo") or {}).get("kubeletVersion", ""),
        })
    node_rows.sort(key=lambda r: (r["pool"], r["name"]))

    deployments_by_namespace = {}
    degraded_by_namespace = {}
    for deployment in deployments:
        namespace = (deployment.get("metadata") or {}).get("namespace", "")
        deployments_by_namespace[namespace] = deployments_by_namespace.get(namespace, 0) + 1
        status = deployment.get("status") or {}
        wanted = (deployment.get("spec") or {}).get("replicas")
        if status.get("unavailableReplicas") or (wanted and (status.get("readyReplicas") or 0) < wanted):
            degraded_by_namespace[namespace] = degraded_by_namespace.get(namespace, 0) + 1
    services_by_namespace = {}
    for service in services:
        namespace = (service.get("metadata") or {}).get("namespace", "")
        services_by_namespace[namespace] = services_by_namespace.get(namespace, 0) + 1

    # Namespace landing zones (GKE Namespace blueprint) are sub-resources of
    # the cluster; link each one from its row so the owning team and the
    # CloudBolt resource are visible next to the live namespace.
    landing_zones = {}
    for zone in gke.active_namespaces(resource).select_related("group"):
        try:
            url = zone.get_absolute_url()
        except Exception:
            url = ""
        landing_zones[_attr(zone, NAMESPACE_MARKER_FIELD) or zone.name] = {
            "url": url,
            "group": zone.group.name if zone.group else "",
            "tier": _attr(zone, "gke_namespace_size_tier") or "",
        }

    namespace_rows = []
    for namespace in namespaces:
        name = (namespace.get("metadata") or {}).get("name", "")
        phase = (namespace.get("status") or {}).get("phase", "")
        if phase == "Active":
            pill = _pill("Active", "ok", "bi-check-circle")
        else:
            pill = _pill(phase or "Unknown", "warn", "bi-hourglass-split")
        namespace_rows.append({
            "name": name,
            "status": pill,
            "landing_zone": landing_zones.get(name),
            "pods": pods_by_namespace.get(name, 0),
            "pods_not_running": waiting_by_namespace.get(name, 0),
            "deployments": deployments_by_namespace.get(name, 0),
            "deployments_unavailable": degraded_by_namespace.get(name, 0),
            "services": services_by_namespace.get(name, 0),
        })
    namespace_rows.sort(key=lambda r: r["name"])

    return {
        "nodes": node_rows,
        "namespaces": namespace_rows,
        "not_ready": not_ready,
        "pressured": pressured,
        "totals": {
            "cpu_requests": _cores_text(totals["cpu_requests"]),
            "cpu_allocatable": _cores_text(totals["cpu_allocatable"]),
            "memory_requests": _memory_text(totals["memory_requests"]),
            "memory_allocatable": _memory_text(totals["memory_allocatable"]),
            "pods": scheduled_pods,
            "all_pods": len(pods),
            "deployments": len(deployments),
            "services": len(services),
        },
        "warnings": [],
    }


# ---------------------------------------------------------------------------
# Panels (HTML fragments loaded asynchronously by the tabs)
# ---------------------------------------------------------------------------
def _panel(request, resource_id, template, build):
    resource = get_object_or_404(Resource, pk=resource_id)
    profile = request.get_user_profile()
    if not _can_view(profile, resource):
        context = {"error": "You do not have permission to view this resource."}
    else:
        try:
            context = build(resource, profile)
        except PanelError as exc:
            context = {"error": str(exc)}
        except Exception as exc:
            if _is_gke_error(exc):
                logger.warning("Google Cloud call failed for resource %s: %s", resource.id, exc)
                context = {"error": "Google Cloud returned an error: {}".format(exc)}
            else:
                logger.exception("Panel %s failed for resource %s", template, resource.id)
                context = {"error": "Could not load this panel: {}".format(exc)}
    context["resource"] = resource
    return render(request, TEMPLATES + template, context)


def cluster_panel(request, resource_id):
    return _panel(request, resource_id, "cluster_panel.html", _cluster_context)


def node_pools_panel(request, resource_id):
    return _panel(request, resource_id, "node_pools_panel.html", _node_pools_context)


def workloads_panel(request, resource_id):
    return _panel(request, resource_id, "workloads_panel.html", _workloads_context)


# ---------------------------------------------------------------------------
# Groups tab: who may order namespaces on the cluster
# ---------------------------------------------------------------------------
# A group may order a GKE namespace on the cluster when the cluster's
# per-cluster Environment is available to it: entitled to the group or an
# ancestor, or unconstrained (no groups). CloudBolt's Unassigned group is a
# placeholder that entitles no one. Changes run as Manage GKE Cluster Groups
# jobs; this tab only reads.
def _cluster_environment(resource):
    """The cluster's per-cluster Environment, or PanelError."""
    try:
        return _gke().cluster_environment(resource)
    except PanelError:
        raise
    except Exception as exc:
        if _is_gke_error(exc):
            raise PanelError(str(exc))
        raise


def _groups_context(resource, profile):
    gke = _gke()
    env = _cluster_environment(resource)
    state, groups = gke.environment_groups(env)
    action = _action_for(profile, resource, ACTION_MANAGE_GROUPS)
    rows = [
        {
            "group": group,
            "remove_url": reverse("google_gke_remove_cluster_group", args=[resource.id, group.global_id])
            if action else "",
        }
        for group in groups
    ]
    return {
        "profile": profile,
        "environment": env,
        "state": state,
        "rows": rows,
        "allow_all_url": reverse("google_gke_allow_all_cluster_groups", args=[resource.id])
        if action and state != gke.UNCONSTRAINED else "",
        "action_label": action.label if action else "",
    }


def groups_panel(request, resource_id):
    return _panel(request, resource_id, "groups_panel.html", _groups_context)


# ---------------------------------------------------------------------------
# Namespace Connect tab: how to reach one landing zone, and who may
# ---------------------------------------------------------------------------
# Access to a landing zone is a RoleBinding in the namespace to the built-in
# Kubernetes edit, view, or admin ClusterRole for a Google account, Google
# service account, or Google Group (Google Groups for RBAC). gcloud
# container clusters get-credentials also needs container.clusters.get on
# the project (Kubernetes Engine Cluster Viewer).
#   https://cloud.google.com/kubernetes-engine/docs/how-to/role-based-access-control
#   https://cloud.google.com/kubernetes-engine/docs/how-to/google-groups-rbac
#   https://cloud.google.com/kubernetes-engine/docs/how-to/cluster-access-for-kubectl
ACCESS_SCOPES = {"namespace": "This namespace", "cluster": "Whole cluster"}
EXTRA_PRINCIPAL_KINDS = {"kubernetes": "Kubernetes service account"}


def _connect_context(resource, profile):
    """Connection details for a namespace landing zone, from gke_namespace."""
    ns = _gke_namespace()
    try:
        details = ns.connection_details(resource)
    except ns.AccessError as exc:
        raise PanelError(str(exc))
    cluster = details.pop("cluster_resource", None)
    cluster_url = ""
    if cluster is not None and _can_view(profile, cluster):
        try:
            cluster_url = cluster.get_absolute_url()
        except Exception:
            cluster_url = ""
    levels = ns.ACCESS_LEVELS
    kinds = dict(ns.PRINCIPAL_KINDS)
    kinds.update(EXTRA_PRINCIPAL_KINDS)
    details["access"] = [
        dict(
            row,
            name=str(row.get("principal") or "").split(":", 1)[-1],
            scope_text=ACCESS_SCOPES.get(row.get("scope"), row.get("scope") or ""),
            type_text=kinds.get(row.get("kind"), row.get("kind") or ""),
        )
        for row in details.get("access") or []
    ]
    details["pipelines"] = [
        dict(item, level_text=(levels.get(item.get("level")) or {}).get("label") or item.get("level") or "")
        for item in details.get("pipelines") or []
    ]
    details.update({
        "cluster_url": cluster_url,
        "kubeconfig_filename": "kubeconfig-{}-{}.yaml".format(details.get("cluster_name"), details.get("namespace")),
        "level_roles": ", ".join(level["cluster_role"] for level in levels.values()),
    })
    return details


def namespace_connect_panel(request, resource_id):
    return _panel(request, resource_id, "connect_panel.html", _connect_context)


# ---------------------------------------------------------------------------
# Namespace Workloads tab: what runs in one landing zone
# ---------------------------------------------------------------------------
# Read-only, with the orchestrator's token like the cluster's Workloads tab.
# Deliberately no pod logs, Secrets or ConfigMaps (anyone who may view the
# namespace resource would see them), and no scale or restart buttons (they
# would run as the orchestrator, not as the viewer).
#   ResourceQuota  https://kubernetes.io/docs/reference/kubernetes-api/policy-resources/resource-quota-v1/
#   Deployment     https://kubernetes.io/docs/reference/kubernetes-api/workload-resources/deployment-v1/
#   StatefulSet    https://kubernetes.io/docs/reference/kubernetes-api/workload-resources/stateful-set-v1/
#   DaemonSet      https://kubernetes.io/docs/reference/kubernetes-api/workload-resources/daemon-set-v1/
#   Pod            https://kubernetes.io/docs/reference/kubernetes-api/workload-resources/pod-v1/
#   Service        https://kubernetes.io/docs/reference/kubernetes-api/service-resources/service-v1/
#   Ingress        https://kubernetes.io/docs/reference/kubernetes-api/service-resources/ingress-v1/
#   PVC            https://kubernetes.io/docs/reference/kubernetes-api/config-and-storage-resources/persistent-volume-claim-v1/
#   Event          https://kubernetes.io/docs/reference/kubernetes-api/cluster-resources/event-v1/

# ResourceQuota keys in display order: label, how to read the quantity, and
# the unit shown after a count in the meter's hover text. CloudBolt's quota
# carries all of them.
QUOTA_KEYS = [
    ("requests.cpu", "CPU requests", "cpu", ""),
    ("limits.cpu", "CPU limits", "cpu", ""),
    ("requests.memory", "Memory requests", "memory", ""),
    ("limits.memory", "Memory limits", "memory", ""),
    ("requests.storage", "Storage", "memory", ""),
    ("persistentvolumeclaims", "Volume claims", "count", "claims"),
    ("pods", "Pods", "count", "pods"),
]
# Warning events shown, newest first. The API server keeps events for an hour
# by default (kube-apiserver --event-ttl):
# https://kubernetes.io/docs/reference/command-line-tools-reference/kube-apiserver/
MAX_EVENTS = 25


def _quantity(kind, value):
    if kind == "cpu":
        return _cpu_cores(value)
    if kind == "memory":
        return _memory_bytes(value)
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _quantity_text(kind, amount):
    if kind == "cpu":
        return "{} cores".format(_cores_text(amount))
    if kind == "memory":
        return _memory_text(amount)
    return "{:.0f}".format(amount)


def _usage_text(kind, used, total, unit):
    """Hover text for a quota meter: '0.3 / 4 cores', '384 MiB / 16.0 GiB', '3 / 20 pods'."""
    if kind == "cpu":
        return "{} / {} cores".format(_cores_text(used), _cores_text(total))
    if kind == "memory":
        return "{} / {}".format(_memory_text(used), _memory_text(total))
    return "{:.0f} / {:.0f}{}".format(used, total, " " + unit if unit else "")


def _quota_rows(usage, cloudbolt_quota):
    """One meter per hard limit of CloudBolt's ResourceQuota, and every other
    quota object in the namespace (GKE may add its own) as plain rows, from
    gke_namespace.quota_usage ({quota: {resource: (hard, used)}}).
    Returns (meters, other_quotas)."""
    known = [key for key, _, _, _ in QUOTA_KEYS]
    meters, others = [], []
    for quota_name in sorted(usage):
        limits = usage[quota_name] or {}
        if quota_name != cloudbolt_quota:
            others.append({
                "name": quota_name,
                "limits": [{"key": key, "hard": limits[key][0], "used": limits[key][1]} for key in sorted(limits)],
            })
            continue
        ordered = [entry for entry in QUOTA_KEYS if entry[0] in limits]
        ordered += [(k, k, "count", "") for k in sorted(limits) if k not in known]
        for key, label, kind, unit in ordered:
            hard, used = limits[key]
            total = _quantity(kind, hard)
            current = _quantity(kind, used)
            meter = _meter(current, total, _quantity_text(kind, current), _quantity_text(kind, total))
            meter.update({"label": label, "usage": _usage_text(kind, current, total, unit), "quota": quota_name})
            meters.append(meter)
    return meters, others


def _images(pod_spec):
    return sorted({c.get("image", "") for c in (pod_spec or {}).get("containers", []) or [] if c.get("image")})


def _workload_row(kind, item):
    metadata, spec, status = item.get("metadata") or {}, item.get("spec") or {}, item.get("status") or {}
    if kind == "DaemonSet":
        wanted, ready = status.get("desiredNumberScheduled") or 0, status.get("numberReady") or 0
    else:
        wanted = spec.get("replicas") if spec.get("replicas") is not None else 1
        ready = status.get("readyReplicas") or 0
    if wanted == 0:
        pill = _pill("Scaled to 0", "muted", "bi-pause-circle")
    elif ready >= wanted:
        pill = _pill("Ready", "ok", "bi-check-circle-fill")
    elif ready:
        pill = _pill("Degraded", "warn", "bi-exclamation-triangle-fill")
    else:
        pill = _pill("Unavailable", "bad", "bi-x-circle-fill")
    return {
        "kind": kind,
        "name": metadata.get("name", ""),
        "ready": "{}/{}".format(ready, wanted),
        "status": pill,
        "images": _images((spec.get("template") or {}).get("spec")),
        "age": _age(metadata.get("creationTimestamp")),
    }


def _pod_row(pod):
    metadata, spec, status = pod.get("metadata") or {}, pod.get("spec") or {}, pod.get("status") or {}
    containers = status.get("containerStatuses") or []
    reason = status.get("reason") or ""
    for container in containers:
        state = container.get("state") or {}
        waiting = (state.get("waiting") or {}).get("reason")
        terminated = (state.get("terminated") or {}).get("reason")
        if waiting or (terminated and terminated != "Completed"):
            reason = waiting or terminated
            break
    phase = status.get("phase", "Unknown")
    if not reason and phase == "Pending":
        for condition in status.get("conditions") or []:
            if condition.get("type") == "PodScheduled" and condition.get("status") == "False":
                reason = condition.get("reason") or ""
    text = reason or phase
    if phase == "Running" and not reason:
        pill = _pill(text, "ok", "bi-check-circle-fill")
    elif phase == "Succeeded":
        pill = _pill(text, "muted", "bi-check-circle")
    elif phase == "Pending" and not reason:
        pill = _pill(text, "live", "bi-hourglass-split")
    else:
        pill = _pill(text, "bad", "bi-x-circle-fill")
    owners = metadata.get("ownerReferences") or []
    return {
        "name": metadata.get("name", ""),
        "status": pill,
        "running": phase == "Running" and not reason,
        "ready": "{}/{}".format(sum(1 for c in containers if c.get("ready")), len(spec.get("containers") or [])),
        "restarts": sum(c.get("restartCount") or 0 for c in containers),
        "node": spec.get("nodeName", ""),
        "owner": "{} {}".format(owners[0].get("kind", ""), owners[0].get("name", "")) if owners else "",
        "age": _age(metadata.get("creationTimestamp")),
    }


def _service_row(service):
    metadata, spec = service.get("metadata") or {}, service.get("spec") or {}
    ports = []
    for port in spec.get("ports") or []:
        text = "{}/{}".format(port.get("port"), port.get("protocol", "TCP"))
        if port.get("targetPort") not in (None, port.get("port")):
            text += " → {}".format(port.get("targetPort"))
        ports.append(text)
    external = [
        entry.get("hostname") or entry.get("ip")
        for entry in ((service.get("status") or {}).get("loadBalancer") or {}).get("ingress") or []
    ]
    return {
        "kind": "Service",
        "name": metadata.get("name", ""),
        "type": spec.get("type", ""),
        "address": spec.get("clusterIP", ""),
        "ports": ", ".join(ports),
        "external": ", ".join(e for e in external if e),
        "age": _age(metadata.get("creationTimestamp")),
    }


def _ingress_row(ingress):
    metadata, spec = ingress.get("metadata") or {}, ingress.get("spec") or {}
    hosts = [rule.get("host") for rule in spec.get("rules") or [] if rule.get("host")]
    external = [
        entry.get("hostname") or entry.get("ip")
        for entry in ((ingress.get("status") or {}).get("loadBalancer") or {}).get("ingress") or []
    ]
    return {
        "kind": "Ingress",
        "name": metadata.get("name", ""),
        "type": spec.get("ingressClassName", ""),
        "address": "",
        "ports": ", ".join(hosts) or "*",
        "external": ", ".join(e for e in external if e),
        "age": _age(metadata.get("creationTimestamp")),
    }


def _claim_row(claim):
    metadata, spec, status = claim.get("metadata") or {}, claim.get("spec") or {}, claim.get("status") or {}
    phase = status.get("phase", "")
    bound = phase == "Bound"
    return {
        "name": metadata.get("name", ""),
        "status": _pill(phase or "Unknown", "ok" if bound else "warn",
                        "bi-check-circle-fill" if bound else "bi-hourglass-split"),
        "size": (status.get("capacity") or {}).get("storage")
        or ((spec.get("resources") or {}).get("requests") or {}).get("storage", ""),
        "storage_class": spec.get("storageClassName", ""),
        "age": _age(metadata.get("creationTimestamp")),
    }


def _event_time(event):
    return (event.get("lastTimestamp") or event.get("eventTime")
            or (event.get("metadata") or {}).get("creationTimestamp") or "")


def _event_rows(events):
    """The newest Warning events; filtered here rather than with a field selector."""
    warnings = [e for e in events if e.get("type") == "Warning"]
    warnings.sort(key=_event_time, reverse=True)
    rows = []
    for event in warnings[:MAX_EVENTS]:
        involved = event.get("involvedObject") or {}
        when = _event_time(event)
        rows.append({
            "age": _age(when[:19] + "Z" if len(when) >= 19 else when),
            "object": "{} {}".format(involved.get("kind", ""), involved.get("name", "")).strip(),
            "reason": event.get("reason", ""),
            "message": event.get("message", ""),
            "count": event.get("count") or 1,
        })
    return rows, len(warnings)


def _namespace_workloads_context(resource, profile):
    ns = _gke_namespace()
    gke = _gke()
    try:
        namespace, orchestrator, cluster_name = ns.load_cluster(resource)
    except ValueError as exc:
        raise PanelError("This namespace cannot be read: {}.".format(exc))
    if not ns.NAMESPACE_NAME_RE.match(str(namespace)):
        raise PanelError("'{}' is not a valid Kubernetes namespace name.".format(namespace))
    try:
        kube = gke.KubeClient.from_orchestrator(orchestrator)
    except gke.GKEError as exc:
        raise PanelError("The cluster API could not be reached with container orchestrator {}: {}".format(
            orchestrator.name, exc))

    warnings = []

    def items(label, path):
        try:
            return _kube_items(kube, path)
        except Exception as exc:
            logger.warning("Reading %s in %s failed: %s", label, namespace, exc)
            warnings.append("{} could not be read: {}".format(label, exc))
            return []

    try:
        try:
            quota, other_quotas = _quota_rows(ns.quota_usage(resource), ns.QUOTA_NAME)
        except Exception as exc:
            logger.warning("Reading the resource quotas of %s failed: %s", namespace, exc)
            warnings.append("Resource quotas could not be read: {}".format(exc))
            quota, other_quotas = [], []

        path = "/namespaces/{}".format(namespace)
        workloads = (
            [_workload_row("Deployment", d) for d in items("Deployments", "/apis/apps/v1" + path + "/deployments")]
            + [_workload_row("StatefulSet", s) for s in items("StatefulSets", "/apis/apps/v1" + path + "/statefulsets")]
            + [_workload_row("DaemonSet", s) for s in items("DaemonSets", "/apis/apps/v1" + path + "/daemonsets")]
        )
        workloads.sort(key=lambda r: (r["kind"], r["name"]))
        pods = sorted((_pod_row(p) for p in items("Pods", "/api/v1" + path + "/pods")), key=lambda r: r["name"])
        endpoints = (
            [_service_row(s) for s in items("Services", "/api/v1" + path + "/services")]
            + [_ingress_row(i) for i in items("Ingresses", "/apis/networking.k8s.io/v1" + path + "/ingresses")]
        )
        claims = sorted(
            (_claim_row(c) for c in items("Volume claims", "/api/v1" + path + "/persistentvolumeclaims")),
            key=lambda r: r["name"],
        )
        events, warning_count = _event_rows(items("Events", "/api/v1" + path + "/events"))
    finally:
        kube.close()

    return {
        "namespace": namespace,
        "cluster_name": cluster_name,
        "quota": quota,
        "other_quotas": other_quotas,
        "workloads": workloads,
        "workloads_ready": sum(1 for w in workloads if w["status"]["style"] in ("ok", "muted")),
        "pods": pods,
        "pods_running": sum(1 for p in pods if p["running"]),
        "pods_failing": sum(1 for p in pods if p["status"]["style"] == "bad"),
        "endpoints": endpoints,
        "claims": claims,
        "events": events,
        "warning_count": warning_count,
        "warnings": warnings,
    }


def namespace_workloads_panel(request, resource_id):
    return _panel(request, resource_id, "namespace_workloads_panel.html", _namespace_workloads_context)


# ---------------------------------------------------------------------------
# Dialogs that start the blueprint's resource actions as jobs
# ---------------------------------------------------------------------------
def _dialog_message(title, text, css="text-danger"):
    return {"title": title, "content": format_html('<p class="{}">{}</p>', css, text)}


def _dialog_guard(request, resource_id, name, title, action_name):
    """Checks every action dialog shares: the viewer may see the resource,
    the name is well formed, and the action is available to the viewer.
    Returns (resource, profile, action, refusal) where refusal is a dialog
    context or None."""
    resource = get_object_or_404(Resource, pk=resource_id)
    profile = request.get_user_profile()
    if not _can_view(profile, resource):
        return resource, profile, None, _dialog_message(
            title, "You do not have permission to view this resource."
        )
    if not POOL_NAME_RE.match(name or ""):
        return resource, profile, None, _dialog_message(title, "Invalid node pool name.")
    action = _action_for(profile, resource, action_name)
    if action is None:
        return resource, profile, None, _dialog_message(
            title,
            "The {} action is not available on this resource: it is not synced, not on the "
            "resource's blueprint, or you are not allowed to run it.".format(action_name),
        )
    return resource, profile, action, None


def _run_action(request, profile, resource, action, values, what):
    """Start the resource action as a job (or an order when it requires
    approval) with the given input values, the same path CloudBolt's own
    action dialog takes, and report the outcome as a page message."""
    try:
        context = get_hook_action_input_dict(action.hook, values, input_names_clean=True)
        result = action.run_hook_as_job(owner=profile, resources=[resource], context=context)
    except Exception as exc:
        logger.exception("%s failed to start on resource %s", what, resource.id)
        messages.error(request, "{} could not be started: {}".format(what, exc))
        return

    if isinstance(result, (list, tuple)):
        jobs = [job for job in result if hasattr(job, "get_absolute_url")]
        if jobs:
            links = format_html_join(
                ", ", '<a href="{}">Job {}</a>', ((job.get_absolute_url(), job.id) for job in jobs)
            )
            messages.info(request, format_html("{} started: {}.", what, links))
        else:
            messages.error(
                request,
                "{} did not start a job; check the action's configuration and the CloudBolt log.".format(what),
            )
        return

    order = getattr(result, "order", None)
    if order is not None:
        messages.info(
            request,
            format_html(
                '{} was added to <a href="{}">Order {}</a>, which needs approval before it runs.',
                what, order.get_absolute_url(), order.id,
            ),
        )
    else:
        messages.info(request, "{} was submitted.".format(what))


def _redirect_back(request, resource):
    return HttpResponseRedirect(request.META.get("HTTP_REFERER") or resource.get_absolute_url())


# Shows Nodes per zone for Manual and the minimum and maximum for Autoscale.
# Crispy wraps each field in div#div_id_<name>; without it every field stays
# visible, which still works.
SCALE_FORM_SCRIPT = mark_safe(
    "<script>(function () {"
    "var $m = $('#id_scale_method'); if (!$m.length) { return; }"
    "function row(id) { var $f = $('#' + id), $w = $f.closest('[id^=\"div_id_\"]');"
    " return $w.length ? $w : $f.parent(); }"
    "function sync() { var manual = $m.val() === 'Manual';"
    " row('id_node_count').toggle(manual); row('id_min_count').toggle(!manual);"
    " row('id_max_count').toggle(!manual); }"
    "$m.on('change', sync); sync();"
    "})();</script>"
)


def _nodes_per_zone(resource, pool_name, zones, fallback):
    """The pool's current nodes per zone, from the cluster's Kubernetes nodes, else ``fallback``."""
    if not zones:
        return fallback
    try:
        kube = _kube(resource)
        try:
            total, _ = _nodes_by_pool(_kube_items(kube, "/api/v1/nodes"))
        finally:
            kube.close()
    except Exception as exc:
        logger.info("Node count of pool %s not read for resource %s: %s", pool_name, resource.id, exc)
        return fallback
    count = total.get(pool_name)
    if count is None:
        return fallback
    return int(round(float(count) / len(zones)))


@dialog_view
def scale_node_pool(request, resource_id, pool_name):
    """Change a node pool's scale method and nodes per zone through the Scale
    GKE Node Pool action, prefilled from Google Cloud.
    https://cloud.google.com/kubernetes-engine/docs/how-to/node-pools#resizing_a_node_pool
    https://cloud.google.com/kubernetes-engine/docs/concepts/cluster-autoscaler"""
    title = "Scale node pool {}".format(pool_name)
    resource, profile, action, refusal = _dialog_guard(
        request, resource_id, pool_name, title, ACTION_SCALE_POOL
    )
    if refusal:
        return refusal
    try:
        # https://cloud.google.com/kubernetes-engine/docs/reference/rest/v1/projects.locations.clusters/get
        gke = _gke()
        data = gke.cluster_for_resource(resource).get()
    except PanelError as exc:
        return _dialog_message(title, str(exc))
    except Exception as exc:
        if not _is_gke_error(exc):
            raise
        return _dialog_message(title, "Google Cloud could not read the cluster: {}".format(exc))

    pools = data.get("nodePools") or []
    pool = next((p for p in pools if p.get("name") == pool_name), None)
    if pool is None:
        return _dialog_message(title, "The cluster has no node pool named {}.".format(pool_name))

    autoscaling = pool.get("autoscaling") or {}
    autoscale = bool(autoscaling.get("enabled"))
    zones = gke.pool_zones(pool, data)
    count = _nodes_per_zone(resource, pool_name, zones, pool.get("initialNodeCount") or 0)
    only_pool = len(pools) <= 1
    initial = {
        "scale_method": "Autoscale" if autoscale else "Manual",
        "node_count": count,
        # A pool autoscaled with whole-pool limits has no per-zone ones; the dialog sets per-zone limits.
        "min_count": autoscaling.get("minNodeCount", 0) if autoscale else count,
        "max_count": (autoscaling.get("maxNodeCount") if autoscale else None) or max(count, 1),
    }
    if request.method == "POST":
        form = ScaleNodePoolForm(request.POST, initial=initial, only_pool=only_pool)
        if form.is_valid():
            data = form.cleaned_data
            method = data["scale_method"]
            if method == "Manual":
                node_count = data["node_count"]
                min_count = data.get("min_count") if data.get("min_count") is not None else node_count
                max_count = data.get("max_count") if data.get("max_count") is not None else max(node_count, 1)
            else:
                min_count, max_count = data["min_count"], data["max_count"]
                # Every input gets a value; the action reads the method to
                # decide which ones apply.
                node_count = min(max(count or min_count, min_count), max_count)
            values = {
                "gke_scale_pool": pool_name,
                "gke_scale_method": method,
                "gke_scale_node_count": node_count,
                "gke_scale_min_count": min_count,
                "gke_scale_max_count": max_count,
            }
            _run_action(request, profile, resource, action, values, "Scaling node pool {}".format(pool_name))
            return _redirect_back(request, resource)
    else:
        form = ScaleNodePoolForm(initial=initial, only_pool=only_pool)

    zone_text = format_html("{} zone{}", len(zones), "" if len(zones) == 1 else "s") if zones else "its zones"
    if autoscale:
        now = format_html(
            "autoscales between <b>{}</b> and <b>{}</b> nodes per zone in {} and runs about {} per zone now",
            autoscaling.get("minNodeCount", 0), autoscaling.get("maxNodeCount"), zone_text, count,
        )
    else:
        now = format_html("runs <b>{}</b> node{} per zone in {}, scaled manually",
                          count, "" if count == 1 else "s", zone_text)
    content = format_html(
        "<p>Node pool <b>{}</b> {}. The change runs as a CloudBolt job through the <b>{}</b> action.</p>",
        pool_name, now, action.label,
    )
    return {
        "title": title,
        "content": content,
        "form": form,
        "bottom_content": SCALE_FORM_SCRIPT,
        "action_url": reverse("google_gke_scale_node_pool", args=[resource.id, pool_name]),
        "use_ajax": True,
        "submit": "Scale",
    }


# Operations of the Manage GKE Cluster Groups action.
GROUPS_ADD = "add"
GROUPS_REMOVE = "remove"
GROUPS_ALLOW_ALL = "allow_all"


def _groups_dialog_guard(request, resource_id, title):
    """Checks the group dialogs share: the viewer may see the resource and
    run Manage GKE Cluster Groups on it, and the cluster has an environment.
    Returns (resource, profile, action, environment, refusal) where refusal
    is a dialog context or None."""
    resource = get_object_or_404(Resource, pk=resource_id)
    profile = request.get_user_profile()
    if not _can_view(profile, resource):
        return resource, profile, None, None, _dialog_message(
            title, "You do not have permission to view this resource."
        )
    action = _action_for(profile, resource, ACTION_MANAGE_GROUPS)
    if action is None:
        return resource, profile, None, None, _dialog_message(
            title,
            "The {} action is not available on this resource: it is not synced, not on the "
            "resource's blueprint, or you are not allowed to run it.".format(ACTION_MANAGE_GROUPS),
        )
    try:
        env = _cluster_environment(resource)
    except PanelError as exc:
        return resource, profile, None, None, _dialog_message(title, str(exc))
    return resource, profile, action, env, None


@dialog_view
def add_cluster_groups(request, resource_id):
    """Entitle more CloudBolt groups to the cluster's environment."""
    title = "Add groups"
    resource, profile, action, env, refusal = _groups_dialog_guard(request, resource_id, title)
    if refusal:
        return refusal
    gke = _gke()
    state, entitled = gke.environment_groups(env)
    entitled_ids = {group.id for group in entitled}
    choices = [(g.global_id, g.name) for g in gke.assignable_groups() if g.id not in entitled_ids]
    if not choices:
        return _dialog_message(title, "Every CloudBolt group is already entitled to this cluster.", css="")

    if request.method == "POST":
        form = AddClusterGroupsForm(request.POST, choices=choices)
        if form.is_valid():
            values = {"gke_groups_operation": GROUPS_ADD, "gke_groups": form.cleaned_data["groups"]}
            _run_action(request, profile, resource, action, values, "Adding groups")
            return _redirect_back(request, resource)
    else:
        form = AddClusterGroupsForm(choices=choices)

    if state == gke.UNCONSTRAINED:
        now = mark_safe(
            '<p class="text-danger">Every group may order namespaces on this cluster now. Once you add '
            "groups, only those groups (and their subgroups) may.</p>"
        )
    elif state == gke.RESTRICTED:
        now = mark_safe("<p>The groups you add replace CloudBolt's Unassigned placeholder.</p>")
    else:
        now = ""
    content = format_html(
        "<p>The groups you choose may order namespaces on cluster <b>{}</b> (environment <b>{}</b>). "
        "The change runs as a CloudBolt job through the <b>{}</b> action.</p>{}",
        _cluster_name(resource), env.name, action.label, now,
    )
    return {
        "title": title,
        "content": content,
        "form": form,
        "action_url": reverse("google_gke_add_cluster_groups", args=[resource.id]),
        "use_ajax": True,
        "submit": "Add",
    }


@dialog_view
def remove_cluster_group(request, resource_id, group_id):
    """Take one group off the cluster's environment."""
    title = "Remove group"
    resource, profile, action, env, refusal = _groups_dialog_guard(request, resource_id, title)
    if refusal:
        return refusal
    entitled = _gke().environment_groups(env)[1]
    group = next((g for g in entitled if g.global_id == group_id), None)
    if group is None:
        return _dialog_message(title, "That group is not entitled to this cluster.")

    if request.method == "POST":
        form = ConfirmForm(request.POST)
        if form.is_valid():
            values = {"gke_groups_operation": GROUPS_REMOVE, "gke_groups": [group.global_id]}
            _run_action(request, profile, resource, action, values, "Removing group {}".format(group.name))
            return _redirect_back(request, resource)
    else:
        form = ConfirmForm()

    content = format_html(
        "<p>Group <b>{}</b> will no longer be able to order namespaces on cluster <b>{}</b>. Namespaces it "
        "already ordered stay. The change runs as a CloudBolt job through the <b>{}</b> action.</p>",
        group.name, _cluster_name(resource), action.label,
    )
    if len(entitled) == 1:
        content += format_html(
            '<p class="text-danger"><b>{}</b> is the last group entitled. Removing it leaves environment '
            "<b>{}</b> with no groups, so <b>every group</b> may order namespaces on this cluster.</p>",
            group.name, env.name,
        )
    return {
        "title": title,
        "content": content,
        "form": form,
        "action_url": reverse("google_gke_remove_cluster_group", args=[resource.id, group.global_id]),
        "use_ajax": True,
        "submit": "Remove",
    }


@dialog_view
def allow_all_cluster_groups(request, resource_id):
    """Remove every group from the cluster's environment, so every group may order namespaces."""
    title = "Allow all groups"
    resource, profile, action, env, refusal = _groups_dialog_guard(request, resource_id, title)
    if refusal:
        return refusal
    gke = _gke()
    state, entitled = gke.environment_groups(env)
    if state == gke.UNCONSTRAINED:
        return _dialog_message(title, "Every group may already order namespaces on this cluster.", css="")

    if request.method == "POST":
        form = ConfirmForm(request.POST)
        if form.is_valid():
            values = {"gke_groups_operation": GROUPS_ALLOW_ALL}
            _run_action(request, profile, resource, action, values, "Allowing all groups")
            return _redirect_back(request, resource)
    else:
        form = ConfirmForm()

    if entitled:
        removed = format_html_join(", ", "<b>{}</b>", ((g.name,) for g in entitled))
    else:
        removed = mark_safe("CloudBolt's Unassigned placeholder")
    content = format_html(
        '<p class="text-danger"><b>Every CloudBolt group</b> will be able to order namespaces on cluster '
        "<b>{}</b>.</p><p>This removes {} from environment <b>{}</b>, leaving it unconstrained. The change "
        "runs as a CloudBolt job through the <b>{}</b> action.</p>",
        _cluster_name(resource), removed, env.name, action.label,
    )
    return {
        "title": title,
        "content": content,
        "form": form,
        "action_url": reverse("google_gke_allow_all_cluster_groups", args=[resource.id]),
        "use_ajax": True,
        "submit": "Allow all groups",
    }


# ---------------------------------------------------------------------------
# Tabs
# ---------------------------------------------------------------------------
class ClusterTabDelegate(TabExtensionDelegate):
    """Kubernetes Cluster resources of the Google Kubernetes Engine blueprint."""

    def should_display(self):
        return bool(_attr(self.instance, CLUSTER_MARKER_FIELD))


class NamespaceTabDelegate(TabExtensionDelegate):
    """Namespace landing zones of the GKE Namespace blueprint."""

    def should_display(self):
        resource_type = getattr(self.instance, "resource_type", None)
        return (
            getattr(resource_type, "name", "") == NAMESPACE_RESOURCE_TYPE
            and bool(_attr(self.instance, NAMESPACE_MARKER_FIELD))
        )


def _tab_context(resource):
    """Identity shown above the panels, from the resource's custom fields; the
    console link is built locally (no Google Cloud call)."""
    project = _attr(resource, "gke_project_id") or ""
    location = _attr(resource, "gke_location") or ""
    console = ""
    if project and location:
        try:
            console = _gke().console_url(project, location, _cluster_name(resource))
        except PanelError as exc:
            # The panel explains a missing module.
            logger.info("No console link for resource %s: %s", resource.id, exc)
    return {
        "resource": resource,
        "cluster_name": _cluster_name(resource),
        "project": project,
        "location": location,
        "environment_name": _environment_name(resource),
        "console_url": console,
    }


@tab_extension(
    model=Resource,
    title="Cluster",
    delegate=ClusterTabDelegate,
    description="Live GKE cluster status, basics, networking, security, upgrades, and features",
)
def gke_cluster_tab(request, obj_id):
    resource = get_object_or_404(Resource, pk=obj_id)
    context = _tab_context(resource)
    context["cluster_url"] = reverse("google_gke_cluster_panel", args=[resource.id])
    return render(request, TEMPLATES + "cluster_tab.html", context)


@tab_extension(
    model=Resource,
    title="Node pools",
    delegate=ClusterTabDelegate,
    description="GKE node pools and nodes with Scale, Upgrade, and Delete actions",
)
def gke_node_pools_tab(request, obj_id):
    resource = get_object_or_404(Resource, pk=obj_id)
    profile = request.get_user_profile()
    context = _tab_context(resource)
    context.update({
        "node_pools_url": reverse("google_gke_node_pools_panel", args=[resource.id]),
        "add_url": _action_url(profile, resource, ACTION_ADD_POOL),
        "list_url": _action_url(profile, resource, ACTION_LIST_POOLS),
    })
    return render(request, TEMPLATES + "node_pools_tab.html", context)


@tab_extension(
    model=Resource,
    title="Workloads",
    delegate=ClusterTabDelegate,
    description="Kubernetes nodes and namespaces read with the cluster's container orchestrator credentials",
)
def gke_workloads_tab(request, obj_id):
    resource = get_object_or_404(Resource, pk=obj_id)
    context = _tab_context(resource)
    orchestrator = _orchestrator(resource)
    orchestrator_url = ""
    if orchestrator is not None:
        try:
            orchestrator_url = orchestrator.get_absolute_url()
        except Exception:
            orchestrator_url = ""
    context.update({
        "workloads_url": reverse("google_gke_workloads_panel", args=[resource.id]),
        "orchestrator_name": orchestrator.name if orchestrator is not None else "",
        "orchestrator_url": orchestrator_url,
        "create_namespace_url": _action_url(
            request.get_user_profile(), resource, ACTION_CREATE_NAMESPACE
        ),
    })
    return render(request, TEMPLATES + "workloads_tab.html", context)


@tab_extension(
    model=Resource,
    title="Groups",
    delegate=ClusterTabDelegate,
    description="CloudBolt groups that may order namespaces on a GKE cluster, with Add, Remove, and Allow all groups",
)
def gke_groups_tab(request, obj_id):
    resource = get_object_or_404(Resource, pk=obj_id)
    profile = request.get_user_profile()
    context = _tab_context(resource)
    context.update({
        "groups_url": reverse("google_gke_cluster_groups_panel", args=[resource.id]),
        "add_url": reverse("google_gke_add_cluster_groups", args=[resource.id])
        if _action_for(profile, resource, ACTION_MANAGE_GROUPS) else "",
    })
    return render(request, TEMPLATES + "groups_tab.html", context)


def _namespace_tab_context(resource):
    return {
        "resource": resource,
        "namespace": _attr(resource, NAMESPACE_MARKER_FIELD) or resource.name,
        "cluster_name": _attr(resource, "gke_namespace_cluster") or "",
    }


@tab_extension(
    model=Resource,
    title="Connect",
    delegate=NamespaceTabDelegate,
    description="gcloud and kubectl commands, a kubeconfig, and the Google identities with access to a GKE namespace",
)
def gke_namespace_connect_tab(request, obj_id):
    resource = get_object_or_404(Resource, pk=obj_id)
    profile = request.get_user_profile()
    context = _namespace_tab_context(resource)
    context.update({
        "connect_url": reverse("google_gke_namespace_connect_panel", args=[resource.id]),
        "manage_access_url": _action_url(profile, resource, ACTION_MANAGE_NAMESPACE_ACCESS),
        "pipeline_url": _action_url(profile, resource, ACTION_MANAGE_PIPELINE_IDENTITY),
    })
    return render(request, TEMPLATES + "connect_tab.html", context)


@tab_extension(
    model=Resource,
    title="Workloads",
    delegate=NamespaceTabDelegate,
    description="Read-only inventory of one GKE namespace: quota usage, workloads, pods, services, storage, and warning events",
)
def gke_namespace_workloads_tab(request, obj_id):
    resource = get_object_or_404(Resource, pk=obj_id)
    context = _namespace_tab_context(resource)
    context.update({
        "workloads_url": reverse("google_gke_namespace_workloads_panel", args=[resource.id]),
        "quota_change_url": _action_url(
            request.get_user_profile(), resource, ACTION_REQUEST_QUOTA_CHANGE
        ),
    })
    return render(request, TEMPLATES + "namespace_workloads_tab.html", context)
