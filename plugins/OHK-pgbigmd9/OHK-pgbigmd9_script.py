"""
Teardown for the AWS EKS blueprint: node groups, cluster, the cluster IAM role
the build created, then the CloudBolt orchestrator, its environment, and the
node group sub-resources.
"""

import time

from botocore.exceptions import ClientError

from common.methods import set_progress
from containerorchestrators.kuberneteshandler.models import Kubernetes
from infrastructure.models import Environment
from resourcehandlers.aws.models import AWSHandler
from resources.models import Resource
from utilities.logger import ThreadLogger

logger = ThreadLogger(__name__)

CLUSTER_POLICY_ARNS = ["arn:aws:iam::aws:policy/AmazonEKSClusterPolicy"]


def get_boto_client(aws_rh, resource, service="ec2"):
    """
    get_boto_client
    """
    return aws_rh.get_boto3_client(resource.aws_region, service)


def error_code(err):
    return err.response.get("Error", {}).get("Code", "")


def delete_node_groups(client, resource):
    """
    Delete every managed node group and wait until EKS lists none.
    https://docs.aws.amazon.com/eks/latest/APIReference/API_DeleteNodegroup.html
    """
    try:
        node_groups = client.list_nodegroups(clusterName=resource.name)["nodegroups"]
    except ClientError as err:
        if error_code(err) == "ResourceNotFoundException":
            return
        raise

    for node_group in node_groups:
        set_progress(f"Deleting node group {node_group}")
        client.delete_nodegroup(clusterName=resource.name, nodegroupName=node_group)

    while node_groups:
        time.sleep(30)
        node_groups = client.list_nodegroups(clusterName=resource.name)["nodegroups"]
        if node_groups:
            set_progress(
                f"Still deleting node groups: {', '.join(node_groups)}",
                increment_tasks=1,
            )


def delete_cluster(client, resource):
    """
    Delete the cluster and wait until EKS no longer knows it. The cluster IAM
    role is deleted only afterwards: the control plane still uses it while it
    removes its network interfaces.
    https://docs.aws.amazon.com/eks/latest/APIReference/API_DeleteCluster.html
    """
    try:
        client.describe_cluster(name=resource.name)
    except ClientError as err:
        if error_code(err) == "ResourceNotFoundException":
            set_progress(f"Cluster {resource.name} is already gone from AWS")
            return
        raise

    set_progress(f"Deleting cluster {resource.name}")
    client.delete_cluster(name=resource.name)

    while True:
        time.sleep(30)
        try:
            status = client.describe_cluster(name=resource.name)["cluster"]["status"]
        except ClientError as err:
            if error_code(err) == "ResourceNotFoundException":
                break
            raise
        set_progress(f"Cluster {resource.name} is {status}", increment_tasks=1)

    set_progress(f"Cluster {resource.name} deleted")


def delete_cluster_role(aws_rh, resource):
    """
    Delete the per-cluster IAM role the build plugin created. A discovered
    cluster's role belongs to whoever built that cluster (eksctl, Terraform,
    a shared role), so only a role named <cluster>-<suffix>-Role is removed.
    """
    role_arn = resource.get_cf_values_as_dict().get("role_arn") or ""
    role_name = role_arn.split("/")[-1]

    if not (
        role_name.startswith(f"{resource.name}-") and role_name.endswith("-Role")
    ):
        set_progress(
            f"Leaving IAM role {role_name or '(none)'} in place: not created by this blueprint"
        )
        return

    iam_client = get_boto_client(aws_rh, resource, "iam")

    for policy_arn in CLUSTER_POLICY_ARNS:
        try:
            iam_client.detach_role_policy(RoleName=role_name, PolicyArn=policy_arn)
        except ClientError as err:
            if error_code(err) != "NoSuchEntity":
                raise

    try:
        iam_client.delete_role(RoleName=role_name)
        set_progress(f"Deleted IAM role {role_name}")
    except ClientError as err:
        if error_code(err) != "NoSuchEntity":
            raise


def delete_orchestrator(resource):
    """
    Remove the Kubernetes orchestrator and the Environment the build created
    for it. Environment.container_orchestrator is SET_NULL, so without this the
    environment would be orphaned. Only an environment with no resource
    handler is touched: an AWS environment is never deleted here.
    """
    orchestrator_id = resource.get_cf_values_as_dict().get(
        "container_orchestrator_id"
    )
    if not orchestrator_id:
        return

    try:
        kubernetes = Kubernetes.objects.get(id=orchestrator_id)
    except Kubernetes.DoesNotExist:
        return

    removed = Environment.objects.filter(
        container_orchestrator=kubernetes,
        resource_handler__isnull=True,
        name__startswith="Resource-",
    ).delete()[0]

    kubernetes.delete()
    set_progress(
        f"Removed container orchestrator {kubernetes.name} and {removed} environment(s)"
    )


def run(job, resource, *args, **kwargs):
    set_progress(f"Deleting EKS cluster resource {resource.name}")
    logger.info(f"Elastic K8S Delete plugin running for resource: {resource.name}")

    aws_rh_id = resource.get_cf_values_as_dict().get("aws_rh_id")

    if aws_rh_id:
        aws_rh: AWSHandler = AWSHandler.objects.get(id=aws_rh_id)
        client = get_boto_client(aws_rh, resource, "eks")

        delete_node_groups(client, resource)
        delete_cluster(client, resource)
        delete_cluster_role(aws_rh, resource)
    else:
        set_progress(
            "No AWS handler is recorded on the resource; removing it from CloudBolt only"
        )

    delete_orchestrator(resource)

    Resource.objects.filter(
        parent_resource=resource, resource_type__name="eks_nodegroup"
    ).delete()

    return (
        "SUCCESS",
        "Kubernetes Cluster '{0}' deleted successfully".format(resource.name),
        "",
    )
