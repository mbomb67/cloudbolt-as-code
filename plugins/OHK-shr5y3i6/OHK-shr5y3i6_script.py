"""
Build plugin for the Google Kubernetes Engine blueprint.

Creates a GKE Standard cluster in the chosen GCP environment's project,
registers it with CloudBolt as a Kubernetes container orchestrator with its
own Environment (which the GKE Namespace blueprint uses to decide who may
order namespaces on it), records the cluster and its node pools on the
resource, and returns once GKE reports the cluster ready.

The cluster follows Google's hardening guidance for a shared, multi-team
cluster (gcp_gke.cluster_body):
  * a release channel, so GKE upgrades the control plane and nodes
  * VPC-native networking on the chosen subnetwork
  * GKE Dataplane V2, which enforces the namespace NetworkPolicies the
    namespace blueprint applies
  * Workload Identity Federation for GKE
  * Shielded GKE nodes on Container-Optimized OS with auto-upgrade and
    auto-repair; no legacy ABAC and no client certificate
  * optional private nodes and Google Groups for RBAC
  https://cloud.google.com/kubernetes-engine/docs/reference/rest/v1/projects.locations.clusters/create
  https://cloud.google.com/kubernetes-engine/docs/how-to/hardening-your-cluster
  https://cloud.google.com/kubernetes-engine/docs/concepts/regional-clusters

CloudBolt reaches the API server with the token of a cloudbolt-admin
ServiceAccount that the build creates in kube-system with the handler's own
Google credentials (gcp_gke.register_orchestrator). Cluster Admin Principals
are bound to cluster-admin after that, through the orchestrator's token.

Entitled CloudBolt Groups, when chosen, are entitled to the per-cluster
Environment in place of CloudBolt's Unassigned placeholder, so they can order
namespaces on the cluster at once; cluster owners change the list later on
the cluster's Groups tab (Manage GKE Cluster Groups).

RBAC: users pick an Environment from group.get_available_environments(); the
GCP resource handler is never shown (docs/agents/rbac-and-security.md).
"""

import re

from common.methods import set_progress
from infrastructure.models import Environment
from shared_modules.gcp_gke import (
    CLUSTER_NAME_RE,
    DEFAULT_POOL_NAME,
    GKEApi,
    GKECluster,
    GKEError,
    KubeClient,
    RELEASE_CHANNELS,
    add_environment_groups,
    apply_cluster_admins,
    assignable_groups,
    channel_versions,
    cluster_body,
    ensure_cluster_fields,
    environment_machine_types,
    environment_regions,
    environment_zones,
    gcp_environments_for_group,
    group_ids_in,
    groups_for_ids,
    handler_for_environment,
    int_input,
    node_pool_body,
    parse_rbac_principals,
    pick_secondary_ranges,
    project_for_environment,
    register_orchestrator,
    relative_name,
    resolve_group,
    resource_label,
    stamp_cluster,
    subnetwork_parts,
    sync_pool_sub_resources,
    text_input,
    truthy,
)
from utilities.logger import ThreadLogger

logger = ThreadLogger(__name__)

REGIONAL = "regional"
ZONAL = "zonal"
AVAILABILITY = [
    (REGIONAL, "Regional - the control plane and nodes span several zones of the region"),
    (ZONAL, "Zonal - the control plane and nodes run in one zone"),
]

# Fallback when an environment offers no gcp_node_size options. Shared-core
# types (-micro, -small) are left out of the dropdown.
DEFAULT_MACHINE_TYPE = "e2-standard-4"
DEFAULT_MACHINE_TYPES = ["e2-standard-4", "e2-standard-8", "n2-standard-4", "n2-standard-8"]
SHARED_CORE_SUFFIXES = ("-micro", "-small")

# A sanity bound on the order form's per-zone node counts.
MAX_NODES = 1000

# Google Groups for RBAC needs a group named gke-security-groups in the domain.
# https://cloud.google.com/kubernetes-engine/docs/how-to/google-groups-rbac
SECURITY_GROUP_RE = re.compile(r"^gke-security-groups@[a-z0-9.-]+\.[a-z]{2,}$")


def _last(url):
    """The last path segment of a Google resource URL or relative name."""
    return str(url or "").rstrip("/").rsplit("/", 1)[-1]


def _environment(value):
    try:
        return Environment.objects.filter(id=int(str(value).strip())).first() if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _region_zones(env, region):
    """The environment's zones in a region, sorted."""
    return [zone for zone in environment_zones(env) if zone.rsplit("-", 1)[0] == region]


# ---------------------------------------------------------------------------
# Order-form options
# ---------------------------------------------------------------------------

def generate_options_for_gke_env_id(field, **kwargs):
    """GCP environments the ordering group may use."""
    envs = gcp_environments_for_group(resolve_group(kwargs.get("group")))
    if not envs:
        return [("", "------ No GCP environments available to this group ------")]
    return [(env.id, env.name) for env in envs]


def generate_options_for_gke_region(field, control_value=None, **kwargs):
    """Regions of the zones the environment offers (its gcp_zone options)."""
    env = _environment(control_value)
    if env is None:
        return [("", "------ First, select an environment ------")]
    regions = environment_regions(env)
    if not regions:
        return [("", "------ The environment offers no zones; import them on the environment ------")]
    return [(region, region) for region in regions]


def generate_options_for_gke_availability(field, **kwargs):
    return {"options": AVAILABILITY, "initial_value": REGIONAL, "sort": False}


def generate_options_for_gke_subnetwork(field, control_value=None, control_value_dict=None, **kwargs):
    """Subnetworks in the region the environment's handler may use, Shared VPC subnets included."""
    values = control_value_dict or {}
    env = _environment(values.get("gke_env_id"))
    region = text_input(values.get("gke_region"))
    if env is None or not region:
        return [("", "------ First, select an environment and a region ------")]
    try:
        rh = handler_for_environment(env)
        project = project_for_environment(env, rh)
        subnets = GKEApi(rh).usable_subnetworks(project, region)
    except GKEError as exc:
        logger.warning("Could not list subnetworks for %s in %s: %s", env, region, exc)
        return [("", f"------ Could not list subnetworks: {str(exc)[:120]} ------")]
    options = []
    for item in subnets:
        subnetwork = relative_name(item.get("subnetwork"))
        try:
            subnet_project, _, subnet_name = subnetwork_parts(subnetwork)
        except GKEError:
            continue
        label = f"{_last(item.get('network'))} / {subnet_name} ({item.get('ipCidrRange', '')})"
        if subnet_project != project:
            label += f" - Shared VPC host project {subnet_project}"
        options.append((subnetwork, label))
    if not options:
        return [("", f"------ No subnetwork in {region} is usable by the environment ------")]
    return options


def generate_options_for_gke_channel(field, **kwargs):
    return {"options": RELEASE_CHANNELS, "initial_value": "REGULAR", "sort": False}


def generate_options_for_gke_kubernetes_version(field, control_value=None, control_value_dict=None, **kwargs):
    """The release channel's valid versions in the region, newest first, after its default."""
    values = control_value_dict or {}
    env = _environment(values.get("gke_env_id"))
    region = text_input(values.get("gke_region"))
    channel = text_input(values.get("gke_channel")) or "REGULAR"
    if env is None or not region:
        return [("", "------ First, select an environment and a region ------")]
    try:
        rh = handler_for_environment(env)
        config = GKEApi(rh).server_config(project_for_environment(env, rh), region)
    except GKEError as exc:
        logger.warning("Could not read GKE versions for %s in %s: %s", env, region, exc)
        return [("", f"------ Could not list versions: {str(exc)[:120]} ------")]
    default, versions, _ = channel_versions(config, channel)
    options = [("-", f"Channel default ({default})" if default else "Channel default")]
    for version in versions:
        options.append((version, f"{version} - channel default" if version == default else version))
    return {"options": options, "initial_value": "-", "sort": False}


def generate_options_for_gke_machine_type(field, control_value=None, **kwargs):
    """The environment's machine types (gcp_node_size options) without shared-core types."""
    env = _environment(control_value)
    if env is None:
        return [("", "------ First, select an environment ------")]
    types = [t for t in environment_machine_types(env) if not t.lower().endswith(SHARED_CORE_SUFFIXES)]
    types = types or DEFAULT_MACHINE_TYPES
    initial = DEFAULT_MACHINE_TYPE if DEFAULT_MACHINE_TYPE in types else types[0]
    return {"options": [(t, t) for t in types], "initial_value": initial}


def generate_options_for_gke_entitled_groups(field, **kwargs):
    """Every CloudBolt group but the Unassigned placeholders."""
    return [(g.global_id, g.name) for g in assignable_groups()]


# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------

def _labels(resource):
    """Resource labels that tie the cluster back to CloudBolt."""
    labels = {"cloudbolt-resource": resource_label(resource.global_id)}
    if resource.group is not None:
        labels["cloudbolt-group"] = resource_label(resource.group.name)
    return labels


def _record_partial(resource, cluster, project, rh, env):
    """After a failed create, stamp whatever GKE created so teardown and discovery find it."""
    try:
        existing = cluster.get(allow_404=True)
    except GKEError as exc:
        logger.warning("Could not read %s after a failed create: %s", cluster.name, exc)
        return
    if existing is not None:
        stamp_cluster(resource, existing, project, rh=rh, env=env)
        set_progress(f"GKE reports '{cluster.name}' {existing.get('status')}; it is recorded on the resource.")


def run(job, resource, **kwargs):
    ensure_cluster_fields()
    name = text_input("{{ gke_cluster_name }}")
    env_id = text_input("{{ gke_env_id }}")
    region = text_input("{{ gke_region }}")
    availability = text_input("{{ gke_availability }}") or REGIONAL
    subnetwork = text_input("{{ gke_subnetwork }}")
    channel = text_input("{{ gke_channel }}") or "REGULAR"
    version = text_input("{{ gke_kubernetes_version }}")
    machine_type = text_input("{{ gke_machine_type }}")
    node_count = int_input("{{ gke_node_count }}", 1)
    max_count = int_input("{{ gke_max_node_count }}", 0)
    private = truthy("{{ gke_enable_private_nodes }}")
    admins_text = text_input("""{{ gke_admin_principals }}""")
    security_group = text_input("{{ gke_security_group }}").lower()
    entitled_ids = group_ids_in("""{{ gke_entitled_groups }}""")

    # ---- Validate ----------------------------------------------------------
    if not CLUSTER_NAME_RE.match(name):
        return ("FAILURE", "", f"'{name}' is not a valid GKE cluster name: up to 40 lowercase letters, digits, "
                "and hyphens, starting with a letter and ending with a letter or digit.")
    env = _environment(env_id)
    if env is None:
        return "FAILURE", "", "Choose a GCP environment."
    group = resource.group
    if group is not None and env.id not in [e.id for e in gcp_environments_for_group(group)]:
        return "FAILURE", "", f"Group '{group.name}' may not order into environment '{env.name}'."
    if region not in environment_regions(env):
        return "FAILURE", "", f"Environment '{env.name}' offers no zones in region '{region}'."
    if availability not in (REGIONAL, ZONAL):
        return "FAILURE", "", f"Availability must be {REGIONAL} or {ZONAL}, not '{availability}'."
    if channel not in [value for value, _ in RELEASE_CHANNELS]:
        return "FAILURE", "", f"'{channel}' is not a release channel."
    if not subnetwork:
        return "FAILURE", "", "Choose a subnetwork."
    try:
        _, subnet_region, _ = subnetwork_parts(subnetwork)
    except GKEError as exc:
        return "FAILURE", "", str(exc)
    if subnet_region != region:
        return "FAILURE", "", f"Subnetwork '{subnetwork}' is not in region '{region}'."
    if not machine_type:
        return "FAILURE", "", "Choose a machine type."
    if not 1 <= node_count <= MAX_NODES:
        return "FAILURE", "", f"Nodes per zone must be between 1 and {MAX_NODES}."
    if max_count and not node_count <= max_count <= MAX_NODES:
        return ("FAILURE", "", f"Autoscale Maximum ({max_count}) must be at least the nodes per zone "
                f"({node_count}) and at most {MAX_NODES}.")
    if security_group and not SECURITY_GROUP_RE.match(security_group):
        return ("FAILURE", "", f"'{security_group}' is not a Google Groups for RBAC group; it must be "
                "gke-security-groups@YOUR-DOMAIN.")
    admin_subjects, bad = parse_rbac_principals(admins_text)
    if bad:
        return ("FAILURE", "", f"Not Google principals: {', '.join(bad)}. Give user:EMAIL, group:EMAIL, "
                "serviceAccount:EMAIL, or an email address.")
    if any(s["kind"] == "Group" for s in admin_subjects) and not security_group:
        return ("FAILURE", "", "Google Group cluster admins work only with Google Groups for RBAC; set the RBAC "
                "Security Group, or name the admins as users.")
    entitled_groups, unknown = groups_for_ids(entitled_ids)
    if unknown:
        return "FAILURE", "", f"No CloudBolt group to entitle has the ID(s) {', '.join(unknown)}."

    try:
        rh = handler_for_environment(env)
        project = project_for_environment(env, rh)
        api = GKEApi(rh)
    except GKEError as exc:
        return "FAILURE", "", str(exc)

    location = region
    if availability == ZONAL:
        zones = _region_zones(env, region)
        if not zones:
            return "FAILURE", "", f"Environment '{env.name}' offers no zone in region '{region}'."
        location = zones[0]

    # ---- Network, version, and name checks against GCP ---------------------
    try:
        usable = {relative_name(item.get("subnetwork")): item for item in api.usable_subnetworks(project, region)}
        if subnetwork not in usable:
            return ("FAILURE", "", f"The environment's handler may not use subnetwork '{subnetwork}' "
                    f"(compute.subnetworks.use).")
        if version and version != "-":
            _, valid, _ = channel_versions(api.server_config(project, location), channel)
            if version not in valid:
                return ("FAILURE", "", f"GKE does not offer version {version} on the {channel} channel in "
                        f"{location}. It offers: {', '.join(valid) or 'none'}.")
        cluster = GKECluster(api, project, location, name)
        if cluster.get(allow_404=True) is not None:
            return ("FAILURE", "", f"A GKE cluster named '{name}' already exists in {location} of project "
                    f"{project}. Choose another name, or run the blueprint's discovery to import it.")
    except GKEError as exc:
        return "FAILURE", "", str(exc)

    # cluster_body names a subnetwork in the cluster's own project by its short
    # name and a Shared VPC one by its full path. A Shared VPC subnetwork also
    # needs existing secondary ranges for Pods and Services.
    network = relative_name(usable[subnetwork].get("network"))
    pod_range = service_range = ""
    if subnetwork_parts(subnetwork)[0] != project:
        try:
            pod_range, service_range = pick_secondary_ranges(api.secondary_ranges(project, subnetwork))
        except GKEError as exc:
            return "FAILURE", "", str(exc)
        set_progress(f"Shared VPC subnetwork: Pods use secondary range '{pod_range}', Services '{service_range}'.")

    resource.name = name
    resource.set_value_for_custom_field("gke_cluster_name", name)
    resource.set_value_for_custom_field("gke_project_id", project)
    resource.set_value_for_custom_field("gke_location", location)
    resource.set_value_for_custom_field("gcp_rh_id", str(rh.id))
    resource.set_value_for_custom_field("env_id", str(env.id))
    resource.save()

    # ---- Create ----------------------------------------------------------
    pool = node_pool_body(DEFAULT_POOL_NAME, machine_type, node_count, max_count)
    body = cluster_body(
        name, project, location, subnetwork, network, pool,
        channel=channel,
        pod_range=pod_range,
        service_range=service_range,
        version="" if version in ("", "-") else version,
        private_nodes=private,
        security_group=security_group,
        labels=_labels(resource),
        description=f"Created by CloudBolt ({resource.global_id}).",
    )
    scale = f"{node_count} node(s) per zone" + (f", autoscaling to {max_count}" if max_count > node_count else "")
    set_progress(
        f"Creating {availability} GKE cluster '{name}' in {location} (project {project}): "
        f"{channel} channel, {'channel default version' if version in ('', '-') else version}, "
        f"{DEFAULT_POOL_NAME} {machine_type} x {scale}"
        + (", private nodes" if private else "") + "..."
    )
    try:
        created = cluster.create(body)
    except GKEError as exc:
        logger.exception("GKE create failed")
        _record_partial(resource, cluster, project, rh, env)
        if "did not finish within" in str(exc):
            return ("FAILURE", "", f"{exc} GKE carries on; run the blueprint's discovery once the cluster is "
                    "running to recover this resource.")
        return "FAILURE", "", f"GKE refused or failed to create '{name}': {exc}"
    stamp_cluster(resource, created, project, rh=rh, env=env)
    set_progress(f"GKE reports '{name}' {created.get('status')} on version {created.get('currentMasterVersion')}.")

    # ---- Node pools --------------------------------------------------------
    try:
        pools = cluster.list_pools()
        sync_pool_sub_resources(resource, pools, created, cluster.pool_sizes(pools))
        set_progress(f"Recorded {len(pools)} node pool(s) as GKE Node Pool sub-resources.")
    except GKEError as exc:
        set_progress(f"Warning: the node pools could not be recorded: {exc}")

    # ---- CloudBolt orchestrator and per-cluster environment ----------------
    try:
        orchestrator, cluster_env, _ = register_orchestrator(resource, cluster, created)
    except GKEError as exc:
        return ("FAILURE", "", f"The cluster was created, but CloudBolt could not register it as a container "
                f"orchestrator: {exc} Run List GKE Node Pools on the cluster to try again.")
    set_progress(f"Registered container orchestrator '{orchestrator.name}' and environment '{cluster_env.name}'.")

    # ---- Cluster admins --------------------------------------------------
    if admin_subjects:
        kube = None
        try:
            kube = KubeClient.from_orchestrator(orchestrator)
            apply_cluster_admins(kube, admin_subjects)
        except GKEError as exc:
            return ("FAILURE", "", f"The cluster was created, but its cluster admins could not be bound: {exc}")
        finally:
            if kube is not None:
                kube.close()
        set_progress("Bound cluster-admin to " + ", ".join(
            f"{s['kind'].lower()}:{s['name']}" for s in admin_subjects) + ".")

    # ---- Groups that may order namespaces on the cluster -------------------
    ready = f"GKE cluster '{name}' is ready (Kubernetes {created.get('currentMasterVersion')}, {location})."
    if not entitled_groups:
        return ("SUCCESS", f"{ready} Entitle groups on the cluster's Groups tab (environment "
                f"'{cluster_env.name}') to let them order namespaces on it.", "")
    names = add_environment_groups(cluster_env, entitled_groups, profile=job.owner, job=job)
    set_progress(f"Entitled group(s) {', '.join(names)} to environment '{cluster_env.name}'.")
    return ("SUCCESS", f"{ready} Group(s) {', '.join(g.name for g in entitled_groups)} may order "
            "namespaces on it.", "")
