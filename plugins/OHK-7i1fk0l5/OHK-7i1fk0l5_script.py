"""
CloudBolt "Generated Parameter Options" plugin: Ansible job template maps.

Attached (through orchestration_actions/HPA-aualk6ei) to the blueprint
parameter ansible_job_template_maps that the "Run Ansible Job Templates on
Servers" build step (plugins/OHK-cwqouaqn) reads, so an admin pins maps from a
dropdown instead of typing module names. On the blueprint's Parameters tab,
"Add option" opens CloudBolt's AddParameterValueForm, which runs
get_options_list with the blueprint and no group (common/forms.py ->
form_field_for_cf(constrained=False, blueprint=...)); that is the only context
in which this plugin lists values: one option per shared module whose
module_name starts with ``ansible_job_template_``, valued with the module
name and labeled with the AAP job template the map launches ("Linux VM Build
(ansible_job_template_linux_vm_build)"). A map that cannot be imported is
still listed, labeled with the import error, so the problem is visible where
the admin is looking.

At order time (CloudBolt passes the ordering group) the plugin returns None
on purpose: the pinned options are used as-is (CloudBolt intersects generated
options with the pinned ones, so a listing could never widen the choice) and
no module is imported on every order. With no map in the repo it also returns
None, and the dialog falls back to a text field.

Signature (CloudBolt's contract for this hook point):
  get_options_list(field, **kwargs) -> list | dict | None
  kwargs seen here: blueprint, group, profile, environment, ... Return
  {"options": [(value, label), ...]} or None.
"""

import importlib

from cbhooks.models import SharedModule
from utilities.logger import ThreadLogger

logger = ThreadLogger(__name__)

MAPS_FIELD = "ansible_job_template_maps"
MAP_MODULE_PREFIX = "ansible_job_template_"


def _label_for(module_name):
    """``"<job template> (<module name>[, manager <name>])"``, or the module
    name with the reason it could not be read."""
    try:
        module = importlib.reload(importlib.import_module("shared_modules." + module_name))
        spec = getattr(module, "JOB_TEMPLATE", None) or {}
        template = str(spec.get("job_template") or "").strip()
        manager = str(spec.get("manager") or "").strip()
    except Exception as exc:  # noqa: BLE001 -- show the broken map, do not hide it
        return "{} (cannot load: {})".format(module_name, exc)
    if not template:
        return "{} (JOB_TEMPLATE names no job_template)".format(module_name)
    suffix = ", manager {}".format(manager) if manager else ""
    return "{} ({}{})".format(template, module_name, suffix)


def _map_options():
    """(module_name, label) for every job template map synced to the
    instance."""
    module_names = (
        SharedModule.objects.filter(module_name__startswith=MAP_MODULE_PREFIX)
        .order_by("module_name")
        .values_list("module_name", flat=True)
    )
    return [(name, _label_for(name)) for name in module_names]


def get_options_list(field, blueprint=None, group=None, **kwargs):
    """Map module names for the admin's Add-option dialog; None everywhere
    else (see the module docstring)."""
    name = getattr(field, "name", "") or ""
    if name != MAPS_FIELD or group is not None:
        return None
    try:
        options = _map_options()
    except Exception as exc:  # noqa: BLE001 -- a listing failure must never block the dialog
        logger.warning(
            "Could not list Ansible job template maps for %s on blueprint %s: %s",
            name, getattr(blueprint, "global_id", "?"), exc,
        )
        return None
    if not options:
        return None
    return {"options": options}
