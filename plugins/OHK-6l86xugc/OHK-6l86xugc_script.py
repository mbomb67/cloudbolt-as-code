"""
Discovery plugin for the Google Kubernetes Engine blueprint: returns one
dictionary per GKE cluster in the Google Cloud projects of the GCP
environments, and one per node pool of each cluster CloudBolt already tracks,
and CloudBolt's discovery sync creates, updates, or reactivates the matching
Kubernetes Cluster resources and gke_node_pool sub-resources from them
(servicecatalog/models.py, ServiceBlueprint.sync_resources). The plugin never
creates resources or parameters itself; new clusters land in the group named
Unassigned.

The one thing the sync cannot do is register the cluster as a Kubernetes
container orchestrator, which the namespace blueprint and the cluster tabs
need, so that is done here once per cluster, the way the build plugin does
it, and its ID is returned with the cluster.
  https://cloud.google.com/kubernetes-engine/docs/reference/rest/v1/projects.locations.clusters/list

Retiring on proof only. The blueprint keeps auto_historical_resources off,
so the sync never retires a resource just because it was not returned. This
plugin retires a tracked Active cluster or node pool itself, by returning its
identifier with lifecycle HISTORICAL, and only when the listing that should
contain it succeeded, it is missing from that listing, and a direct GET of
it answers 404. Node pools of a cluster proven gone are retired with it.
Anything else (permission denied, a location GKE could not list, a job still
running on the resource) proves nothing, and the resource is returned with
its identifier alone, which changes nothing. A Historical resource returned
without a lifecycle is set back to Active by the sync.
"""

from common.methods import set_progress
from infrastructure.models import Environment
from jobs.models import Job
from shared_modules.gcp_gke import (
    NODE_POOL_RESOURCE_TYPE,
    GKECluster,
    GKEApi,
    GKEError,
    cluster_values,
    ensure_orchestrator,
    node_pool_resource_type,
    pool_identifier,
    pool_values,
    project_for_environment,
)
from utilities.logger import ThreadLogger

logger = ThreadLogger(__name__)

# GKE's own unique ID for the cluster; a cluster rebuilt with the same name
# gets a new one. The build plugin stores it on every cluster it creates, and
# node pool records carry <cluster ID>/nodePools/<pool name> under it.
RESOURCE_IDENTIFIER = "gke_cluster_id"

# Cluster statuses in which the API server answers, so the orchestrator can be
# registered or refreshed.
# https://cloud.google.com/kubernetes-engine/docs/reference/rest/v1/projects.locations.clusters#status
SERVING = ("RUNNING", "RECONCILING", "DEGRADED")

RETIRED = {"lifecycle": "HISTORICAL"}


def handler_projects():
    """{(handler id, project): (handler, [environments])} for every GCP environment, sorted by name."""
    found = {}
    for env in Environment.objects.filter(resource_handler__resource_technology__slug="gcp").order_by("name"):
        rh = env.resource_handler.cast()
        try:
            project = project_for_environment(env, rh)
        except GKEError as exc:
            logger.info(f"Skipping environment {env.name} for GKE discovery: {exc}")
            continue
        found.setdefault((rh.id, project), (rh, []))[1].append(env)
    return found


def choose_env_id(current_env_id, environments):
    """Keep the environment the cluster was built or first discovered in while it still
    belongs to the handler and project; several environments can share a project."""
    ids = [str(env.id) for env in environments]
    current = str(current_env_id or "").strip()
    return current if current in ids else ids[0]


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


def get_orchestrator_id(api, project, cluster, current_id):
    """The ID of the cluster's Kubernetes orchestrator, created or refreshed with a
    cloudbolt-admin token (gcp_gke.ensure_orchestrator). A cluster whose API
    server cannot be reached keeps the orchestrator it has."""
    try:
        orchestrator, _, created = ensure_orchestrator(api, cluster, project, current_id=current_id)
    except GKEError as exc:
        set_progress(f"'{cluster['name']}': the orchestrator was not registered or refreshed: {exc}")
        return current_id or None
    if created:
        logger.info(f"Registered GKE cluster {cluster['name']} as an orchestrator")
    return orchestrator.id


def cluster_dict(api, project, cluster, handler, environments, current):
    """The values the sync sets on the cluster's resource, matching the
    parameters and value types the build plugin stores. ``current`` holds the
    parameter values of the resource already tracking the cluster, if any."""
    resource_dict = {
        key: value for key, value in cluster_values(cluster, project).items() if value not in (None, "")
    }
    resource_dict.update(
        name=cluster["name"],
        gcp_rh_id=str(handler.id),
        env_id=choose_env_id(current.get("env_id"), environments),
    )
    if cluster.get("endpoint") and cluster.get("status") in SERVING:
        orchestrator_id = get_orchestrator_id(api, project, cluster, current.get("container_orchestrator_id"))
        if orchestrator_id:
            resource_dict["container_orchestrator_id"] = orchestrator_id
    if current.get("lifecycle") == "PROVFAILED":
        # The build failed after GKE created the cluster (it records the ID as
        # soon as the cluster exists); the cluster is there, so the resource is too.
        resource_dict["lifecycle"] = "ACTIVE"
        set_progress(f"Recovering '{cluster['name']}': its CloudBolt build failed after GKE created it.")
    return resource_dict


def pool_dict(pool, cluster, cluster_resource, resource_type, total_nodes):
    """The values the sync sets on a node pool's gke_node_pool sub-resource, the
    same parameters List and Add GKE Node Pool store. resource_type,
    parent_resource and group are Resource attributes a discovery dictionary
    may set; no owner, so the one Add GKE Node Pool records is kept.
    https://cloud.google.com/kubernetes-engine/docs/reference/rest/v1/projects.locations.clusters.nodePools#NodePool
    """
    values = {key: value for key, value in pool_values(pool, cluster, total_nodes).items() if value is not None}
    values.update(name=pool.get("name"), resource_type=resource_type, parent_resource=cluster_resource,
                  group=cluster_resource.group)
    return values


def cluster_is_gone(values, listings):
    """True when the cluster's project was listed without it, GKE did not
    report its location missing from that listing, and clusters.get answers
    404. False when anything is unknown.
    https://cloud.google.com/kubernetes-engine/docs/reference/rest/v1/projects.locations.clusters/get
    """
    name, location = values.get("gke_cluster_name"), values.get("gke_location")
    try:
        key = (int(str(values.get("gcp_rh_id")).strip()), str(values.get("gke_project_id") or "").strip())
    except (TypeError, ValueError):
        return False
    listing = listings.get(key)
    if not name or not location or listing is None:
        return False
    if (location, name) in listing["clusters"] or location in listing["missing"]:
        return False
    try:
        return GKECluster(listing["api"], key[1], location, name).get(allow_404=True) is None
    except GKEError:
        return False


def pool_is_gone(resource, pool_listings):
    """True when the parent cluster's node pools were listed without this one
    and nodePools.get answers 404. False when anything is unknown.
    https://cloud.google.com/kubernetes-engine/docs/reference/rest/v1/projects.locations.clusters.nodePools/get
    """
    listing = pool_listings.get(resource.parent_resource_id)
    if listing is None or resource.name in listing["names"]:
        return False
    try:
        return listing["cluster"].get_pool(resource.name, allow_404=True) is None
    except GKEError:
        return False


def discover_resources(**kwargs):
    blueprint = kwargs.get("blueprint")
    tracked = list(blueprint.resource_set.all()) if blueprint is not None else []
    values_of = {
        resource.id: dict(resource.get_cf_values_as_dict(), lifecycle=resource.lifecycle, resource_name=resource.name)
        for resource in tracked
    }

    def is_pool(resource):
        return getattr(resource.resource_type, "name", "") == NODE_POOL_RESOURCE_TYPE

    cluster_resources = [r for r in tracked if not is_pool(r)]
    pool_resources = [r for r in tracked if is_pool(r)]

    # an active resource wins over Historical ones for the same cluster
    ordered = sorted(cluster_resources, key=lambda r: r.lifecycle == "ACTIVE")
    known_by_id = {values_of[r.id][RESOURCE_IDENTIFIER]: values_of[r.id]
                   for r in ordered if values_of[r.id].get(RESOURCE_IDENTIFIER)}
    active_by_id = {values_of[r.id][RESOURCE_IDENTIFIER]: r for r in cluster_resources
                    if r.lifecycle == "ACTIVE" and values_of[r.id].get(RESOURCE_IDENTIFIER)}
    # The build records name, project, and location before it asks GKE for
    # the cluster, and the ID once the cluster exists.
    known_by_place = {
        (values_of[r.id].get("gke_cluster_name"), values_of[r.id].get("gke_project_id"),
         values_of[r.id].get("gke_location")): values_of[r.id]
        for r in ordered if values_of[r.id].get("gke_cluster_name")
    }
    pools_by_cluster = {}
    for resource in pool_resources:
        if resource.lifecycle == "ACTIVE":
            pools_by_cluster.setdefault(resource.parent_resource_id, []).append(resource)

    pool_type = None
    discovered = []
    returned = set()
    listings = {}
    pool_listings = {}

    for (handler_id, project), (handler, environments) in handler_projects().items():
        # Not every GCP handler is set up for GKE: one whose credentials cannot
        # list clusters is skipped for that project, so it does not stop the others.
        try:
            api = GKEApi(handler)
            clusters, missing_zones = api.list_clusters(project)
        except GKEError as err:
            logger.info(f"Skipping GCP handler {handler.name} for GKE discovery in {project}: {err}")
            continue
        if missing_zones:
            set_progress(f"GKE could not list clusters in {', '.join(missing_zones)} of {project}.")
        listings[(handler_id, project)] = {
            "api": api,
            "clusters": {(c.get("location"), c.get("name")) for c in clusters},
            "missing": set(missing_zones),
        }

        for cluster in clusters:
            cluster_id = cluster.get("id")
            # Two handlers on the same project see the same cluster.
            if not cluster_id or cluster_id in returned:
                continue
            current = known_by_id.get(cluster_id)
            if current is None:
                # A resource that names this cluster but has no ID yet: a build
                # still running, or one that failed before GKE created the cluster.
                by_place = known_by_place.get((cluster.get("name"), project, cluster.get("location")))
                current = by_place if by_place and not by_place.get(RESOURCE_IDENTIFIER) else {}

            if current.get("lifecycle") == "PROV":
                set_progress(f"Skipping '{cluster.get('name')}': its CloudBolt build is still running.")
                if current.get(RESOURCE_IDENTIFIER):
                    returned.add(cluster_id)
                continue
            if current and not current.get(RESOURCE_IDENTIFIER):
                # The sync matches on the ID alone; adopting this cluster now would
                # duplicate the resource that recorded it without one.
                set_progress(
                    f"Skipping '{cluster.get('name')}': resource '{current.get('resource_name')}' "
                    f"({current.get('lifecycle')}) recorded it before GKE assigned an ID, and the sync matches "
                    "on the ID. Run List GKE Node Pools on that resource to record it, then run discovery again."
                )
                continue
            if cluster.get("status") == "PROVISIONING":
                # Not ready yet; a tracked one is matched and left alone.
                if current.get(RESOURCE_IDENTIFIER):
                    discovered.append({RESOURCE_IDENTIFIER: cluster_id})
                    returned.add(cluster_id)
                continue

            returned.add(cluster_id)
            discovered.append(cluster_dict(api, project, cluster, handler, environments, current))

            # Node pools are sub-resources of a cluster resource that already
            # exists; a newly discovered cluster gets them on the next run. A
            # job on the cluster (Add, Scale, Upgrade, Delete GKE Node Pool)
            # may be creating or removing one, so its node pools wait too.
            cluster_resource = active_by_id.get(cluster_id)
            if cluster_resource is None or has_running_job(cluster_resource):
                continue
            gke_cluster = GKECluster(api, project, cluster.get("location"), cluster.get("name"))
            try:
                pools = gke_cluster.list_pools()
                sizes = gke_cluster.pool_sizes(pools)
            except GKEError as err:
                logger.info(f"Could not list node pools of {cluster.get('name')}: {err}")
                continue
            pool_listings[cluster_resource.id] = {"cluster": gke_cluster,
                                                  "names": {p.get("name") for p in pools}}
            # A record made before node pools carried their identifier cannot be
            # matched, so its pool is not imported (that would duplicate it)
            # until List GKE Node Pools has recorded the identifier.
            unmatched_names = {r.name for r in pools_by_cluster.get(cluster_resource.id, [])
                               if not values_of[r.id].get(RESOURCE_IDENTIFIER)}
            if pool_type is None:
                pool_type = node_pool_resource_type()
            for pool in pools:
                identifier = pool_identifier(cluster_id, pool.get("name"))
                # Add GKE Node Pool files the record once GKE reports the pool.
                if pool.get("status") == "PROVISIONING" or identifier in returned \
                        or pool.get("name") in unmatched_names:
                    continue
                returned.add(identifier)
                discovered.append(pool_dict(pool, cluster, cluster_resource, pool_type,
                                            sizes.get(pool.get("name"))))

    # Every tracked Active cluster not returned above is retired only on
    # proof; the rest are returned with their identifier alone.
    gone_cluster_ids = set()
    for resource in cluster_resources:
        identifier = values_of[resource.id].get(RESOURCE_IDENTIFIER)
        if resource.lifecycle != "ACTIVE" or not identifier or identifier in returned:
            continue
        returned.add(identifier)
        if not has_running_job(resource) and cluster_is_gone(values_of[resource.id], listings):
            set_progress(f"GKE cluster {resource.name} no longer exists; retiring it.")
            gone_cluster_ids.add(resource.id)
            discovered.append(dict(RETIRED, **{RESOURCE_IDENTIFIER: identifier}))
        else:
            discovered.append({RESOURCE_IDENTIFIER: identifier})

    # A cluster already Historical in CloudBolt is checked the same way
    # before its Active node pools are retired with it.
    parents_checked = {}

    def parent_is_gone(resource):
        parent_id = resource.parent_resource_id
        if parent_id in gone_cluster_ids:
            return True
        if parent_id not in parents_checked:
            parent = next((r for r in cluster_resources if r.id == parent_id), None)
            parents_checked[parent_id] = (
                parent is not None and parent.lifecycle != "ACTIVE"
                and cluster_is_gone(values_of[parent.id], listings)
            )
        return parents_checked[parent_id]

    for resource in pool_resources:
        identifier = values_of[resource.id].get(RESOURCE_IDENTIFIER)
        if resource.lifecycle != "ACTIVE" or not identifier or identifier in returned:
            continue
        returned.add(identifier)
        if not has_running_job(resource) and (parent_is_gone(resource) or pool_is_gone(resource, pool_listings)):
            set_progress(f"GKE node pool {resource.name} no longer exists; retiring it.")
            discovered.append(dict(RETIRED, **{RESOURCE_IDENTIFIER: identifier}))
        else:
            discovered.append({RESOURCE_IDENTIFIER: identifier})

    return discovered
