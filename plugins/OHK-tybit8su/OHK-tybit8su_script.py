"""
CloudBolt discovery plugin: AKS Namespaces.

Inventories the AKS managed namespaces on every AKS cluster CloudBolt
manages (the Kubernetes Cluster resources of the Azure Kubernetes Service
blueprint that have a container orchestrator) and returns one dict per
namespace for CloudBolt to create/update Resources under the "AKS Namespace"
blueprint. A managed namespace is an Azure resource, so it is read through
Azure Resource Manager:
  https://learn.microsoft.com/en-us/rest/api/aks/managed-namespaces/list-by-managed-cluster
  https://learn.microsoft.com/en-us/azure/aks/concepts-managed-namespaces

Every managed namespace is adopted, whether CloudBolt created it or not,
except one AKS is still provisioning or failed to provision. Plain
Kubernetes namespaces that are not managed namespaces are not imported.

Each adopted namespace gets what an ordered one has:
  - the CloudBolt Group in its cloudbolt.io/group-id annotation as owner;
    a new resource without one goes to the first group named Unassigned,
    and an existing resource without one keeps its group;
  - the cluster resource as parent, so it shows on the cluster's page;
  - the Environment pinned to the namespace, created when missing and
    entitled to the owning group (an Unassigned owner is entitled only to a
    new environment, which keeps that environment from being offered to
    every group until an admin assigns one);
  - the tier from its cloudbolt.io/size-tier label when valid, else the tier
    whose CPU and memory match its quota, and the quota numbers;
  - its network policy, Kubernetes UID, and who holds an AKS RBAC role on it.

RBAC: discovery enumerates every AKS cluster; CloudBolt filters the returned
resources by each user's access.

Discovery contract:
  - Every dict MUST include a "name" key.
  - RESOURCE_IDENTIFIER names the field carrying the cloud-native unique ID
    (the managed namespace's Azure resource ID). CloudBolt matches it
    exactly, and Azure resource IDs are case-insensitive, so an ID that
    matches an existing resource's apart from case is returned as stored.
  - "parent_resource_id" nests the namespace under its cluster resource and
    "group" hands it to its owning CloudBolt Group; both are Resource
    attributes CloudBolt lets a discovery dict set.
  - All other keys are the same aks_namespace_* fields the build and teardown
    plugins use.

Entry point: discover_resources(**kwargs) -> list[dict]
"""

import re

from accounts.models import Group
from common.methods import set_progress
from resources.models import Resource
from shared_modules.aks_namespace import (
    ANNOTATION_EXPIRES,
    CF_PREFIX,
    LABEL_TIER,
    NamespaceClient,
    TIERS,
    adoptable_namespace,
    cluster_region,
    cluster_resources,
    describe_network_policy,
    ensure_custom_fields,
    ensure_environment_for_namespace,
    entitle_group,
    entitled_group_names,
    list_managed_namespaces,
    load_orchestrator,
    namespace_access,
    namespace_blueprint,
    owning_group_for_namespace,
    register_namespace_record,
    render_access,
    set_environment_quota,
)
from shared_modules.azure_aks import AKSError, cluster_for_resource
from utilities.logger import ThreadLogger

logger = ThreadLogger(__name__)

# The managed namespace's Azure resource ID is the unique key for a landing zone.
RESOURCE_IDENTIFIER = CF_PREFIX + "resource_id"

_QUANTITY_RE = re.compile(r"^(\d+(?:\.\d+)?)([A-Za-z]*)$")
_GI_FACTORS = {"": 1 / (1024 ** 3), "Ki": 1 / (1024 ** 2), "Mi": 1 / 1024, "Gi": 1, "Ti": 1024,
               "k": 1e3 / (1024 ** 3), "M": 1e6 / (1024 ** 3), "G": 1e9 / (1024 ** 3), "T": 1e12 / (1024 ** 3)}


def _quantity_to_int(value, unit):
    """Parse a Kubernetes quantity into an int of cores or GiB.

    https://kubernetes.io/docs/reference/kubernetes-api/common-definitions/quantity/
    The managed namespace quota uses the same quantities (CPU in millicores,
    memory in Mi or Gi):
    https://learn.microsoft.com/en-us/rest/api/aks/managed-namespaces/create-or-update#resourcequota
    """
    match = _QUANTITY_RE.match(str(value or "").strip())
    if not match:
        return None
    number, suffix = float(match.group(1)), match.group(2)
    if unit == "cpu":
        return int(round(number / 1000)) if suffix == "m" else int(round(number))
    factor = _GI_FACTORS.get(suffix)
    return int(round(number * factor)) if factor is not None else None


def _tier_key(labelled, quota):
    """The tier label when valid; else the tier whose CPU and memory match the quota; else 'custom'."""
    labelled = str(labelled or "").strip().lower()
    if labelled in TIERS:
        return labelled
    cpu = _quantity_to_int(quota.get("cpuLimit"), "cpu")
    memory = _quantity_to_int(quota.get("memoryLimit"), "gib")
    for key, tier in TIERS.items():
        if tier["cpu"] == cpu and tier["memory_gb"] == memory:
            return key
    return "custom"


def _known_resources(blueprint):
    """Lower-cased managed namespace ID -> (ID as stored, resource), preferring active resources."""
    known = {}
    if blueprint is None:
        return known
    for resource in Resource.objects.filter(blueprint=blueprint):
        stored = resource.get_value_for_custom_field(RESOURCE_IDENTIFIER)
        if not stored:
            continue
        key = str(stored).lower()
        if key not in known or resource.lifecycle == "ACTIVE":
            known[key] = (str(stored), resource)
    return known


def _kubernetes_client(orchestrator, cluster_name):
    if orchestrator is None:
        return None
    try:
        return NamespaceClient.from_orchestrator(orchestrator)
    except Exception as exc:
        logger.warning("No Kubernetes API client for %s: %s", cluster_name, exc)
        return None


def _kubernetes_uid(client, namespace):
    if client is None:
        return None
    try:
        return ((client.get_namespace(namespace) or {}).get("metadata") or {}).get("uid") or None
    except Exception as exc:
        logger.warning("Could not read the UID of %s: %s", namespace, exc)
        return None


def _access_text(cluster, namespace):
    try:
        return render_access(namespace_access(cluster, namespace))
    except Exception as exc:
        logger.warning("Could not list access to %s: %s", namespace, exc)
        return None


def _pin_environment(orchestrator, cluster_name, namespace, region, group, tier):
    """The Environment pinned to the namespace, created when missing and entitled to ``group``."""
    if orchestrator is None:
        return None
    # TEMP diagnostics
    from infrastructure.models import Environment as _Env
    for _e in _Env.objects.filter(container_orchestrator_id=orchestrator.id):
        _vals = [(c.field.name, c.field_id, repr(c.value)) for c in _e.custom_field_options.all()]
        set_progress(f"DEBUG env {_e.id} {_e.name!r} orch={_e.container_orchestrator_id} cfvs={_vals} get_cfv={_e.get_cfv('kubernetes_namespace')!r}")
    try:
        env, created = ensure_environment_for_namespace(
            orchestrator, cluster_name, namespace,
            description=f"AKS namespace '{namespace}' on {cluster_name}, adopted by discovery.",
            region=region,
        )
    except Exception as exc:
        logger.exception("Environment creation failed")
        set_progress(f"Warning: no CloudBolt environment for '{namespace}': {exc}")
        return None
    if created:
        set_progress(f"Created environment '{env.name}' for '{namespace}'.")
        if tier is not None:
            set_environment_quota(env, tier)
        register_namespace_record(orchestrator, namespace)
    if group is not None and (created or not group.name.startswith("Unassigned")) and entitle_group(env, group):
        set_progress(f"Entitled CloudBolt group '{group.name}' to environment '{env.name}'.")
    return env


def discover_resources(**kwargs):
    """Discover the managed namespaces on every AKS cluster CloudBolt manages."""
    ensure_custom_fields()
    known = _known_resources(kwargs.get("blueprint") or namespace_blueprint())
    unassigned = Group.objects.filter(name__startswith="Unassigned").order_by("name").first()
    discovered = []

    for cluster_resource, orchestrator_id in cluster_resources():
        try:
            cluster = cluster_for_resource(cluster_resource)
            set_progress(f"Discovering managed namespaces on '{cluster.name}'...")
            items = list_managed_namespaces(cluster)
        except AKSError as exc:
            set_progress(f"Skipping cluster {cluster_resource.name}: {exc}")
            logger.warning("Skipping cluster %s: %s", cluster_resource, exc)
            continue

        orchestrator = load_orchestrator(orchestrator_id)
        if orchestrator is None:
            set_progress(f"Cluster {cluster_resource.name}: orchestrator {orchestrator_id} not found, so no "
                         "environments are pinned.")
        client = _kubernetes_client(orchestrator, cluster.name)

        for item in items:
            name, mns_id = item.get("name"), item.get("id")
            if not name or not mns_id:
                continue
            reason = adoptable_namespace(item)
            if reason:
                set_progress(f"Skipping '{name}' on '{cluster.name}': {reason}.")
                continue
            props = item.get("properties") or {}
            labels = props.get("labels") or {}
            annotations = props.get("annotations") or {}
            quota = props.get("defaultResourceQuota") or {}

            stored_id, existing = known.get(mns_id.lower(), (mns_id, None))
            annotated_group = owning_group_for_namespace(annotations)
            owner_group = annotated_group or (existing.group if existing is not None else unassigned)
            tier_key = _tier_key(labels.get(LABEL_TIER), quota)
            tier = TIERS.get(tier_key)

            record = {
                "name": name,  # REQUIRED
                RESOURCE_IDENTIFIER: stored_id,
                "parent_resource_id": cluster_resource.id,
                # An existing resource without the annotation keeps its group.
                "group": annotated_group or (None if existing is not None else unassigned),
                CF_PREFIX + "name": name,
                CF_PREFIX + "uid": _kubernetes_uid(client, name),
                CF_PREFIX + "cluster": cluster.name,
                CF_PREFIX + "cluster_resource_id": cluster_resource.id,
                CF_PREFIX + "orchestrator_id": orchestrator.id if orchestrator is not None else None,
                CF_PREFIX + "network_policy": describe_network_policy(props.get("defaultNetworkPolicy")),
                CF_PREFIX + "size_tier": tier_key,
                CF_PREFIX + "cpu_limit": tier["cpu"] if tier else _quantity_to_int(quota.get("cpuLimit"), "cpu"),
                CF_PREFIX + "memory_limit_gb": (
                    tier["memory_gb"] if tier else _quantity_to_int(quota.get("memoryLimit"), "gib")
                ),
                CF_PREFIX + "storage_limit_gb": tier["storage_gb"] if tier else None,
                CF_PREFIX + "max_pods": tier["pods"] if tier else None,
                CF_PREFIX + "entra_access": _access_text(cluster, name),
                "expiration_date": annotations.get(ANNOTATION_EXPIRES) or None,
            }

            region = item.get("location") or (cluster_region(cluster_resource, orchestrator) if orchestrator else "")
            env = _pin_environment(orchestrator, cluster.name, name, region, owner_group, tier)
            if env is not None:
                record[CF_PREFIX + "environment_id"] = env.id
                record[CF_PREFIX + "environment_name"] = env.name
                record[CF_PREFIX + "entitled_groups"] = "\n".join(entitled_group_names(env))

            # Drop unknowns so discovery does not overwrite good values with None.
            discovered.append({k: v for k, v in record.items() if v is not None})

    set_progress(f"Discovered {len(discovered)} AKS managed namespace(s).")
    return discovered
