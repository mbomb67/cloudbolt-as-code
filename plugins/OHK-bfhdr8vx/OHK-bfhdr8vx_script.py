"""
Resource action for AWS EKS Cluster Nodegroup Service blueprint.
"""

import time
import json
from botocore.exceptions import ClientError
from infrastructure.models import CustomField, Environment
from resourcehandlers.aws.models import AWSHandler
from resources.models import ResourceType, Resource
from common.methods import set_progress
from utilities.logger import ThreadLogger

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
        name="eks_nodegroup",
        defaults={"label": "EKS Node Group", "icon": "far fa-file"},
    )

    return rt


def get_boto_client(env_id=None, boto_service="ec2"):
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


def generate_options_for_ami_type(server=None, **kwargs):
    """
    AMI families EKS still publishes for every supported Kubernetes version.
    EKS stopped publishing Amazon Linux 2 AMIs on 2025-11-26, so AL2 types fail
    on 1.33 and later clusters: https://docs.aws.amazon.com/eks/latest/userguide/al2023.html
    Valid amiType values: https://docs.aws.amazon.com/eks/latest/APIReference/API_CreateNodegroup.html
    The value carries the CPU architecture (for the instance-type options) and
    the resource id (so the dependent generate-options call can find the cluster).
    """
    resource = kwargs.get("resource", None)

    if resource is None or resource == "":
        return []

    return [
        ("", "-------Select AMI Type-------"),
        (f"x86_64/AL2023_x86_64_STANDARD/{resource.id}", "Amazon Linux 2023 (AL2023_x86_64_STANDARD)"),
        (f"arm64/AL2023_ARM_64_STANDARD/{resource.id}", "Amazon Linux 2023 Arm (AL2023_ARM_64_STANDARD)"),
        (f"x86_64/BOTTLEROCKET_x86_64/{resource.id}", "Bottlerocket (BOTTLEROCKET_x86_64)"),
        (f"arm64/BOTTLEROCKET_ARM_64/{resource.id}", "Bottlerocket Arm (BOTTLEROCKET_ARM_64)"),
    ]


def generate_options_for_instance_type(server=None, control_value=None, **kwargs):
    """
    Instance types of the chosen architecture offered in every availability
    zone the cluster's subnets span. Both EC2 calls page their results, so
    they go through paginators; a single call returns only the first page
    (describe_instance_types caps at 100), which hid most sizes, t3 included.
    https://boto3.amazonaws.com/v1/documentation/api/latest/reference/services/ec2/paginator/DescribeInstanceTypes.html
    https://boto3.amazonaws.com/v1/documentation/api/latest/reference/services/ec2/paginator/DescribeInstanceTypeOfferings.html
    """
    options = []

    if control_value is None or control_value == "":
        return options

    architecture, _, resource_id = control_value.split("/")
    resource = Resource.objects.get(id=resource_id)

    client, env = get_boto_client(env_id=resource.env_id, boto_service="ec2")

    eks_subnets = [sub.strip(" '[]") for sub in resource.eks_subnets.split(",")]
    rh = env.resource_handler.cast()

    availability_zones = sorted(
        {
            vpc_subnet["availability_zone"]
            for vpc_subnet in rh.get_all_vpc_subnets(env.aws_region, env.vpc_id)
            if vpc_subnet["network"] in eks_subnets
        }
    )

    if not availability_zones:
        return options

    # instance types offered per zone; a type must be offered in every zone
    offered_in_zone = {zone: set() for zone in availability_zones}
    offerings_paginator = client.get_paginator("describe_instance_type_offerings")
    for page in offerings_paginator.paginate(
        LocationType="availability-zone",
        Filters=[{"Name": "location", "Values": availability_zones}],
    ):
        for offering in page["InstanceTypeOfferings"]:
            offered_in_zone[offering["Location"]].add(offering["InstanceType"])

    offered_everywhere = set.intersection(*offered_in_zone.values())

    if not offered_everywhere:
        return options

    types_paginator = client.get_paginator("describe_instance_types")
    for page in types_paginator.paginate(
        Filters=[
            {"Name": "processor-info.supported-architecture", "Values": [architecture]}
        ]
    ):
        for instance_type in page["InstanceTypes"]:
            ins_type = instance_type["InstanceType"]
            if ins_type not in offered_everywhere:
                continue
            vcpu = instance_type["VCpuInfo"]["DefaultVCpus"]
            memory = instance_type["MemoryInfo"]["SizeInMiB"] / 1024

            options.append(
                (ins_type, f"{ins_type} ({vcpu} vCPU, {memory:g} GiB)")
            )

    return sorted(options, key=lambda tup: tup[1].lower())


def _get_or_create_node_group_role(env_obj):
    """
    Find a custom node group role or Create a new node group role
    """
    # get iam boto3 client
    iam_client, _ = get_boto_client(env_obj.id, "iam")

    # assume_role_policy_document for role
    assume_role_policy_document = json.dumps(
        {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Principal": {"Service": "ec2.amazonaws.com"},
                    "Action": "sts:AssumeRole",
                }
            ],
        }
    )

    role_name = "CustomAmazonEKSNodeRole"

    try:
        # get eks node group role
        return iam_client.get_role(RoleName=role_name)["Role"]["Arn"]
    except Exception as err:
        pass

    try:
        # create eks node group role
        role_rsp = iam_client.create_role(
            Path="/",
            RoleName=role_name,
            AssumeRolePolicyDocument=assume_role_policy_document,
            Description="Allows EC2 instances to call nodegroup on your behalf.",
            MaxSessionDuration=3600,
        )
    except Exception as err:
        iam_client.delete_role(RoleName=role_name)
        raise RuntimeError(f"Role could not be created...{err}")

    for policy in [
        "arn:aws:iam::aws:policy/AmazonEKSWorkerNodePolicy",
        "arn:aws:iam::aws:policy/AmazonEC2ContainerRegistryReadOnly",
        "arn:aws:iam::aws:policy/AmazonEKS_CNI_Policy",
    ]:
        try:
            # attach policy to eks node group role
            iam_client.attach_role_policy(RoleName=role_name, PolicyArn=policy)
        except Exception as error:
            raise RuntimeError(f"policy could not be created...{error}")

    return role_rsp["Role"]["Arn"]


def create_eks_nodegroup(
    client,
    resource,
    nodegroup_name,
    nodegroup_role,
    ami_type,
    desired_size,
    min_size,
    max_size,
):
    """
    create_eks_nodegroup
    """
    # subnet from cluster resource
    subnets = [sub.strip(" '[]") for sub in resource.eks_subnets.split(",")]

    try:
        node_rsp = client.create_nodegroup(
            clusterName=resource.name,
            nodegroupName=nodegroup_name,
            scalingConfig={
                "minSize": min_size,
                "maxSize": max_size,
                "desiredSize": desired_size,
            },
            diskSize=int("{{disk_size}}"),
            subnets=subnets,
            instanceTypes=["{{instance_type}}"],
            amiType=ami_type,
            nodeRole=nodegroup_role,
        )["nodegroup"]
    except ClientError as err:
        set_progress("AWS ClientError: {}".format(err))
        raise RuntimeError(f"EKS NodeGroup creation failed - {err}")
    except Exception as err:
        raise RuntimeError(f"EKS NodeGroup creation failed - {err}")

    while node_rsp["status"] == "CREATING":
        set_progress(
            'Node Group "{}" is being created'.format(node_rsp["nodegroupName"]),
            increment_tasks=1,
        )
        time.sleep(60)

        node_rsp = client.describe_nodegroup(
            clusterName=resource.name, nodegroupName=node_rsp["nodegroupName"]
        )["nodegroup"]

    logger.info(f"Node Group response: {node_rsp}")

    if node_rsp["status"] == "ACTIVE":
        return node_rsp

    raise RuntimeError(f"EKS NodeGroup creation failed - {node_rsp['status']}")


def create_eks_nodegroup_cb_subresource(resource, resource_type, create_ng_obj, job):
    """
    Create nodegroup cb sub resource
    params: resource : resource object
    params: resource : resource_type object
    params: create_ng_obj : eks nodegroup object
    params: job : request job object

    """
    # create nodegroup as a sub resource of blueprint
    res = Resource.objects.create(
        group=resource.group,
        parent_resource=resource,
        resource_type=resource_type,
        name=create_ng_obj["nodegroupName"],
        blueprint=resource.blueprint,
        lifecycle="ACTIVE",
        owner=job.owner,
    )

    res.eks_nodegroup_resource_status = create_ng_obj["status"]
    res.eks_nodegroup_arn = create_ng_obj["nodegroupArn"]
    res.eks_nodegroup_ami_type = create_ng_obj["amiType"]
    res.eks_nodegroup_instance_types = create_ng_obj["instanceTypes"]
    res.eks_nodegroup_noderole_arn = create_ng_obj["nodeRole"]
    res.eks_nodegroup_node_min_size = create_ng_obj["scalingConfig"]["minSize"]
    res.eks_nodegroup_node_max_size = create_ng_obj["scalingConfig"]["maxSize"]
    res.eks_nodegroup_desired_size = create_ng_obj["scalingConfig"]["desiredSize"]
    res.save()

    logger.info(f"Sub Resource {res} created successfully.")


def run(job, resource, **kwargs):
    set_progress(f"Starting Provision of {resource} resource nodegroup.")

    # get or create nodegroup custom fields
    get_or_create_custom_fields()

    # get or create resource type and custom fields
    resource_type = get_or_create_resource_type()

    # eks client
    client, _ = get_boto_client(resource.env_id, "eks")

    nodegroup_name = "{{ nodegroup_name }}"
    ami_type = "{{ami_type}}".split("/")[1]

    desired_size = int("{{desired_nodes}}")
    min_size = int("{{min_nodes}}")
    max_size = int("{{max_nodes}}")

    if max_size < desired_size:
        raise RuntimeError(
            f"Desired nodes {desired_size} should be equal or less than Max node {max_size}".format(
                desired_size, max_size
            )
        )

    elif min_size > desired_size:
        raise RuntimeError(
            f"Min nodes {min_size} should not be greater than desired nodes {desired_size}".format(
                min_size, desired_size
            )
        )

    env_obj = Environment.objects.get(id=resource.env_id)

    # create unique role for nodegroup
    nodegroup_role = _get_or_create_node_group_role(env_obj)

    # create nodegroup
    create_ng_obj = create_eks_nodegroup(
        client,
        resource,
        nodegroup_name,
        nodegroup_role,
        ami_type,
        desired_size,
        min_size,
        max_size,
    )

    # create eks nodegroup cb sub resource
    create_eks_nodegroup_cb_subresource(resource, resource_type, create_ng_obj, job)

    return "SUCCESS", "EKS NodeGroup created successfully", ""