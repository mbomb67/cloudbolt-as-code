"""
Resource action for AWS EKS Cluster update kubernetes version Service blueprint.
"""

import time
from botocore.exceptions import ClientError
from common.methods import set_progress
from resourcehandlers.aws.models import AWSHandler
from utilities.logger import ThreadLogger

logger = ThreadLogger(__name__)


def get_boto_client(rh: AWSHandler, region=None, boto_service="eks"):
    """
    get_boto_client
    """

    if region is None:
        return None

    # get aws client object
    client = rh.get_boto3_client(region, boto_service)

    return client


def generate_options_for_kubernetes_version(control_value=None, server=None, **kwargs):
    """
    generate options for kubernetes version if required version not available then add here
    """
    # Keep in step with generate_options_for_k8s_version in the Deploy EKS Cluster plugin.
    k8_version = [
        ("", "--Select Kubernetes Version--"),
        ("1.34", "1.34"),
        ("1.33", "1.33"),
        ("1.32", "1.32"),
        ("1.31", "1.31"),
    ]

    resource = kwargs.get("resource", None)
    initial_value = ""

    if resource:
        initial_value = resource.get_cf_values_as_dict().get("kubernetes_version", "")

    return {"initial_value": initial_value.strip(), "options": k8_version}


def update_cluster_version(client, resource, update_version, current_version):
    """
    :param client: eks
    :param resource: eks cluster
    :param version: update version (must be updated to earlier first)
    :return: cluster with updated version
    """
    try:
        response = client.update_cluster_version(
            name=resource.name,
            version=update_version,
        )
        # wait for update version. Check status every minutes
        update = response["update"]
        while update["status"] in ["InProgress", "UPDATING"]:
            set_progress(
                'Cluster "{}" is being updated'.format(resource.name), increment_tasks=1
            )

            set_progress('status is  "{}"'.format(update["status"]), increment_tasks=1)

            time.sleep(30)

            update = client.describe_cluster(name=resource.name)["cluster"]

        if update["status"] == "ACTIVE":
            # endpoint does not exist when cluster has not created completely
            resource.kubernetes_version = update["version"]
            resource.save()

            logger.info(f"resource {resource} updated successfully.")
            return "SUCCESS", "Updated cluster version successfully", ""
        else:
            return (
                "FAILURE",
                f"EKS update cluster kubernetes version failed - {update['status']}",
                "",
            )
    except ClientError as e:
        return (
            "FAILURE",
            f"First update to earlier version {(float(current_version)+float(0.01)):.2f} or check available version for update",
            e,
        )
    except Exception as err:
        return "FAILURE", "Amazon EKS cluster version could not be updated.", str(err)


def run(resource, **kwargs):
    set_progress(
        f"Starting Provision of {resource} resource  for cluster version update."
    )
    logger.debug(
        f"Starting Provision of {resource} resource  for cluster version update."
    )

    current_version = resource.get_cf_values_as_dict()["kubernetes_version"]

    # aws handler
    rh = AWSHandler.objects.get(id=resource.aws_rh_id)

    # region
    region = resource.aws_region

    set_progress("Connecting to Amazon EKS")

    # eks client
    client = get_boto_client(rh, region, "eks")

    # version for update
    version = "{{ kubernetes_version }}"

    # update kubernetes version for cluster
    msg = update_cluster_version(client, resource, version, current_version)

    return msg