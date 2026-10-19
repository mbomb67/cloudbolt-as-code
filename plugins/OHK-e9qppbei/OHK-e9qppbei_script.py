"""
Discovery plugin for the AWS EKS blueprint: returns one dictionary per EKS
cluster in every AWS environment, and CloudBolt's discovery sync creates,
updates or reactivates the matching Kubernetes Cluster resources from them
(servicecatalog/models.py, ServiceBlueprint.sync_resources). The plugin never
creates resources or parameters itself.

The one thing the sync cannot do is register the cluster as a Kubernetes
container orchestrator, which the namespace blueprint and the cluster tabs
need, so that is done here once per cluster, the same way the build plugin
does it, and its ID is returned with the cluster.
"""

from containerorchestrators.kuberneteshandler.models import Kubernetes
from containerorchestrators.models import ContainerOrchestratorTechnology
from infrastructure.models import Environment
from utilities.logger import ThreadLogger

logger = ThreadLogger(__name__)

# The cluster ARN is unique across accounts and regions, and the build plugin
# stores it on every cluster it creates.
RESOURCE_IDENTIFIER = "arn"


def get_orchestrator_id(cluster, handler, region, current_id):
    """
    Return the ID of the cluster's Kubernetes orchestrator: the one its
    resource already points at, else one registered for its endpoint, else a
    new one with its own environment.
    """
    host = cluster["endpoint"].removeprefix("https://")
    kubernetes = (
        current_id and Kubernetes.objects.filter(id=current_id).first()
    ) or Kubernetes.objects.filter(ip=host).first()
    if kubernetes:
        return kubernetes.id

    kubernetes = Kubernetes.objects.create(
        name=cluster["name"],
        ip=host,
        port=443,
        protocol="https",
        auth_type="TOKEN",
        serviceaccount=handler.serviceaccount,
        servicepasswd="",
        container_technology=ContainerOrchestratorTechnology.objects.get(
            name="Kubernetes"
        ),
        resource_handler=handler,
        auto_rotate_token=True,
    )
    # Same per-cluster Environment the build plugin creates; the AWS
    # environment is not re-pointed at the orchestrator.
    Environment.objects.create(
        name=f"Resource-{cluster['name']} Environment#{region}",
        container_orchestrator=kubernetes,
    )
    logger.info(f"Registered EKS cluster {cluster['name']} as an orchestrator")
    return kubernetes.id


def choose_env_id(current_env_id, environment):
    """
    Keep the environment the cluster was built or first discovered in when it
    still matches this account and region: several environments can share
    both, and the day-2 actions read the cluster's subnets and security groups
    through env_id's VPC.
    """
    if (
        current_env_id
        and Environment.objects.filter(
            id=current_env_id,
            resource_handler=environment.resource_handler,
            aws_region=environment.aws_region,
        ).exists()
    ):
        return str(current_env_id)
    return str(environment.id)


def cluster_dict(cluster, handler, environment, current):
    """
    The values the sync sets on the cluster's resource, matching the
    parameters and value types the build plugin stores. `current` holds the
    parameter values of the resource already tracking the cluster, if any.
    """
    vpc_config = cluster["resourcesVpcConfig"]
    resource_dict = {
        "name": cluster["name"],
        "arn": cluster["arn"],
        "eks_cluster_name": cluster["name"],
        "aws_region": environment.aws_region,
        "aws_rh_id": str(handler.id),
        "env_id": choose_env_id(current.get("env_id"), environment),
        "created_at": str(cluster["createdAt"]),
        "kubernetes_version": cluster["version"],
        "platform_version": cluster["platformVersion"],
        "role_arn": cluster["roleArn"],
        "status": cluster["status"],
        "vpc_id": vpc_config["vpcId"],
        # Stored as the text of the list, which the node group actions parse
        "eks_subnets": str(vpc_config["subnetIds"]),
        "eks_security_groups": str(vpc_config["securityGroupIds"]),
    }
    if cluster.get("endpoint"):
        resource_dict["endpoint"] = cluster["endpoint"]
        resource_dict["container_orchestrator_id"] = get_orchestrator_id(
            cluster,
            handler,
            environment.aws_region,
            current.get("container_orchestrator_id"),
        )
    return resource_dict


def discover_resources(**kwargs):
    blueprint = kwargs.get("blueprint")
    known = []
    if blueprint is not None:
        known = [
            dict(resource.get_cf_values_as_dict(), lifecycle=resource.lifecycle)
            for resource in blueprint.resource_set.all()
        ]
    # an active resource wins over Historical ones for the same cluster
    known_by_arn = {
        values["arn"]: values
        for values in sorted(known, key=lambda v: v["lifecycle"] == "ACTIVE")
        if values.get("arn")
    }

    discovered = []
    returned = set()
    listed = set()
    skipped_handler_ids = set()

    for environment in Environment.objects.filter(
        resource_handler__resource_technology__name="Amazon Web Services"
    ):
        handler = environment.resource_handler.cast()
        region = environment.aws_region

        # One listing per handler and region, however many environments
        # share them
        if (
            not region
            or handler.id in skipped_handler_ids
            or (handler.id, region) in listed
        ):
            continue

        # Not every AWS handler is set up for Kubernetes: one whose
        # credentials cannot list or describe EKS clusters is skipped, with
        # its remaining environments, so it does not stop the other handlers.
        # https://boto3.amazonaws.com/v1/documentation/api/latest/reference/services/eks/paginator/ListClusters.html
        # https://docs.aws.amazon.com/eks/latest/APIReference/API_DescribeCluster.html
        try:
            client = handler.get_boto3_client(region, "eks")
            names = [
                name
                for page in client.get_paginator("list_clusters").paginate()
                for name in page["clusters"]
            ]
            clusters = [
                client.describe_cluster(name=name)["cluster"] for name in names
            ]
        except Exception as err:
            logger.info(
                f"Skipping AWS handler {handler.name} for EKS discovery after "
                f"{region} failed: {err}"
            )
            skipped_handler_ids.add(handler.id)
            continue

        listed.add((handler.id, region))

        for cluster in clusters:
            arn = cluster["arn"]
            # A cluster still being created has no endpoint yet, and one the
            # build plugin is creating has no ARN on its resource until it is
            # active; the next discovery picks it up. Two handlers on the same
            # account see the same cluster, and the sync skips a duplicate.
            if cluster["status"] == "CREATING" or arn in returned:
                continue
            returned.add(arn)
            discovered.append(
                cluster_dict(cluster, handler, environment, known_by_arn.get(arn, {}))
            )

    # The sync treats an active cluster it is not given as gone and, when the
    # blueprint's auto_historical_resources is on, marks it Historical. A
    # skipped handler's clusters were not checked, so each is returned with
    # its identifier alone, which matches it and changes nothing.
    skipped = {str(handler_id) for handler_id in skipped_handler_ids}
    for values in known:
        if (
            values["lifecycle"] == "ACTIVE"
            and values.get("arn")
            and values["arn"] not in returned
            and str(values.get("aws_rh_id")) in skipped
        ):
            discovered.append({RESOURCE_IDENTIFIER: values["arn"]})

    return discovered
