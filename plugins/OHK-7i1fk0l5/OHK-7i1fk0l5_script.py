"""
CloudBolt "Generated Parameter Options" plugin: Ansible job template maps.

Attached (through orchestration_actions/HPA-aualk6ei) to the blueprint
parameter ansible_job_template_maps that the "Run Ansible Job Templates on
Servers" build step (plugins/OHK-cwqouaqn) reads, so an admin pins maps from a
dropdown instead of typing names. On the blueprint's Parameters tab,
"Add option" opens CloudBolt's AddParameterValueForm, which runs
get_options_list with the blueprint and no group (common/forms.py ->
form_field_for_cf(constrained=False, blueprint=...)); that is the only context
in which this plugin lists values: one option per shared Variable Map (Admin >
Variable Maps) whose JSON has a ``job_template`` key, valued with the map's
global ID (MAP-...) and labeled with the AAP job template it launches and the
map's name ("Linux VM Build (Ansible: Linux VM Build, MAP-abcd1234)").
Variable Maps without that key are Terraform variable maps and are left out.

At order time (CloudBolt passes the ordering group) the plugin returns None
on purpose: the pinned options are used as-is (CloudBolt intersects generated
options with the pinned ones, so a listing could never widen the choice) and
no query runs on every order. With no job template map on the instance it
also returns None, and the dialog falls back to a text field, where a map's
name or global ID can be typed.

Signature (CloudBolt's contract for this hook point):
  get_options_list(field, **kwargs) -> list | dict | None
  kwargs seen here: blueprint, group, profile, environment, ... Return
  {"options": [(value, label), ...]} or None.
"""

from generic_jobs.models import VariableMap
from utilities.logger import ThreadLogger

logger = ThreadLogger(__name__)

MAPS_FIELD = "ansible_job_template_maps"


def _map_options():
    """(global_id, label) for every shared Variable Map that is a job template
    map, by name."""
    options = []
    for variable_map in VariableMap.objects.order_by("name", "id"):
        spec = variable_map.map if isinstance(variable_map.map, dict) else {}
        template = str(spec.get("job_template") or "").strip()
        if not template:
            continue
        detail = "{}, {}".format(variable_map.name, variable_map.global_id)
        options.append((variable_map.global_id, "{} ({})".format(template, detail)))
    return options


def get_options_list(field, blueprint=None, group=None, **kwargs):
    """Job template maps for the admin's Add-option dialog; None everywhere
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
