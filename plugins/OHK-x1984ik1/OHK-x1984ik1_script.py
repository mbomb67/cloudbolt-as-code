"""
Day-2 action for the AWS EKS blueprint: change a managed node group's
minimum, maximum, and desired node counts.

The node group keeps serving while EKS applies the new scaling
configuration. The job polls the update AWS returns until it reaches a
terminal status and then refreshes the node group's eks_nodegroup
sub-resource in CloudBolt when one exists.
"""

import time

from botocore.exceptions import ClientError

from common.methods import set_progress
from infrastructure.models import Environment
from resourcehandlers.aws.models import AWSHandler
from resources.models import Resource
from utilities.logger import ThreadLogger

logger = ThreadLogger(__name__)

POLL_SECONDS = 15
NODEGROUP_RESOURCE_TYPE = "eks_nodegroup"


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


def generate_options_for_nodegroup_name(server=None, **kwargs):
    """
    The cluster's node groups as AWS lists them, with their current scaling
    configuration, so the dialog works even when the sub-resources are stale.
    """
    resource = kwargs.get("resource", None)
    if resource is None:
        return []

    client = get_eks_client(resource)
    cluster = cluster_name(resource)

    options = []
    for name in list_nodegroups(client, cluster):
        nodegroup = client.describe_nodegroup(clusterName=cluster, nodegroupName=name)[
            "nodegroup"
        ]
        scaling = nodegroup.get("scalingConfig", {})
        options.append(
            (
                name,
                f"{name} (min {scaling.get('minSize')} / desired "
                f"{scaling.get('desiredSize')} / max {scaling.get('maxSize')}, "
                f"{nodegroup.get('status')})",
            )
        )
    return options


def run(job, resource, **kwargs):
    nodegroup_name = "{{ nodegroup_name }}".strip()
    min_size = int("{{ min_nodes }}")
    max_size = int("{{ max_nodes }}")
    desired_size = int("{{ desired_nodes }}")

    if not nodegroup_name:
        return "FAILURE", "", "No node group was chosen."
    if min_size < 0 or max_size < 1:
        return (
            "FAILURE",
            "",
            "Minimum nodes must be 0 or more and maximum nodes at least 1.",
        )
    if not min_size <= desired_size <= max_size:
        return (
            "FAILURE",
            "",
            f"Desired nodes ({desired_size}) must be between minimum ({min_size}) "
            f"and maximum ({max_size}).",
        )

    client = get_eks_client(resource)
    cluster = cluster_name(resource)

    try:
        nodegroup = client.describe_nodegroup(
            clusterName=cluster, nodegroupName=nodegroup_name
        )["nodegroup"]
    except ClientError as err:
        return "FAILURE", f"Node group {nodegroup_name} could not be read", str(err)

    current = nodegroup.get("scalingConfig", {})
    wanted = {"minSize": min_size, "maxSize": max_size, "desiredSize": desired_size}
    if all(current.get(key) == value for key, value in wanted.items()):
        return (
            "SUCCESS",
            f"Node group {nodegroup_name} already runs min {min_size} / desired "
            f"{desired_size} / max {max_size}.",
            "",
        )

    set_progress(
        f"Scaling node group {nodegroup_name} from min {current.get('minSize')} / "
        f"desired {current.get('desiredSize')} / max {current.get('maxSize')} to "
        f"min {min_size} / desired {desired_size} / max {max_size}"
    )

    # https://docs.aws.amazon.com/eks/latest/APIReference/API_UpdateNodegroupConfig.html
    try:
        update = client.update_nodegroup_config(
            clusterName=cluster, nodegroupName=nodegroup_name, scalingConfig=wanted
        )["update"]
    except ClientError as err:
        return "FAILURE", f"EKS refused the scaling change for {nodegroup_name}", str(err)

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
        errors = "; ".join(
            f"{e.get('errorCode')}: {e.get('errorMessage')}"
            for e in update.get("errors", [])
        )
        return (
            "FAILURE",
            f"Node group update finished with status {update['status']}",
            errors,
        )

    nodegroup = client.describe_nodegroup(
        clusterName=cluster, nodegroupName=nodegroup_name
    )["nodegroup"]
    scaling = nodegroup.get("scalingConfig", {})

    sub_resource = Resource.objects.filter(
        parent_resource=resource,
        resource_type__name=NODEGROUP_RESOURCE_TYPE,
        name=nodegroup_name,
        lifecycle="ACTIVE",
    ).first()
    if sub_resource is not None:
        sub_resource.eks_nodegroup_node_min_size = scaling.get("minSize")
        sub_resource.eks_nodegroup_node_max_size = scaling.get("maxSize")
        sub_resource.eks_nodegroup_desired_size = scaling.get("desiredSize")
        sub_resource.eks_nodegroup_resource_status = nodegroup.get("status")
        sub_resource.save()
    else:
        set_progress(
            f"No eks_nodegroup sub-resource is named {nodegroup_name}; run "
            "List AWS EKS Nodegroup to create it."
        )

    logger.info(f"node group {nodegroup_name} on {cluster} scaled to {scaling}")

    return (
        "SUCCESS",
        f"Node group {nodegroup_name} now runs min {scaling.get('minSize')} / "
        f"desired {scaling.get('desiredSize')} / max {scaling.get('maxSize')}.",
        "",
    )
