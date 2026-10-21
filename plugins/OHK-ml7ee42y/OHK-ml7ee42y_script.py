"""
CloudBolt discovery plugin: GKE Namespaces.

Inventories landing-zone namespaces on every GKE cluster CloudBolt manages
(the Kubernetes Cluster resources of the Google Kubernetes Engine blueprint
that have a container orchestrator) and returns one dict per namespace for
CloudBolt to create/update Resources under the "GKE Namespace" blueprint.

A landing zone is any namespace carrying the ``cloudbolt.io/landing-zone=true``
label, which the build stamps on creation. Plain namespaces are not
imported; order the blueprint (or label them) to bring them under management.

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
  - its network policy, Kubernetes UID, and who holds a RoleBinding in it.

RBAC: discovery enumerates every GKE cluster; CloudBolt filters the returned
resources by each user's access.

Discovery contract (servicecatalog/models.py, ServiceBlueprint.sync_resources;
docs/agents/plugin-templates.md section 2):
  - The plugin only returns dicts; the sync creates, updates or reactivates
    the resources and creates any parameter that does not exist yet.
  - RESOURCE_IDENTIFIER names the field carrying the cluster-native unique ID
    (the namespace UID).
  - A namespace on a cluster that was not listed (no orchestrator, or its API
    unreachable) is returned with its UID alone, so the sync leaves it as it
    is instead of marking it Historical.
  - "parent_resource_id" nests the namespace under its cluster resource and
    "group" hands it to its owning CloudBolt Group; both are Resource
    attributes CloudBolt lets a discovery dict set.
  - All other keys are the same gke_namespace_* fields the build and teardown
    plugins use.

Entry point: discover_resources(**kwargs) -> list[dict]
"""

import datetime
import re

from accounts.models import Group
from common.methods import set_progress
from shared_modules.gke_namespace import (
    ANNOTATION_EXPIRES,
    ANNOTATION_NETWORK_POLICY,
    CF_PREFIX,
    LABEL_TIER,
    NETWORK_POLICY_CHOICES,
    NamespaceClient,
    NamespaceError,
    TIERS,
    cluster_region,
    cluster_resources,
    describe_network_policy,
    ensure_environment_for_namespace,
    entitle_group,
    entitled_group_names,
    load_orchestrator,
    namespace_access,
    namespace_blueprint,
    owning_group_for_namespace,
    parse_network_policy,
    register_namespace_record,
    render_access,
    set_environment_quota,
    tier_for_quota,
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


def _tier_key(labelled, hard):
    """The tier label when valid; else the tier whose CPU and memory match the quota; else 'custom'."""
    labelled = str(labelled or "").strip().lower()
    if labelled in TIERS:
        return labelled
    return tier_for_quota(hard) or "custom"


def _network_policy(annotations, policy_names):
    """The ingress/egress description from the namespace annotation, else from CloudBolt's NetworkPolicies."""
    ingress, egress = parse_network_policy(annotations.get(ANNOTATION_NETWORK_POLICY, ""))
    if ingress in NETWORK_POLICY_CHOICES and egress in NETWORK_POLICY_CHOICES:
        return describe_network_policy(ingress, egress)
    names = set(policy_names)
    if "cloudbolt-deny-ingress" not in names:
        ingress = "AllowAll"
    elif "cloudbolt-allow-ingress-same-namespace" in names:
        ingress = "AllowSameNamespace"
    else:
        ingress = "DenyAll"
    if "cloudbolt-deny-egress" not in names:
        egress = "AllowAll"
    elif "cloudbolt-allow-egress-same-namespace" in names:
        egress = "AllowSameNamespace"
    else:
        egress = "DenyAll"
    return describe_network_policy(ingress, egress)


def _expiration(annotations):
    """The lease annotation as the datetime set_resource_expiration stores, or None."""
    expires = to_date(annotations.get(ANNOTATION_EXPIRES))
    if expires is None:
        return None
    return datetime.datetime.combine(expires, datetime.time(hour=23, minute=59))


def _known_resources(blueprint):
    """Namespace UID -> resource for the blueprint's resources, preferring active ones."""
    known = {}
    if blueprint is None:
        return known
    for resource in blueprint.resource_set.all():
        uid = resource.get_value_for_custom_field(RESOURCE_IDENTIFIER)
        if uid and (uid not in known or resource.lifecycle == "ACTIVE"):
            known[str(uid)] = resource
    return known


def _access_text(client, namespace):
    try:
        return render_access(namespace_access(client, namespace))
    except Exception as exc:
        logger.warning("Could not list access to %s: %s", namespace, exc)
        return None


def _pin_environment(orchestrator, cluster_name, namespace, region, group, tier):
    """The Environment pinned to the namespace, created when missing and entitled to ``group``."""
    try:
        env, created = ensure_environment_for_namespace(
            orchestrator, cluster_name, namespace,
            description=f"GKE namespace '{namespace}' on {cluster_name}, adopted by discovery.",
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


def _untouched(blueprint, listed_cluster_ids, returned_uids):
    """The UID alone for each active namespace on a cluster that was not listed.

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
    """Discover landing-zone namespaces on every GKE cluster CloudBolt manages."""
    blueprint = kwargs.get("blueprint") or namespace_blueprint()
    known = _known_resources(blueprint)
    unassigned = Group.objects.filter(name__startswith="Unassigned").order_by("name").first()
    discovered = []
    listed_cluster_ids = set()
    returned_uids = set()

    for cluster_resource, orchestrator_id in cluster_resources():
        cluster_name = cluster_resource.get_cf_values_as_dict().get("gke_cluster_name") or cluster_resource.name
        orchestrator = load_orchestrator(orchestrator_id)
        if orchestrator is None:
            set_progress(f"Skipping cluster {cluster_name}: orchestrator {orchestrator_id} not found.")
            continue
        try:
            client = NamespaceClient.from_orchestrator(orchestrator)
            set_progress(f"Discovering namespaces on '{cluster_name}'...")
            namespaces = client.list_landing_zone_namespaces()
        except NamespaceError as exc:
            set_progress(f"Skipping cluster {cluster_name}; its namespaces are left as they are: {exc}")
            logger.warning("Skipping cluster %s: %s", cluster_resource, exc)
            continue

        listed_cluster_ids.add(str(cluster_resource.id))
        region = cluster_region(cluster_resource, orchestrator)

        for ns in namespaces:
            meta = ns.get("metadata") or {}
            name, uid = meta.get("name"), meta.get("uid")
            if not name or not uid:
                continue
            if (ns.get("status") or {}).get("phase") == "Terminating":
                set_progress(f"Skipping '{name}' on '{cluster_name}': it is being deleted.")
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
                policy_names = [(p.get("metadata") or {}).get("name", "")
                                for p in client.list_managed_network_policies(name)]
            except NamespaceError as exc:
                logger.warning("Could not read network policies for %s: %s", name, exc)
                policy_names = []

            existing = known.get(str(uid))
            annotated_group = owning_group_for_namespace(annotations)
            owner_group = annotated_group or (existing.group if existing is not None else unassigned)
            tier_key = _tier_key(labels.get(LABEL_TIER), hard)
            tier = TIERS.get(tier_key)

            record = {
                "name": name,  # REQUIRED
                RESOURCE_IDENTIFIER: uid,
                "parent_resource_id": cluster_resource.id,
                # An existing resource without the annotation keeps its group.
                "group": annotated_group or (None if existing is not None else unassigned),
                CF_PREFIX + "name": name,
                CF_PREFIX + "cluster": cluster_name,
                CF_PREFIX + "cluster_resource_id": cluster_resource.id,
                CF_PREFIX + "orchestrator_id": orchestrator.id,
                CF_PREFIX + "size_tier": tier_key,
                CF_PREFIX + "cpu_limit": tier["cpu"] if tier else _quantity_to_int(hard.get("limits.cpu"), "cpu"),
                CF_PREFIX + "memory_limit_gb": (
                    tier["memory_gb"] if tier else _quantity_to_int(hard.get("limits.memory"), "gib")
                ),
                CF_PREFIX + "storage_limit_gb": (
                    tier["storage_gb"] if tier else _quantity_to_int(hard.get("requests.storage"), "gib")
                ),
                CF_PREFIX + "max_pods": tier["pods"] if tier else _quantity_to_int(hard.get("pods")),
                CF_PREFIX + "network_policy": _network_policy(annotations, policy_names),
                CF_PREFIX + "google_access": _access_text(client, name),
                "expiration_date": _expiration(annotations),
            }

            env = _pin_environment(orchestrator, cluster_name, name, region, owner_group, tier)
            if env is not None:
                record[CF_PREFIX + "environment_id"] = env.id
                record[CF_PREFIX + "environment_name"] = env.name
                record[CF_PREFIX + "entitled_groups"] = "\n".join(entitled_group_names(env))

            # Drop unknowns so discovery does not overwrite good values with None.
            discovered.append({k: v for k, v in record.items() if v is not None})
            returned_uids.add(uid)

    set_progress(f"Discovered {len(discovered)} GKE namespace landing zone(s).")
    discovered.extend(_untouched(blueprint, listed_cluster_ids, returned_uids))
    return discovered
