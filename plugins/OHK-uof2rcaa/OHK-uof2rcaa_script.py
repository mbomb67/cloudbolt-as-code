"""
Discovery plugin for the Azure Kubernetes Service blueprint: returns one
dictionary per AKS managed cluster in every Azure resource handler's
subscription, plus one per node pool of the clusters CloudBolt already
tracks, and CloudBolt's discovery sync creates, updates or reactivates the
matching resources from them (servicecatalog/models.py,
ServiceBlueprint.sync_resources). The plugin never creates resources or
parameters itself.

Clusters and node pools are matched by Azure resource ID, so a cluster of the
same name in another resource group, or an EKS cluster of the same name, is
never confused with it. New clusters land in the Unassigned group; their node
pools are recorded on the next discovery (or by List AKS Node Pools), since a
node pool dictionary names its cluster's resource as its parent.

The one thing the sync cannot do is register the cluster as a Kubernetes
container orchestrator, which the namespace blueprint and the cluster tabs
need, so that is done here once per running cluster, the same way the build
plugin does it, and its ID is returned with the cluster.
  GET /subscriptions/{sub}/providers/Microsoft.ContainerService/managedClusters
  https://learn.microsoft.com/en-us/rest/api/aks/managed-clusters/list
  https://learn.microsoft.com/en-us/rest/api/aks/agent-pools/list
"""

from common.methods import set_progress
from infrastructure.models import Environment
from resourcehandlers.azure_arm.models import AzureARMHandler
from shared_modules.azure_aks import (
    AKS_API_VERSION,
    AKSCluster,
    AKSError,
    ArmClient,
    CLUSTER_ORCHESTRATOR_FIELD,
    cluster_values,
    ensure_orchestrator,
    node_pool_resource_type,
    parse_resource_id,
    pool_sub_resources,
    pool_values,
)
from utilities.logger import ThreadLogger

logger = ThreadLogger(__name__)

# The Azure resource ID of a cluster or node pool. The build records it on the
# cluster before asking Azure to create it, and on every node pool record.
RESOURCE_IDENTIFIER = "aks_resource_id"

# Lifecycles the build gives a cluster resource while it runs and when it
# fails (jobengine/jobmodules/deploy_blueprint_job.py).
BUILDING = "PROV"
BUILD_FAILED = "PROVFAILED"


def _key(resource_id):
    """Azure resource IDs compare case-insensitively."""
    return str(resource_id or "").lower()


def _known_resources(blueprint):
    """The blueprint's resources by resource ID: {key: resource}, an active
    resource winning over others with the same ID."""
    known = {}
    if blueprint is None:
        return known
    for resource in blueprint.resource_set.all().order_by("id"):
        key = _key(resource.get_cf_values_as_dict().get(RESOURCE_IDENTIFIER))
        if key and (key not in known or resource.lifecycle == "ACTIVE"):
            known[key] = resource
    return known


def _stored_id(resource):
    """The resource ID exactly as the resource stores it, which the sync matches on."""
    return resource.get_cf_values_as_dict().get(RESOURCE_IDENTIFIER)


def _environment_id(rh, location, current_env_id):
    """An Azure environment of this handler in the cluster's region, keeping the recorded one."""
    envs = Environment.objects.filter(resource_handler_id=rh.id, node_location=location)
    try:
        if current_env_id and envs.filter(id=int(current_env_id)).exists():
            return str(current_env_id)
    except (TypeError, ValueError):
        pass
    env = envs.order_by("name").first()
    return str(env.id) if env else None


def _without_none(values):
    return {k: v for k, v in values.items() if v not in (None, "")}


def cluster_dict(item, rh, arm, resource):
    """The values the sync sets on a cluster's resource, matching the parameters
    the build stores. ``resource`` is the resource already tracking the cluster,
    if any."""
    _, resource_group, name = parse_resource_id(item.get("id"))
    current = resource.get_cf_values_as_dict() if resource is not None else {}
    values = cluster_values(item)
    values.update({
        "name": name,
        "azure_rh_id": str(rh.id),
        "env_id": _environment_id(rh, item.get("location"), current.get("env_id")),
    })
    if resource is not None:
        values[RESOURCE_IDENTIFIER] = _stored_id(resource)
    if resource is not None and resource.lifecycle == BUILD_FAILED:
        # The sync only reactivates Historical resources by itself.
        values["lifecycle"] = "ACTIVE"
        set_progress(f"Recovering '{name}': its CloudBolt build failed after Azure created it.")

    power = ((item.get("properties") or {}).get("powerState") or {}).get("code")
    if power == "Stopped":
        set_progress(f"'{name}' is stopped; its orchestrator credentials were not refreshed.")
        return _without_none(values)
    try:
        orchestrator, env, created = ensure_orchestrator(
            AKSCluster(arm, resource_group, name), item.get("location"), current.get(CLUSTER_ORCHESTRATOR_FIELD)
        )
        values[CLUSTER_ORCHESTRATOR_FIELD] = orchestrator.id
        if created:
            set_progress(f"Registered container orchestrator '{orchestrator.name}' and environment '{env.name}'.")
    except AKSError as exc:
        set_progress(f"'{name}' was found, but its orchestrator could not be registered: {exc}")
    return _without_none(values)


def pool_dict(pool, cluster_resource, record):
    """The values the sync sets on a node pool's AKS Node Pool sub-resource,
    creating it under the cluster's resource when ``record`` is None."""
    values = pool_values(pool)
    values.update({
        "name": pool.get("name"),
        "resource_type": node_pool_resource_type(),
        "parent_resource": cluster_resource,
        "group": cluster_resource.group,
    })
    if cluster_resource.owner is not None:
        values["owner"] = cluster_resource.owner
    if record is not None:
        values[RESOURCE_IDENTIFIER] = _stored_id(record)
    return _without_none(values)


def _pool_records(cluster_resource, known):
    """The cluster's active node pool records by resource ID."""
    return {
        key: resource for key, resource in known.items()
        if resource.parent_resource_id == cluster_resource.id and resource.lifecycle == "ACTIVE"
    }


def discover_resources(**kwargs):
    known = _known_resources(kwargs.get("blueprint"))
    discovered = []
    returned = set()
    skipped_rh_ids = set()

    for rh in AzureARMHandler.objects.all():
        try:
            arm = ArmClient(rh)
            clusters = arm.list_json(
                f"/subscriptions/{arm.subscription_id}/providers/Microsoft.ContainerService/managedClusters",
                api_version=AKS_API_VERSION, what=f"Listing AKS clusters for {rh.name}",
            )
        except AKSError as exc:
            set_progress(f"Skipping Azure handler '{rh.name}': {exc}")
            skipped_rh_ids.add(str(rh.id))
            continue
        set_progress(f"Azure handler '{rh.name}': {len(clusters)} AKS cluster(s).")

        for item in clusters:
            key = _key(item.get("id"))
            # Two handlers on the same subscription see the same clusters.
            if not key or key in returned:
                continue
            returned.add(key)
            _, resource_group, name = parse_resource_id(item.get("id"))
            resource = known.get(key)
            if resource is not None and resource.lifecycle == BUILDING:
                set_progress(f"Skipping '{name}': its CloudBolt build is still running.")
                continue
            state = (item.get("properties") or {}).get("provisioningState")
            if resource is None and state == "Creating":
                set_progress(f"Skipping '{name}': AKS is still creating it; the next discovery adopts it.")
                continue
            discovered.append(cluster_dict(item, rh, arm, resource))
            if resource is None:
                continue

            # Node pools of a cluster CloudBolt already tracks.
            records = _pool_records(resource, known)
            try:
                pools = AKSCluster(arm, resource_group, name).list_pools()
            except AKSError as exc:
                set_progress(f"Node pools of '{name}' could not be read; their records are left as they are: {exc}")
                discovered.extend({RESOURCE_IDENTIFIER: _stored_id(r)} for r in records.values())
                continue
            # A record made before node pool records carried their resource ID
            # cannot be matched; List AKS Node Pools adds the ID to it.
            unmatched = {r.name for r in pool_sub_resources(resource) if not _stored_id(r)}
            for pool in pools:
                record = records.get(_key(pool.get("id")))
                if record is None and pool.get("name") in unmatched:
                    continue
                discovered.append(pool_dict(pool, resource, record))

    # The sync treats an active resource it is not given as gone and, when the
    # blueprint's auto_historical_resources is on, marks it Historical. A
    # skipped handler's clusters and their node pools were not checked, so each
    # is returned with its identifier alone, which matches it and changes
    # nothing.
    for key, resource in known.items():
        if resource.lifecycle != "ACTIVE" or key in returned or resource.parent_resource_id:
            continue
        if str(resource.get_cf_values_as_dict().get("azure_rh_id")) in skipped_rh_ids:
            discovered.append({RESOURCE_IDENTIFIER: _stored_id(resource)})
            discovered.extend({RESOURCE_IDENTIFIER: _stored_id(r)} for r in _pool_records(resource, known).values())

    return discovered
