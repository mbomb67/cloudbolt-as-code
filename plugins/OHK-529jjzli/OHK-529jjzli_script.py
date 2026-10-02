"""
CloudBolt "Generated Parameter Options" plugin: HCP Terraform coordinates.

Attached (through orchestration_actions/HPA-swe1kifa) to the four blueprint
parameters that pin an HCP Terraform no-code blueprint to its module --
tfc_connection_info, tfc_organization, tfc_project, tfc_nocode_module_id --
so an admin pins them from dropdowns instead of typing IDs. On the
blueprint's Parameters tab, "Add option" opens CloudBolt's
AddParameterValueForm, which runs get_options_list with the blueprint and
no group (common/forms.py -> form_field_for_cf(constrained=False,
blueprint=...)); that is the only context in which this plugin lists live
values:

  tfc_connection_info  every ConnectionInfo labeled 'tf-cloud'
  tfc_organization     the organizations the pinned connection's token sees
  tfc_project          the projects of the pinned organization
  tfc_nocode_module_id the pinned organization's private registry modules
                       with no-code provisioning enabled, as
                       "<name> (<provider>) -- nocode-..."

Each lookup after the first needs the earlier pins (connection, then
organization), read from the blueprint itself with
env_options.blueprint_pinned_inputs -- so pin in that order. When a lookup
cannot run yet (nothing pinned, token blank, HCP unreachable) the plugin
returns None and CloudBolt falls back to its default behavior, which in the
dialog is a free-text field: the admin can always type a value.

At order time (CloudBolt passes the ordering group) the plugin returns None
on purpose. A blueprint parameter with exactly one option is "provided":
hidden from the order form and applied to the deployment's resource; a live
listing here would only add HCP round-trips and failure modes to every
order (CloudBolt intersects generated options with the pinned one, so a
listing could never widen the choice anyway). The build plugin validates
the pinned IDs against HCP Terraform when it runs.

Signature (CloudBolt's contract for this hook point):
  get_options_list(field, **kwargs) -> list | dict | None
  kwargs seen here: blueprint, group, profile, environment, ... Return
  {"options": [(value, label), ...], "initial_value": ...} or None.
"""

from utilities.logger import ThreadLogger
from utilities.models import ConnectionInfo

from shared_modules.env_options import blueprint_pinned_inputs
from shared_modules.tfc_api import CONNECTION_INFO_LABEL, get_options_client

logger = ThreadLogger(__name__)

CONNECTION_FIELD = "tfc_connection_info"
ORGANIZATION_FIELD = "tfc_organization"
PROJECT_FIELD = "tfc_project"
MODULE_FIELD = "tfc_nocode_module_id"
COORDINATE_FIELDS = (CONNECTION_FIELD, ORGANIZATION_FIELD, PROJECT_FIELD, MODULE_FIELD)


def _pinned(blueprint, name):
    """The blueprint's single pinned option for ``name``, or "" (a FILL-ME
    placeholder counts as unpinned)."""
    if blueprint is None:
        return ""
    value = str(blueprint_pinned_inputs(blueprint, (name,)).get(name) or "").strip()
    return "" if "FILL-ME" in value else value


def _connection_options():
    """(global_id, "name (global_id)") for every 'tf-cloud' ConnectionInfo."""
    connections = ConnectionInfo.objects.filter(labels__name=CONNECTION_INFO_LABEL).order_by("name")
    return [
        (connection.global_id, "{} ({})".format(connection.name, connection.global_id))
        for connection in connections
    ]


def _organization_options(blueprint):
    connection = _pinned(blueprint, CONNECTION_FIELD)
    if not connection:
        return None
    client = get_options_client(connection)
    return [(name, name) for name in client.list_organizations()]


def _project_options(blueprint):
    connection = _pinned(blueprint, CONNECTION_FIELD)
    organization = _pinned(blueprint, ORGANIZATION_FIELD)
    if not connection or not organization:
        return None
    client = get_options_client(connection, organization=organization)
    return [(name, name) for name in client.list_projects()]


def _module_options(blueprint):
    connection = _pinned(blueprint, CONNECTION_FIELD)
    organization = _pinned(blueprint, ORGANIZATION_FIELD)
    if not connection or not organization:
        return None
    client = get_options_client(connection, organization=organization)
    options = []
    for module in client.list_no_code_modules():
        label = "{} ({}) -- {}".format(
            module["name"] or module["registry_module_id"],
            module["provider"] or "?",
            module["nocode_module_id"],
        )
        options.append((module["nocode_module_id"], label))
    return sorted(options, key=lambda option: option[1].lower())


LOOKUPS = {
    CONNECTION_FIELD: lambda blueprint: _connection_options(),
    ORGANIZATION_FIELD: _organization_options,
    PROJECT_FIELD: _project_options,
    MODULE_FIELD: _module_options,
}


def get_options_list(field, blueprint=None, group=None, **kwargs):
    """Live HCP Terraform choices for the admin's Add-option dialog; None
    everywhere else (see the module docstring)."""
    name = getattr(field, "name", "") or ""
    lookup = LOOKUPS.get(name)
    if lookup is None or group is not None:
        return None
    try:
        options = lookup(blueprint)
    except Exception as exc:  # noqa: BLE001 -- a listing failure must never block the dialog
        logger.warning(
            "Could not list HCP Terraform options for %s on blueprint %s: %s",
            name, getattr(blueprint, "global_id", "?"), exc,
        )
        return None
    if not options:
        return None
    pinned = _pinned(blueprint, name)
    result = {"options": options}
    if pinned and any(option[0] == pinned for option in options):
        result["initial_value"] = pinned
    return result
