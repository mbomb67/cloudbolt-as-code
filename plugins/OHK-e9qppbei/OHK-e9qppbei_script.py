"""
Build service item action for AWS  EKS Cluster Service blueprint.
"""

from __future__ import unicode_literals
from containerorchestrators.kuberneteshandler.models import Kubernetes
from containerorchestrators.models import (
    ContainerOrchestratorTechnology,
)
from common.methods import set_progress
from infrastructure.models import Environment, CustomField
from resourcehandlers.aws.models import AWSHandler
from servicecatalog.models import ServiceBlueprint
from resources.models import Resource, ResourceType
from accounts.models import Group
from utilities.logger import ThreadLogger

logger = ThreadLogger(__name__)

RESOURCE_IDENTIFIER = ["cluster_name", "aws_region"]


def get_boto3_eks_client(env_obj):
    """
    Return boto connection to the eks in the specified environment's region.
    """
    rh: AWSHandler = env_obj.resource_handler.cast()

    if env_obj.aws_region:
        client = rh.get_boto3_client(env_obj.aws_region, "eks")
        return client

    return None


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
        defaults={"label": "VPC ID", "type": "STR", "show_as_attribute": True},
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
    CustomField.objects.get_or_create(
        name="env_id",
        defaults={
            "label": "ENV ID",
            "type": "STR",
            "show_as_attribute": True,
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
            "show_as_attribute": True,
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
            "show_as_attribute": True,
        },
    )
    CustomField.objects.get_or_create(
        name="eks_subnets",
        type="STR",
        defaults={
            "label": "Subnets",
            "description": "Used by the AWS EKS blueprint.",
            "show_as_attribute": True,
        },
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


def discover_resources(**kwargs):
    created, updated = 0, 0

    # create custom fields if needed
    get_or_create_custom_fields()

    # get all aws environments
    environments = Environment.objects.filter(
        resource_handler__resource_technology__name="Amazon Web Services"
    )

    # get AWS  EKS BP object
    bp = ServiceBlueprint.objects.filter(name__iexact="AWS EKS").first()

    group = Group.objects.filter(name__icontains="unassigned").first()

    resource_type, _ = ResourceType.objects.get_or_create(
        name="cluster", defaults={"label": "Cluster", "icon": "far fa-file"}
    )

    discovered = []

    for environment in environments:
        client = get_boto3_eks_client(environment)

        if client is None:
            continue

        handler = environment.resource_handler.cast()

        try:
            response_dict = client.list_clusters()
        except Exception as err:
            raise Exception(err)

        for cluster_name in response_dict["clusters"]:
            cluster = client.describe_cluster(name=cluster_name)["cluster"]

            # identifier for aws region
            idtf_resource_name = f"{cluster_name}:{environment.aws_region}"

            if idtf_resource_name in discovered:
                continue

            discovered.append(idtf_resource_name)

            # get cluster resource object
            resource = Resource.objects.filter(
                name=cluster["name"], lifecycle="ACTIVE"
            ).first()

            if not resource:
                resource = Resource.objects.create(
                    name=cluster["name"],
                    blueprint=bp,
                    group=group,
                    resource_type=resource_type,
                    lifecycle="ACTIVE",
                )
                set_progress(f"Creating new resource {cluster['name']}")

                tech = ContainerOrchestratorTechnology.objects.get(name="Kubernetes")

                kubernetes = Kubernetes.objects.create(
                    name=cluster["name"],
                    ip=cluster["endpoint"].strip("https://"),
                    port=443,
                    protocol="https",
                    auth_type="TOKEN",
                    serviceaccount=handler.serviceaccount,
                    servicepasswd="",
                    container_technology=tech,
                    resource_handler=handler,
                    auto_rotate_token=True,
                )

                kubernetes.environment_set.add(environment)

                resource.container_orchestrator_id = kubernetes.id

                # create a new Environment for the new container orchestrator
                Environment.objects.create(
                    name="Resource-{0} Environment #{1}".format(
                        resource.id, environment.aws_region
                    ),
                    container_orchestrator=kubernetes,
                )

                created += 1
            else:
                updated += 1

            if "endpoint" in cluster.keys():
                resource.endpoint = cluster["endpoint"]

            # Store the resource handler's ID on this resource so the teardown action
            # knows which credentials to use.
            resource.aws_rh_id = handler.id
            resource.aws_region = environment.aws_region
            resource.lifecycle = "ACTIVE"
            resource.eks_cluster_name = str(handler.id) + cluster["name"]
            resource.arn = cluster["arn"]
            resource.created_at = cluster["createdAt"]
            resource.kubernetes_version = cluster["version"]
            resource.role_arn = cluster["roleArn"]
            resource.status = cluster["status"]
            resource.platform_version = cluster["platformVersion"]
            resource.security_groups = cluster["resourcesVpcConfig"]["securityGroupIds"]
            resource.eks_subnets = cluster["resourcesVpcConfig"]["subnetIds"]
            resource.vpc_id = cluster["resourcesVpcConfig"]["vpcId"]
            resource.env_id = environment.id
            resource.save()

    set_progress(
        f"AWS  EKS BP discovered {created + updated} resources where {created} created and {updated} updated."
    )

    return []