"""
Resource action for AWS EKS Cluster Nodegroup Service blueprint.

The order-form options read the cluster resource; creating the node group is
in shared_modules/SHM-uj1izdsm (eks_nodegroups), which the blueprint's Create
Node Group build item uses too.
"""

from resources.models import Resource
from shared_modules import eks_nodegroups
from shared_modules.eks_nodegroups import AMI_FAMILIES, get_boto_client
from utilities.logger import ThreadLogger

logger = ThreadLogger(__name__)


def cluster_kubernetes_version(resource, client):
    version = resource.get_cf_values_as_dict().get("kubernetes_version", "")
    if version:
        return str(version).strip()
    return client.describe_cluster(name=resource.name)["cluster"]["version"]


def generate_options_for_ami_type(server=None, **kwargs):
    """
    AMI families AWS publishes for the cluster's Kubernetes version, checked
    through the SSM public parameters AWS documents for AMI lookup. The value
    carries the architecture (for the instance-type options) and the resource
    id (so the dependent generate-options call can find the cluster).
    """
    resource = kwargs.get("resource", None)

    if resource is None or resource == "":
        return []

    eks_client, env = get_boto_client(env_id=resource.env_id, boto_service="eks")
    version = cluster_kubernetes_version(resource, eks_client)

    ssm_client = env.resource_handler.cast().get_boto3_client(env.aws_region, "ssm")
    names = {
        family[3].format(version=version): family for family in AMI_FAMILIES
    }
    try:
        published = ssm_client.get_parameters(Names=list(names))["Parameters"]
        available = [names[param["Name"]] for param in published]
    except Exception as err:
        # Without ssm:GetParameters fall back to the families AWS publishes for
        # every supported version (AL2023 and Bottlerocket).
        logger.warning(f"Could not check AMI availability through SSM: {err}")
        available = [f for f in AMI_FAMILIES if not f[0].startswith("AL2_")]

    options = [("", "-------Select AMI Type-------")]
    for ami_type, architecture, label, _ in AMI_FAMILIES:
        if any(f[0] == ami_type for f in available):
            options.append(
                (f"{architecture}/{ami_type}/{resource.id}", f"{label} ({ami_type})")
            )

    return options


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


def run(job, resource, **kwargs):
    try:
        eks_nodegroups.add_nodegroup(
            job,
            resource,
            nodegroup_name="{{ nodegroup_name }}",
            ami_type="{{ ami_type }}",
            instance_type="{{ instance_type }}",
            disk_size="{{ disk_size }}",
            min_size="{{ min_nodes }}",
            max_size="{{ max_nodes }}",
            desired_size="{{ desired_nodes }}",
        )
    except RuntimeError as err:
        return "FAILURE", "", str(err)

    return "SUCCESS", "EKS NodeGroup created successfully", ""
