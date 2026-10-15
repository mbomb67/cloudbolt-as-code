"""
Day-2 action for the AWS EKS blueprint: delete one managed node group.
"""

from common.methods import set_progress
from infrastructure.models import Environment
from resourcehandlers.aws.models import AWSHandler
from resources.models import Resource
from utilities.logger import ThreadLogger

logger = ThreadLogger(__name__)


def generate_options_for_delete_nodegroup(server=None, **kwargs):
    """
    The cluster's node groups, as the eks_nodegroup sub-resources CloudBolt
    tracks. Run List AWS EKS Nodegroup first if they look stale.
    """
    resource = kwargs.get("resource", None)

    if resource is None:
        return []

    options = []

    for sub_resource in Resource.objects.filter(
        parent_resource=resource,
        resource_type__name="eks_nodegroup",
        lifecycle="ACTIVE",
    ).order_by("name"):
        status = sub_resource.get_cf_values_as_dict().get(
            "eks_nodegroup_resource_status", ""
        )
        label = f"{sub_resource.name} ({status})" if status else sub_resource.name
        options.append((sub_resource.id, label))

    return options


def get_boto_client(env_id=None, boto_service=""):
    """
    get_boto_client
    """
    if env_id is None:
        return None
    env = Environment.objects.get(id=env_id)
    rh: AWSHandler = env.resource_handler.cast()

    # get aws client object
    client = rh.get_boto3_client(env.aws_region, boto_service)

    return client, env


def delete_nodegroup(job, nodegroup_object, resource, client):
    """
    Delete the node group in AWS and wait for it to disappear.
    https://docs.aws.amazon.com/eks/latest/APIReference/API_DeleteNodegroup.html
    """
    try:
        # verify nodegroup
        client.describe_nodegroup(
            clusterName=resource.name, nodegroupName=nodegroup_object.name
        )
    except Exception as err:
        if "ResourceNotFound" in str(err):
            job.set_progress(
                f"Node group {nodegroup_object.name} is already gone from AWS"
            )
            return
        raise RuntimeError(err)

    job.set_progress("Deleting nodegroup {0}...".format(nodegroup_object.name))

    try:
        ng_delete_resp = client.delete_nodegroup(
            clusterName=resource.name, nodegroupName=nodegroup_object.name
        )
    except Exception as err:
        raise RuntimeError(
            f"Amazon EKS cluster nodegroup could not be deleted. Error: {err}"
        )

    try:
        # It takes a while for the nodegroup to be deleted.
        nodegroup_delete_waiter = client.get_waiter("nodegroup_deleted")
        nodegroup_delete_waiter.wait(
            clusterName=resource.name, nodegroupName=nodegroup_object.name
        )
    except Exception as err:
        raise RuntimeError(
            f"Amazon EKS cluster nodegroup could not be deleted. Error: {err}"
        )

    logger.info(f"nodegroup response {ng_delete_resp}")


def run(job, resource, **kwargs):
    set_progress(f"Deleting a node group from cluster {resource.name}.")

    delete_nodegroup_object = "{{delete_nodegroup}}"

    client, _ = get_boto_client(resource.env_id, "eks")

    nodegroup_object = Resource.objects.get(id=int(delete_nodegroup_object))

    # delete node group
    delete_nodegroup(job, nodegroup_object, resource, client)

    nodegroup_name = nodegroup_object.name
    nodegroup_object.delete()

    set_progress(f"Node Group {nodegroup_name} deleted successfully.")

    return "SUCCESS", f"Node Group {nodegroup_name} deleted successfully.", ""
