"""
CloudBolt "Generated Parameter Options" plugin: Azure execution environment.

Attached (through orchestration_actions/HPA-o8ri018x) to the
azps_execution_environment blueprint parameter of the "Run an Azure
PowerShell Script (Deployment Script)" blueprint (BP-qo1stxre), so an admin
pins the Environment whose subscription hosts the deployment script
containers from a dropdown instead of typing an ID. On the blueprint's
Parameters tab, "Add option" opens CloudBolt's AddParameterValueForm, which
runs get_options_list with the blueprint and no group (common/forms.py ->
form_field_for_cf(constrained=False, blueprint=...)); that is the only
context in which this plugin lists values:

  azps_execution_environment  every Environment backed by an Azure resource
                              handler, as its global ID (ENV-...) labelled
                              "<environment> / <handler> / <location>"

The option VALUE is the environment's global ID, which survives renames and
is what the runner (plugins/OHK-oblhxhw2) resolves; the runner also accepts
a numeric ID or a name for options typed by hand.

At order time (CloudBolt passes the ordering group) the plugin returns None
on purpose: a blueprint parameter with exactly one option is "provided",
hidden from the order form, and CloudBolt intersects generated options with
the pinned one anyway. The custom order form never shows blueprint
parameters at all.

Signature (CloudBolt's contract for this hook point):
  get_options_list(field, **kwargs) -> list | dict | None
  Return {"options": [(value, label), ...], "initial_value": ...} or None.
"""

from infrastructure.models import Environment
from utilities.logger import ThreadLogger

from shared_modules.env_options import blueprint_pinned_inputs

logger = ThreadLogger(__name__)

EXECUTION_ENVIRONMENT_FIELD = "azps_execution_environment"


def _environment_options():
    """(global_id, label) for every Azure-backed Environment in the system."""
    environments = (
        Environment.objects.filter(resource_handler__azurearmhandler__isnull=False)
        .select_related("resource_handler")
        .order_by("name")
    )
    options = []
    for env in environments:
        rh = env.resource_handler.cast()
        location = ""
        try:
            location = (rh.get_env_location(env) or "").strip()
        except Exception:  # noqa: BLE001 -- a label detail must never block the listing
            location = ""
        label = "{} / {}".format(env.name, rh.name)
        if location:
            label += " / " + location
        options.append((env.global_id, label))
    return options


def get_options_list(field, blueprint=None, group=None, **kwargs):
    """Azure-backed environments for the admin's Add-option dialog; None
    everywhere else (see the module docstring)."""
    name = getattr(field, "name", "") or ""
    if name != EXECUTION_ENVIRONMENT_FIELD or group is not None:
        return None
    try:
        options = _environment_options()
    except Exception as exc:  # noqa: BLE001 -- a listing failure must never block the dialog
        logger.warning(
            "Could not list Azure environments for %s on blueprint %s: %s",
            name, getattr(blueprint, "global_id", "?"), exc,
        )
        return None
    if not options:
        return None
    result = {"options": options}
    if blueprint is not None:
        pinned = str(blueprint_pinned_inputs(blueprint, (name,)).get(name) or "").strip()
        if pinned and any(option[0] == pinned for option in options):
            result["initial_value"] = pinned
    return result
