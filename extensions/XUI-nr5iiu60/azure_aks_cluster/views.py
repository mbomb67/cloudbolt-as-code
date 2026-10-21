"""
Azure AKS Cluster XUI

Adds four tabs to the Kubernetes Cluster resources of the Azure Kubernetes
Service blueprint (BP-20q58q9z) -- any resource carrying an aks_cluster_name
custom field. The AWS EKS Cluster extension gates on eks_cluster_name, so the
two never show on the same resource.

- Cluster: the status the Azure portal shows ("Succeeded (Running)"), pricing
  tier and Kubernetes version support in the header; the portal's Essentials
  (resource group, status, location, subscription, Kubernetes version, API
  server address, network configuration, node pools, pricing tier); property
  cards for Kubernetes, Networking, and Security; an Upgrades card with the
  control-plane versions AKS offers and an Upgrade button (Upgrade AKS
  Version); the add-ons and cluster extensions installed; the jobs running on
  the resource; and an Open in Azure portal link.
- Node pools: every agent pool with the portal's columns (provisioning and
  power state, scale method, target and Ready nodes, autoscale range, mode,
  operating system, Kubernetes and node image version, node size, zones) and
  Scale, Upgrade, and Delete buttons, plus the Kubernetes nodes with their
  status, pool, versions, zone, and address. Add node pool and Refresh (List
  AKS Node Pools) in the toolbar.
- Workloads: read through the cluster's container orchestrator: nodes with
  readiness, pressure conditions, CPU and memory requests against allocatable
  capacity and pod counts, plus namespaces with pod, deployment and service
  counts, each namespace landing zone linked to its CloudBolt resource, and a
  Create namespace button (Create AKS Namespace).
- Groups: the CloudBolt groups entitled to the cluster's environment, which
  may order namespaces on the cluster, shown as CloudBolt's own environment
  Groups tab does (no groups is Unconstrained; only the Unassigned
  placeholder is Unassigned), with Add groups, Remove, and Allow all groups
  (Manage AKS Cluster Groups).

Namespace landing zones of the AKS Namespace blueprint (kubernetes_namespace
resources carrying aks_namespace_name; the EKS extension gates on
eks_namespace_name) get two tabs, built by the aks_namespace shared module
(SHM-cozv547n). Each landing zone is an AKS managed namespace.

- Connect: where the namespace lives, the Microsoft Entra principals whose
  Azure RBAC roles cover it (object IDs with their type; display names are
  not looked up), the GitHub Actions pipeline identities with a workflow
  each, the az aks namespace get-credentials and kubelogin commands, and the
  kubeconfig when it holds no secret; Manage access (Manage AKS Namespace
  Access) and Pipeline identity (Manage AKS Namespace Pipeline Identity)
  buttons.
- Workloads: a read-only inventory of the namespace read through the
  cluster's orchestrator: usage of the AKS-managed CPU and memory quota and
  of CloudBolt's storage, claim and pod quota, Deployments, StatefulSets and
  DaemonSets, pods, services and ingresses, persistent volume claims, and
  recent warning events; Request quota change (Request AKS Namespace Quota
  Change). No logs, Secrets or ConfigMaps, and no buttons that change the
  cluster.

Every button starts one of the blueprints' resource actions as a CloudBolt
job (Upgrade AKS Version, Add, Scale, Upgrade, Delete and List AKS Node
Pools, Create AKS Namespace, Manage AKS Cluster Groups, and the namespace
actions above), so approvals,
permissions and job history apply. Buttons appear only when the action is synced, applies to the
resource's blueprint and the viewer may run it (resource_action_permitted),
and the job runs as the viewer.

Azure is read through the azure_aks shared module (SHM-ptvp0km4) with the
service principal of the environment the cluster was ordered into (env_id),
or of the handler a discovered cluster records (azure_rh_id); the handler is
never shown. The module is imported inside each view, so a CloudBolt without
it shows an alert instead of failing to load the extension. Kubernetes reads
go through the Kubernetes container orchestrator the blueprint registers
(container_orchestrator_id), which signs in with the cluster's admin client
certificate. Both run under a service identity, not the viewer's, so every
endpoint re-checks the viewer's CloudBolt permission on the resource. Panels
load asynchronously so a slow Azure or cluster API never stalls the page.

Vendor API references (each call site cites its own page):
    https://learn.microsoft.com/en-us/rest/api/aks/managed-clusters/get
    https://learn.microsoft.com/en-us/rest/api/aks/managed-clusters/get-upgrade-profile
    https://learn.microsoft.com/en-us/rest/api/aks/managed-clusters/list-kubernetes-versions
    https://learn.microsoft.com/en-us/rest/api/aks/agent-pools/list
    https://learn.microsoft.com/en-us/rest/api/aks/agent-pools/get
    https://learn.microsoft.com/en-us/rest/api/aks/agent-pools/get-upgrade-profile
    https://learn.microsoft.com/en-us/rest/api/kubernetesconfiguration/extensions/extensions/list
    https://learn.microsoft.com/en-us/rest/api/resources/subscriptions/get
    https://learn.microsoft.com/en-us/rest/api/aks/managed-namespaces
    https://learn.microsoft.com/en-us/azure/aks/managed-namespaces
    https://learn.microsoft.com/en-us/azure/aks/upgrade-aks-cluster
    https://learn.microsoft.com/en-us/azure/aks/supported-kubernetes-versions
    https://learn.microsoft.com/en-us/azure/aks/integrations
    https://learn.microsoft.com/en-us/azure/aks/cluster-extensions
    https://learn.microsoft.com/en-us/azure/aks/manage-node-pools
    https://kubernetes.io/docs/reference/kubernetes-api/
"""

import importlib
import re
from datetime import datetime, timezone

from django.contrib import messages
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

from xui.azure_aks_cluster.forms import AddClusterGroupsForm, ConfirmForm, ScaleNodePoolForm

logger = ThreadLogger(__name__)

TEMPLATES = "azure_aks_cluster/templates/"

# The azure_aks (SHM-ptvp0km4) and aks_namespace (SHM-cozv547n) shared
# modules, imported lazily by _aks() and _aks_namespace().
SHARED_MODULE = "shared_modules.azure_aks"
NAMESPACE_MODULE = "shared_modules.aks_namespace"

# Viewing the tabs needs VIEW_PERMISSION on the resource. Every button maps
# to a resource action and is shown only when resource_action_permitted
# allows that action for the viewer.
VIEW_PERMISSION = "resource.view"

# Plugin (hook) names of the Azure Kubernetes Service blueprint's day-2 actions.
ACTION_UPGRADE_VERSION = "Upgrade AKS Version"
ACTION_ADD_POOL = "Add AKS Node Pool"
ACTION_DELETE_POOL = "Delete AKS Node Pool"
ACTION_LIST_POOLS = "List AKS Node Pools"
ACTION_SCALE_POOL = "Scale AKS Node Pool"
ACTION_UPGRADE_POOL = "Upgrade AKS Node Pool"
ACTION_CREATE_NAMESPACE = "Create AKS Namespace"
ACTION_MANAGE_GROUPS = "Manage AKS Cluster Groups"
# ...and of the AKS Namespace blueprint's.
ACTION_MANAGE_NAMESPACE_ACCESS = "Manage AKS Namespace Access"
ACTION_MANAGE_PIPELINE_IDENTITY = "Manage AKS Namespace Pipeline Identity"
ACTION_REQUEST_QUOTA_CHANGE = "Request AKS Namespace Quota Change"

# Custom fields that mark the resources the tabs belong on.
CLUSTER_MARKER_FIELD = "aks_cluster_name"
NAMESPACE_MARKER_FIELD = "aks_namespace_name"
NAMESPACE_RESOURCE_TYPE = "kubernetes_namespace"

# Node pool names: Linux pools 1-12 lowercase letters and digits starting
# with a letter (Windows pools 1-6, which the same pattern admits).
# https://learn.microsoft.com/en-us/azure/aks/create-node-pools#limitations
POOL_NAME_RE = re.compile(r"^[a-z][a-z0-9]{0,11}$")

# autoUpgradeProfile.upgradeChannel, labelled as the portal's Automatic
# upgrade setting. https://learn.microsoft.com/en-us/azure/aks/auto-upgrade-cluster
UPGRADE_CHANNELS = {
    "patch": "Enabled with patch",
    "stable": "Enabled with stable",
    "rapid": "Enabled with rapid",
    "node-image": "Enabled with node image",
    "none": "Disabled",
}

# autoUpgradeProfile.nodeOSUpgradeChannel, the portal's Node security channel type.
# https://learn.microsoft.com/en-us/rest/api/aks/managed-clusters/get#nodeosupgradechannel
NODE_OS_CHANNELS = {
    "None": "None",
    "Unmanaged": "Unmanaged",
    "NodeImage": "Node Image",
    "SecurityPatch": "Security Patch",
}

# properties.supportPlan. https://learn.microsoft.com/en-us/rest/api/aks/managed-clusters/get#kubernetessupportplan
SUPPORT_PLANS = {
    "KubernetesOfficial": "Standard (Kubernetes community support)",
    "AKSLongTermSupport": "Long-term support (LTS)",
}

# networkProfile enums. https://learn.microsoft.com/en-us/rest/api/aks/managed-clusters/get#networkpolicy
NETWORK_POLICIES = {"none": "None", "calico": "Calico", "azure": "Azure", "cilium": "Cilium"}
NETWORK_DATAPLANES = {"azure": "Azure", "cilium": "Cilium"}
OUTBOUND_TYPES = {
    "loadBalancer": "Load balancer",
    "userDefinedRouting": "User-defined routing",
    "managedNATGateway": "Managed NAT gateway",
    "userAssignedNATGateway": "User-assigned NAT gateway",
    "none": "None",
}
LOAD_BALANCER_SKUS = {"standard": "Standard", "basic": "Basic"}

# Cluster extension types with the names the cluster extensions page uses;
# any other type is shown as AKS reports it.
# https://learn.microsoft.com/en-us/azure/aks/cluster-extensions#currently-available-extensions
EXTENSION_TYPES = {
    "microsoft.flux": "Flux (GitOps)",
    "microsoft.dapr": "Dapr",
    "microsoft.dataprotection.kubernetes": "Azure Backup for AKS",
    "microsoft.azureml.kubernetes": "Azure Machine Learning",
}

# Kubernetes node conditions that signal pressure when their status is True.
PRESSURE_CONDITIONS = ("MemoryPressure", "DiskPressure", "PIDPressure", "NetworkUnavailable")

# Node labels AKS and Kubernetes apply.
# https://learn.microsoft.com/en-us/azure/aks/use-labels
# https://kubernetes.io/docs/reference/labels-annotations-taints/
LABEL_POOL = ("kubernetes.azure.com/agentpool", "agentpool")
LABEL_NODE_IMAGE = ("kubernetes.azure.com/node-image-version",)
LABEL_INSTANCE_TYPE = ("node.kubernetes.io/instance-type", "beta.kubernetes.io/instance-type")
LABEL_ZONE = ("topology.kubernetes.io/zone", "failure-domain.beta.kubernetes.io/zone")


class PanelError(Exception):
    """A readable problem the panel shows instead of a traceback."""


# ---------------------------------------------------------------------------
# Shared module, resource and permission helpers
# ---------------------------------------------------------------------------
def _aks():
    """The azure_aks shared module, or PanelError when it is not synced."""
    try:
        return importlib.import_module(SHARED_MODULE)
    except ImportError:
        raise PanelError("The azure_aks shared module (SHM-ptvp0km4) is not on this CloudBolt.")


def _aks_namespace():
    """The aks_namespace shared module, or PanelError when it (or azure_aks) is not synced."""
    try:
        return importlib.import_module(NAMESPACE_MODULE)
    except ImportError:
        raise PanelError(
            "The aks_namespace shared module (SHM-cozv547n), or the azure_aks module it needs "
            "(SHM-ptvp0km4), is not on this CloudBolt."
        )


def _is_aks_error(exc):
    try:
        return isinstance(exc, _aks().AKSError)
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


def _action_for(profile, resource, hook_name):
    """The enabled resource action whose plugin is named hook_name, when it
    applies to the resource's blueprint and the viewer may run it on this
    resource (the checks the resource page's action sidebar applies), else
    None."""
    try:
        for action in ResourceAction.objects.filter(enabled=True, hook__name=hook_name):
            blueprint_ids = list(action.blueprints.values_list("id", flat=True))
            if blueprint_ids and resource.blueprint_id not in blueprint_ids:
                continue
            if not resource_action_permitted(profile, resource, action):
                continue
            return action
    except Exception:
        logger.exception("Could not resolve the '%s' action for resource %s", hook_name, resource.id)
    return None


def _action_url(profile, resource, hook_name):
    """CloudBolt's own run dialog for the action, or '' when unavailable."""
    action = _action_for(profile, resource, hook_name)
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


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------
def _pill(text, style, icon, spin=False):
    return {"text": text, "style": style, "icon": icon, "spin": spin}


def _cluster_status(provisioning_state, power_state):
    """The portal's cluster status, "Succeeded (Running)", as a pill."""
    state = provisioning_state or ""
    power = power_state or ""
    label = "{} ({})".format(state, power) if state and power else (state or power or "Unknown")
    if state == "Succeeded":
        if power == "Stopped":
            return _pill("Stopped", "muted", "bi-stop-circle")
        return _pill(label, "ok", "bi-check-circle-fill")
    if state == "Failed":
        return _pill(label, "bad", "bi-x-circle-fill")
    if state in ("Canceled", "Cancelled"):
        return _pill(label, "warn", "bi-exclamation-triangle-fill")
    # Creating, Updating, Upgrading, Scaling, Starting, Stopping, Deleting,
    # and any state AKS adds later: an operation is running.
    # https://learn.microsoft.com/en-us/rest/api/aks/managed-clusters/get
    return _pill(label, "live", "bi-arrow-repeat", spin=True)


def _provisioning_pill(state):
    state = state or ""
    if state == "Succeeded":
        return _pill(state, "ok", "bi-check-circle-fill")
    if state == "Failed":
        return _pill(state, "bad", "bi-x-circle-fill")
    if state in ("Canceled", "Cancelled"):
        return _pill(state, "warn", "bi-exclamation-triangle-fill")
    return _pill(state or "Unknown", "live", "bi-arrow-repeat", spin=True)


def _power_pill(code):
    if code == "Running":
        return _pill("Running", "ok", "bi-play-circle-fill")
    if code == "Stopped":
        return _pill("Stopped", "muted", "bi-stop-circle")
    return _pill(code or "Unknown", "muted", "bi-question-circle")


def _enabled(value):
    return "Enabled" if value else "Disabled"


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


def _minor_parts(version):
    """(major, minor) of '1.33' or '1.33.2', or None."""
    parts = str(version or "").split(".")
    try:
        return int(parts[0]), int(parts[1])
    except (IndexError, ValueError):
        return None


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
# Cluster tab: managed cluster properties as the Azure portal groups them
# ---------------------------------------------------------------------------
def _network_configuration(network, pools):
    """The portal's Network configuration name and, for Cilium, the IPAM mode.
    https://learn.microsoft.com/en-us/azure/aks/concepts-network-cni-overview
    https://learn.microsoft.com/en-us/azure/aks/azure-cni-powered-by-cilium"""
    plugin = str(network.get("networkPlugin") or "").lower()
    overlay = str(network.get("networkPluginMode") or "").lower() == "overlay"
    pod_subnet = any(pool.get("podSubnetID") for pool in pools)
    if plugin == "kubenet":
        return "Kubenet", ""
    if plugin == "none":
        return "Bring your own CNI", ""
    if plugin != "azure":
        return network.get("networkPlugin") or "", ""
    ipam = "Overlay" if overlay else "Pod Subnet" if pod_subnet else "Node Subnet"
    if str(network.get("networkDataplane") or "").lower() == "cilium":
        return "Azure CNI Powered by Cilium", "Azure CNI {}".format(ipam)
    return "Azure CNI {}".format(ipam), ""


def _authentication(props):
    """The portal's three Authentication and Authorization choices.
    https://learn.microsoft.com/en-us/azure/aks/concepts-identity"""
    aad = props.get("aadProfile") or {}
    if aad.get("managed") or aad.get("tenantID"):
        if aad.get("enableAzureRBAC"):
            return "Microsoft Entra ID authentication with Azure RBAC"
        return "Microsoft Entra ID authentication with Kubernetes RBAC"
    if props.get("enableRBAC") is False:
        return "Local accounts without Kubernetes RBAC"
    return "Local accounts with Kubernetes RBAC"


def _integration(name, enabled, detail=""):
    return {
        "name": name,
        "enabled": bool(enabled),
        "detail": detail if enabled else "",
    }


def _integrations(props):
    """The managed add-ons worth showing, read from the cluster properties.
    Virtual nodes, AGIC and Open Service Mesh are left out on purpose.
    https://learn.microsoft.com/en-us/azure/aks/integrations
    https://learn.microsoft.com/en-us/rest/api/aks/managed-clusters/get#managedclusteraddonprofile"""
    # addonProfiles keys are matched without regard to case.
    addons = {str(key).lower(): value or {} for key, value in (props.get("addonProfiles") or {}).items()}
    monitor = props.get("azureMonitorProfile") or {}
    insights = bool(addons.get("omsagent", {}).get("enabled") or (monitor.get("containerInsights") or {}).get("enabled"))
    prometheus = bool((monitor.get("metrics") or {}).get("enabled"))
    key_vault = addons.get("azurekeyvaultsecretsprovider", {})
    rotation = str((key_vault.get("config") or {}).get("enableSecretRotation") or "").lower() == "true"
    keda = ((props.get("workloadAutoScalerProfile") or {}).get("keda") or {}).get("enabled")
    routing = (props.get("ingressProfile") or {}).get("webAppRouting") or {}
    zones = len(routing.get("dnsZoneResourceIds") or [])
    mesh = props.get("serviceMeshProfile") or {}
    revisions = (mesh.get("istio") or {}).get("revisions") or []
    return [
        _integration(
            "Monitoring", insights or prometheus,
            ", ".join(name for name, on in (("Container insights", insights), ("Managed Prometheus", prometheus)) if on),
        ),
        _integration("Azure Policy", addons.get("azurepolicy", {}).get("enabled")),
        _integration("Azure Key Vault Secrets Provider", key_vault.get("enabled"),
                     "Secret rotation on" if rotation else ""),
        _integration("KEDA", keda, "Event-driven autoscaling"),
        _integration("Application routing", routing.get("enabled"),
                     "{} DNS zone{}".format(zones, "" if zones == 1 else "s") if zones else "Managed NGINX ingress"),
        _integration("Istio service mesh", mesh.get("mode") == "Istio",
                     "Revision {}".format(", ".join(revisions)) if revisions else ""),
    ]


def _extension_rows(extensions):
    """Cluster extensions with their installed versions.
    https://learn.microsoft.com/en-us/rest/api/kubernetesconfiguration/extensions/extensions/list"""
    rows = []
    for extension in extensions:
        props = extension.get("properties") or {}
        extension_type = props.get("extensionType") or ""
        rows.append({
            "name": extension.get("name") or "",
            "type": EXTENSION_TYPES.get(extension_type.lower(), extension_type),
            "extension_type": extension_type,
            "version": props.get("currentVersion") or props.get("version") or "",
            "status": _provisioning_pill(props.get("provisioningState")),
            # autoUpgradeMinorVersion defaults to true.
            "auto_upgrade": props.get("autoUpgradeMinorVersion") is not False,
            "release_train": props.get("releaseTrain") or "",
        })
    return sorted(rows, key=lambda r: r["name"].lower())


def _version_support(aks, arm, location, current, support_plan, warnings):
    """Where the cluster's minor version sits in the AKS support window: AKS
    supports the newest GA minor (N) and two before it; N-3 has platform
    support only; anything older is out of support. LTS versions on the
    long-term support plan stay supported.
    https://learn.microsoft.com/en-us/azure/aks/supported-kubernetes-versions
    https://learn.microsoft.com/en-us/rest/api/aks/managed-clusters/list-kubernetes-versions"""
    if not current or not location:
        return None
    try:
        versions = aks.kubernetes_versions(arm, location)
    except aks.AKSError as exc:
        warnings.append("Kubernetes version support could not be read: {}".format(exc))
        return None
    current_minor = aks.minor(current)
    entry = next((v for v in versions if aks.minor(v["version"]) == current_minor), None)
    community = [
        v for v in versions
        if not v["is_preview"] and (not v["support_plans"] or "KubernetesOfficial" in v["support_plans"])
    ]
    if entry and entry["is_preview"]:
        return {"pill": _pill("Preview", "live", "bi-exclamation-circle"), "hint": "Preview versions are not covered by AKS support"}
    if support_plan == "AKSLongTermSupport" and entry and "AKSLongTermSupport" in entry["support_plans"]:
        return {"pill": _pill("Long-term support", "ok", "bi-shield-check"), "hint": "Supported on the LTS plan"}
    if not community:
        return None
    newest = max((aks.minor(v["version"]) for v in community), key=aks.version_key)
    newest_parts, current_parts = _minor_parts(newest), _minor_parts(current_minor)
    if not newest_parts or not current_parts or newest_parts[0] != current_parts[0]:
        return None
    lag = newest_parts[1] - current_parts[1]
    if lag < 0:
        return {"pill": _pill("Preview", "live", "bi-exclamation-circle"), "hint": "Newer than the latest GA version, {}".format(newest)}
    if lag <= 2:
        return {
            "pill": _pill("Supported", "ok", "bi-shield-check"),
            "hint": "N, the latest GA version" if lag == 0 else "N-{} of {}".format(lag, newest),
        }
    if lag == 3:
        return {
            "pill": _pill("Platform support", "warn", "bi-shield-exclamation"),
            "hint": "N-3: Microsoft supports platform issues only; upgrade to stay supported",
        }
    return {
        "pill": _pill("Out of support", "bad", "bi-shield-x"),
        "hint": "Older than N-3; AKS upgrades the cluster when the platform support grace period ends",
    }


def _upgrades(aks, cluster, current, channel, pools, url, warnings):
    """Control-plane versions AKS offers this cluster and the node pools left
    behind it. AKS only lists the targets it allows (one minor version at a
    time unless the cluster is on LTS). While an automatic upgrade channel is
    set, AKS upgrades the control plane and node pools together and refuses
    a control-plane-only upgrade.
    https://learn.microsoft.com/en-us/rest/api/aks/managed-clusters/get-upgrade-profile
    https://learn.microsoft.com/en-us/azure/aks/upgrade-aks-cluster
    https://learn.microsoft.com/en-us/azure/aks/auto-upgrade-cluster#control-plane-upgrade-constraints"""
    targets = []
    try:
        profile = cluster.upgrade_profile()
        control_plane = (profile.get("properties") or {}).get("controlPlaneProfile") or {}
        for item in control_plane.get("upgrades") or []:
            if item.get("kubernetesVersion"):
                targets.append({"version": item["kubernetesVersion"], "preview": bool(item.get("isPreview"))})
    except aks.AKSError as exc:
        warnings.append("Available upgrades could not be read: {}".format(exc))
    targets.sort(key=lambda t: aks.version_key(t["version"]), reverse=True)
    ga = [t for t in targets if not t["preview"]]
    behind = sorted(
        pool.get("name") or ""
        for pool in pools
        if current and aks.version_key(pool.get("currentOrchestratorVersion") or pool.get("orchestratorVersion"))
        < aks.version_key(current)
    )
    return {
        "targets": targets,
        "available": bool(ga),
        "latest": ga[0]["version"] if ga else "",
        "auto": str(channel or "none").lower() != "none",
        "pools_behind": behind,
        "url": url if targets else "",
    }


def _cluster_context(resource, profile):
    aks = _aks()
    cluster = aks.cluster_for_resource(resource)
    warnings = []

    # https://learn.microsoft.com/en-us/rest/api/aks/managed-clusters/get
    data = cluster.get()
    props = data.get("properties") or {}
    sku = data.get("sku") or {}
    location = data.get("location") or ""
    current = props.get("currentKubernetesVersion") or props.get("kubernetesVersion") or ""
    pools = props.get("agentPoolProfiles") or []
    network = props.get("networkProfile") or {}
    access = props.get("apiServerAccessProfile") or {}
    auto = props.get("autoUpgradeProfile") or {}
    channel = auto.get("upgradeChannel") or "none"
    subscription_id, resource_group, _ = aks.parse_resource_id(data.get("id") or cluster.id)

    subscription_name = ""
    try:
        subscription_name = aks.subscription_display_name(cluster.arm)
    except aks.AKSError as exc:
        # Reading the subscription needs Reader on it; the id is shown instead.
        logger.info("Subscription name of %s not readable: %s", subscription_id, exc)

    network_name, network_hint = _network_configuration(network, pools)
    private = bool(access.get("enablePrivateCluster"))
    authorized = access.get("authorizedIPRanges") or []
    security = props.get("securityProfile") or {}
    oidc = props.get("oidcIssuerProfile") or {}
    tier = sku.get("tier") or "Free"

    upgrades = _upgrades(
        aks, cluster, current, channel, pools,
        _action_url(profile, resource, ACTION_UPGRADE_VERSION), warnings,
    )
    try:
        extensions = _extension_rows(cluster.list_extensions())
    except aks.AKSError as exc:
        warnings.append("Cluster extensions could not be read: {}".format(exc))
        extensions = []

    return {
        "cluster": {
            "status": _cluster_status(props.get("provisioningState"), (props.get("powerState") or {}).get("code")),
            "resource_group": resource_group,
            "resource_group_url": aks.portal_url(
                cluster.arm, "/subscriptions/{}/resourceGroups/{}".format(subscription_id, resource_group)
            ) if subscription_id and resource_group else "",
            "location": location,
            "subscription_id": subscription_id,
            "subscription_name": subscription_name,
            "version": current,
            "requested_version": props.get("kubernetesVersion") or "",
            "api_server": props.get("fqdn") or props.get("privateFQDN") or "",
            "private_fqdn": props.get("privateFQDN") or "",
            "network": network_name,
            "network_hint": network_hint,
            "pool_count": len(pools),
            "tier": tier,
            "automatic": sku.get("name") == "Automatic",
            "node_resource_group": props.get("nodeResourceGroup") or "",
        },
        "kubernetes": {
            "support_plan": SUPPORT_PLANS.get(props.get("supportPlan") or "KubernetesOfficial", props.get("supportPlan")),
            "support": _version_support(aks, cluster.arm, location, current, props.get("supportPlan"), warnings),
            "upgrade_channel": UPGRADE_CHANNELS.get(str(channel).lower(), channel),
            "node_os_channel": NODE_OS_CHANNELS.get(auto.get("nodeOSUpgradeChannel") or "", auto.get("nodeOSUpgradeChannel") or ""),
        },
        "networking": {
            "private": private,
            "authorized_ranges": authorized,
            "policy": NETWORK_POLICIES.get(str(network.get("networkPolicy") or "none").lower(), network.get("networkPolicy")),
            "dataplane": NETWORK_DATAPLANES.get(str(network.get("networkDataplane") or "").lower(), network.get("networkDataplane") or ""),
            "pod_cidr": network.get("podCidr") or "",
            "service_cidr": network.get("serviceCidr") or "",
            "dns_service_ip": network.get("dnsServiceIP") or "",
            "load_balancer": LOAD_BALANCER_SKUS.get(str(network.get("loadBalancerSku") or "").lower(), network.get("loadBalancerSku") or ""),
            "outbound": OUTBOUND_TYPES.get(network.get("outboundType") or "", network.get("outboundType") or ""),
        },
        "security": {
            "authentication": _authentication(props),
            "local_accounts": _enabled(not props.get("disableLocalAccounts")),
            "workload_identity": _enabled((security.get("workloadIdentity") or {}).get("enabled")),
            "oidc_enabled": bool(oidc.get("enabled")),
            "oidc_issuer": oidc.get("issuerURL") or "",
        },
        "integrations": _integrations(props),
        "extensions": extensions,
        "upgrades": upgrades,
        "jobs": _active_jobs(resource),
        "warnings": warnings,
    }


# ---------------------------------------------------------------------------
# Kubernetes reads (through the orchestrator the blueprint registered)
# ---------------------------------------------------------------------------
def _k8s_wrapper(resource):
    orchestrator = _orchestrator(resource)
    if orchestrator is None:
        raise PanelError(
            "No container orchestrator is linked to this resource (container_orchestrator_id). "
            "Run the blueprint's discovery to register one."
        )
    try:
        return orchestrator, orchestrator.get_api_wrapper()
    except Exception as exc:
        logger.exception("Kubernetes API wrapper failed for resource %s", resource.id)
        raise PanelError(
            "The cluster API could not be reached through orchestrator {}: {}".format(orchestrator.name, exc)
        )


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
        rows.append({
            "name": metadata.get("name", ""),
            "status": pill,
            "cordoned": cordoned and ready != "True",
            "pool": _label(labels, LABEL_POOL),
            "kubelet": (status.get("nodeInfo") or {}).get("kubeletVersion", ""),
            "node_image": _label(labels, LABEL_NODE_IMAGE),
            "zone": _label(labels, LABEL_ZONE),
            "internal_ip": internal_ip or "",
            "age": _age(metadata.get("creationTimestamp")),
        })
    rows.sort(key=lambda r: (r["pool"], r["name"]))
    return rows


# ---------------------------------------------------------------------------
# Node pools tab
# ---------------------------------------------------------------------------
def _node_pools_context(resource, profile):
    aks = _aks()
    cluster = aks.cluster_for_resource(resource)
    warnings = []

    # https://learn.microsoft.com/en-us/rest/api/aks/managed-clusters/get
    cluster_props = cluster.get().get("properties") or {}
    cluster_version = cluster_props.get("currentKubernetesVersion") or cluster_props.get("kubernetesVersion") or ""
    # https://learn.microsoft.com/en-us/rest/api/aks/agent-pools/list
    pools = cluster.list_pools()
    tracked = set(aks.pool_sub_resources(resource).values_list("name", flat=True))

    nodes, nodes_error = [], ""
    try:
        _, wrapper = _k8s_wrapper(resource)
        # GET /api/v1/nodes
        nodes = wrapper.get_nodes_raw().get("items", []) or []
    except PanelError as exc:
        nodes_error = str(exc)
    except Exception as exc:
        logger.exception("Kubernetes node read failed for resource %s", resource.id)
        nodes_error = "The cluster's nodes could not be read: {}".format(exc)
    ready_by_pool = {}
    for node in nodes:
        if _ready(node) == "True":
            pool_name = _label((node.get("metadata") or {}).get("labels") or {}, LABEL_POOL)
            ready_by_pool[pool_name] = ready_by_pool.get(pool_name, 0) + 1

    system_pools = sum(1 for p in pools if (p.get("properties") or {}).get("mode") == "System")
    can_scale = _action_for(profile, resource, ACTION_SCALE_POOL) is not None
    # CloudBolt's own dialogs: each action's dropdown lists every node pool,
    # so one URL serves every row.
    upgrade_url = _action_url(profile, resource, ACTION_UPGRADE_POOL)
    delete_url = _action_url(profile, resource, ACTION_DELETE_POOL)

    rows = []
    for pool in pools:
        props = pool.get("properties") or {}
        name = pool.get("name") or ""
        image = props.get("nodeImageVersion") or ""
        latest_image = ""
        try:
            # https://learn.microsoft.com/en-us/rest/api/aks/agent-pools/get-upgrade-profile
            upgrade = cluster.pool_upgrade_profile(name)
            latest_image = (upgrade.get("properties") or {}).get("latestNodeImageVersion") or ""
        except aks.AKSError as exc:
            warnings.append("Upgrades for node pool {} could not be read: {}".format(name, exc))
        version = props.get("currentOrchestratorVersion") or props.get("orchestratorVersion") or ""
        behind = bool(version and cluster_version and aks.version_key(version) < aks.version_key(cluster_version))
        new_image = bool(latest_image and image and latest_image != image)
        autoscale = bool(props.get("enableAutoScaling"))
        mode = props.get("mode") or ""
        only_system = mode == "System" and system_pools <= 1
        rows.append({
            "name": name,
            "state": _provisioning_pill(props.get("provisioningState")),
            "power": _power_pill((props.get("powerState") or {}).get("code")),
            "scale_method": "Autoscale" if autoscale else "Manual",
            "target": props.get("count"),
            "ready": ready_by_pool.get(name, 0) if not nodes_error else None,
            "min": props.get("minCount") if autoscale else None,
            "max": props.get("maxCount") if autoscale else None,
            "mode": mode,
            "os_type": props.get("osType") or "Linux",
            "os_sku": props.get("osSKU") or "",
            "version": version,
            "behind": behind,
            "node_image": image,
            "latest_image": latest_image,
            "new_image": new_image,
            "vm_size": props.get("vmSize") or "",
            "spot": props.get("scaleSetPriority") == "Spot",
            "zones": [str(z) for z in props.get("availabilityZones") or []],
            "tracked": name in tracked,
            "scale_url": (
                reverse("azure_aks_scale_node_pool", args=[resource.id, name])
                if can_scale and POOL_NAME_RE.match(name) else ""
            ),
            "upgrade_url": upgrade_url if (behind or new_image) else "",
            "delete_url": delete_url if not only_system else "",
        })
    return {
        "rows": rows,
        "kubernetes_version": cluster_version,
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


def _k8s_deployments(wrapper):
    """Every deployment in the cluster (apps/v1). The wrapper's own
    get_deployments_raw builds the wrong URL when no namespace is given, so
    the cluster-wide path is built here; requests_get adds the orchestrator's
    credentials and CloudBolt's SSL trust settings.
    https://kubernetes.io/docs/reference/kubernetes-api/workload-resources/deployment-v1/"""
    base = wrapper.get_clusterrole_url(None, skip_base_url_version=True)
    return wrapper.requests_get("{}/apps/v1/deployments".format(base)).json()


def _workloads_context(resource, profile):
    aks = _aks()
    orchestrator, wrapper = _k8s_wrapper(resource)
    try:
        # The wrapper's raw getters read the core API cluster-wide when no
        # namespace is given (GET /api/v1/nodes, /pods, /namespaces, /services).
        nodes = wrapper.get_nodes_raw().get("items", []) or []
        pods = wrapper.get_pods_raw().get("items", []) or []
        namespaces = wrapper.get_namespaces_raw().get("items", []) or []
        services = wrapper.get_services_raw().get("items", []) or []
        deployments = _k8s_deployments(wrapper).get("items", []) or []
    except Exception as exc:
        logger.exception("Kubernetes API read failed for resource %s", resource.id)
        raise PanelError(
            "The cluster API could not be read through orchestrator {}: {}".format(orchestrator.name, exc)
        )

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

    # Namespace landing zones (AKS Namespace blueprint) are sub-resources of
    # the cluster; link each one from its row so the owning team and the
    # CloudBolt resource are visible next to the live namespace.
    landing_zones = {}
    for zone in aks.active_namespaces(resource).select_related("group"):
        try:
            url = zone.get_absolute_url()
        except Exception:
            url = ""
        landing_zones[_attr(zone, NAMESPACE_MARKER_FIELD) or zone.name] = {
            "url": url,
            "group": zone.group.name if zone.group else "",
            "tier": _attr(zone, "aks_namespace_size_tier") or "",
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
            if _is_aks_error(exc):
                logger.warning("Azure call failed for resource %s: %s", resource.id, exc)
                context = {"error": "Azure returned an error: {}".format(exc)}
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
# A group may order an AKS namespace on the cluster when the cluster's
# per-cluster Environment is available to it: entitled to the group or an
# ancestor, or unconstrained (no groups). CloudBolt's Unassigned group is a
# placeholder that entitles no one. Changes run as Manage AKS Cluster Groups
# jobs; this tab only reads.
def _cluster_environment(resource):
    """The cluster's per-cluster Environment, or PanelError."""
    try:
        return _aks().cluster_environment(resource)
    except PanelError:
        raise
    except Exception as exc:
        if _is_aks_error(exc):
            raise PanelError(str(exc))
        raise


def _groups_context(resource, profile):
    aks = _aks()
    env = _cluster_environment(resource)
    state, groups = aks.environment_groups(env)
    action = _action_for(profile, resource, ACTION_MANAGE_GROUPS)
    rows = [
        {
            "group": group,
            "remove_url": reverse("azure_aks_remove_cluster_group", args=[resource.id, group.global_id])
            if action else "",
        }
        for group in groups
    ]
    return {
        "profile": profile,
        "environment": env,
        "state": state,
        "rows": rows,
        "allow_all_url": reverse("azure_aks_allow_all_cluster_groups", args=[resource.id])
        if action and state != aks.UNCONSTRAINED else "",
        "action_label": action.label if action else "",
    }


def groups_panel(request, resource_id):
    return _panel(request, resource_id, "groups_panel.html", _groups_context)


# ---------------------------------------------------------------------------
# Namespace Connect tab: how to reach one landing zone, and who may
# ---------------------------------------------------------------------------
# Access to a managed namespace is two Azure role assignments on it: Azure
# Kubernetes Service Namespace User (see it, get its credentials) and an AKS
# RBAC Reader, Writer or Admin role that AKS enforces inside it.
#   https://learn.microsoft.com/en-us/azure/aks/managed-namespaces#assign-role
# az aks namespace get-credentials needs Azure CLI 2.80.0 or later, and an
# Entra ID cluster's kubeconfig needs kubelogin.
#   https://learn.microsoft.com/en-us/cli/azure/aks/namespace#az-aks-namespace-get-credentials
#   https://learn.microsoft.com/en-us/azure/aks/kubelogin-authentication
ACCESS_SCOPES = {"namespace": "This namespace", "cluster": "Whole cluster"}
PRINCIPAL_TYPES = {"User": "User", "Group": "Group", "ServicePrincipal": "Service principal"}


def _connect_context(resource, profile):
    """Connection details for a namespace landing zone, from aks_namespace."""
    ns = _aks_namespace()
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
    details["access"] = [
        dict(
            row,
            scope_text=ACCESS_SCOPES.get(row.get("scope"), row.get("scope") or ""),
            type_text=PRINCIPAL_TYPES.get(row.get("principal_type"), row.get("principal_type") or ""),
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
        "namespace_user_role": ns.NAMESPACE_USER_ROLE["role"],
        "level_roles": ", ".join(level["role"] for level in levels.values()),
    })
    return details


def namespace_connect_panel(request, resource_id):
    return _panel(request, resource_id, "connect_panel.html", _connect_context)


# ---------------------------------------------------------------------------
# Namespace Workloads tab: what runs in one landing zone
# ---------------------------------------------------------------------------
# Read-only, through the cluster's orchestrator like the cluster's Workloads
# tab. Deliberately no pod logs, Secrets or ConfigMaps (anyone who may view
# the namespace resource would see them), and no scale or restart buttons
# (they would run as the orchestrator, not as the viewer).
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
# the unit shown after a count in the meter's hover text. The AKS-managed
# quota carries the CPU and memory keys, CloudBolt's quota the rest.
# https://learn.microsoft.com/en-us/azure/aks/concepts-managed-namespaces
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


def _namespace_api(wrapper):
    """GET helper for the cluster API root, from the orchestrator's wrapper.

    get_clusterrole_url("v1", skip version) is "<scheme>://<host>[/prefix]/api";
    the root both API groups hang off is that without "/api". requests_get
    adds the orchestrator's credentials and CloudBolt's SSL trust settings.
    """
    base = wrapper.get_clusterrole_url("v1", skip_base_url_version=True)
    root = base[: -len("/api")] if base.endswith("/api") else base

    def get(path, params=None):
        kwargs = {"params": params} if params else {}
        return wrapper.requests_get(root + path, **kwargs).json()

    return get


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
    """One meter per hard limit across the namespace's ResourceQuotas, from
    aks_namespace.quota_usage ({quota: {resource: (hard, used)}}): the
    AKS-managed quota first, then CloudBolt's."""
    known = [key for key, _, _, _ in QUOTA_KEYS]
    rows = []
    for quota_name in sorted(usage, key=lambda name: (name == cloudbolt_quota, name)):
        limits = usage[quota_name] or {}
        ordered = [entry for entry in QUOTA_KEYS if entry[0] in limits]
        ordered += [(k, k, "count", "") for k in sorted(limits) if k not in known]
        for key, label, kind, unit in ordered:
            hard, used = limits[key]
            total = _quantity(kind, hard)
            current = _quantity(kind, used)
            meter = _meter(current, total, _quantity_text(kind, current), _quantity_text(kind, total))
            meter.update({
                "label": label,
                "usage": _usage_text(kind, current, total, unit),
                "quota": quota_name,
            })
            rows.append(meter)
    return rows


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
    ns = _aks_namespace()
    try:
        namespace, orchestrator, cluster_name = ns.load_cluster(resource)
    except ValueError as exc:
        raise PanelError("This namespace cannot be read: {}.".format(exc))
    if not ns.NAMESPACE_NAME_RE.match(str(namespace)):
        raise PanelError("'{}' is not a valid Kubernetes namespace name.".format(namespace))
    try:
        get = _namespace_api(orchestrator.get_api_wrapper())
    except Exception as exc:
        raise PanelError("The cluster API could not be reached through orchestrator {}: {}".format(orchestrator.name, exc))

    warnings = []

    def items(label, path):
        try:
            return get(path).get("items", []) or []
        except Exception as exc:
            logger.warning("Reading %s in %s failed: %s", label, namespace, exc)
            warnings.append("{} could not be read: {}".format(label, exc))
            return []

    try:
        quota = _quota_rows(ns.quota_usage(resource), ns.QUOTA_NAME)
    except Exception as exc:
        logger.warning("Reading the resource quotas of %s failed: %s", namespace, exc)
        warnings.append("Resource quotas could not be read: {}".format(exc))
        quota = []

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

    return {
        "namespace": namespace,
        "cluster_name": cluster_name,
        "quota": quota,
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


def _dialog_guard(request, resource_id, name, title, hook_name):
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
    action = _action_for(profile, resource, hook_name)
    if action is None:
        return resource, profile, None, _dialog_message(
            title,
            "The {} action is not available on this resource: it is not synced, not on the "
            "resource's blueprint, or you are not allowed to run it.".format(hook_name),
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


# Shows Node count for Manual and the minimum and maximum for Autoscale.
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


@dialog_view
def scale_node_pool(request, resource_id, pool_name):
    """Change a node pool's scale method and node counts through the Scale
    AKS Node Pool action, prefilled from Azure.
    https://learn.microsoft.com/en-us/azure/aks/scale-node-pools"""
    title = "Scale node pool {}".format(pool_name)
    resource, profile, action, refusal = _dialog_guard(
        request, resource_id, pool_name, title, ACTION_SCALE_POOL
    )
    if refusal:
        return refusal
    try:
        # https://learn.microsoft.com/en-us/rest/api/aks/agent-pools/get
        pool = _aks().cluster_for_resource(resource).get_pool(pool_name)
    except PanelError as exc:
        return _dialog_message(title, str(exc))
    except Exception as exc:
        if not _is_aks_error(exc):
            raise
        return _dialog_message(title, "Azure could not read the node pool: {}".format(exc))

    props = pool.get("properties") or {}
    autoscale = bool(props.get("enableAutoScaling"))
    count = props.get("count")
    system_pool = props.get("mode") == "System"
    initial = {
        "scale_method": "Autoscale" if autoscale else "Manual",
        "node_count": count,
        "min_count": props.get("minCount") if autoscale else count,
        "max_count": props.get("maxCount") if autoscale else count,
    }
    if request.method == "POST":
        form = ScaleNodePoolForm(request.POST, initial=initial, system_pool=system_pool)
        if form.is_valid():
            data = form.cleaned_data
            method = data["scale_method"]
            if method == "Manual":
                node_count = data["node_count"]
                min_count = data.get("min_count") if data.get("min_count") is not None else node_count
                max_count = data.get("max_count") if data.get("max_count") is not None else node_count
            else:
                min_count, max_count = data["min_count"], data["max_count"]
                # Every input gets a value; the action reads the method to
                # decide which ones apply.
                node_count = min(max(count or min_count, min_count), max_count)
            values = {
                "aks_scale_pool": pool_name,
                "aks_scale_method": method,
                "aks_scale_count": node_count,
                "aks_scale_min_count": min_count,
                "aks_scale_max_count": max_count,
            }
            _run_action(request, profile, resource, action, values, "Scaling node pool {}".format(pool_name))
            return _redirect_back(request, resource)
    else:
        form = ScaleNodePoolForm(initial=initial, system_pool=system_pool)

    if autoscale:
        now = format_html(
            "autoscales between <b>{}</b> and <b>{}</b> nodes and runs {} now",
            props.get("minCount"), props.get("maxCount"), count,
        )
    else:
        now = format_html("runs <b>{}</b> nodes, scaled manually", count)
    content = format_html(
        "<p>{} node pool <b>{}</b> {}. The change runs as a CloudBolt job through the <b>{}</b> action.</p>",
        props.get("mode") or "The", pool_name, now, action.label,
    )
    return {
        "title": title,
        "content": content,
        "form": form,
        "bottom_content": SCALE_FORM_SCRIPT,
        "action_url": reverse("azure_aks_scale_node_pool", args=[resource.id, pool_name]),
        "use_ajax": True,
        "submit": "Scale",
    }


# Operations of the Manage AKS Cluster Groups action (plugins/OHK-yzmq0kzu).
GROUPS_ADD = "Add"
GROUPS_REMOVE = "Remove"
GROUPS_ALLOW_ALL = "Allow all groups"


def _groups_dialog_guard(request, resource_id, title):
    """Checks the group dialogs share: the viewer may see the resource and
    run Manage AKS Cluster Groups on it, and the cluster has an environment.
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
    aks = _aks()
    state, entitled = aks.environment_groups(env)
    entitled_ids = {group.id for group in entitled}
    choices = [(g.global_id, g.name) for g in aks.assignable_groups() if g.id not in entitled_ids]
    if not choices:
        return _dialog_message(title, "Every CloudBolt group is already entitled to this cluster.", css="")

    if request.method == "POST":
        form = AddClusterGroupsForm(request.POST, choices=choices)
        if form.is_valid():
            values = {"aks_groups_operation": GROUPS_ADD, "aks_groups": form.cleaned_data["groups"]}
            _run_action(request, profile, resource, action, values, "Adding groups")
            return _redirect_back(request, resource)
    else:
        form = AddClusterGroupsForm(choices=choices)

    if state == aks.UNCONSTRAINED:
        now = mark_safe(
            '<p class="text-danger">Every group may order namespaces on this cluster now. Once you add '
            "groups, only those groups (and their subgroups) may.</p>"
        )
    elif state == aks.RESTRICTED:
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
        "action_url": reverse("azure_aks_add_cluster_groups", args=[resource.id]),
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
    entitled = _aks().environment_groups(env)[1]
    group = next((g for g in entitled if g.global_id == group_id), None)
    if group is None:
        return _dialog_message(title, "That group is not entitled to this cluster.")

    if request.method == "POST":
        form = ConfirmForm(request.POST)
        if form.is_valid():
            values = {"aks_groups_operation": GROUPS_REMOVE, "aks_groups": [group.global_id]}
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
        "action_url": reverse("azure_aks_remove_cluster_group", args=[resource.id, group.global_id]),
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
    aks = _aks()
    state, entitled = aks.environment_groups(env)
    if state == aks.UNCONSTRAINED:
        return _dialog_message(title, "Every group may already order namespaces on this cluster.", css="")

    if request.method == "POST":
        form = ConfirmForm(request.POST)
        if form.is_valid():
            values = {"aks_groups_operation": GROUPS_ALLOW_ALL}
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
        "action_url": reverse("azure_aks_allow_all_cluster_groups", args=[resource.id]),
        "use_ajax": True,
        "submit": "Allow all groups",
    }


# ---------------------------------------------------------------------------
# Tabs
# ---------------------------------------------------------------------------
class ClusterTabDelegate(TabExtensionDelegate):
    """Kubernetes Cluster resources of the Azure Kubernetes Service blueprint."""

    def should_display(self):
        return bool(_attr(self.instance, CLUSTER_MARKER_FIELD))


class NamespaceTabDelegate(TabExtensionDelegate):
    """Namespace landing zones of the AKS Namespace blueprint."""

    def should_display(self):
        resource_type = getattr(self.instance, "resource_type", None)
        return (
            getattr(resource_type, "name", "") == NAMESPACE_RESOURCE_TYPE
            and bool(_attr(self.instance, NAMESPACE_MARKER_FIELD))
        )


def _tab_context(resource):
    """Identity shown above the panels, from the resource's custom fields; the
    portal link is built locally (no Azure call)."""
    portal = ""
    try:
        aks = _aks()
        cluster = aks.cluster_for_resource(resource)
        portal = aks.portal_url(cluster.arm, cluster.id)
    except Exception as exc:
        # The panel explains a missing module, handler or resource group.
        logger.info("No Azure portal link for resource %s: %s", resource.id, exc)
    return {
        "resource": resource,
        "cluster_name": _cluster_name(resource),
        "resource_group": _attr(resource, "aks_resource_group") or "",
        "location": _attr(resource, "aks_location") or "",
        "environment_name": _environment_name(resource),
        "portal_url": portal,
    }


@tab_extension(
    model=Resource,
    title="Cluster",
    delegate=ClusterTabDelegate,
    description="Live AKS cluster status, essentials, properties, upgrades, and integrations",
)
def aks_cluster_tab(request, obj_id):
    resource = get_object_or_404(Resource, pk=obj_id)
    context = _tab_context(resource)
    context["cluster_url"] = reverse("azure_aks_cluster_panel", args=[resource.id])
    return render(request, TEMPLATES + "cluster_tab.html", context)


@tab_extension(
    model=Resource,
    title="Node pools",
    delegate=ClusterTabDelegate,
    description="AKS node pools and nodes with Scale, Upgrade, and Delete actions",
)
def aks_node_pools_tab(request, obj_id):
    resource = get_object_or_404(Resource, pk=obj_id)
    profile = request.get_user_profile()
    context = _tab_context(resource)
    context.update({
        "node_pools_url": reverse("azure_aks_node_pools_panel", args=[resource.id]),
        "add_url": _action_url(profile, resource, ACTION_ADD_POOL),
        "list_url": _action_url(profile, resource, ACTION_LIST_POOLS),
    })
    return render(request, TEMPLATES + "node_pools_tab.html", context)


@tab_extension(
    model=Resource,
    title="Workloads",
    delegate=ClusterTabDelegate,
    description="Kubernetes nodes and namespaces read through the cluster's container orchestrator",
)
def aks_workloads_tab(request, obj_id):
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
        "workloads_url": reverse("azure_aks_workloads_panel", args=[resource.id]),
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
    description="CloudBolt groups that may order namespaces on an AKS cluster, with Add, Remove, and Allow all groups",
)
def aks_groups_tab(request, obj_id):
    resource = get_object_or_404(Resource, pk=obj_id)
    profile = request.get_user_profile()
    context = _tab_context(resource)
    context.update({
        "groups_url": reverse("azure_aks_cluster_groups_panel", args=[resource.id]),
        "add_url": reverse("azure_aks_add_cluster_groups", args=[resource.id])
        if _action_for(profile, resource, ACTION_MANAGE_GROUPS) else "",
    })
    return render(request, TEMPLATES + "groups_tab.html", context)


def _namespace_tab_context(resource):
    return {
        "resource": resource,
        "namespace": _attr(resource, NAMESPACE_MARKER_FIELD) or resource.name,
        "cluster_name": _attr(resource, "aks_namespace_cluster") or "",
    }


@tab_extension(
    model=Resource,
    title="Connect",
    delegate=NamespaceTabDelegate,
    description="kubectl commands, a kubeconfig, and the Microsoft Entra principals with access to an AKS namespace",
)
def aks_namespace_connect_tab(request, obj_id):
    resource = get_object_or_404(Resource, pk=obj_id)
    profile = request.get_user_profile()
    context = _namespace_tab_context(resource)
    context.update({
        "connect_url": reverse("azure_aks_namespace_connect_panel", args=[resource.id]),
        "manage_access_url": _action_url(profile, resource, ACTION_MANAGE_NAMESPACE_ACCESS),
        "pipeline_url": _action_url(profile, resource, ACTION_MANAGE_PIPELINE_IDENTITY),
    })
    return render(request, TEMPLATES + "connect_tab.html", context)


@tab_extension(
    model=Resource,
    title="Workloads",
    delegate=NamespaceTabDelegate,
    description="Read-only inventory of one AKS namespace: quota usage, workloads, pods, services, storage, and warning events",
)
def aks_namespace_workloads_tab(request, obj_id):
    resource = get_object_or_404(Resource, pk=obj_id)
    context = _namespace_tab_context(resource)
    context.update({
        "workloads_url": reverse("azure_aks_namespace_workloads_panel", args=[resource.id]),
        "quota_change_url": _action_url(
            request.get_user_profile(), resource, ACTION_REQUEST_QUOTA_CHANGE
        ),
    })
    return render(request, TEMPLATES + "namespace_workloads_tab.html", context)
