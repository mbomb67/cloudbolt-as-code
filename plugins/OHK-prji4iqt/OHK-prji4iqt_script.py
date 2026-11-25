"""
Resource action for AWS EKS Cluster Nodegroup List Service blueprint.
"""

from infrastructure.models import CustomField, Environment
from resourcehandlers.aws.models import AWSHandler
from resources.models import ResourceType, Resource
from utilities.logger import ThreadLogger
from common.methods import set_progress

logger = ThreadLogger(__name__)


def get_or_create_custom_fields():
    """
    get_or_create_custom_fields
    """

    CustomField.objects.get_or_create(
        name="eks_nodegroup_name",
        type="STR",
        defaults={
            "label": "AWS EKS Cluster Node Group Name",
            "description": "Used by the AWS EKS blueprint",
            "show_as_attribute": True,
            "show_on_servers": True,
        },
    )

    CustomField.objects.get_or_create(
        name="eks_nodegroup_resource_status",
        defaults={
            "label": "Resource status",
            "type": "STR",
            "description": "Used by the AWS EKS blueprint",
            "required": True,
            "show_as_attribute": True,
            "show_on_servers": True,
        },
    )

    CustomField.objects.get_or_create(
        name="eks_nodegroup_arn",
        defaults={
            "label": "Nodegroup Arn",
            "type": "STR",
            "description": "Used by the AWS EKS blueprint",
            "required": True,
            "show_as_attribute": True,
            "show_on_servers": True,
        },
    )
    CustomField.objects.get_or_create(
        name="eks_nodegroup_ami_type",
        defaults={
            "label": "Resource AMI Type",
            "type": "STR",
            "description": "Used by the AWS EKS blueprint",
            "required": True,
            "show_as_attribute": True,
            "show_on_servers": True,
        },
    )
    CustomField.objects.get_or_create(
        name="eks_nodegroup_instance_types",
        defaults={
            "label": "Resource Instance Types",
            "type": "STR",
            "description": "Used by the AWS EKS blueprint",
            "required": True,
            "show_as_attribute": True,
            "show_on_servers": True,
        },
    )
    CustomField.objects.get_or_create(
        name="eks_nodegroup_noderole_arn",
        defaults={
            "label": "Resource Noderole Arn",
            "type": "STR",
            "description": "Used by the AWS EKS blueprint",
            "required": True,
            "show_as_attribute": True,
            "show_on_servers": True,
        },
    )
    CustomField.objects.get_or_create(
        name="eks_nodegroup_node_min_size",
        defaults={
            "label": "Resource node min size",
            "type": "STR",
            "description": "Used by the AWS EKS blueprint",
            "required": True,
            "show_as_attribute": True,
            "show_on_servers": True,
        },
    )

    CustomField.objects.get_or_create(
        name="eks_nodegroup_node_max_size",
        defaults={
            "label": "Resource node max size",
            "type": "STR",
            "description": "Used by the AWS EKS blueprint",
            "required": True,
            "show_as_attribute": True,
            "show_on_servers": True,
        },
    )
    CustomField.objects.get_or_create(
        name="eks_nodegroup_desired_size",
        defaults={
            "label": "Resource node desired size",
            "type": "STR",
            "description": "Used by the AWS EKS blueprint",
            "required": True,
            "show_as_attribute": True,
            "show_on_servers": True,
        },
    )


def get_or_create_resource_type():
    """
    get or create resource type
    """
    rt, _ = ResourceType.objects.get_or_create(
        name="eks_nodegroup", defaults={"label": "EKS Nodegroup", "icon": "far fa-file"}
    )

    return rt


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


def create_eks_nodegroup_cb_subresource(
    resource, resource_type, nodegroup_resp, client
):
    """
    Create nodegroup cb sub resource
    params: resource : resource object
    params: resource_type : resource_type object
    params: nodegroup_resp :nodegroup object
    params: client : eks boto client

    """
    nodegroup_name = nodegroup_resp["nodegroupName"]
    response = client.describe_nodegroup(
        clusterName=resource.name, nodegroupName=nodegroup_name
    )

    nodegroup_resp = response["nodegroup"]
    # create nodegroup as a sub resource of blueprint
    res = Resource.objects.create(
        group=resource.group,
        parent_resource=resource,
        resource_type=resource_type,
        name=nodegroup_name,
        blueprint=resource.blueprint,
        lifecycle="ACTIVE",
    )

    res.eks_nodegroup_resource_status = nodegroup_resp["status"]
    res.eks_nodegroup_arn = nodegroup_resp["nodegroupArn"]
    # The AWS EKS discovery matches node group records by arn.
    res.arn = nodegroup_resp["nodegroupArn"]
    res.eks_nodegroup_ami_type = nodegroup_resp["amiType"]
    res.eks_nodegroup_instance_types = nodegroup_resp["instanceTypes"]
    res.eks_nodegroup_noderole_arn = nodegroup_resp["nodeRole"]
    res.eks_nodegroup_node_min_size = nodegroup_resp["scalingConfig"]["minSize"]
    res.eks_nodegroup_node_max_size = nodegroup_resp["scalingConfig"]["maxSize"]
    res.eks_nodegroup_desired_size = nodegroup_resp["scalingConfig"]["desiredSize"]
    res.save()

    logger.info(f"Sub Resource {res} created successfully.")


def list_eks_nodegroup(client, sub_resources, resource_type, resource):
    """
    Fetch the eks nodegroups and create it on cb server if it does not exist
    Return list
    """

    discovered_objects = []

    try:
        node_groups = client.list_nodegroups(clusterName=resource.name)["nodegroups"]
    except Exception as err:
        return "FAILURE", "", str(err)

    # clean node groups from CMP
    Resource.objects.filter(
        parent_resource=resource, resource_type=resource_type, lifecycle="ACTIVE"
    ).exclude(name__in=node_groups).delete()

    # fetch all eks node groups
    for nodegroup in node_groups:

        discovered_objects.append(
            {
                "name": nodegroup,
            }
        )
        response = client.describe_nodegroup(
            clusterName=resource.name, nodegroupName=nodegroup
        )

        nodegroup_resp = response["nodegroup"]
        nodegroup_name = nodegroup_resp["nodegroupName"]
        nodegroup_status = nodegroup_resp["status"]

        # search nodegroup name in cb resource
        res = sub_resources.filter(name=nodegroup_name).first()

        if not res:
            set_progress(
                "Found new eks nodegroup '{0}', creating sub-resource...".format(
                    nodegroup
                )
            )

            # create nodegroup cb resource
            create_eks_nodegroup_cb_subresource(
                resource, resource_type, nodegroup_resp, client
            )
        else:

            res.eks_nodegroup_resource_status = nodegroup_status
            # Records made before the ARN was stored under arn get it here,
            # so the AWS EKS discovery can match them.
            res.eks_nodegroup_arn = nodegroup_resp["nodegroupArn"]
            res.arn = nodegroup_resp["nodegroupArn"]
            res.save()

    return discovered_objects


def run(job, resource, **kwargs):
    set_progress(f"Starting Provision of {resource} resource nodegroup.")
    logger.info(f"Starting Provision of {resource} resource nodegroup.")

    # get or create custom fields as needed
    get_or_create_custom_fields()

    # get or create resource type and custom fields
    resource_type = get_or_create_resource_type()

    # get all sub resource objects
    sub_resources = Resource.objects.filter(
        parent_resource=resource, resource_type=resource_type, lifecycle="ACTIVE"
    )

    # eks client
    client, _ = get_boto_client(resource.env_id, "eks")

    # available nodegroup list
    node_groups = list_eks_nodegroup(client, sub_resources, resource_type, resource)

    logger.info(f"Eks Node Groups:  {node_groups}")

    set_progress(node_groups)

    return "SUCCESS", "Eks Node Groups synced successfully", ""