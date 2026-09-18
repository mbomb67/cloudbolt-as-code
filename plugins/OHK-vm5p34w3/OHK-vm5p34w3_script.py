"""
Remove Resource Group from Environments — teardown counterpart of the "Add
Resource Group to Environment" build step (OHK-r1imfgdx).

When the Bicep resource-group resource is deleted, the resource group's name is
removed from the `resource_group_arm` options of every Environment where it is
now stale:

  * every Environment on the SAME Azure resource handler (= subscription) that
    offers it. An Azure resource group is identified by (subscription, name):
    once deleted it is stale for every region-environment of that subscription,
    while a same-named resource group in ANOTHER subscription lives on another
    handler and is left untouched; plus
  * the Environments the build step recorded (bicep_rg_option_env_ids), in case
    one has since been re-homed to a different handler.

Only the environment -> value LINK is removed. The CustomFieldValue itself is
never deleted: it is shared per (field, value) across environments and may also
be the stored value of that parameter on servers or resources.

Idempotent and PROVFAILED-tolerant: missing metadata or an already-absent
option yields SUCCESS/WARNING, never FAILURE, so a half-built resource still
deletes cleanly. No action inputs.

Returns (status, output_msg, error_msg).
"""
import json

from common.methods import set_progress
from infrastructure.models import CustomField, Environment
from orders.models import CustomFieldValue
from utilities.logger import ThreadLogger

logger = ThreadLogger(__name__)

RG_PARAMETER_NAME = "resource_group_arm"
# Keep in sync with the build step OHK-r1imfgdx.
RG_NAME_SOURCES = [
    "bicep_var_rgName",
    "bicep_var_resourceGroupName",
    "bicep_var_name",
    "bicep_out_resourceGroupName",
    "bicep_out_rgName",
]
TRACKING_CF = "bicep_rg_option_env_ids"


def _cf(resource, name):
    try:
        return resource.get_value_for_custom_field(name)
    except Exception:
        return None


def resolve_rg_name(resource):
    for name in RG_NAME_SOURCES:
        val = _cf(resource, name)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return (getattr(resource, "name", "") or "").strip()


def _recorded_env_ids(resource):
    raw = _cf(resource, TRACKING_CF)
    try:
        ids = json.loads(raw) if raw else []
    except (TypeError, ValueError):
        return []
    return [i for i in ids if isinstance(i, int)] if isinstance(ids, list) else []


def run(job, **kwargs):
    resource = kwargs.get("resource")
    if resource is None:
        return "WARNING", "No resource in job context; nothing to clean up.", ""

    rg_name = resolve_rg_name(resource)
    if not rg_name:
        return ("WARNING",
                "No resource group name recorded on this resource; no environment "
                "options to remove.", "")

    cf = CustomField.objects.filter(name=RG_PARAMETER_NAME).first()
    if cf is None:
        return ("SUCCESS",
                f"Parameter '{RG_PARAMETER_NAME}' does not exist; nothing to remove.", "")

    cfvs = list(CustomFieldValue.objects.filter(field=cf, str_value=rg_name))
    if not cfvs:
        return ("SUCCESS",
                f"'{rg_name}' is not a Resource Group option anywhere; nothing to "
                f"remove.", "")

    # Scope: every environment on the same subscription (handler) offering the
    # value, plus whatever the build step recorded.
    envs = {}
    rh_id = _cf(resource, "bicep_rh_id")
    if rh_id:
        try:
            for env in Environment.objects.filter(
                    resource_handler_id=int(rh_id),
                    custom_field_options__in=cfvs).distinct():
                envs[env.id] = env
        except (TypeError, ValueError):
            logger.warning(f"Unusable bicep_rh_id {rh_id!r}; using recorded envs only.")
    recorded = _recorded_env_ids(resource)
    if recorded:
        for env in Environment.objects.filter(
                id__in=recorded, custom_field_options__in=cfvs).distinct():
            envs[env.id] = env

    if not envs:
        return ("SUCCESS",
                f"'{rg_name}' was not offered as a Resource Group option on any "
                f"environment of this subscription; nothing to remove.", "")

    removed_from = []
    for env in envs.values():
        set_progress(f"Removing resource group option '{rg_name}' from environment "
                     f"'{env.name}'...")
        for cfv in cfvs:
            env.custom_field_options.remove(cfv)  # link only; the value is shared
        removed_from.append(env.name)

    try:
        if recorded:
            resource.set_value_for_custom_field(TRACKING_CF, "[]")
            resource.save()
    except Exception:  # noqa: BLE001 — bookkeeping must not fail teardown
        logger.debug("Could not clear the tracking field; ignoring.")

    return ("SUCCESS",
            f"Removed resource group option '{rg_name}' from "
            f"{len(removed_from)} environment(s): {', '.join(sorted(removed_from))}.",
            "")
