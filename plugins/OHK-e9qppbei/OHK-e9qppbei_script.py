"""
Discovery plugin for the AWS EKS blueprint: adopts every EKS cluster in every
AWS environment as a Kubernetes Cluster resource and container orchestrator.
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

RESOURCE_IDENTIFIER = ["eks_cluster_name", "aws_region"]


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

    # CloudBolt passes the blueprint that owns this discovery plugin
    bp = kwargs.get("blueprint") or ServiceBlueprint.objects.filter(
        name__iexact="AWS EKS"
    ).first()

    group = Group.objects.filter(name__icontains="unassigned").first()

    # use the blueprint's resource type so discovered clusters list with built ones
    resource_type = bp.resource_type if bp is not None else None
    if resource_type is None:
        resource_type, _ = ResourceType.objects.get_or_create(
            name="kubernetes_cluster",
            defaults={"label": "Kubernetes Cluster", "icon": "fas fa-dharmachakra"},
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

            # one resource per cluster per account and region, however many
            # environments share that account and region
            idtf_resource_name = f"{handler.id}:{environment.aws_region}:{cluster_name}"

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
                    ip=cluster["endpoint"].removeprefix("https://"),
                    port=443,
                    protocol="https",
                    auth_type="TOKEN",
                    serviceaccount=handler.serviceaccount,
                    servicepasswd="",
                    container_technology=tech,
                    resource_handler=handler,
                    auto_rotate_token=True,
                )

                resource.container_orchestrator_id = kubernetes.id

                # Same per-cluster Environment the build plugin creates; the AWS
                # environment is not re-pointed at the orchestrator.
                Environment.objects.create(
                    name="Resource-{0} Environment#{1}".format(
                        resource.name, environment.aws_region
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
            resource.eks_cluster_name = cluster["name"]
            resource.arn = cluster["arn"]
            resource.created_at = cluster["createdAt"]
            resource.kubernetes_version = cluster["version"]
            resource.role_arn = cluster["roleArn"]
            resource.status = cluster["status"]
            resource.platform_version = cluster["platformVersion"]
            resource.eks_security_groups = cluster["resourcesVpcConfig"]["securityGroupIds"]
            resource.eks_subnets = cluster["resourcesVpcConfig"]["subnetIds"]
            resource.vpc_id = cluster["resourcesVpcConfig"]["vpcId"]

            # Keep the environment the cluster was built or first discovered
            # in when it still matches this account and region: several
            # environments can share both, and the day-2 actions read the
            # cluster's subnets and security groups through env_id's VPC.
            current_env_id = resource.get_cf_values_as_dict().get("env_id")
            keep_env = (
                current_env_id
                and Environment.objects.filter(
                    id=current_env_id,
                    resource_handler=environment.resource_handler,
                    aws_region=environment.aws_region,
                ).exists()
            )
            if not keep_env:
                resource.env_id = environment.id
            resource.save()

    set_progress(
        f"AWS  EKS BP discovered {created + updated} resources where {created} created and {updated} updated."
    )

    return []