"""
Day-2 action for the AWS EKS blueprint: update a managed node group to the
cluster's Kubernetes version, or to the latest AMI release for the version it
already runs.

Update EKS Version moves only the control plane. AWS requires every managed
node group to be on the control plane's minor version before the next
control-plane update, and publishes new AMI releases (kernel, containerd and
security patches) for every supported version. This action closes that gap
with UpdateNodegroupVersion: EKS launches replacement nodes from the new AMI,
drains the old nodes (honouring Pod disruption budgets unless the update is
forced) and terminates them. The job polls the update AWS returns until it
reaches a terminal status, then refreshes the node group's eks_nodegroup
sub-resource.

One dropdown lists each node group's valid targets, so no field dependency
is needed: the cluster's Kubernetes version when the node group is behind,
and the latest AMI release for its current version otherwise. Node groups
whose AMI comes from a launch template (amiType CUSTOM) cannot be updated
this way; they are listed so the message says what to do instead.

Vendor references:
  https://docs.aws.amazon.com/eks/latest/APIReference/API_UpdateNodegroupVersion.html
  https://docs.aws.amazon.com/eks/latest/APIReference/API_DescribeNodegroup.html
  https://docs.aws.amazon.com/eks/latest/APIReference/API_DescribeUpdate.html
  https://docs.aws.amazon.com/eks/latest/userguide/update-managed-node-group.html
"""

import time

from botocore.exceptions import ClientError

from common.methods import set_progress
from infrastructure.models import CustomField, Environment
from resourcehandlers.aws.models import AWSHandler
from resources.models import Resource
from utilities.logger import ThreadLogger

logger = ThreadLogger(__name__)

# A node group update replaces nodes in batches and usually takes 10 to 30
# minutes, so poll less often than the scaling action does.
POLL_SECONDS = 30
NODEGROUP_RESOURCE_TYPE = "eks_nodegroup"

# amiType EKS reports for a node group whose launch template names its own
# AMI. UpdateNodegroupVersion refuses version and releaseVersion for those.
# https://docs.aws.amazon.com/eks/latest/APIReference/API_Nodegroup.html
CUSTOM_AMI_TYPE = "CUSTOM"

# Marker in the option value for node groups this action cannot update.
CUSTOM_TARGET = "custom"

# DescribeUpdate error code EKS reports when nodes could not be drained
# because of a Pod disruption budget; the force flag is the documented remedy.
# https://docs.aws.amazon.com/eks/latest/APIReference/API_ErrorDetail.html
POD_EVICTION_ERROR = "PodEvictionFailure"


def version_key(version):
    return tuple(int(part) for part in str(version).split("."))


def get_eks_client(resource):
    """
    EKS client for the cluster's account and region: the environment the
    cluster was ordered into (env_id), or the handler and region the build
    and discovery plugins store on the resource.
    """
    values = resource.get_cf_values_as_dict()
    env_id = values.get("env_id")
    if env_id:
        env = Environment.objects.get(id=int(env_id))
        rh: AWSHandler = env.resource_handler.cast()
        return rh.get_boto3_client(env.aws_region, "eks")
    rh = AWSHandler.objects.get(id=int(values["aws_rh_id"]))
    return rh.get_boto3_client(values["aws_region"], "eks")


def cluster_name(resource):
    return resource.get_cf_values_as_dict().get("eks_cluster_name") or resource.name


def cluster_version(client, cluster):
    """
    The control plane's Kubernetes version, read live: the kubernetes_version
    custom field is only refreshed by the Update EKS Version action.
    https://docs.aws.amazon.com/eks/latest/APIReference/API_DescribeCluster.html
    """
    return client.describe_cluster(name=cluster)["cluster"]["version"]


def list_nodegroups(client, cluster):
    """
    Every managed node group in the cluster. The API pages with nextToken.
    https://docs.aws.amazon.com/eks/latest/APIReference/API_ListNodegroups.html
    """
    names = []
    token = None
    while True:
        params = {"clusterName": cluster}
        if token:
            params["nextToken"] = token
        page = client.list_nodegroups(**params)
        names.extend(page.get("nodegroups", []))
        token = page.get("nextToken")
        if not token:
            break
    return sorted(names)


def describe_nodegroup(client, cluster, name):
    # https://docs.aws.amazon.com/eks/latest/APIReference/API_DescribeNodegroup.html
    return client.describe_nodegroup(clusterName=cluster, nodegroupName=name)["nodegroup"]


def ensure_custom_fields():
    """
    The version fields this action records on the eks_nodegroup sub-resource.
    The other eks_nodegroup_* fields are created by Add and List AWS EKS
    Nodegroup.
    """
    CustomField.objects.get_or_create(
        name="eks_nodegroup_kubernetes_version",
        defaults={
            "label": "Kubernetes Version",
            "type": "STR",
            "description": "Kubernetes version the node group's nodes run. Used by the AWS EKS blueprint.",
            "show_as_attribute": True,
        },
    )
    CustomField.objects.get_or_create(
        name="eks_nodegroup_release_version",
        defaults={
            "label": "AMI Release Version",
            "type": "STR",
            "description": "AMI release version the node group's nodes run. Used by the AWS EKS blueprint.",
            "show_as_attribute": True,
        },
    )


def generate_options_for_nodegroup_upgrade(server=None, **kwargs):
    """
    One entry per node group and target. A node group behind the control
    plane gets an entry for the cluster's Kubernetes version; every node
    group gets one for the latest AMI release of the version it runs. The
    value is 'name|version'; AWS resolves the version to its latest AMI
    release, which the job reports when it finishes.
    """
    resource = kwargs.get("resource", None)
    if resource is None:
        return []

    client = get_eks_client(resource)
    cluster = cluster_name(resource)
    control_plane = cluster_version(client, cluster)

    options = []
    for name in list_nodegroups(client, cluster):
        nodegroup = describe_nodegroup(client, cluster, name)
        version = nodegroup.get("version") or ""
        release = nodegroup.get("releaseVersion") or "unknown release"
        status = nodegroup.get("status") or ""

        if nodegroup.get("amiType") == CUSTOM_AMI_TYPE:
            options.append(
                (
                    f"{name}|{CUSTOM_TARGET}",
                    f"{name}: custom AMI from a launch template; update the launch "
                    "template version instead",
                )
            )
            continue

        if version and version_key(version) < version_key(control_plane):
            options.append(
                (
                    f"{name}|{control_plane}",
                    f"{name}: Kubernetes {version} to {control_plane} (cluster version), "
                    f"latest AMI release ({status})",
                )
            )
        options.append(
            (
                f"{name}|{version}",
                f"{name}: latest AMI release for Kubernetes {version}, now on {release} "
                f"({status})",
            )
        )

    if not options:
        return [("", "This cluster has no managed node groups")]
    return options


def refresh_sub_resource(resource, nodegroup):
    """Copy the node group's version and status onto its sub-resource, if any."""
    name = nodegroup.get("nodegroupName")
    sub_resource = Resource.objects.filter(
        parent_resource=resource,
        resource_type__name=NODEGROUP_RESOURCE_TYPE,
        name=name,
        lifecycle="ACTIVE",
    ).first()
    if sub_resource is None:
        set_progress(
            f"No eks_nodegroup sub-resource is named {name}; run List AWS EKS "
            "Nodegroup to create it."
        )
        return
    sub_resource.eks_nodegroup_resource_status = nodegroup.get("status")
    sub_resource.eks_nodegroup_ami_type = nodegroup.get("amiType")
    sub_resource.eks_nodegroup_kubernetes_version = nodegroup.get("version")
    sub_resource.eks_nodegroup_release_version = nodegroup.get("releaseVersion")
    sub_resource.save()


def run(job, resource, **kwargs):
    choice = "{{ nodegroup_upgrade }}".strip()
    force = "{{ force_update }}".strip().lower() in ("true", "1", "yes", "on")

    if "|" not in choice:
        return "FAILURE", "", "No node group and target were chosen."
    nodegroup_name, target = choice.split("|", 1)
    nodegroup_name, target = nodegroup_name.strip(), target.strip()
    if not nodegroup_name or not target:
        return "FAILURE", "", "No node group and target were chosen."
    if target == CUSTOM_TARGET:
        return (
            "FAILURE",
            "",
            f"Node group {nodegroup_name} runs a custom AMI from its launch template. "
            "EKS cannot update its Kubernetes version or AMI release directly: build a "
            "new AMI, publish a new launch template version with it, and update the node "
            "group to that launch template version.",
        )

    ensure_custom_fields()
    client = get_eks_client(resource)
    cluster = cluster_name(resource)

    try:
        nodegroup = describe_nodegroup(client, cluster, nodegroup_name)
    except ClientError as err:
        return "FAILURE", f"Node group {nodegroup_name} could not be read", str(err)

    current_version = nodegroup.get("version") or ""
    current_release = nodegroup.get("releaseVersion") or ""
    status = nodegroup.get("status") or ""

    if nodegroup.get("amiType") == CUSTOM_AMI_TYPE:
        return (
            "FAILURE",
            "",
            f"Node group {nodegroup_name} runs a custom AMI from its launch template; "
            "update the launch template version instead.",
        )
    if status != "ACTIVE":
        return (
            "FAILURE",
            "",
            f"Node group {nodegroup_name} is {status}; it can be updated only while ACTIVE.",
        )

    control_plane = cluster_version(client, cluster)
    try:
        if version_key(target) > version_key(control_plane):
            return (
                "FAILURE",
                "",
                f"Kubernetes {target} is newer than the control plane ({control_plane}); "
                "run Update EKS Version first.",
            )
        if current_version and version_key(target) < version_key(current_version):
            return (
                "FAILURE",
                "",
                f"Node group {nodegroup_name} already runs Kubernetes {current_version}; "
                f"this action does not roll back to {target}.",
            )
    except ValueError:
        return "FAILURE", "", f"'{target}' is not a Kubernetes version."

    if version_key(target) > version_key(current_version or target):
        what = f"from Kubernetes {current_version} ({current_release}) to {target}"
    else:
        what = f"from AMI release {current_release} to the latest release for Kubernetes {target}"
    set_progress(
        f"Updating node group {nodegroup_name} on {cluster} {what}"
        f"{' with Pod disruption budgets ignored' if force else ''}"
    )

    # Passing the version makes AWS pick the latest AMI release for it; the
    # same version as the node group's own means "latest release, same
    # Kubernetes". releaseVersion is left to AWS so nothing here has to know
    # which release is current for each AMI family.
    # https://docs.aws.amazon.com/eks/latest/APIReference/API_UpdateNodegroupVersion.html
    try:
        update = client.update_nodegroup_version(
            clusterName=cluster,
            nodegroupName=nodegroup_name,
            version=target,
            force=force,
        )["update"]
    except ClientError as err:
        return "FAILURE", f"EKS refused the update for {nodegroup_name}", str(err)

    requested = {p.get("type"): p.get("value") for p in update.get("params", []) or []}
    if requested.get("ReleaseVersion"):
        set_progress(
            f"EKS is moving {nodegroup_name} to Kubernetes {requested.get('Version', target)}, "
            f"AMI release {requested['ReleaseVersion']}"
        )

    # Track the update AWS returned; its status is InProgress, Failed,
    # Cancelled, or Successful.
    # https://docs.aws.amazon.com/eks/latest/APIReference/API_DescribeUpdate.html
    while update["status"] == "InProgress":
        set_progress(
            f'Node group "{nodegroup_name}" update {update["id"]} is {update["status"]}',
            increment_tasks=1,
        )
        time.sleep(POLL_SECONDS)
        update = client.describe_update(
            name=cluster, nodegroupName=nodegroup_name, updateId=update["id"]
        )["update"]

    if update["status"] != "Successful":
        errors = update.get("errors", []) or []
        detail = "; ".join(f"{e.get('errorCode')}: {e.get('errorMessage')}" for e in errors)
        if any(e.get("errorCode") == POD_EVICTION_ERROR for e in errors) and not force:
            detail += (
                " A Pod disruption budget blocked draining; rerun with Force update "
                "to replace the nodes regardless."
            )
        return (
            "FAILURE",
            f"Node group update finished with status {update['status']}",
            detail,
        )

    nodegroup = describe_nodegroup(client, cluster, nodegroup_name)
    refresh_sub_resource(resource, nodegroup)
    new_version = nodegroup.get("version") or ""
    new_release = nodegroup.get("releaseVersion") or ""

    logger.info(
        f"node group {nodegroup_name} on {cluster} updated to {new_version} / {new_release}"
    )

    if new_version == current_version and new_release == current_release:
        return (
            "SUCCESS",
            f"Node group {nodegroup_name} was already on Kubernetes {new_version}, "
            f"AMI release {new_release}; nothing changed.",
            "",
        )
    return (
        "SUCCESS",
        f"Node group {nodegroup_name} now runs Kubernetes {new_version}, AMI release "
        f"{new_release} (was {current_version}, {current_release}).",
        "",
    )
