"""
Discovery plugin for the Google Kubernetes Engine blueprint: returns one
dictionary per GKE cluster in the Google Cloud projects of the GCP
environments, and CloudBolt's discovery sync creates, updates, or reactivates
the matching Kubernetes Cluster resources from them
(servicecatalog/models.py, ServiceBlueprint.sync_resources). The plugin never
creates resources or parameters itself; new clusters land in the group named
Unassigned.

The one thing the sync cannot do is register the cluster as a Kubernetes
container orchestrator, which the namespace blueprint and the cluster tabs
need, so that is done here once per cluster, the way the build plugin does
it, and its ID is returned with the cluster.
  https://cloud.google.com/kubernetes-engine/docs/reference/rest/v1/projects.locations.clusters/list
"""

from common.methods import set_progress
from infrastructure.models import Environment
from shared_modules.gcp_gke import (
    GKEApi,
    GKEError,
    cluster_values,
    ensure_orchestrator,
    project_for_environment,
)
from utilities.logger import ThreadLogger

logger = ThreadLogger(__name__)

# GKE's own unique ID for the cluster; a cluster rebuilt with the same name
# gets a new one. The build plugin stores it on every cluster it creates.
RESOURCE_IDENTIFIER = "gke_cluster_id"

# Cluster statuses in which the API server answers, so the orchestrator can be
# registered or refreshed.
# https://cloud.google.com/kubernetes-engine/docs/reference/rest/v1/projects.locations.clusters#status
SERVING = ("RUNNING", "RECONCILING", "DEGRADED")


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


def discover_resources(**kwargs):
    blueprint = kwargs.get("blueprint")
    known = []
    if blueprint is not None:
        known = [
            dict(resource.get_cf_values_as_dict(), lifecycle=resource.lifecycle, resource_name=resource.name)
            for resource in blueprint.resource_set.all()
        ]
    # an active resource wins over Historical ones for the same cluster
    ordered = sorted(known, key=lambda v: v["lifecycle"] == "ACTIVE")
    known_by_id = {v[RESOURCE_IDENTIFIER]: v for v in ordered if v.get(RESOURCE_IDENTIFIER)}
    # The build records name, project, and location before it asks GKE for
    # the cluster, and the ID once the cluster exists.
    known_by_place = {
        (v.get("gke_cluster_name"), v.get("gke_project_id"), v.get("gke_location")): v
        for v in ordered if v.get("gke_cluster_name")
    }

    discovered = []
    returned = set()
    unchecked = []  # (handler id, project, locations or None for the whole project)

    for (handler_id, project), (handler, environments) in handler_projects().items():
        # Not every GCP handler is set up for GKE: one whose credentials cannot
        # list clusters is skipped for that project, so it does not stop the others.
        try:
            api = GKEApi(handler)
            clusters, missing_zones = api.list_clusters(project)
        except GKEError as err:
            logger.info(f"Skipping GCP handler {handler.name} for GKE discovery in {project}: {err}")
            unchecked.append((handler_id, project, None))
            continue
        if missing_zones:
            set_progress(f"GKE could not list clusters in {', '.join(missing_zones)} of {project}.")
            unchecked.append((handler_id, project, set(missing_zones)))

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

    # The sync treats an active cluster it is not given as gone and, when the
    # blueprint's auto_historical_resources is on, marks it Historical. A
    # cluster in an unchecked project or location was not looked at, so it is
    # returned with its identifier alone, which matches it and changes nothing.
    for values in known:
        identifier = values.get(RESOURCE_IDENTIFIER)
        if values["lifecycle"] != "ACTIVE" or not identifier or identifier in returned:
            continue
        for handler_id, project, locations in unchecked:
            if (str(values.get("gcp_rh_id")) == str(handler_id) and values.get("gke_project_id") == project
                    and (locations is None or values.get("gke_location") in locations)):
                discovered.append({RESOURCE_IDENTIFIER: identifier})
                returned.add(identifier)
                break

    return discovered
