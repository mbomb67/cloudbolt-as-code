"""
CloudBolt discovery plugin: OpenShift Project Landing Zones.

Inventories landing-zone projects across ALL OpenShift Virtualization resource
handlers and returns one dict per project for CloudBolt to create/update
Resources under the "OpenShift Project Landing Zone" blueprint.

A landing zone is any namespace carrying the ``cloudbolt.io/landing-zone=true``
label, which the build plugin stamps on creation. Plain namespaces are not
imported; use the build plugin (or label them) to bring them under management.

RBAC: discovery enumerates every handler; CloudBolt filters the returned
resources by each user's Environment access.

Discovery contract:
  - Every dict MUST include a "name" key.
  - RESOURCE_IDENTIFIER names the field carrying the cluster-native unique ID
    (the namespace UID).
  - All other keys are the same openshift_project_* fields the build, teardown,
    and day-2 plugins use.

Entry point: discover_resources(**kwargs) -> list[dict]
"""

import re

from common.methods import set_progress
from shared_modules.openshift_landing_zone import (
    ANNOTATION_EXPIRES,
    ANNOTATION_OS_DISPLAY_NAME,
    CF_PREFIX,
    LABEL_TIER,
    LandingZoneError,
    OpenShiftLandingZoneClient,
    TIERS,
    entitled_group_names,
    find_environment_for_namespace,
    render_bindings,
)
from utilities.logger import ThreadLogger

logger = ThreadLogger(__name__)

# The namespace UID is the cluster-unique key for a project.
RESOURCE_IDENTIFIER = CF_PREFIX + "uid"

_QUANTITY_RE = re.compile(r"^(\d+(?:\.\d+)?)([A-Za-z]*)$")
_GI_FACTORS = {"": 1 / (1024 ** 3), "Ki": 1 / (1024 ** 2), "Mi": 1 / 1024, "Gi": 1, "Ti": 1024,
               "k": 1e3 / (1024 ** 3), "M": 1e6 / (1024 ** 3), "G": 1e9 / (1024 ** 3), "T": 1e12 / (1024 ** 3)}


def _quantity_to_int(value, unit="count"):
    """Parse a Kubernetes quantity into an int of cores, GiB, or a plain count.

    Docs: https://kubernetes.io/docs/reference/kubernetes-api/common-definitions/quantity/
          Quantities are a decimal number with an optional SI (k, M, G) or
          binary (Ki, Mi, Gi) suffix; CPU may use the 'm' (milli) suffix.
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


def discover_resources(**kwargs):
    """Discover landing-zone projects across all OpenShift Virtualization handlers."""
    discovered = []

    try:
        from resourcehandlers.ovirt.models import OVirtHandler
    except ImportError as exc:
        logger.warning("OpenShift Virtualization handler model unavailable: %s", exc)
        return discovered

    for rh in OVirtHandler.objects.all():
        try:
            client = OpenShiftLandingZoneClient.from_handler(rh)
        except Exception as exc:
            set_progress(f"Skipping OpenShift handler {rh.name}: {exc}")
            logger.warning("Skipping handler %s due to client error: %s", rh, exc)
            continue

        set_progress(f"Discovering landing zones on '{rh.name}'...")
        try:
            namespaces = client.list_landing_zone_namespaces()
        except LandingZoneError as exc:
            set_progress(f"Error listing projects on {rh.name}: {exc}")
            logger.warning("Error listing namespaces for handler %s: %s", rh, exc)
            continue

        console_base = client.get_console_url()

        for ns in namespaces:
            meta = ns.get("metadata") or {}
            name, uid = meta.get("name"), meta.get("uid")
            if not name or not uid:
                continue
            labels = meta.get("labels") or {}
            annotations = meta.get("annotations") or {}

            # Hydrate quota and bindings per namespace (thin list objects).
            hard = {}
            try:
                quota = client.get_resource_quota(name)
                hard = ((quota or {}).get("spec") or {}).get("hard") or {}
            except LandingZoneError as exc:
                logger.warning("Could not read quota for %s: %s", name, exc)
            try:
                bindings = client.list_managed_role_bindings(name)
            except LandingZoneError as exc:
                logger.warning("Could not read role bindings for %s: %s", name, exc)
                bindings = []

            env = find_environment_for_namespace(rh, name)
            tier_key = _tier_from_quota(hard, labels.get(LABEL_TIER, ""))

            record = {
                "name": name,  # REQUIRED
                RESOURCE_IDENTIFIER: uid,
                CF_PREFIX + "name": name,
                CF_PREFIX + "display_name": annotations.get(ANNOTATION_OS_DISPLAY_NAME, name),
                CF_PREFIX + "cluster": rh.name,
                CF_PREFIX + "rh_id": rh.id,
                CF_PREFIX + "size_tier": tier_key,
                CF_PREFIX + "cpu_limit": _quantity_to_int(hard.get("limits.cpu"), "cpu"),
                CF_PREFIX + "memory_limit_gb": _quantity_to_int(hard.get("limits.memory"), "gib"),
                CF_PREFIX + "storage_limit_gb": _quantity_to_int(hard.get("requests.storage"), "gib"),
                CF_PREFIX + "max_pods": _quantity_to_int(hard.get("pods")),
                CF_PREFIX + "max_vms": _quantity_to_int(hard.get("count/virtualmachines.kubevirt.io")),
                CF_PREFIX + "team_bindings": render_bindings(bindings),
                "expiration_date": annotations.get(ANNOTATION_EXPIRES) or None,
                CF_PREFIX + "console_url": f"{console_base}/k8s/cluster/projects/{name}" if console_base else "",
            }
            if env is not None:
                record[CF_PREFIX + "environment_id"] = env.id
                record[CF_PREFIX + "environment_name"] = env.name
                record[CF_PREFIX + "entitled_groups"] = "\n".join(entitled_group_names(env))

            # Drop unknowns so discovery does not overwrite good values with None.
            discovered.append({k: v for k, v in record.items() if v is not None})

    set_progress(f"Discovered {len(discovered)} OpenShift landing zone(s).")
    return discovered
