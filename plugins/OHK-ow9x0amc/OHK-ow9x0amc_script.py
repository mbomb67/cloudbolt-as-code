"""
CloudBolt discovery plugin: AWS EKS Namespaces.

Inventories landing-zone namespaces across every Kubernetes container
orchestrator that backs a Kubernetes Cluster resource (the orchestrators the
AWS EKS blueprint registers) and returns one dict per namespace for
CloudBolt to create/update Resources under the "AWS EKS Namespace" blueprint.

A landing zone is any namespace carrying the ``cloudbolt.io/landing-zone=true``
label, which the build plugin stamps on creation. Plain namespaces are not
imported; use the build plugin (or label them) to bring them under management.

RBAC: discovery enumerates every orchestrator; CloudBolt filters the returned
resources by each user's access.

Discovery contract (servicecatalog/models.py, ServiceBlueprint.sync_resources):
  - The plugin only returns dicts; the sync creates, updates or reactivates
    the resources and creates any parameter that does not exist yet.
  - RESOURCE_IDENTIFIER names the field carrying the cluster-native unique ID
    (the namespace UID).
  - A namespace on a cluster that was not listed is returned with its UID
    alone, so the sync leaves it as it is instead of marking it Historical.
  - "parent_resource_id" nests the namespace under its cluster resource and
    "group" hands it to the CloudBolt Group recorded in the namespace's
    cloudbolt.io/group-id annotation; both are Resource attributes CloudBolt
    lets a discovery dict set.
  - All other keys are the same eks_namespace_* fields the build and teardown
    plugins use.

Entry point: discover_resources(**kwargs) -> list[dict]
"""

import datetime
import re

from common.methods import set_progress
from shared_modules.eks_namespace import (
    ANNOTATION_EXPIRES,
    CF_PREFIX,
    LABEL_TIER,
    NamespaceClient,
    NamespaceError,
    TIERS,
    cluster_resources,
    entitled_group_names,
    find_environment_for_namespace,
    load_orchestrator,
    owning_group_for_namespace,
    to_date,
)
from utilities.logger import ThreadLogger

logger = ThreadLogger(__name__)

# The namespace UID is the cluster-unique key for a landing zone.
RESOURCE_IDENTIFIER = CF_PREFIX + "uid"

_QUANTITY_RE = re.compile(r"^(\d+(?:\.\d+)?)([A-Za-z]*)$")
_GI_FACTORS = {"": 1 / (1024 ** 3), "Ki": 1 / (1024 ** 2), "Mi": 1 / 1024, "Gi": 1, "Ti": 1024,
               "k": 1e3 / (1024 ** 3), "M": 1e6 / (1024 ** 3), "G": 1e9 / (1024 ** 3), "T": 1e12 / (1024 ** 3)}


def _quantity_to_int(value, unit="count"):
    """Parse a Kubernetes quantity into an int of cores, GiB, or a plain count.

    https://kubernetes.io/docs/reference/kubernetes-api/common-definitions/quantity/
    Quantities are a decimal number with an optional SI (k, M, G) or binary
    (Ki, Mi, Gi) suffix; CPU may use the 'm' (milli) suffix.
    """
    if value is None:
        return None
    match = _QUANTITY_RE.match(str(value).strip())
    if not match:
        return None
    number, suffix = float(match.group(1)), match.group(2)
    if unit == "cpu":
        return int(round(number / 1000)) if suffix == "m" else int(round(number))
    if unit == "gib":
        factor = _GI_FACTORS.get(suffix)
        return int(round(number * factor)) if factor is not None else None
    return int(round(number))


def _tier_from_quota(hard, labelled_tier):
    """Prefer the tier label; fall back to matching the quota to a known tier."""
    if labelled_tier in TIERS:
        return labelled_tier
    cpu = _quantity_to_int(hard.get("limits.cpu"), "cpu")
    for key, tier in TIERS.items():
        if tier["cpu"] == cpu:
            return key
    return labelled_tier or "custom"


def _expiration(annotations):
    """The lease annotation as the datetime set_resource_expiration stores, or None."""
    expires = to_date(annotations.get(ANNOTATION_EXPIRES))
    if expires is None:
        return None
    return datetime.datetime.combine(expires, datetime.time(hour=23, minute=59))


def _untouched(blueprint, listed_cluster_ids, returned_uids):
    """
    The UID alone for each active namespace on a cluster that was not listed:
    no orchestrator, unreachable, or the cluster resource itself not Active.
    The sync matches them and changes nothing, so they are not marked
    Historical when the blueprint's auto_historical_resources is on.
    """
    if blueprint is None:
        return []
    kept = []
    for resource in blueprint.resource_set.filter(lifecycle="ACTIVE"):
        values = resource.get_cf_values_as_dict()
        uid = values.get(RESOURCE_IDENTIFIER)
        cluster_id = resource.parent_resource_id or values.get(CF_PREFIX + "cluster_resource_id")
        if uid and uid not in returned_uids and str(cluster_id) not in listed_cluster_ids:
            kept.append({RESOURCE_IDENTIFIER: uid})
    return kept


def discover_resources(**kwargs):
    """Discover landing-zone namespaces across every EKS cluster's orchestrator."""
    discovered = []
    listed_cluster_ids = set()
    returned_uids = set()

    for cluster, orchestrator_id in cluster_resources():
        orchestrator = load_orchestrator(orchestrator_id)
        if orchestrator is None:
            set_progress(f"Skipping cluster {cluster.name}: orchestrator {orchestrator_id} not found")
            continue
        try:
            client = NamespaceClient.from_orchestrator(orchestrator)
        except Exception as exc:
            set_progress(f"Skipping cluster {cluster.name}: {exc}")
            logger.warning("Skipping orchestrator %s due to client error: %s", orchestrator, exc)
            continue

        set_progress(f"Discovering namespaces on '{cluster.name}'...")
        try:
            namespaces = client.list_landing_zone_namespaces()
        except NamespaceError as exc:
            set_progress(f"Error listing namespaces on {cluster.name}: {exc}")
            logger.warning("Error listing namespaces for orchestrator %s: %s", orchestrator, exc)
            continue

        listed_cluster_ids.add(str(cluster.id))

        for ns in namespaces:
            meta = ns.get("metadata") or {}
            name, uid = meta.get("name"), meta.get("uid")
            if not name or not uid:
                continue
            labels = meta.get("labels") or {}
            annotations = meta.get("annotations") or {}

            # Hydrate quota and policies per namespace (thin list objects).
            hard = {}
            try:
                quota = client.get_resource_quota(name)
                hard = ((quota or {}).get("spec") or {}).get("hard") or {}
            except NamespaceError as exc:
                logger.warning("Could not read quota for %s: %s", name, exc)
            try:
                policies = client.list_managed_network_policies(name)
            except NamespaceError as exc:
                logger.warning("Could not read network policies for %s: %s", name, exc)
                policies = []

            env = find_environment_for_namespace(orchestrator, name)
            tier_key = _tier_from_quota(hard, labels.get(LABEL_TIER, ""))

            record = {
                "name": name,
                RESOURCE_IDENTIFIER: uid,
                "parent_resource_id": cluster.id,
                "group": owning_group_for_namespace(annotations),
                CF_PREFIX + "name": name,
                CF_PREFIX + "cluster": cluster.name,
                CF_PREFIX + "cluster_resource_id": cluster.id,
                CF_PREFIX + "orchestrator_id": orchestrator.id,
                CF_PREFIX + "size_tier": tier_key,
                CF_PREFIX + "cpu_limit": _quantity_to_int(hard.get("limits.cpu"), "cpu"),
                CF_PREFIX + "memory_limit_gb": _quantity_to_int(hard.get("limits.memory"), "gib"),
                CF_PREFIX + "storage_limit_gb": _quantity_to_int(hard.get("requests.storage"), "gib"),
                CF_PREFIX + "max_pods": _quantity_to_int(hard.get("pods")),
                CF_PREFIX + "network_isolation": "deny-by-default" in policies,
                "expiration_date": _expiration(annotations),
            }
            if env is not None:
                record[CF_PREFIX + "environment_id"] = env.id
                record[CF_PREFIX + "environment_name"] = env.name
                record[CF_PREFIX + "entitled_groups"] = "\n".join(entitled_group_names(env))

            # Drop unknowns so discovery does not overwrite good values with None.
            discovered.append({k: v for k, v in record.items() if v is not None})
            returned_uids.add(uid)

    set_progress(f"Discovered {len(discovered)} EKS namespace landing zone(s).")
    discovered.extend(_untouched(kwargs.get("blueprint"), listed_cluster_ids, returned_uids))
    return discovered
