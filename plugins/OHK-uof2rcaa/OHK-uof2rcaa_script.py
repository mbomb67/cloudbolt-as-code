"""
Discovery plugin for the Azure Kubernetes Service blueprint.

Adopts every AKS managed cluster in every Azure resource handler's
subscription as a Kubernetes Cluster resource of this blueprint, and keeps
the ones already recorded up to date: cluster details, AKS Node Pool
sub-resources, and the Kubernetes container orchestrator and per-cluster
Environment CloudBolt reaches the cluster through.

A cluster is matched to its resource by Azure resource ID, so a cluster of
the same name in another resource group, or an EKS cluster of the same name,
is never confused with it. New resources land in the Unassigned group.
  GET /subscriptions/{sub}/providers/Microsoft.ContainerService/managedClusters
  https://learn.microsoft.com/en-us/rest/api/aks/managed-clusters/list
"""

from accounts.models import Group
from common.methods import set_progress
from infrastructure.models import Environment
from resources.models import Resource, ResourceType
from resourcehandlers.azure_arm.models import AzureARMHandler
from shared_modules.azure_aks import (
    AKS_API_VERSION,
    AKSCluster,
    AKSError,
    ArmClient,
    CLUSTER_RESOURCE_TYPE,
    parse_resource_id,
    register_orchestrator,
    stamp_cluster,
    sync_pool_sub_resources,
)
from utilities.logger import ThreadLogger

logger = ThreadLogger(__name__)


def _existing(resource_id):
    """The active Kubernetes Cluster resource recorded with this Azure resource ID, or None."""
    wanted = str(resource_id).lower()
    for resource in Resource.objects.filter(resource_type__name=CLUSTER_RESOURCE_TYPE, lifecycle="ACTIVE"):
        if str(resource.get_cf_values_as_dict().get("aks_resource_id") or "").lower() == wanted:
            return resource
    return None


def _environment_for(rh, location, current_env_id=None):
    """An Azure environment of this handler in the cluster's region, keeping the recorded one."""
    envs = Environment.objects.filter(resource_handler_id=rh.id, node_location=location)
    if current_env_id and envs.filter(id=int(current_env_id)).exists():
        return envs.get(id=int(current_env_id))
    return envs.order_by("name").first()


def discover_resources(**kwargs):
    blueprint = kwargs.get("blueprint")
    resource_type = (
        getattr(blueprint, "resource_type", None)
        or ResourceType.objects.filter(name=CLUSTER_RESOURCE_TYPE).first()
    )
    unassigned = Group.objects.filter(name__startswith="Unassigned").first()
    created = updated = 0
    seen = set()

    for rh in AzureARMHandler.objects.all():
        try:
            arm = ArmClient(rh)
            clusters = arm.list_json(
                f"/subscriptions/{arm.subscription_id}/providers/Microsoft.ContainerService/managedClusters",
                api_version=AKS_API_VERSION, what=f"Listing AKS clusters for {rh.name}",
            )
        except AKSError as exc:
            set_progress(f"Skipping Azure handler '{rh.name}': {exc}")
            continue
        set_progress(f"Azure handler '{rh.name}': {len(clusters)} AKS cluster(s).")

        for item in clusters:
            resource_id = item.get("id", "")
            if resource_id.lower() in seen:
                # Two handlers on the same subscription see the same clusters.
                continue
            seen.add(resource_id.lower())
            _, resource_group, name = parse_resource_id(resource_id)

            resource = _existing(resource_id)
            if resource is None:
                resource = Resource.objects.create(
                    name=name,
                    blueprint=blueprint,
                    group=unassigned,
                    resource_type=resource_type,
                    lifecycle="ACTIVE",
                )
                created += 1
                set_progress(f"Adopting AKS cluster '{name}' ({resource_group}).")
            else:
                updated += 1

            env = _environment_for(rh, item.get("location"), resource.get_cf_values_as_dict().get("env_id"))
            stamp_cluster(resource, item, rh=rh, env=env)

            cluster = AKSCluster(arm, resource_group, name)
            try:
                sync_pool_sub_resources(resource, cluster.list_pools())
            except AKSError as exc:
                set_progress(f"Node pools of '{name}' could not be read: {exc}")

            power = ((item.get("properties") or {}).get("powerState") or {}).get("code")
            if power == "Stopped":
                set_progress(f"'{name}' is stopped; its orchestrator credentials were not refreshed.")
                continue
            try:
                register_orchestrator(resource, cluster, item.get("location"))
            except AKSError as exc:
                set_progress(f"'{name}' was recorded, but its orchestrator could not be registered: {exc}")

    set_progress(f"AKS discovery: {created} cluster(s) adopted, {updated} updated.")
    return []
