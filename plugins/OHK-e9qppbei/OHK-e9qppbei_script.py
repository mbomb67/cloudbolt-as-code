"""
Discovery plugin for the AWS EKS blueprint: returns one dictionary per EKS
cluster in every AWS environment, and one per managed node group of each
cluster CloudBolt already tracks, and CloudBolt's discovery sync creates,
updates or reactivates the matching Kubernetes Cluster resources and
eks_nodegroup sub-resources from them (servicecatalog/models.py,
ServiceBlueprint.sync_resources). The plugin never creates resources or
parameters itself.

The one thing the sync cannot do is register the cluster as a Kubernetes
container orchestrator, which the namespace blueprint and the cluster tabs
need, so that is done here once per cluster, the same way the build plugin
does it, and its ID is returned with the cluster.

Retiring on proof only. The blueprint keeps auto_historical_resources off,
so the sync never retires a resource just because it was not returned. This
plugin retires a tracked Active cluster or node group itself, by returning
its identifier with lifecycle HISTORICAL, and only when the listing that
should contain it succeeded, it is missing from that listing, and a direct
describe of it returns ResourceNotFoundException. Node groups of a cluster
proven gone are retired with it. Anything else (access denied, throttling,
an unreadable handler or region, a job still running on the resource)
proves nothing, and the resource is returned with its identifier alone,
which changes nothing. A Historical resource returned without a lifecycle
is set back to Active by the sync, so reactivation stays automatic.
"""

from botocore.exceptions import ClientError
from containerorchestrators.kuberneteshandler.models import Kubernetes
from containerorchestrators.models import ContainerOrchestratorTechnology
from infrastructure.models import Environment
from jobs.models import Job
from resources.models import ResourceType
from utilities.logger import ThreadLogger

logger = ThreadLogger(__name__)

# The cluster ARN is unique across accounts and regions, and the build plugin
# stores it on every cluster it creates. Node group records carry their node
# group ARN under the same parameter so the sync can match them too.
RESOURCE_IDENTIFIER = "arn"

NODEGROUP_RESOURCE_TYPE = "eks_nodegroup"
RETIRED = {"lifecycle": "HISTORICAL"}


def not_found(err):
    """
    True only for the error EKS returns when the named cluster or node group
    does not exist (HTTP 404).
    https://docs.aws.amazon.com/eks/latest/APIReference/API_DescribeCluster.html#API_DescribeCluster_Errors
    https://docs.aws.amazon.com/eks/latest/APIReference/API_DescribeNodegroup.html#API_DescribeNodegroup_Errors
    """
    return (
        isinstance(err, ClientError)
        and err.response.get("Error", {}).get("Code") == "ResourceNotFoundException"
    )


def has_running_job(resource):
    """A job still running on the resource, such as a build, an action or a
    teardown, may be about to create or remove the cloud object."""
    if resource is None:
        return False
    return (
        resource.jobs.filter(status__in=Job.ACTIVE_STATUSES).exists()
        or Job.objects.filter(
            status__in=Job.ACTIVE_STATUSES,
            job_parameters__hookparameters__resources=resource,
        ).exists()
    )


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


def nodegroup_dict(nodegroup, cluster_resource, resource_type):
    """
    The values the sync sets on a node group's eks_nodegroup sub-resource,
    the same parameters (all text) List and Add AWS EKS Nodegroup store.
    resource_type, parent_resource and group are Resource attributes a
    discovery dictionary may set.
    https://docs.aws.amazon.com/eks/latest/APIReference/API_Nodegroup.html
    """
    scaling = nodegroup.get("scalingConfig") or {}
    # No owner: the sync would overwrite the one Add AWS EKS Nodegroup records.
    return {
        "name": nodegroup["nodegroupName"],
        "arn": nodegroup["nodegroupArn"],
        "resource_type": resource_type,
        "parent_resource": cluster_resource,
        "group": cluster_resource.group,
        "eks_nodegroup_arn": nodegroup["nodegroupArn"],
        "eks_nodegroup_resource_status": nodegroup["status"],
        "eks_nodegroup_ami_type": nodegroup.get("amiType", ""),
        "eks_nodegroup_instance_types": str(nodegroup.get("instanceTypes") or []),
        "eks_nodegroup_noderole_arn": nodegroup.get("nodeRole", ""),
        "eks_nodegroup_node_min_size": str(scaling.get("minSize", "")),
        "eks_nodegroup_node_max_size": str(scaling.get("maxSize", "")),
        "eks_nodegroup_desired_size": str(scaling.get("desiredSize", "")),
        "eks_nodegroup_kubernetes_version": nodegroup.get("version", ""),
        "eks_nodegroup_release_version": nodegroup.get("releaseVersion", ""),
    }


def list_nodegroups(client, cluster_name):
    """
    Every managed node group of the cluster, described.
    https://docs.aws.amazon.com/boto3/latest/reference/services/eks/paginator/ListNodegroups.html
    https://docs.aws.amazon.com/eks/latest/APIReference/API_DescribeNodegroup.html
    """
    names = [
        name
        for page in client.get_paginator("list_nodegroups").paginate(
            clusterName=cluster_name
        )
        for name in page["nodegroups"]
    ]
    return [
        client.describe_nodegroup(clusterName=cluster_name, nodegroupName=name)[
            "nodegroup"
        ]
        for name in names
    ]


def cluster_is_gone(values, listings):
    """
    True when the cluster's account and region were listed without it and
    DescribeCluster says it does not exist. False when anything is unknown.
    """
    name = values.get("eks_cluster_name")
    try:
        key = (int(str(values.get("aws_rh_id")).strip()), str(values.get("aws_region") or "").strip())
    except (TypeError, ValueError):
        return False
    listing = listings.get(key)
    if not name or listing is None or name in listing["names"]:
        return False
    try:
        listing["client"].describe_cluster(name=name)
    except Exception as err:
        return not_found(err)
    return False


def nodegroup_is_gone(resource, nodegroup_listings):
    """
    True when the parent cluster's node groups were listed without this one
    and DescribeNodegroup says it does not exist. False when anything is
    unknown.
    """
    listing = nodegroup_listings.get(resource.parent_resource_id)
    if listing is None or resource.name in listing["names"]:
        return False
    try:
        listing["client"].describe_nodegroup(
            clusterName=listing["cluster"], nodegroupName=resource.name
        )
    except Exception as err:
        return not_found(err)
    return False


def discover_resources(**kwargs):
    blueprint = kwargs.get("blueprint")
    tracked = list(blueprint.resource_set.all()) if blueprint is not None else []
    values_of = {resource.id: resource.get_cf_values_as_dict() for resource in tracked}

    def is_nodegroup(resource):
        return getattr(resource.resource_type, "name", "") == NODEGROUP_RESOURCE_TYPE

    cluster_resources = [r for r in tracked if not is_nodegroup(r)]
    nodegroup_resources = [r for r in tracked if is_nodegroup(r)]

    # an active resource wins over Historical ones for the same cluster
    known_by_arn = {
        values_of[r.id]["arn"]: values_of[r.id]
        for r in sorted(cluster_resources, key=lambda r: r.lifecycle == "ACTIVE")
        if values_of[r.id].get("arn")
    }
    active_cluster_by_arn = {
        values_of[r.id]["arn"]: r
        for r in cluster_resources
        if r.lifecycle == "ACTIVE" and values_of[r.id].get("arn")
    }
    nodegroups_by_cluster = {}
    for resource in nodegroup_resources:
        if resource.lifecycle == "ACTIVE":
            nodegroups_by_cluster.setdefault(resource.parent_resource_id, []).append(resource)

    nodegroup_type = None
    discovered = []
    returned = set()
    listings = {}
    nodegroup_listings = {}
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
            or (handler.id, region) in listings
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

        listings[(handler.id, region)] = {"client": client, "names": set(names)}

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

            # Node groups are sub-resources of a cluster resource that already
            # exists; a newly discovered cluster gets them on the next run. A
            # job on the cluster (Add, Delete, Upgrade AWS EKS Nodegroup) may
            # be creating or removing one, so its node groups wait too.
            cluster_resource = active_cluster_by_arn.get(arn)
            if cluster_resource is None or has_running_job(cluster_resource):
                continue
            try:
                nodegroups = list_nodegroups(client, cluster["name"])
            except Exception as err:
                logger.info(f"Could not list node groups of {cluster['name']}: {err}")
                continue
            nodegroup_listings[cluster_resource.id] = {
                "client": client,
                "cluster": cluster["name"],
                "names": {ng["nodegroupName"] for ng in nodegroups},
            }
            # A record made before node groups carried their ARN under the
            # identifier cannot be matched, so its node group is not imported
            # (that would duplicate it) until List AWS EKS Nodegroup has
            # recorded the ARN.
            unmatched_names = {
                r.name
                for r in nodegroups_by_cluster.get(cluster_resource.id, [])
                if not values_of[r.id].get(RESOURCE_IDENTIFIER)
            }
            if nodegroup_type is None:
                nodegroup_type, _ = ResourceType.objects.get_or_create(
                    name=NODEGROUP_RESOURCE_TYPE,
                    defaults={"label": "EKS Nodegroup", "icon": "far fa-file"},
                )
            for nodegroup in nodegroups:
                # Add AWS EKS Nodegroup files the record once AWS reports the
                # node group active.
                if (
                    nodegroup["status"] == "CREATING"
                    or nodegroup["nodegroupArn"] in returned
                    or nodegroup["nodegroupName"] in unmatched_names
                ):
                    continue
                returned.add(nodegroup["nodegroupArn"])
                discovered.append(nodegroup_dict(nodegroup, cluster_resource, nodegroup_type))

    # Every tracked Active cluster not returned above is retired only on
    # proof; the rest are returned with their identifier alone.
    gone_cluster_ids = set()
    for resource in cluster_resources:
        arn = values_of[resource.id].get("arn")
        if resource.lifecycle != "ACTIVE" or not arn or arn in returned:
            continue
        returned.add(arn)
        if not has_running_job(resource) and cluster_is_gone(values_of[resource.id], listings):
            logger.info(f"EKS cluster {resource.name} no longer exists; retiring it")
            gone_cluster_ids.add(resource.id)
            discovered.append(dict(RETIRED, **{RESOURCE_IDENTIFIER: arn}))
        else:
            discovered.append({RESOURCE_IDENTIFIER: arn})

    # A cluster already Historical in CloudBolt is checked the same way
    # before its Active node groups are retired with it.
    parents_checked = {}

    def parent_is_gone(resource):
        parent_id = resource.parent_resource_id
        if parent_id in gone_cluster_ids:
            return True
        if parent_id not in parents_checked:
            parent = next((r for r in cluster_resources if r.id == parent_id), None)
            parents_checked[parent_id] = (
                parent is not None
                and parent.lifecycle != "ACTIVE"
                and cluster_is_gone(values_of[parent.id], listings)
            )
        return parents_checked[parent_id]

    for resource in nodegroup_resources:
        arn = values_of[resource.id].get("arn")
        if resource.lifecycle != "ACTIVE" or not arn or arn in returned:
            continue
        returned.add(arn)
        if not has_running_job(resource) and (
            parent_is_gone(resource) or nodegroup_is_gone(resource, nodegroup_listings)
        ):
            logger.info(f"EKS node group {resource.name} no longer exists; retiring it")
            discovered.append(dict(RETIRED, **{RESOURCE_IDENTIFIER: arn}))
        else:
            discovered.append({RESOURCE_IDENTIFIER: arn})

    return discovered
