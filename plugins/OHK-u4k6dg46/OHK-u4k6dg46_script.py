"""
Build service item action for AWS EKS Cluster Service blueprint.
"""

from __future__ import unicode_literals
import json
import random
import string
import time
from botocore.exceptions import ClientError

from accounts.models import Group

from common.methods import set_progress
from infrastructure.models import CustomField, Environment, Namespace
from containerorchestrators.kuberneteshandler.models import Kubernetes
from containerorchestrators.models import ContainerOrchestratorTechnology
from resourcehandlers.aws.models import AWSHandler

from utilities.logger import ThreadLogger

logger = ThreadLogger(__name__)


def create_required_parameters():
    """
    We create the containerorchestrator namespace, to keep this CF from adding noise to
    the Parameters list page.
    """
    namespace, _ = Namespace.objects.get_or_create(name="containerorchestrators")
    CustomField.objects.get_or_create(
        name="container_orchestrator_id",
        defaults=dict(
            label="Container Orchestrator ID",
            description=(
                "Used by the Multi-Node Kubernetes Blueprint. Maps the provisioned CloudBolt resource"
                "to the Container Orchestrator used to manage the Kubernetes cluster."
            ),
            type="INT",
            namespace=namespace,
        ),
    )

    CustomField.objects.get_or_create(
        name="create_eks_k8s_cluster_name",
        defaults=dict(
            label="EKS Cluster: Cluster Name",
            description="Used by the EKS blueprint",
            type="STR",
            namespace=namespace,
        ),
    )


def get_or_create_custom_fields():
    """
    Helper functions for main function to create custom field as needed
    """
    CustomField.objects.get_or_create(
        name="aws_rh_id",
        defaults={
            "label": "RH ID",
            "type": "STR",
        },
    )
    CustomField.objects.get_or_create(
        name="aws_region",
        defaults={
            "label": "Region ID",
            "type": "STR",
        },
    )
    CustomField.objects.get_or_create(
        name="vpc_id",
        defaults={
            "label": "VPC ID",
            "type": "STR",
        },
    )

    CustomField.objects.get_or_create(
        name="env_id",
        defaults={
            "label": "ENV ID",
            "type": "STR",
        },
    )
    CustomField.objects.get_or_create(
        name="eks_cluster_name",
        type="STR",
        defaults={
            "label": "Cluster Name",
            "description": "Used by the AWS EKS blueprint.",
        },
    )
    CustomField.objects.get_or_create(
        name="arn",
        type="STR",
        defaults={
            "label": "ARN",
            "description": "Used by the AWS EKS blueprint.",
            "show_as_attribute": True,
        },
    )
    CustomField.objects.get_or_create(
        name="created_at",
        type="STR",
        defaults={
            "label": "Create At",
            "description": "Used by the AWS EKS blueprint.",
        },
    )
    CustomField.objects.get_or_create(
        name="kubernetes_version",
        type="STR",
        defaults={
            "label": "Kubernetes Version",
            "description": "Used by the AWS EKS blueprint.",
            "show_as_attribute": True,
        },
    )
    CustomField.objects.get_or_create(
        name="status",
        type="STR",
        defaults={
            "label": "Status",
            "description": "Used by the AWS EKS blueprint.",
            "show_as_attribute": True,
        },
    )
    CustomField.objects.get_or_create(
        name="role_arn",
        type="STR",
        defaults={
            "label": "Cluster Role ARN",
            "description": "Used by the AWS EKS blueprint.",
            "show_as_attribute": True,
        },
    )
    CustomField.objects.get_or_create(
        name="platform_version",
        type="STR",
        defaults={
            "label": "Cluster Platform Version",
            "description": "Used by the AWS EKS blueprint.",
        },
    )
    CustomField.objects.get_or_create(
        name="eks_subnets",
        type="STR",
        defaults={"label": "Subnets", "description": "Used by the AWS EKS blueprint."},
    )
    CustomField.objects.get_or_create(
        name="eks_security_groups",
        type="STR",
        defaults={
            "label": "Cluster Security Groups",
            "description": "Used by the AWS EKS blueprint.",
            "show_as_attribute": True,
        },
    )

    CustomField.objects.get_or_create(
        name="endpoint",
        type="URL",
        defaults={
            "label": "Cluster Endpoint",
            "description": "Used by the AWS EKS blueprint.",
            "required": False,
            "show_as_attribute": True,
            "show_on_servers": True,
        },
    )


def get_boto_client(env_id=None, boto_service="ec2"):
    """
    get_boto_client
    """
    if env_id is None:
        return None

    env = Environment.objects.get(id=env_id)
    rh: AWSHandler = env.resource_handler.cast()

    client = rh.get_boto3_client(env.aws_region, boto_service)
    return client, env


def create_cluster_role(cluster_name, iam_client):
    """
    create_cluster_role
    """
    assume_role_policy_document = json.dumps(
        {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Principal": {"Service": "eks.amazonaws.com"},
                    "Action": "sts:AssumeRole",
                }
            ],
        }
    )

    random_string_role = "".join(
        random.choices(
            string.ascii_uppercase + string.ascii_lowercase + string.digits, k=6
        )
    )
    role_name = cluster_name + "-" + random_string_role + "-Role"

    try:
        cluster_role_rsp = iam_client.create_role(
            Path="/",
            RoleName=role_name,
            AssumeRolePolicyDocument=assume_role_policy_document,
            Description="Allows EKS cluster creation on your behalf.",
            MaxSessionDuration=3600,
        )
    except Exception as err:
        raise RuntimeError(f"Role could not be created...{err}")

    try:
        for policy in ["arn:aws:iam::aws:policy/AmazonEKSClusterPolicy"]:
            iam_client.attach_role_policy(RoleName=role_name, PolicyArn=policy)
    except Exception as error:
        raise RuntimeError(f"policy could not be created...{error}")

    return cluster_role_rsp["Role"]["Arn"]


def _resolve_group(group):
    """The group kwarg arrives as a Group or as its name depending on caller."""
    if group is None or isinstance(group, Group):
        return group
    return Group.objects.filter(name=str(group)).first()


def generate_options_for_env_id(**kwargs):
    """
    List the AWS environments the ordering group is entitled to.
    """
    group = _resolve_group(kwargs.get("group"))
    if group is None:
        return []

    # fetch all group environment
    envs = group.get_available_environments()

    aws_envs = [
        env
        for env in envs
        if env.resource_handler is not None
        and env.resource_handler.resource_technology.slug.startswith("aws")
    ]

    if not aws_envs:
        return [("", "Select Valid Group Associated with Environment")]

    options = [("", "Select Environment")]

    for env in aws_envs:
        options.append((env.id, env.name))

    return options


def generate_options_for_k8s_version(**kwargs):
    """
    generate options for kubernetes version if required version not available then add here
    """
    return ["1.33", "1.34", "1.32", "1.31"]


def generate_options_for_security_groups(control_value=None, server=None, **kwargs):
    """
    generate_options_for_security_groups
    """
    if control_value is None or control_value == "":
        return []

    client, env = get_boto_client(control_value, "ec2")

    response = client.describe_security_groups(
        Filters=[{"Name": "vpc-id", "Values": [env.vpc_id]}]
    )

    return [(scy["GroupId"], scy["GroupName"]) for scy in response["SecurityGroups"]]


def generate_options_for_subnet_1(control_value=None, server=None, **kwargs):
    """
    generate_options_for_subnet_1
    """
    if control_value is None or control_value == "":
        return []

    env = Environment.objects.get(id=control_value)

    rh = env.resource_handler.cast()

    return [
        (
            "{0}@{1}@{2}".format(
                subnet["network"], env.id, subnet["availability_zone"]
            ),
            "{0} | {1} | Is Public: {2}".format(
                subnet["name"],
                subnet["availability_zone"],
                subnet["map_public_ip_on_launch"],
            ),
        )
        for subnet in rh.get_all_vpc_subnets(env.aws_region, env.vpc_id)
    ]


def generate_options_for_subnet_2(control_value=None, server=None, **kwargs):
    """
    generate_options_for_subnet_2
    """
    if control_value is None or control_value == "":
        return []

    subnet_1, env_id, subnet_1_zone = control_value.split("@")

    env = Environment.objects.get(id=env_id)
    rh = env.resource_handler.cast()

    return [
        (
            f"{subnet['network']}@{env.id}@{subnet_1_zone}@{subnet['availability_zone']}",
            "{0} | {1} | Is Public: {2}".format(
                subnet["name"],
                subnet["availability_zone"],
                subnet["map_public_ip_on_launch"],
            ),
        )
        for subnet in rh.get_all_vpc_subnets(env.aws_region, env.vpc_id)
        if subnet["network"] != subnet_1
    ]


def boto_instance_to_dict(cluster, env_id):
    """
    Create a pared-down representation of an eks instance from the full boto
    dictionary.
    """
    instance = {
        "eks_cluster_name": cluster["name"],
        "arn": cluster["arn"],
        "created_at": cluster["createdAt"],
        "kubernetes_version": cluster["version"],
        "role_arn": cluster["roleArn"],
        "platform_version": cluster["platformVersion"],
        "eks_security_groups": cluster["resourcesVpcConfig"]["securityGroupIds"],
        "eks_subnets": cluster["resourcesVpcConfig"]["subnetIds"],
        "vpc_id": cluster["resourcesVpcConfig"]["vpcId"],
        "env_id": env_id,
        "endpoint": cluster["endpoint"],
        "status": cluster["status"],
    }

    logger.info(f"eks instance created: {instance}")

    return instance


def run(job, resource, logger=None, **kwargs):
    set_progress("Starting Provision of EKS Cluster...")

    # get or create custom fields as needed
    get_or_create_custom_fields()

    # create required parameter for container orchestrator
    create_required_parameters()

    security_groups = "{{security_groups}}"
    cluster_name = "{{ cluster_name }}"
    env_id = "{{ env_id }}"

    # Network Configuration
    subnets = ["{{ subnet_1 }}", "{{ subnet_2 }}"]
    subnets = [i.split("@")[0] for i in subnets]

    # eks client
    client, environment = get_boto_client(env_id, "eks")

    # iam client
    iam_client, _ = get_boto_client(env_id, "iam")

    # cluster role arn
    role_arn = create_cluster_role(cluster_name, iam_client)

    # resource handler
    rh = environment.resource_handler.cast()

    resource.name = cluster_name
    resource.cluster_name = cluster_name
    resource.aws_region = environment.aws_region

    # Store the resource handler's ID on this resource so the teardown action
    # knows which credentials to use.
    resource.aws_rh_id = rh.id

    # Cluster creation typically takes between 10 and 15 minutes.
    try:
        cluster_rsp = client.create_cluster(
            name=cluster_name,
            version="{{ k8s_version }}",
            roleArn=role_arn,
            resourcesVpcConfig={
                "subnetIds": subnets,
                "endpointPublicAccess": True,
                "securityGroupIds": [security_groups],
            },
        )["cluster"]

    except ClientError as e:
        set_progress("AWS ClientError: {}".format(e))
        return "FAILURE", "", e
    except Exception as err:
        return "FAILURE", "Amazon EKS cluster could not be created", str(err)

    while cluster_rsp["status"] == "CREATING":
        set_progress(
            'Cluster "{}" is being created'.format(cluster_name), increment_tasks=1
        )
        time.sleep(60)

        cluster_rsp = client.describe_cluster(name=cluster_name)["cluster"]

    logger.info(f"Cluster response: {cluster_rsp}")

    # get container orchestrator tech object
    tech = ContainerOrchestratorTechnology.objects.get(name="Kubernetes")

    if hasattr(Kubernetes, "resource_handler"):
        kubernetes = Kubernetes.objects.create(
            name=cluster_name,
            ip=cluster_rsp["endpoint"].strip("https://"),
            port=443,
            protocol="https",
            auth_type="TOKEN",
            serviceaccount=rh.serviceaccount,
            servicepasswd="",
            container_technology=tech,
            resource_handler=rh,
            auto_rotate_token=True,
        )
    else:
        kubernetes = Kubernetes.objects.create(
            name=cluster_name,
            ip=cluster_rsp["endpoint"].strip("https://"),
            port=443,
            protocol="https",
            auth_type="TOKEN",
            serviceaccount=rh.serviceaccount,
            servicepasswd="",
            container_technology=tech,
        )

    kubernetes.environment_set.add(environment)

    resource.container_orchestrator_id = kubernetes.id

    eks_instance = boto_instance_to_dict(cluster_rsp, env_id)

    for key, value in eks_instance.items():
        setattr(resource, key, value)  # set custom field value

    resource.save()

    # create a new Environment for the new container orchestrator, and attach it to eks cluster
    Environment.objects.create(
        name="Resource-{} Environment#{}".format(resource.name, environment.aws_region),
        container_orchestrator=kubernetes,
    )

    job.set_progress("EkS cluster created successfully")

    return "SUCCESS", "EKS Cluster configured successfully", ""