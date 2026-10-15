"""
Day-2 action for the AWS EKS blueprint: upgrade the cluster's control plane to
the next Kubernetes minor version EKS offers.
"""

import time

from botocore.exceptions import ClientError

from common.methods import set_progress
from resourcehandlers.aws.models import AWSHandler
from utilities.logger import ThreadLogger

logger = ThreadLogger(__name__)


def version_key(version):
    return tuple(int(part) for part in str(version).split("."))


def list_available_cluster_versions(client):
    """
    Kubernetes versions EKS offers today, newest first, without the ones AWS
    reports as unsupported. The API pages with nextToken. Keep in step with
    the same helper in the Deploy EKS Cluster plugin.
    https://docs.aws.amazon.com/eks/latest/APIReference/API_DescribeClusterVersions.html
    """
    versions = []
    token = None
    while True:
        params = {"clusterType": "eks", "includeAll": True}
        if token:
            params["nextToken"] = token
        page = client.describe_cluster_versions(**params)
        versions.extend(page.get("clusterVersions", []))
        token = page.get("nextToken")
        if not token:
            break

    supported = [
        v
        for v in versions
        if (v.get("versionStatus") or v.get("status") or "")
        .upper()
        .replace("-", "_")
        != "UNSUPPORTED"
    ]
    return sorted(
        supported, key=lambda v: version_key(v["clusterVersion"]), reverse=True
    )


def version_status_label(version_info):
    status = version_info.get("versionStatus") or version_info.get("status") or ""
    return status.replace("_", " ").replace("-", " ").lower()


def get_eks_client(resource):
    rh = AWSHandler.objects.get(id=resource.aws_rh_id)
    return rh.get_boto3_client(resource.aws_region, "eks")


def current_cluster_version(resource, client):
    version = resource.get_cf_values_as_dict().get("kubernetes_version", "")
    if version:
        return str(version).strip()
    return client.describe_cluster(name=resource.name)["cluster"]["version"]


def generate_options_for_kubernetes_version(server=None, **kwargs):
    """
    The next minor version after the cluster's current one. EKS updates a
    control plane one minor version at a time, so that is the only value AWS
    accepts: https://docs.aws.amazon.com/eks/latest/userguide/update-cluster.html
    """
    resource = kwargs.get("resource", None)

    if resource is None:
        return []

    client = get_eks_client(resource)
    current = current_cluster_version(resource, client)

    newer = [
        v
        for v in list_available_cluster_versions(client)
        if version_key(v["clusterVersion"]) > version_key(current)
    ]

    if not newer:
        return [("", f"No newer version is available; the cluster is on {current}")]

    next_version = min(newer, key=lambda v: version_key(v["clusterVersion"]))
    label = f"{next_version['clusterVersion']} ({version_status_label(next_version)})"

    return [(next_version["clusterVersion"], label)]


def run(job, resource, **kwargs):
    version = "{{ kubernetes_version }}".strip()

    if not version:
        return (
            "FAILURE",
            "",
            "No Kubernetes version was chosen; EKS offers nothing newer than the cluster's version.",
        )

    client = get_eks_client(resource)
    current = current_cluster_version(resource, client)

    set_progress(f"Updating cluster {resource.name} from Kubernetes {current} to {version}")

    try:
        update = client.update_cluster_version(name=resource.name, version=version)[
            "update"
        ]
    except ClientError as err:
        return "FAILURE", f"EKS refused the update to {version}", str(err)

    # Track the update object AWS returned; its status is InProgress, Failed,
    # Cancelled, or Successful.
    # https://docs.aws.amazon.com/eks/latest/APIReference/API_DescribeUpdate.html
    while update["status"] == "InProgress":
        set_progress(
            f'Cluster "{resource.name}" update {update["id"]} is {update["status"]}',
            increment_tasks=1,
        )
        time.sleep(30)
        update = client.describe_update(name=resource.name, updateId=update["id"])[
            "update"
        ]

    if update["status"] != "Successful":
        errors = "; ".join(
            f"{e.get('errorCode')}: {e.get('errorMessage')}"
            for e in update.get("errors", [])
        )
        return (
            "FAILURE",
            f"Cluster update finished with status {update['status']}",
            errors,
        )

    cluster = client.describe_cluster(name=resource.name)["cluster"]
    resource.kubernetes_version = cluster["version"]
    resource.platform_version = cluster["platformVersion"]
    resource.status = cluster["status"]
    resource.save()

    logger.info(f"resource {resource} updated to {cluster['version']}")

    return (
        "SUCCESS",
        f"Cluster {resource.name} is now on Kubernetes {cluster['version']}. Update its node groups next.",
        "",
    )
