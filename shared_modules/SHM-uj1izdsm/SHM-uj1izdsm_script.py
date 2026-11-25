"""
Managed node groups on the EKS clusters the AWS EKS blueprint (BP-r5apcf7e)
builds or discovers.

Shared by the blueprint's Create Node Group build item (plugins/OHK-c4gue79a)
and the Add AWS EKS Nodegroup action (RSA-f9ivvn5q, plugins/OHK-bfhdr8vx), so
both create a node group the same way:

  add_nodegroup(job, resource, nodegroup_name, ami_type, instance_type,
                disk_size, min_size, max_size, desired_size)
      Checks the scaling bounds and that the instance type matches the AMI's
      architecture and is offered in every availability zone of the cluster's
      subnets, creates or reuses the shared CustomAmazonEKSNodeRole, calls
      CreateNodegroup, waits until the node group is ACTIVE, and records it as
      an eks_nodegroup sub-resource of the cluster. Raises RuntimeError with
      a readable message on any failure.
  wait_for_ready_nodes(resource, wanted)
      Waits until the cluster's Kubernetes API reports that many Ready nodes;
      returns a note instead of raising when they are late.
  AMI_FAMILIES
      The EKS-optimized AMI families the order forms offer.

The two plugins keep separate inputs because one plugin bound to both a
resource action and a blueprint build item shares its input defaults between
them, which breaks the action's sync.

Import from plugins as::

    from shared_modules import eks_nodegroups

Shared-module code is cached by the running CloudBolt process: after changing
this file and syncing, restart CloudBolt for the change to take effect.
"""

import json
import time

from botocore.exceptions import ClientError

from common.methods import set_progress
from infrastructure.models import CustomField, Environment
from resources.models import Resource, ResourceType
from utilities.logger import ThreadLogger

logger = ThreadLogger(__name__)

NODEGROUP_RESOURCE_TYPE = "eks_nodegroup"
NODE_ROLE_NAME = "CustomAmazonEKSNodeRole"
NODE_ROLE_POLICIES = [
    "arn:aws:iam::aws:policy/AmazonEKSWorkerNodePolicy",
    "arn:aws:iam::aws:policy/AmazonEC2ContainerRegistryReadOnly",
    "arn:aws:iam::aws:policy/AmazonEKS_CNI_Policy",
]
POLL_SECONDS = 30

# amiType, CPU architecture, label, and the SSM public parameter that holds the
# recommended AMI for a Kubernetes version. Add AWS EKS Nodegroup offers a
# family only when AWS publishes an AMI for the cluster's version (AL2 ended
# with 1.32); the build item offers the families published for every version.
# Valid amiType values: https://docs.aws.amazon.com/eks/latest/APIReference/API_CreateNodegroup.html
# Amazon Linux parameters: https://docs.aws.amazon.com/eks/latest/userguide/retrieve-ami-id.html
# Bottlerocket parameters: https://docs.aws.amazon.com/eks/latest/userguide/retrieve-ami-id-bottlerocket.html
AMI_FAMILIES = [
    (
        "AL2023_x86_64_STANDARD",
        "x86_64",
        "Amazon Linux 2023 (x86_64)",
        "/aws/service/eks/optimized-ami/{version}/amazon-linux-2023/x86_64/standard/recommended/image_id",
    ),
    (
        "AL2023_ARM_64_STANDARD",
        "arm64",
        "Amazon Linux 2023 (Arm)",
        "/aws/service/eks/optimized-ami/{version}/amazon-linux-2023/arm64/standard/recommended/image_id",
    ),
    (
        "BOTTLEROCKET_x86_64",
        "x86_64",
        "Bottlerocket (x86_64)",
        "/aws/service/bottlerocket/aws-k8s-{version}/x86_64/latest/image_id",
    ),
    (
        "BOTTLEROCKET_ARM_64",
        "arm64",
        "Bottlerocket (Arm)",
        "/aws/service/bottlerocket/aws-k8s-{version}/arm64/latest/image_id",
    ),
    (
        "AL2_x86_64",
        "x86_64",
        "Amazon Linux 2 (x86_64)",
        "/aws/service/eks/optimized-ami/{version}/amazon-linux-2/recommended/image_id",
    ),
    (
        "AL2_ARM_64",
        "arm64",
        "Amazon Linux 2 (Arm)",
        "/aws/service/eks/optimized-ami/{version}/amazon-linux-2-arm64/recommended/image_id",
    ),
]


# ---------------------------------------------------------------------------
# Cluster lookups
# ---------------------------------------------------------------------------

def get_boto_client(env_id=None, boto_service="ec2"):
    """(boto3 client, Environment) for the AWS environment env_id."""
    if env_id is None:
        return None
    env = Environment.objects.get(id=env_id)
    rh = env.resource_handler.cast()
    return rh.get_boto3_client(env.aws_region, boto_service), env


def cluster_name(resource):
    """The EKS cluster's name in AWS, which the build and discovery store."""
    return resource.get_cf_values_as_dict().get("eks_cluster_name") or resource.name


def cluster_subnets(resource):
    """Subnet ids the cluster was created with, stored as the text of a list."""
    raw = str(resource.get_cf_values_as_dict().get("eks_subnets") or "")
    return [sub.strip(" '\"[]") for sub in raw.split(",") if sub.strip(" '\"[]")]


def default_nodegroup_name(resource):
    """
    <cluster>-nodes, cut to the 63 characters EKS allows. A cluster name
    starts with a letter or digit and holds only letters, digits, hyphens,
    and underscores, as a node group name must.
    https://docs.aws.amazon.com/eks/latest/userguide/create-managed-node-group.html
    """
    return f"{cluster_name(resource)[:57]}-nodes"


def parse_ami_type(value):
    """
    (architecture, amiType) from an AMI Type option value: architecture/amiType
    on the build item, architecture/amiType/resource-id on the action.
    """
    parts = [part.strip() for part in str(value or "").split("/")]
    if len(parts) >= 2 and parts[1]:
        return parts[0], parts[1]
    for ami_type, architecture, _, _ in AMI_FAMILIES:
        if ami_type == parts[0]:
            return architecture, ami_type
    raise RuntimeError(f"Unknown AMI type '{value}'.")


# ---------------------------------------------------------------------------
# CloudBolt records
# ---------------------------------------------------------------------------

def get_or_create_custom_fields():
    """The fields stored on each eks_nodegroup sub-resource."""
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
    for name, label in [
        ("eks_nodegroup_resource_status", "Resource status"),
        ("eks_nodegroup_arn", "Nodegroup Arn"),
        ("eks_nodegroup_ami_type", "Resource AMI Type"),
        ("eks_nodegroup_instance_types", "Resource Instance Types"),
        ("eks_nodegroup_noderole_arn", "Resource Noderole Arn"),
        ("eks_nodegroup_node_min_size", "Resource node min size"),
        ("eks_nodegroup_node_max_size", "Resource node max size"),
        ("eks_nodegroup_desired_size", "Resource node desired size"),
    ]:
        CustomField.objects.get_or_create(
            name=name,
            defaults={
                "label": label,
                "type": "STR",
                "description": "Used by the AWS EKS blueprint",
                "required": True,
                "show_as_attribute": True,
                "show_on_servers": True,
            },
        )


def get_or_create_resource_type():
    rt, _ = ResourceType.objects.get_or_create(
        name=NODEGROUP_RESOURCE_TYPE,
        defaults={"label": "EKS Node Group", "icon": "far fa-file"},
    )
    return rt


def create_nodegroup_subresource(resource, nodegroup, owner):
    """Record the node group as a sub-resource of the cluster."""
    res = Resource.objects.create(
        group=resource.group,
        parent_resource=resource,
        resource_type=get_or_create_resource_type(),
        name=nodegroup["nodegroupName"],
        blueprint=resource.blueprint,
        lifecycle="ACTIVE",
        owner=owner,
    )
    res.eks_nodegroup_resource_status = nodegroup["status"]
    res.eks_nodegroup_arn = nodegroup["nodegroupArn"]
    # The AWS EKS discovery matches node group records by arn.
    res.arn = nodegroup["nodegroupArn"]
    res.eks_nodegroup_ami_type = nodegroup["amiType"]
    res.eks_nodegroup_instance_types = nodegroup["instanceTypes"]
    res.eks_nodegroup_noderole_arn = nodegroup["nodeRole"]
    res.eks_nodegroup_node_min_size = nodegroup["scalingConfig"]["minSize"]
    res.eks_nodegroup_node_max_size = nodegroup["scalingConfig"]["maxSize"]
    res.eks_nodegroup_desired_size = nodegroup["scalingConfig"]["desiredSize"]
    res.save()
    logger.info(f"Sub Resource {res} created successfully.")
    return res


# ---------------------------------------------------------------------------
# AWS calls
# ---------------------------------------------------------------------------

def get_or_create_node_role(env):
    """
    (role ARN, created) for the node role every node group shares, created
    with the policies EKS documents for nodes when it does not exist yet.
    https://docs.aws.amazon.com/eks/latest/userguide/create-node-role.html
    """
    iam_client, _ = get_boto_client(env.id, "iam")
    try:
        return iam_client.get_role(RoleName=NODE_ROLE_NAME)["Role"]["Arn"], False
    except ClientError as err:
        if err.response.get("Error", {}).get("Code") != "NoSuchEntity":
            raise RuntimeError(f"Could not read IAM role {NODE_ROLE_NAME}: {err}")

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
    try:
        role = iam_client.create_role(
            Path="/",
            RoleName=NODE_ROLE_NAME,
            AssumeRolePolicyDocument=assume_role_policy_document,
            Description="Allows EC2 instances to call nodegroup on your behalf.",
            MaxSessionDuration=3600,
        )["Role"]
    except ClientError as err:
        raise RuntimeError(f"Role {NODE_ROLE_NAME} could not be created: {err}")

    for policy in NODE_ROLE_POLICIES:
        try:
            iam_client.attach_role_policy(RoleName=NODE_ROLE_NAME, PolicyArn=policy)
        except ClientError as err:
            raise RuntimeError(f"Policy {policy} could not be attached to {NODE_ROLE_NAME}: {err}")

    set_progress(f"Created IAM role {NODE_ROLE_NAME} for EKS nodes")
    return role["Arn"], True


def check_instance_type(env, subnets, instance_type, architecture):
    """
    Fail before creating anything when the instance type does not run the
    AMI's architecture or is not offered in every availability zone of the
    cluster's subnets, which would otherwise surface only as a failed node
    group many minutes later.
    https://docs.aws.amazon.com/AWSEC2/latest/APIReference/API_DescribeInstanceTypes.html
    https://docs.aws.amazon.com/AWSEC2/latest/APIReference/API_DescribeSubnets.html
    https://docs.aws.amazon.com/AWSEC2/latest/APIReference/API_DescribeInstanceTypeOfferings.html
    """
    client, _ = get_boto_client(env.id, "ec2")
    try:
        info = client.describe_instance_types(InstanceTypes=[instance_type])["InstanceTypes"]
    except ClientError as err:
        raise RuntimeError(f"Instance type {instance_type} is not available in {env.aws_region}: {err}")
    supported = []
    if info:
        supported = (info[0].get("ProcessorInfo") or {}).get("SupportedArchitectures") or []
    if architecture not in supported:
        raise RuntimeError(
            f"Instance type {instance_type} runs {', '.join(supported) or 'no known architecture'}, "
            f"not the {architecture} architecture of the chosen AMI type."
        )

    try:
        zones = sorted(
            {s["AvailabilityZone"] for s in client.describe_subnets(SubnetIds=subnets)["Subnets"]}
        )
        offered = {
            o["Location"]
            for o in client.describe_instance_type_offerings(
                LocationType="availability-zone",
                Filters=[
                    {"Name": "instance-type", "Values": [instance_type]},
                    {"Name": "location", "Values": zones},
                ],
            )["InstanceTypeOfferings"]
        }
    except ClientError as err:
        raise RuntimeError(f"Could not check where {instance_type} is offered: {err}")
    missing = [zone for zone in zones if zone not in offered]
    if missing:
        raise RuntimeError(
            f"Instance type {instance_type} is not offered in {', '.join(missing)}, where the cluster "
            "has subnets. Choose another instance type."
        )


def create_and_wait(client, cluster, params, role_created):
    """
    CreateNodegroup, then poll DescribeNodegroup until the node group leaves
    CREATING; a node group that ends in any status but ACTIVE is reported with
    its health issues.
    A node role created moments ago may not be usable by EKS yet, so its
    first create is retried for about a minute.
    https://docs.aws.amazon.com/eks/latest/APIReference/API_CreateNodegroup.html
    https://docs.aws.amazon.com/eks/latest/APIReference/API_DescribeNodegroup.html
    """
    attempts = 7 if role_created else 1
    for attempt in range(1, attempts + 1):
        try:
            nodegroup = client.create_nodegroup(clusterName=cluster, **params)["nodegroup"]
            break
        except ClientError as err:
            code = err.response.get("Error", {}).get("Code", "")
            if code == "InvalidParameterException" and attempt < attempts:
                set_progress(f"EKS does not accept the new node role yet; retrying ({err})")
                time.sleep(10)
                continue
            raise RuntimeError(f"EKS node group creation failed: {err}")

    name = nodegroup["nodegroupName"]
    while nodegroup["status"] == "CREATING":
        set_progress(f'Node group "{name}" is being created', increment_tasks=1)
        time.sleep(POLL_SECONDS)
        nodegroup = client.describe_nodegroup(clusterName=cluster, nodegroupName=name)["nodegroup"]

    logger.info(f"Node Group response: {nodegroup}")
    if nodegroup["status"] == "ACTIVE":
        return nodegroup

    issues = "; ".join(
        f"{issue.get('code')}: {issue.get('message')}"
        for issue in (nodegroup.get("health") or {}).get("issues") or []
    )
    raise RuntimeError(
        f"EKS node group {name} is {nodegroup['status']}" + (f": {issues}" if issues else "")
    )


def ready_node_count(resource):
    """
    Nodes that can run a pod (Ready and not cordoned), read through the
    cluster's CloudBolt container orchestrator the way the StormForge install
    checks them (shared_modules/SHM-2fd3bhsz, Kube and _ready_nodes).
    https://kubernetes.io/docs/reference/node/node-status/#condition
    """
    from containerorchestrators.kuberneteshandler.models import Kubernetes

    orchestrator_id = resource.get_cf_values_as_dict().get("container_orchestrator_id")
    if orchestrator_id in (None, ""):
        raise RuntimeError("the cluster has no CloudBolt container orchestrator")
    orchestrator = Kubernetes.objects.filter(id=int(str(orchestrator_id).strip())).first()
    if orchestrator is None:
        raise RuntimeError(f"container orchestrator {orchestrator_id} does not exist")
    wrapper = orchestrator.get_api_wrapper()
    base = wrapper.get_clusterrole_url("v1", skip_base_url_version=True)
    root = base[: -len("/api")] if base.endswith("/api") else base
    nodes = wrapper.requests_get(f"{root}/api/v1/nodes", expected_statuses=[200]).json()
    count = 0
    for node in nodes.get("items") or []:
        if (node.get("spec") or {}).get("unschedulable"):
            continue
        conditions = (node.get("status") or {}).get("conditions") or []
        if any(c.get("type") == "Ready" and c.get("status") == "True" for c in conditions):
            count += 1
    return count


def wait_for_ready_nodes(resource, wanted, timeout=600):
    """
    Wait until the cluster has `wanted` Ready nodes, so a step that runs pods
    next finds them. Returns a note when they are not all Ready in time or the
    Kubernetes API cannot be read; the node group itself is fine either way.
    """
    deadline = time.time() + timeout
    ready = 0
    while True:
        try:
            ready = ready_node_count(resource)
        except Exception as err:  # noqa: BLE001 - reported, the node group is already ACTIVE
            return f"Could not read the cluster's nodes through its Kubernetes API: {err}"
        if ready >= wanted:
            set_progress(f"{ready} node(s) Ready")
            return ""
        if time.time() >= deadline:
            return f"Only {ready} of {wanted} node(s) were Ready after {timeout // 60} minutes."
        set_progress(f"{ready} of {wanted} node(s) Ready", increment_tasks=1)
        time.sleep(15)


def add_nodegroup(
    job,
    resource,
    nodegroup_name,
    ami_type,
    instance_type,
    disk_size,
    min_size,
    max_size,
    desired_size,
):
    """Create a managed node group on the cluster resource and return it."""
    nodegroup_name = str(nodegroup_name or "").strip()
    instance_type = str(instance_type or "").strip()
    if not nodegroup_name:
        raise RuntimeError("A node group name is required.")
    if not instance_type:
        raise RuntimeError("An instance type is required.")
    architecture, ami_type = parse_ami_type(ami_type)
    try:
        disk_size, min_size, max_size, desired_size = (
            int(str(v).strip()) for v in (disk_size, min_size, max_size, desired_size)
        )
    except ValueError:
        raise RuntimeError("Disk size and the min, max, and desired node counts must be whole numbers.")
    if max_size < desired_size:
        raise RuntimeError(f"Desired nodes {desired_size} should be equal or less than Max nodes {max_size}")
    if min_size > desired_size:
        raise RuntimeError(f"Min nodes {min_size} should not be greater than desired nodes {desired_size}")

    cluster = cluster_name(resource)
    subnets = cluster_subnets(resource)
    if not subnets:
        raise RuntimeError(f"Cluster {cluster} has no subnets recorded on its resource.")
    env_id = resource.get_cf_values_as_dict().get("env_id")
    if not env_id:
        raise RuntimeError(f"Cluster {cluster} has no AWS environment recorded on its resource.")

    set_progress(
        f"Creating node group {nodegroup_name} on {cluster}: {ami_type}, {instance_type}, "
        f"{disk_size} GiB disk, {min_size}-{max_size} nodes ({desired_size} desired)"
    )
    get_or_create_custom_fields()
    get_or_create_resource_type()

    client, env = get_boto_client(int(str(env_id).strip()), "eks")
    check_instance_type(env, subnets, instance_type, architecture)
    role_arn, role_created = get_or_create_node_role(env)

    nodegroup = create_and_wait(
        client,
        cluster,
        {
            "nodegroupName": nodegroup_name,
            "scalingConfig": {
                "minSize": min_size,
                "maxSize": max_size,
                "desiredSize": desired_size,
            },
            "diskSize": disk_size,
            "subnets": subnets,
            "instanceTypes": [instance_type],
            "amiType": ami_type,
            "nodeRole": role_arn,
        },
        role_created,
    )
    create_nodegroup_subresource(resource, nodegroup, job.owner if job else resource.owner)
    set_progress(f"Node group {nodegroup_name} is ACTIVE with {desired_size} node(s)")
    return nodegroup
