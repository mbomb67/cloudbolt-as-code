"""
AWS EKS Cluster XUI

Adds three tabs to CloudBolt resources created or discovered by the AWS EKS
blueprint (BP-r5apcf7e) -- any resource carrying an eks_cluster_name custom
field -- and a Connect tab to the namespaces of the AWS EKS Namespace
blueprint (BP-e4c0q58z):

- Cluster: live status, Kubernetes and platform version with support status,
  API endpoint and access mode, OIDC issuer, control-plane logging,
  authentication mode, networking, cluster health issues, the jobs running
  on the resource, and an update banner (with an Update EKS Version button)
  when EKS offers a newer Kubernetes version. An add-on table shows each
  add-on's installed and latest compatible version with an Upgrade button.
- Node groups: every managed node group with status, AMI type and release,
  Kubernetes version (flagged when behind the control plane), instance
  types, scaling configuration, in-service node count from the Auto Scaling
  group, and the health issues AWS reports, with Upgrade, Scale and Delete
  buttons.
- Workloads: read through the cluster's container orchestrator token: nodes
  with readiness, pressure conditions, CPU and memory requests against
  allocatable capacity and pod counts, plus namespaces with pod, deployment
  and service counts, and a Create namespace button (Create AWS EKS
  Namespace) in the toolbar.
- Connect (on kubernetes_namespace resources): the update-kubeconfig and
  set-context commands, a ready-made kubeconfig to copy or download, and the
  IAM roles and users whose EKS access policies cover the namespace, the
  GitHub Actions pipeline identities with a workflow snippet each, and
  Manage access (Manage AWS EKS Namespace Access) and Pipeline identity
  (Manage AWS EKS Namespace Pipeline Identity) buttons. Built by the
  eks_namespace shared module (SHM-sa5yqm9j), which lists access entries
  with ListAccessEntries and ListAssociatedAccessPolicies.

Every button starts one of the blueprint's resource actions as a CloudBolt
job (Update EKS Version, Upgrade AWS EKS Nodegroup, Scale AWS EKS Nodegroup,
Delete AWS EKS Nodegroup, Update AWS EKS Add-on; Add and List AWS EKS
Nodegroup from the toolbar), so
approvals, permissions and job history apply. Buttons appear only when the
action is synced, applies to the resource's blueprint and the viewer may run
it (resource_action_permitted), and the job runs as the viewer.

AWS is read with the handler credentials of the environment the cluster was
ordered into (env_id), falling back to the handler and region stored on the
resource (aws_rh_id, aws_region). The Workloads tab uses the Kubernetes
orchestrator the blueprint registers (container_orchestrator_id); its bearer
token is minted from the same handler credentials. Both run under the
handler's identity, not the viewer's, so every endpoint re-checks the
viewer's CloudBolt permission on the resource. Panels load asynchronously so
a slow AWS or cluster API never stalls the resource page.

Vendor API references (each call site cites its own page):
    https://docs.aws.amazon.com/eks/latest/APIReference/API_DescribeCluster.html
    https://docs.aws.amazon.com/eks/latest/APIReference/API_DescribeClusterVersions.html
    https://docs.aws.amazon.com/eks/latest/APIReference/API_ListAddons.html
    https://docs.aws.amazon.com/eks/latest/APIReference/API_DescribeAddon.html
    https://docs.aws.amazon.com/eks/latest/APIReference/API_DescribeAddonVersions.html
    https://docs.aws.amazon.com/eks/latest/APIReference/API_ListNodegroups.html
    https://docs.aws.amazon.com/eks/latest/APIReference/API_DescribeNodegroup.html
    https://docs.aws.amazon.com/eks/latest/APIReference/API_Nodegroup.html
    https://docs.aws.amazon.com/autoscaling/ec2/APIReference/API_DescribeAutoScalingGroups.html
    https://kubernetes.io/docs/reference/kubernetes-api/
"""

import re
from datetime import datetime, timezone

from botocore.exceptions import BotoCoreError, ClientError
from django.contrib import messages
from django.http import HttpResponseRedirect
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.utils.html import format_html, format_html_join

from cbhooks.models import ResourceAction
from cbhooks.services.parsing import get_hook_action_input_dict
from containerorchestrators.kuberneteshandler.models import Kubernetes
from extensions.views import tab_extension, TabExtensionDelegate
from infrastructure.models import Environment
from jobs.models import Job
from resourcehandlers.aws.models import AWSHandler
from resources.models import Resource
from utilities.decorators import dialog_view
from utilities.logger import ThreadLogger
from utilities.permissions import resource_action_permitted

from xui.aws_eks_cluster.forms import ScaleNodegroupForm, UpgradeAddonForm

logger = ThreadLogger(__name__)

TEMPLATES = "aws_eks_cluster/templates/"

# Viewing the tabs needs VIEW_PERMISSION on the resource. Every button maps
# to a resource action and is shown only when resource_action_permitted
# allows that action for the viewer.
VIEW_PERMISSION = "resource.view"

# Plugin (hook) names of the AWS EKS blueprint's day-2 actions.
ACTION_UPDATE_VERSION = "Update EKS Version"
ACTION_ADD_NODEGROUP = "Add AWS EKS Nodegroup"
ACTION_LIST_NODEGROUPS = "List AWS EKS Nodegroup"
ACTION_DELETE_NODEGROUP = "Delete AWS EKS Nodegroup"
ACTION_SCALE_NODEGROUP = "Scale AWS EKS Nodegroup"
ACTION_UPGRADE_NODEGROUP = "Upgrade AWS EKS Nodegroup"
ACTION_UPDATE_ADDON = "Update AWS EKS Add-on"
ACTION_CREATE_NAMESPACE = "Create AWS EKS Namespace"
ACTION_MANAGE_NAMESPACE_ACCESS = "Manage AWS EKS Namespace Access"
ACTION_MANAGE_PIPELINE_IDENTITY = "Manage AWS EKS Namespace Pipeline Identity"

# amiType EKS reports for a node group whose launch template names its own
# AMI; UpdateNodegroupVersion cannot move those, so no Upgrade button.
CUSTOM_AMI_TYPE = "CUSTOM"

# Node groups are stored as sub-resources of this type by the blueprint.
NODEGROUP_RESOURCE_TYPE = "eks_nodegroup"
# Namespace landing zones (AWS EKS Namespace blueprint) are sub-resources of
# this type, owned by their teams.
NAMESPACE_RESOURCE_TYPE = "kubernetes_namespace"

# Where an IAM principal's access to a namespace comes from (the scope
# values of eks_namespace.namespace_access).
ACCESS_SCOPES = {
    "namespace": "This namespace",
    "pattern": "Namespace pattern",
    "cluster": "Every namespace",
}

# Cluster, node group and add-on names as EKS constrains them, plus the dots
# some marketplace add-on names carry.
NAME_RE = re.compile(r"^[0-9A-Za-z][A-Za-z0-9._-]{0,99}$")

# EKS status -> pill style + Bootstrap icon.
STATUS_STYLES = {
    "ACTIVE": ("ok", "bi-check-circle-fill"),
    "CREATING": ("live", "bi-arrow-repeat"),
    "UPDATING": ("live", "bi-arrow-repeat"),
    "PENDING": ("live", "bi-hourglass-split"),
    "DELETING": ("warn", "bi-trash"),
    "DEGRADED": ("warn", "bi-exclamation-triangle-fill"),
    "FAILED": ("bad", "bi-x-circle-fill"),
    "CREATE_FAILED": ("bad", "bi-x-circle-fill"),
    "DELETE_FAILED": ("bad", "bi-x-circle-fill"),
    "UPDATE_FAILED": ("bad", "bi-x-circle-fill"),
}

# Cluster accessConfig.authenticationMode values, spelled out.
AUTH_MODES = {
    "API": "Access entries only",
    "API_AND_CONFIG_MAP": "Access entries and aws-auth ConfigMap",
    "CONFIG_MAP": "aws-auth ConfigMap only",
}

CAPACITY_TYPES = {
    "ON_DEMAND": "On-Demand",
    "SPOT": "Spot",
    "CAPACITY_BLOCK": "Capacity Block",
}

# Kubernetes node conditions that signal pressure when their status is True.
PRESSURE_CONDITIONS = ("MemoryPressure", "DiskPressure", "PIDPressure", "NetworkUnavailable")

# Node labels EKS applies.
LABEL_NODEGROUP = "eks.amazonaws.com/nodegroup"
LABEL_INSTANCE_TYPE = ("node.kubernetes.io/instance-type", "beta.kubernetes.io/instance-type")
LABEL_ZONE = ("topology.kubernetes.io/zone", "failure-domain.beta.kubernetes.io/zone")


class EKSError(Exception):
    """A readable problem the panel shows instead of a traceback."""


# ---------------------------------------------------------------------------
# Resource / permission helpers
# ---------------------------------------------------------------------------
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
    return _attr(resource, "eks_cluster_name") or resource.name


def _aws(resource):
    """(AWS handler, region) for the cluster: the environment the cluster
    was ordered into, or the handler and region the build and discovery
    plugins store on the resource."""
    env_id = _attr(resource, "env_id")
    if env_id:
        try:
            env = (
                Environment.objects.filter(id=int(str(env_id).strip()))
                .select_related("resource_handler")
                .first()
            )
        except (TypeError, ValueError):
            env = None
        if env is not None and env.resource_handler_id and env.aws_region:
            return env.resource_handler.cast(), env.aws_region
    rh_id = _attr(resource, "aws_rh_id")
    region = _attr(resource, "aws_region")
    if rh_id and region:
        try:
            rh = AWSHandler.objects.filter(id=int(str(rh_id).strip())).first()
        except (TypeError, ValueError):
            rh = None
        if rh is not None:
            return rh, str(region).strip()
    raise EKSError(
        "This resource records no AWS environment or handler (env_id, or aws_rh_id "
        "and aws_region), so AWS cannot be queried."
    )


def _client(resource, service="eks"):
    rh, region = _aws(resource)
    return rh.get_boto3_client(region, service)


def _aws_error(exc):
    if isinstance(exc, ClientError):
        error = exc.response.get("Error", {})
        return "{}: {}".format(error.get("Code", "ClientError"), error.get("Message", exc))
    return str(exc)


def _console_url(region, cluster_name):
    if not region or not cluster_name:
        return ""
    return "https://{0}.console.aws.amazon.com/eks/home?region={0}#/clusters/{1}".format(
        region, cluster_name
    )


def _orchestrator(resource):
    """The Kubernetes container orchestrator the blueprint registered, or None."""
    orchestrator_id = _attr(resource, "container_orchestrator_id")
    if not orchestrator_id:
        return None
    try:
        return Kubernetes.objects.filter(id=int(str(orchestrator_id).strip())).first()
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------
def _status(value):
    style, icon = STATUS_STYLES.get(value or "", ("muted", "bi-question-circle"))
    text = (value or "unknown").replace("_", " ").capitalize()
    return {"text": text, "style": style, "icon": icon}


def _when(value):
    if not value:
        return ""
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).strftime("%b %d, %Y %H:%M UTC")
    return str(value)


def _day(value):
    if not value:
        return ""
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).strftime("%b %d, %Y")
    return str(value)[:10]


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


def _issues(health):
    """Health issues as AWS reports them on clusters, node groups and add-ons."""
    rows = []
    for issue in (health or {}).get("issues", []) or []:
        rows.append({
            "code": issue.get("code", ""),
            "message": issue.get("message", ""),
            "resources": ", ".join(issue.get("resourceIds") or []),
        })
    return rows


def _version_key(version):
    """Sortable form of a Kubernetes version such as 1.31."""
    return tuple(int(part) for part in str(version or "").split(".") if part.isdigit())


def _addon_version_key(version):
    """Sortable form of an add-on version such as v1.19.0-eksbuild.1."""
    return tuple(int(part) for part in re.findall(r"\d+", str(version or "")))


def _paginate(call, key, **params):
    """Collect every page of an EKS list call that pages with nextToken."""
    items = []
    token = None
    while True:
        if token:
            params["nextToken"] = token
        page = call(**params)
        items.extend(page.get(key, []) or [])
        token = page.get("nextToken")
        if not token:
            break
    return items


# ---------------------------------------------------------------------------
# AWS reads
# ---------------------------------------------------------------------------
def _available_versions(client):
    """Kubernetes versions EKS offers today, newest first, without the ones
    AWS reports as unsupported. Same selection as the blueprint's build and
    Update EKS Version plugins.
    https://docs.aws.amazon.com/eks/latest/APIReference/API_DescribeClusterVersions.html"""
    versions = _paginate(
        client.describe_cluster_versions, "clusterVersions", clusterType="eks", includeAll=True
    )
    supported = [
        v
        for v in versions
        if (v.get("versionStatus") or v.get("status") or "").upper().replace("-", "_") != "UNSUPPORTED"
    ]
    return sorted(supported, key=lambda v: _version_key(v.get("clusterVersion")), reverse=True)


def _compatible_addon_versions(client, addon_name, kubernetes_version):
    """Versions of the add-on AWS publishes for the cluster's Kubernetes
    version, newest first, plus the one AWS marks as the default for it.
    https://docs.aws.amazon.com/eks/latest/APIReference/API_DescribeAddonVersions.html"""
    versions = {}
    for addon in _paginate(
        client.describe_addon_versions,
        "addons",
        addonName=addon_name,
        kubernetesVersion=kubernetes_version,
    ):
        for info in addon.get("addonVersions", []) or []:
            compatible = [
                c for c in info.get("compatibilities", []) or []
                if c.get("clusterVersion") == kubernetes_version
            ]
            if info.get("compatibilities") and not compatible:
                continue
            versions[info["addonVersion"]] = any(c.get("defaultVersion") for c in compatible)
    ordered = sorted(versions, key=_addon_version_key, reverse=True)
    default = next((v for v in ordered if versions[v]), None)
    return ordered, default


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
# Panel contexts
# ---------------------------------------------------------------------------
def _cluster_context(resource, profile):
    client = _client(resource)
    name = _cluster_name(resource)
    warnings = []

    # https://docs.aws.amazon.com/eks/latest/APIReference/API_DescribeCluster.html
    cluster = client.describe_cluster(name=name)["cluster"]
    current = cluster.get("version") or ""

    versions = []
    try:
        versions = _available_versions(client)
    except (ClientError, BotoCoreError) as exc:
        warnings.append("Available Kubernetes versions could not be read: {}".format(_aws_error(exc)))

    newer = [v for v in versions if _version_key(v.get("clusterVersion")) > _version_key(current)]
    upgrade = None
    if newer:
        upgrade = {
            "next": min(newer, key=lambda v: _version_key(v.get("clusterVersion")))["clusterVersion"],
            "latest": max(newer, key=lambda v: _version_key(v.get("clusterVersion")))["clusterVersion"],
            "url": _action_url(profile, resource, ACTION_UPDATE_VERSION),
        }

    support = ""
    info = next((v for v in versions if v.get("clusterVersion") == current), None)
    if info:
        status = (info.get("versionStatus") or info.get("status") or "").replace("_", " ").replace("-", " ").lower()
        end = info.get("endOfStandardSupportDate") if "standard" in status else info.get("endOfExtendedSupportDate")
        support = status
        if end:
            support += " until {}".format(_day(end))

    logging = sorted({
        log_type
        for entry in (cluster.get("logging") or {}).get("clusterLogging", []) or []
        if entry.get("enabled")
        for log_type in entry.get("types", []) or []
    })

    vpc = cluster.get("resourcesVpcConfig") or {}
    public = vpc.get("endpointPublicAccess")
    private = vpc.get("endpointPrivateAccess")
    if public and private:
        access_mode = "Public and private endpoint access"
    elif public:
        access_mode = "Public endpoint access"
    elif private:
        access_mode = "Private endpoint access only"
    else:
        access_mode = "Endpoint access not reported"
    public_cidrs = ", ".join(vpc.get("publicAccessCidrs") or []) if public else ""
    if public_cidrs == "0.0.0.0/0":
        public_cidrs = "open to 0.0.0.0/0"

    auth_mode = (cluster.get("accessConfig") or {}).get("authenticationMode") or ""
    support_type = (cluster.get("upgradePolicy") or {}).get("supportType") or ""
    network = cluster.get("kubernetesNetworkConfig") or {}

    return {
        "cluster": {
            "status": _status(cluster.get("status")),
            "version": current,
            "platform_version": cluster.get("platformVersion") or "",
            "support": support,
            "endpoint": cluster.get("endpoint") or "",
            "access_mode": access_mode,
            "public_cidrs": public_cidrs,
            "oidc_issuer": ((cluster.get("identity") or {}).get("oidc") or {}).get("issuer") or "",
            "logging": logging,
            "auth_mode": AUTH_MODES.get(auth_mode, auth_mode),
            "support_type": {"STANDARD": "Standard support policy", "EXTENDED": "Extended support policy"}.get(support_type, ""),
            "vpc_id": vpc.get("vpcId") or "",
            "subnet_count": len(vpc.get("subnetIds") or []),
            "cluster_sg": vpc.get("clusterSecurityGroupId") or "",
            "service_cidr": network.get("serviceIpv4Cidr") or network.get("serviceIpv6Cidr") or "",
            "arn": cluster.get("arn") or "",
            "created": _when(cluster.get("createdAt")),
            "issues": _issues(cluster.get("health")),
        },
        "upgrade": upgrade,
        "jobs": _active_jobs(resource),
        "warnings": warnings,
    }


def _addons_context(resource, profile):
    client = _client(resource)
    name = _cluster_name(resource)
    warnings = []
    kubernetes_version = client.describe_cluster(name=name)["cluster"].get("version") or ""
    can_upgrade = _action_for(profile, resource, ACTION_UPDATE_ADDON) is not None

    rows = []
    updates_available = 0
    # https://docs.aws.amazon.com/eks/latest/APIReference/API_ListAddons.html
    for addon_name in sorted(_paginate(client.list_addons, "addons", clusterName=name)):
        # https://docs.aws.amazon.com/eks/latest/APIReference/API_DescribeAddon.html
        addon = client.describe_addon(clusterName=name, addonName=addon_name)["addon"]
        installed = addon.get("addonVersion") or ""
        latest, default = None, None
        try:
            versions, default = _compatible_addon_versions(client, addon_name, kubernetes_version)
            latest = versions[0] if versions else None
        except (ClientError, BotoCoreError) as exc:
            warnings.append("Versions of {} could not be read: {}".format(addon_name, _aws_error(exc)))
        upgrade_available = bool(latest and _addon_version_key(latest) > _addon_version_key(installed))
        if upgrade_available:
            updates_available += 1
        rows.append({
            "name": addon_name,
            "version": installed,
            "latest": latest or "",
            "latest_is_default": bool(latest and latest == default),
            "status": _status(addon.get("status")),
            "issues": _issues(addon.get("health")),
            "owner": addon.get("owner") or "",
            "publisher": addon.get("publisher") or "",
            "modified": _day(addon.get("modifiedAt")),
            "upgrade_available": upgrade_available,
            "upgrade_url": (
                reverse("aws_eks_upgrade_addon", args=[resource.id, addon_name])
                if upgrade_available and can_upgrade and NAME_RE.match(addon_name)
                else ""
            ),
        })
    return {
        "rows": rows,
        "kubernetes_version": kubernetes_version,
        "updates_available": updates_available,
        "warnings": warnings,
    }


def _asg_counts(resource, asg_names, warnings):
    """{Auto Scaling group name: (in-service instances, all instances)} for
    the node groups' groups, or None when the handler may not read them.
    https://docs.aws.amazon.com/autoscaling/ec2/APIReference/API_DescribeAutoScalingGroups.html"""
    counts = {}
    if not asg_names:
        return counts
    try:
        client = _client(resource, "autoscaling")
        for start in range(0, len(asg_names), 50):
            batch = asg_names[start:start + 50]
            token = None
            while True:
                params = {"AutoScalingGroupNames": batch, "MaxRecords": 100}
                if token:
                    params["NextToken"] = token
                page = client.describe_auto_scaling_groups(**params)
                for group in page.get("AutoScalingGroups", []) or []:
                    instances = group.get("Instances") or []
                    in_service = sum(1 for i in instances if i.get("LifecycleState") == "InService")
                    counts[group.get("AutoScalingGroupName")] = (in_service, len(instances))
                token = page.get("NextToken")
                if not token:
                    break
    except (ClientError, BotoCoreError) as exc:
        warnings.append(
            "Node counts need autoscaling:DescribeAutoScalingGroups on the handler: {}".format(_aws_error(exc))
        )
        return None
    return counts


def _nodegroups_context(resource, profile):
    client = _client(resource)
    name = _cluster_name(resource)
    warnings = []
    cluster_version = client.describe_cluster(name=name)["cluster"].get("version") or ""

    # https://docs.aws.amazon.com/eks/latest/APIReference/API_ListNodegroups.html
    # https://docs.aws.amazon.com/eks/latest/APIReference/API_DescribeNodegroup.html
    groups = [
        client.describe_nodegroup(clusterName=name, nodegroupName=nodegroup_name)["nodegroup"]
        for nodegroup_name in sorted(_paginate(client.list_nodegroups, "nodegroups", clusterName=name))
    ]
    tracked = set(
        Resource.objects.filter(
            parent_resource=resource,
            resource_type__name=NODEGROUP_RESOURCE_TYPE,
            lifecycle="ACTIVE",
        ).values_list("name", flat=True)
    )

    def asg_names(group):
        return [a["name"] for a in (group.get("resources") or {}).get("autoScalingGroups", []) or [] if a.get("name")]

    counts = _asg_counts(resource, [n for g in groups for n in asg_names(g)], warnings)
    can_scale = _action_for(profile, resource, ACTION_SCALE_NODEGROUP) is not None
    can_delete = _action_for(profile, resource, ACTION_DELETE_NODEGROUP) is not None
    # CloudBolt's own dialog: the action's dropdown lists every node group
    # and target, so one URL serves every row.
    upgrade_url = _action_url(profile, resource, ACTION_UPGRADE_NODEGROUP)

    rows = []
    for group in groups:
        nodegroup_name = group.get("nodegroupName") or ""
        scaling = group.get("scalingConfig") or {}
        nodes = None
        if counts is not None and asg_names(group):
            nodes = {
                "in_service": sum(counts.get(n, (0, 0))[0] for n in asg_names(group)),
                "total": sum(counts.get(n, (0, 0))[1] for n in asg_names(group)),
            }
        version = group.get("version") or ""
        valid_name = bool(NAME_RE.match(nodegroup_name))
        rows.append({
            "name": nodegroup_name,
            "status": _status(group.get("status")),
            "ami_type": group.get("amiType") or "",
            "release_version": group.get("releaseVersion") or "",
            "version": version,
            "behind": bool(version and cluster_version and _version_key(version) < _version_key(cluster_version)),
            "instance_types": group.get("instanceTypes") or [],
            "capacity_type": CAPACITY_TYPES.get(group.get("capacityType"), group.get("capacityType") or ""),
            "disk_size": group.get("diskSize"),
            "created": _day(group.get("createdAt")),
            "scaling": {
                "min": scaling.get("minSize"),
                "desired": scaling.get("desiredSize"),
                "max": scaling.get("maxSize"),
            },
            "nodes": nodes,
            "issues": _issues(group.get("health")),
            "tracked": nodegroup_name in tracked,
            "upgrade_url": upgrade_url if group.get("amiType") != CUSTOM_AMI_TYPE else "",
            "scale_url": (
                reverse("aws_eks_scale_nodegroup", args=[resource.id, nodegroup_name])
                if can_scale and valid_name else ""
            ),
            "delete_url": (
                reverse("aws_eks_delete_nodegroup", args=[resource.id, nodegroup_name])
                if can_delete and valid_name and nodegroup_name in tracked else ""
            ),
        })
    return {"rows": rows, "kubernetes_version": cluster_version, "warnings": warnings}


# ---------------------------------------------------------------------------
# Kubernetes reads (through the orchestrator's token)
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
    the cluster-wide path is built here; requests_get adds the bearer token
    (minted from the AWS handler for EKS) and CloudBolt's SSL trust settings.
    https://kubernetes.io/docs/reference/kubernetes-api/workload-resources/deployment-v1/"""
    base = wrapper.get_clusterrole_url(None, skip_base_url_version=True)
    return wrapper.requests_get("{}/apps/v1/deployments".format(base)).json()


def _label(labels, keys):
    for key in keys:
        if labels.get(key):
            return labels[key]
    return ""


def _workloads_context(resource, profile):
    orchestrator = _orchestrator(resource)
    if orchestrator is None:
        raise EKSError(
            "No container orchestrator is linked to this resource (container_orchestrator_id). "
            "Run the blueprint's discovery to register one."
        )
    try:
        # The wrapper's raw getters read the core API cluster-wide when no
        # namespace is given (GET /api/v1/nodes, /pods, /namespaces, /services).
        wrapper = orchestrator.get_api_wrapper()
        nodes = wrapper.get_nodes_raw().get("items", []) or []
        pods = wrapper.get_pods_raw().get("items", []) or []
        namespaces = wrapper.get_namespaces_raw().get("items", []) or []
        services = wrapper.get_services_raw().get("items", []) or []
        deployments = _k8s_deployments(wrapper).get("items", []) or []
    except Exception as exc:
        logger.exception("Kubernetes API read failed for resource %s", resource.id)
        raise EKSError(
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
            ready_pill = {"text": "Ready", "style": "ok", "icon": "bi-check-circle-fill"}
        elif ready.get("status") == "False":
            ready_pill = {"text": "Not ready", "style": "bad", "icon": "bi-x-circle-fill"}
            not_ready += 1
        else:
            ready_pill = {"text": "Unknown", "style": "bad", "icon": "bi-question-circle"}
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
            "nodegroup": labels.get(LABEL_NODEGROUP, ""),
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
    node_rows.sort(key=lambda r: (r["nodegroup"], r["name"]))

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

    # Namespace landing zones (AWS EKS Namespace blueprint) are sub-resources
    # of the cluster; link each one from its row so the owning team and the
    # CloudBolt resource are visible next to the live namespace.
    landing_zones = {}
    for zone in Resource.objects.filter(
        parent_resource=resource, resource_type__name=NAMESPACE_RESOURCE_TYPE, lifecycle="ACTIVE"
    ).select_related("group"):
        try:
            url = zone.get_absolute_url()
        except Exception:
            url = ""
        landing_zones[_attr(zone, "eks_namespace_name") or zone.name] = {
            "url": url,
            "group": zone.group.name if zone.group else "",
            "tier": _attr(zone, "eks_namespace_size_tier") or "",
        }

    namespace_rows = []
    for namespace in namespaces:
        name = (namespace.get("metadata") or {}).get("name", "")
        phase = (namespace.get("status") or {}).get("phase", "")
        if phase == "Active":
            pill = {"text": "Active", "style": "ok", "icon": "bi-check-circle"}
        else:
            pill = {"text": phase or "Unknown", "style": "warn", "icon": "bi-hourglass-split"}
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
        except EKSError as exc:
            context = {"error": str(exc)}
        except (ClientError, BotoCoreError) as exc:
            logger.exception("AWS call failed for resource %s", resource.id)
            context = {"error": "AWS returned an error: {}".format(_aws_error(exc))}
        except Exception as exc:
            logger.exception("Panel %s failed for resource %s", template, resource.id)
            context = {"error": "Could not load this panel: {}".format(exc)}
    context["resource"] = resource
    return render(request, TEMPLATES + template, context)


def cluster_panel(request, resource_id):
    return _panel(request, resource_id, "cluster_panel.html", _cluster_context)


def addons_panel(request, resource_id):
    return _panel(request, resource_id, "addons_panel.html", _addons_context)


def nodegroups_panel(request, resource_id):
    return _panel(request, resource_id, "nodegroups_panel.html", _nodegroups_context)


def workloads_panel(request, resource_id):
    return _panel(request, resource_id, "workloads_panel.html", _workloads_context)


def _connect_context(resource, profile):
    """Connection details for a namespace landing zone, from the shared module.

    Imported here rather than at module load so the cluster tabs keep working
    on an appliance where the eks_namespace module is not synced.
    """
    try:
        from shared_modules.eks_namespace import AccessEntryError, connection_details
    except ImportError:
        raise EKSError("The eks_namespace shared module (SHM-sa5yqm9j) is not on this CloudBolt.")
    try:
        details = connection_details(resource)
    except AccessEntryError as exc:
        raise EKSError(str(exc))
    cluster = details.pop("cluster_resource")
    cluster_url = ""
    if _can_view(profile, cluster):
        try:
            cluster_url = cluster.get_absolute_url()
        except Exception:
            cluster_url = ""
    details["access"] = [
        dict(row, scope_text=ACCESS_SCOPES.get(row["scope"], row["scope"])) for row in details["access"]
    ]
    details.update({
        "auth_mode_text": AUTH_MODES.get(details["auth_mode"], details["auth_mode"] or "Unknown"),
        "kubeconfig_filename": "kubeconfig-{}-{}.yaml".format(details["cluster_name"], details["namespace"]),
        "cluster_url": cluster_url,
    })
    return details


def namespace_connect_panel(request, resource_id):
    return _panel(request, resource_id, "connect_panel.html", _connect_context)


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
    if not NAME_RE.match(name or ""):
        return resource, profile, None, _dialog_message(title, "Invalid name.")
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


@dialog_view
def scale_nodegroup(request, resource_id, nodegroup_name):
    """Change a node group's min, desired and max nodes through the Scale AWS
    EKS Nodegroup action, prefilled from AWS."""
    title = "Scale node group {}".format(nodegroup_name)
    resource, profile, action, refusal = _dialog_guard(
        request, resource_id, nodegroup_name, title, ACTION_SCALE_NODEGROUP
    )
    if refusal:
        return refusal
    try:
        nodegroup = _client(resource).describe_nodegroup(
            clusterName=_cluster_name(resource), nodegroupName=nodegroup_name
        )["nodegroup"]
    except EKSError as exc:
        return _dialog_message(title, str(exc))
    except (ClientError, BotoCoreError) as exc:
        return _dialog_message(title, "AWS could not read the node group: {}".format(_aws_error(exc)))

    scaling = nodegroup.get("scalingConfig") or {}
    initial = {
        "min_nodes": scaling.get("minSize"),
        "desired_nodes": scaling.get("desiredSize"),
        "max_nodes": scaling.get("maxSize"),
    }
    if request.method == "POST":
        form = ScaleNodegroupForm(request.POST, initial=initial)
        if form.is_valid():
            values = {
                "nodegroup_name": nodegroup_name,
                "min_nodes": form.cleaned_data["min_nodes"],
                "max_nodes": form.cleaned_data["max_nodes"],
                "desired_nodes": form.cleaned_data["desired_nodes"],
            }
            _run_action(request, profile, resource, action, values, "Scaling node group {}".format(nodegroup_name))
            return _redirect_back(request, resource)
    else:
        form = ScaleNodegroupForm(initial=initial)

    content = format_html(
        "<p>Node group <b>{}</b> is {} with min {} / desired {} / max {} nodes. "
        "The change runs as a CloudBolt job through the <b>{}</b> action.</p>",
        nodegroup_name, _status(nodegroup.get("status"))["text"].lower(),
        scaling.get("minSize"), scaling.get("desiredSize"), scaling.get("maxSize"), action.label,
    )
    return {
        "title": title,
        "content": content,
        "form": form,
        "action_url": reverse("aws_eks_scale_nodegroup", args=[resource.id, nodegroup_name]),
        "use_ajax": True,
        "submit": "Scale",
    }


@dialog_view
def delete_nodegroup(request, resource_id, nodegroup_name):
    """Delete a node group through the Delete AWS EKS Nodegroup action, which
    takes the node group's eks_nodegroup sub-resource."""
    title = "Delete node group {}".format(nodegroup_name)
    resource, profile, action, refusal = _dialog_guard(
        request, resource_id, nodegroup_name, title, ACTION_DELETE_NODEGROUP
    )
    if refusal:
        return refusal
    sub_resource = Resource.objects.filter(
        parent_resource=resource,
        resource_type__name=NODEGROUP_RESOURCE_TYPE,
        name=nodegroup_name,
        lifecycle="ACTIVE",
    ).first()
    if sub_resource is None:
        return _dialog_message(
            title,
            "CloudBolt has no eks_nodegroup sub-resource for this node group. Use Sync from AWS "
            "(List AWS EKS Nodegroup) first, then delete it.",
        )
    if request.method == "POST":
        _run_action(
            request, profile, resource, action,
            {"delete_nodegroup": str(sub_resource.id)},
            "Deleting node group {}".format(nodegroup_name),
        )
        return _redirect_back(request, resource)

    live = ""
    try:
        nodegroup = _client(resource).describe_nodegroup(
            clusterName=_cluster_name(resource), nodegroupName=nodegroup_name
        )["nodegroup"]
        scaling = nodegroup.get("scalingConfig") or {}
        live = "It is {} with {} desired nodes.".format(
            _status(nodegroup.get("status"))["text"].lower(), scaling.get("desiredSize")
        )
    except (EKSError, ClientError, BotoCoreError):
        live = ""
    content = format_html(
        "<p>Delete node group <b>{}</b> from cluster <b>{}</b>? {} Every node in the group is "
        "terminated and its pods are evicted; the sub-resource is removed once AWS finishes. "
        "The deletion runs as a CloudBolt job through the <b>{}</b> action.</p>",
        nodegroup_name, _cluster_name(resource), live, action.label,
    )
    return {
        "title": title,
        "content": content,
        "action_url": reverse("aws_eks_delete_nodegroup", args=[resource.id, nodegroup_name]),
        "use_ajax": True,
        "submit": "Delete",
        "theme": "danger",
    }


@dialog_view
def upgrade_addon(request, resource_id, addon_name):
    """Move an add-on to a newer compatible version through the Update AWS
    EKS Add-on action."""
    title = "Upgrade add-on {}".format(addon_name)
    resource, profile, action, refusal = _dialog_guard(
        request, resource_id, addon_name, title, ACTION_UPDATE_ADDON
    )
    if refusal:
        return refusal
    try:
        client = _client(resource)
        name = _cluster_name(resource)
        kubernetes_version = client.describe_cluster(name=name)["cluster"].get("version") or ""
        addon = client.describe_addon(clusterName=name, addonName=addon_name)["addon"]
        versions, default = _compatible_addon_versions(client, addon_name, kubernetes_version)
    except EKSError as exc:
        return _dialog_message(title, str(exc))
    except (ClientError, BotoCoreError) as exc:
        return _dialog_message(title, "AWS could not read the add-on: {}".format(_aws_error(exc)))

    installed = addon.get("addonVersion") or ""
    newer = [v for v in versions if _addon_version_key(v) > _addon_version_key(installed)]
    if not newer:
        return _dialog_message(
            title,
            "Add-on {} already runs {}, the latest version AWS publishes for Kubernetes {}.".format(
                addon_name, installed or "no version", kubernetes_version
            ),
            css="text-muted",
        )
    choices = []
    for index, version in enumerate(newer):
        notes = []
        if index == 0:
            notes.append("latest")
        if version == default:
            notes.append("EKS default")
        choices.append((version, "{}{}".format(version, " ({})".format(", ".join(notes)) if notes else "")))

    if request.method == "POST":
        form = UpgradeAddonForm(request.POST, versions=choices)
        if form.is_valid():
            values = {
                "addon": "{}/{}".format(addon_name, form.cleaned_data["addon_version"]),
                "resolve_conflicts": form.cleaned_data["resolve_conflicts"],
            }
            _run_action(request, profile, resource, action, values, "Updating add-on {}".format(addon_name))
            return _redirect_back(request, resource)
    else:
        form = UpgradeAddonForm(versions=choices, initial={"addon_version": newer[0]})

    content = format_html(
        "<p>Add-on <b>{}</b> runs <b>{}</b>; AWS publishes <b>{}</b> for Kubernetes {}. "
        "The update runs as a CloudBolt job through the <b>{}</b> action.</p>",
        addon_name, installed or "no version", newer[0], kubernetes_version, action.label,
    )
    return {
        "title": title,
        "content": content,
        "form": form,
        "action_url": reverse("aws_eks_upgrade_addon", args=[resource.id, addon_name]),
        "use_ajax": True,
        "submit": "Upgrade",
    }


# ---------------------------------------------------------------------------
# Tabs
# ---------------------------------------------------------------------------
class EKSTabDelegate(TabExtensionDelegate):
    def should_display(self):
        return bool(_attr(self.instance, "eks_cluster_name"))


class NamespaceTabDelegate(TabExtensionDelegate):
    """Namespace landing zones of the AWS EKS Namespace blueprint."""

    def should_display(self):
        resource_type = getattr(self.instance, "resource_type", None)
        return (
            getattr(resource_type, "name", "") == NAMESPACE_RESOURCE_TYPE
            and bool(_attr(self.instance, "eks_namespace_name"))
        )


def _tab_context(resource):
    region = _attr(resource, "aws_region") or ""
    handler_name = ""
    try:
        handler, handler_region = _aws(resource)
        handler_name = handler.name
        region = region or handler_region
    except EKSError:
        # The panel explains it.
        pass
    environment_name = ""
    env_id = _attr(resource, "env_id")
    if env_id:
        try:
            env = Environment.objects.filter(id=int(str(env_id).strip())).first()
            environment_name = env.name if env else ""
        except (TypeError, ValueError):
            environment_name = ""
    name = _cluster_name(resource)
    return {
        "resource": resource,
        "cluster_name": name,
        "region": region,
        "handler_name": handler_name,
        "environment_name": environment_name,
        "console_url": _console_url(region, name),
    }


@tab_extension(
    model=Resource,
    title="Cluster",
    delegate=EKSTabDelegate,
    description="Live EKS cluster status, Kubernetes update notice, and add-ons",
)
def cluster_tab(request, obj_id):
    resource = get_object_or_404(Resource, pk=obj_id)
    context = _tab_context(resource)
    context.update({
        "cluster_url": reverse("aws_eks_cluster_panel", args=[resource.id]),
        "addons_url": reverse("aws_eks_addons_panel", args=[resource.id]),
    })
    return render(request, TEMPLATES + "overview_tab.html", context)


@tab_extension(
    model=Resource,
    title="Node groups",
    delegate=EKSTabDelegate,
    description="EKS managed node groups with scaling, node counts, health, and Scale and Delete actions",
)
def nodegroups_tab(request, obj_id):
    resource = get_object_or_404(Resource, pk=obj_id)
    profile = request.get_user_profile()
    context = _tab_context(resource)
    context.update({
        "nodegroups_url": reverse("aws_eks_nodegroups_panel", args=[resource.id]),
        "add_url": _action_url(profile, resource, ACTION_ADD_NODEGROUP),
        "sync_url": _action_url(profile, resource, ACTION_LIST_NODEGROUPS),
    })
    return render(request, TEMPLATES + "nodegroups_tab.html", context)


@tab_extension(
    model=Resource,
    title="Workloads",
    delegate=EKSTabDelegate,
    description="Kubernetes nodes and namespaces read through the cluster's orchestrator token",
)
def workloads_tab(request, obj_id):
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
        "workloads_url": reverse("aws_eks_workloads_panel", args=[resource.id]),
        "orchestrator_name": orchestrator.name if orchestrator is not None else "",
        "orchestrator_url": orchestrator_url,
        "create_namespace_url": _action_url(
            request.get_user_profile(), resource, ACTION_CREATE_NAMESPACE
        ),
    })
    return render(request, TEMPLATES + "workloads_tab.html", context)


@tab_extension(
    model=Resource,
    title="Connect",
    delegate=NamespaceTabDelegate,
    description="kubectl commands, a kubeconfig, and the IAM roles and users with access to an EKS namespace",
)
def namespace_connect_tab(request, obj_id):
    resource = get_object_or_404(Resource, pk=obj_id)
    context = {
        "resource": resource,
        "namespace": _attr(resource, "eks_namespace_name") or resource.name,
        "cluster_name": _attr(resource, "eks_namespace_cluster") or "",
        "connect_url": reverse("aws_eks_namespace_connect_panel", args=[resource.id]),
        "manage_access_url": _action_url(
            request.get_user_profile(), resource, ACTION_MANAGE_NAMESPACE_ACCESS
        ),
        "pipeline_url": _action_url(
            request.get_user_profile(), resource, ACTION_MANAGE_PIPELINE_IDENTITY
        ),
    }
    return render(request, TEMPLATES + "connect_tab.html", context)
