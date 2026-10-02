"""
CloudBolt "Generated Parameter Options" plugin: Ansible Automation
Configurations.

Attached (through orchestration_actions/HPA-aualk6ei) to the blueprint
parameter aap_configuration_names that the "Apply Ansible Automation
Configurations to Servers" build step (plugins/OHK-cwqouaqn) reads, so an
admin pins configuration NAMES from a dropdown instead of typing them. On the
blueprint's Parameters tab, "Add option" opens CloudBolt's
AddParameterValueForm, which runs get_options_list with the blueprint and no
group (common/forms.py -> form_field_for_cf(constrained=False,
blueprint=...)); that is the only context in which this plugin lists values:
one option per distinct configuration name defined on any Ansible Automation
Platform configuration manager, labeled with the managers that define it
("Install Postgres (AAP East, AAP West)"). A name is not unique across
managers (connectors.ansible_automation_platform AAPApplication declares no
uniqueness constraint) and the build step resolves each server's own manager
from its environment, so one pinned name serves deployments wherever they
land.

At order time (CloudBolt passes the ordering group) the plugin returns None
on purpose: the pinned options are used as-is (CloudBolt intersects generated
options with the pinned ones, so a listing could never widen the choice) and
no AAP query runs on every order. With no configuration defined anywhere it
also returns None, and the dialog falls back to a text field.

Signature (CloudBolt's contract for this hook point):
  get_options_list(field, **kwargs) -> list | dict | None
  kwargs seen here: blueprint, group, profile, environment, ... Return
  {"options": [(value, label), ...]} or None.
"""

from connectors.ansible_automation_platform.models import AAPApplication
from utilities.logger import ThreadLogger

logger = ThreadLogger(__name__)

NAMES_FIELD = "aap_configuration_names"


def _configuration_options():
    """(name, "name (manager, manager)") for every distinct configuration
    name on the instance's AAP managers."""
    by_name = {}
    rows = AAPApplication.objects.values_list("name", "aap__name").order_by("name", "aap__name")
    for name, manager_name in rows:
        defined_on = by_name.setdefault(name, [])
        if manager_name not in defined_on:
            defined_on.append(manager_name)
    return [
        (name, "{} ({})".format(name, ", ".join(defined_on)))
        for name, defined_on in by_name.items()
    ]


def get_options_list(field, blueprint=None, group=None, **kwargs):
    """Configuration names for the admin's Add-option dialog; None everywhere
    else (see the module docstring)."""
    name = getattr(field, "name", "") or ""
    if name != NAMES_FIELD or group is not None:
        return None
    try:
        options = _configuration_options()
    except Exception as exc:  # noqa: BLE001 -- a listing failure must never block the dialog
        logger.warning(
            "Could not list Ansible Automation Configurations for %s on blueprint %s: %s",
            name, getattr(blueprint, "global_id", "?"), exc,
        )
        return None
    if not options:
        return None
    return {"options": options}
