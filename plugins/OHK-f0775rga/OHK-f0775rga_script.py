"""
Day-2 action for the AWS EKS blueprint: update one EKS add-on to a newer
version that AWS publishes for the cluster's Kubernetes version.

The add-on and its target version arrive together as the addon input, in the
form name/version (for example vpc-cni/v1.19.0-eksbuild.1), so the dialog can
list every available upgrade in a single dropdown. The job starts the update,
polls it until AWS reports a terminal status, and records the result.
"""

import re
import time

from botocore.exceptions import ClientError

from common.methods import set_progress
from infrastructure.models import Environment
from resourcehandlers.aws.models import AWSHandler
from utilities.logger import ThreadLogger

logger = ThreadLogger(__name__)

POLL_SECONDS = 15

# How EKS treats add-on fields that were changed on the cluster. AWS recommends
# testing Preserve on a non-production cluster first.
# https://docs.aws.amazon.com/eks/latest/APIReference/API_UpdateAddon.html
RESOLVE_CONFLICTS = [
    ("PRESERVE", "Preserve: keep values changed on the cluster"),
    ("OVERWRITE", "Overwrite: reset changed values to the EKS defaults"),
    ("NONE", "None: fail the update if a value was changed"),
]


def get_eks_client(resource):
    """
    EKS client for the cluster's account and region: the environment the
    cluster was ordered into (env_id), or the handler and region the build
    and discovery plugins store on the resource.
    """
    values = resource.get_cf_values_as_dict()
    env_id = values.get("env_id")
    if env_id:
        env = Environment.objects.get(id=int(env_id))
        rh: AWSHandler = env.resource_handler.cast()
        return rh.get_boto3_client(env.aws_region, "eks")
    rh = AWSHandler.objects.get(id=int(values["aws_rh_id"]))
    return rh.get_boto3_client(values["aws_region"], "eks")


def cluster_name(resource):
    return resource.get_cf_values_as_dict().get("eks_cluster_name") or resource.name


def version_key(version):
    """Sortable form of an add-on version such as v1.19.0-eksbuild.1."""
    return tuple(int(part) for part in re.findall(r"\d+", str(version or "")))


def list_addons(client, cluster):
    """
    Names of the add-ons installed on the cluster. The API pages with nextToken.
    https://docs.aws.amazon.com/eks/latest/APIReference/API_ListAddons.html
    """
    names = []
    token = None
    while True:
        params = {"clusterName": cluster}
        if token:
            params["nextToken"] = token
        page = client.list_addons(**params)
        names.extend(page.get("addons", []))
        token = page.get("nextToken")
        if not token:
            break
    return sorted(names)


def compatible_versions(client, addon_name, kubernetes_version):
    """
    Versions of the add-on that AWS publishes for the cluster's Kubernetes
    version, newest first, plus the version AWS marks as the default for it.
    The API pages with nextToken.
    https://docs.aws.amazon.com/eks/latest/APIReference/API_DescribeAddonVersions.html
    """
    versions = {}
    token = None
    while True:
        params = {"addonName": addon_name, "kubernetesVersion": kubernetes_version}
        if token:
            params["nextToken"] = token
        page = client.describe_addon_versions(**params)
        for addon in page.get("addons", []):
            for info in addon.get("addonVersions", []):
                compatible = [
                    c
                    for c in info.get("compatibilities", [])
                    if c.get("clusterVersion") == kubernetes_version
                ]
                if info.get("compatibilities") and not compatible:
                    continue
                versions[info["addonVersion"]] = any(
                    c.get("defaultVersion") for c in compatible
                )
        token = page.get("nextToken")
        if not token:
            break

    ordered = sorted(versions, key=version_key, reverse=True)
    default = next((v for v in ordered if versions[v]), None)
    return ordered, default


def generate_options_for_addon(server=None, **kwargs):
    """
    Every upgrade available on the cluster: for each installed add-on, the
    versions newer than the installed one that AWS publishes for the
    cluster's Kubernetes version, newest first.
    """
    resource = kwargs.get("resource", None)
    if resource is None:
        return []

    client = get_eks_client(resource)
    cluster = cluster_name(resource)
    kubernetes_version = client.describe_cluster(name=cluster)["cluster"]["version"]

    options = []
    for name in list_addons(client, cluster):
        installed = client.describe_addon(clusterName=cluster, addonName=name)[
            "addon"
        ].get("addonVersion", "")
        versions, default = compatible_versions(client, name, kubernetes_version)
        newer = [v for v in versions if version_key(v) > version_key(installed)]
        for index, version in enumerate(newer):
            notes = []
            if index == 0:
                notes.append("latest")
            if version == default:
                notes.append("EKS default")
            suffix = f" ({', '.join(notes)})" if notes else ""
            options.append(
                (f"{name}/{version}", f"{name}: {installed} to {version}{suffix}")
            )

    if not options:
        return [("", "Every add-on is on the latest version available for this cluster")]
    return options


def generate_options_for_resolve_conflicts(server=None, **kwargs):
    return {"initial_value": "PRESERVE", "options": RESOLVE_CONFLICTS}


def run(job, resource, **kwargs):
    choice = "{{ addon }}".strip()
    resolve_conflicts = "{{ resolve_conflicts }}".strip().upper() or "PRESERVE"

    if "/" not in choice:
        return (
            "FAILURE",
            "",
            "No add-on upgrade was chosen (expected an add-on name and version).",
        )
    addon_name, version = choice.split("/", 1)

    if resolve_conflicts not in {code for code, _ in RESOLVE_CONFLICTS}:
        return "FAILURE", "", f"Unknown conflict resolution {resolve_conflicts}."

    client = get_eks_client(resource)
    cluster = cluster_name(resource)

    try:
        addon = client.describe_addon(clusterName=cluster, addonName=addon_name)["addon"]
    except ClientError as err:
        return "FAILURE", f"Add-on {addon_name} could not be read", str(err)

    installed = addon.get("addonVersion")
    if installed == version:
        return "SUCCESS", f"Add-on {addon_name} already runs {version}.", ""

    set_progress(
        f"Updating add-on {addon_name} on cluster {cluster} from {installed} to "
        f"{version} (changed fields: {resolve_conflicts.lower()})"
    )

    # https://docs.aws.amazon.com/eks/latest/APIReference/API_UpdateAddon.html
    try:
        update = client.update_addon(
            clusterName=cluster,
            addonName=addon_name,
            addonVersion=version,
            resolveConflicts=resolve_conflicts,
        )["update"]
    except ClientError as err:
        return "FAILURE", f"EKS refused the update of {addon_name} to {version}", str(err)

    # Track the update AWS returned; its status is InProgress, Failed,
    # Cancelled, or Successful.
    # https://docs.aws.amazon.com/eks/latest/APIReference/API_DescribeUpdate.html
    while update["status"] == "InProgress":
        set_progress(
            f'Add-on "{addon_name}" update {update["id"]} is {update["status"]}',
            increment_tasks=1,
        )
        time.sleep(POLL_SECONDS)
        update = client.describe_update(
            name=cluster, addonName=addon_name, updateId=update["id"]
        )["update"]

    if update["status"] != "Successful":
        errors = "; ".join(
            f"{e.get('errorCode')}: {e.get('errorMessage')}"
            for e in update.get("errors", [])
        )
        return (
            "FAILURE",
            f"Add-on update finished with status {update['status']}",
            errors,
        )

    addon = client.describe_addon(clusterName=cluster, addonName=addon_name)["addon"]
    logger.info(f"add-on {addon_name} on {cluster} is now {addon.get('addonVersion')}")

    return (
        "SUCCESS",
        f"Add-on {addon_name} is now {addon.get('addonVersion')} ({addon.get('status')}).",
        "",
    )
