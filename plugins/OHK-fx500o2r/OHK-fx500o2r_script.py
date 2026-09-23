"""
CloudBolt inbound webhook plugin: Form Options (webhooks/IWH-yj93is5z).

One generic GET endpoint that SurveyJS custom order forms call through
choicesByUrl to fill dropdowns, so a template-agnostic build plugin (whose
per-template variables live in a Dynamic Panel, not in declared action inputs)
can still offer environment-derived choices. Reusable by any blueprint: the
form picks a source and passes the selected environment.

URL (trailing slash required):
  GET /api/v3/cmp/inboundWebHooks/form-options/run/?source=<source>&...

Query parameters (all strings; 'filter' and 'last' are reserved by the API):
  source        environment | resource_group | subnet | os_image | vm_size |
                location | cf:<custom_field_name> | tfc_variable_options
  group         the form's group value -- the relative href the standard
                group dropdown submits ('/api/v3/cmp/groups/GRP-xxxxxxxx/'),
                or a group global ID / name. Required for every env source.
  env_id        the selected Environment id. Required for every env source
                except 'environment'.
  cf_name       with source=cf: the custom field whose env options to list.
  service_item  with source=tfc_variable_options: the build deployment item's
                global ID (BDI-...). Its pinned parameter_defaults supply the
                'tf-cloud' ConnectionInfo and the nocode-* module ID, so the
                form never duplicates them.
  variable      with source=tfc_variable_options: the Terraform variable
                whose admin-defined options to return.

Response: {"options": [{"value": ..., "title": ...}, ...]} -- the form uses
  choicesByUrl {"path": "options", "valueName": "value", "titleName": "title"}.
  A dependent source called before env_id has a value (SurveyJS fires every
  choicesByUrl on load) returns 200 with an empty options list, not an error.
  Bad parameters -> 400, anonymous or non-member caller -> 403, both with
  {"options": [], "error": "..."} as the body so a failure is visible in the
  browser's network tab without breaking the form.

Authentication/authorization: the IWH is 'normal' mode, so the form's
same-origin XHR authenticates with the user's session and the runtime hands
this plugin the caller's UserProfile as ``profile``. The IWH endpoint performs
NO RBAC of its own (any authenticated user may call it), so this plugin does:
the caller must be a member of the group it names (or a cb_admin), and every
env source re-checks that the group is entitled to the environment through
group.get_available_environments() (AGENTS.md cardinal rule 4).

Runtime contract (CloudBolt dispatches inbound_web_hook_<method>): the plugin
receives ``parameters`` (request.GET for GET), ``profile``, ``files``, ``job``
(None) and ``logger``; the return value is JSON-rendered; a non-200 status is
requested by returning {"iwh_status_code": N, "iwh_embedded_response": body}.
No job or action-history row is written per call.
"""

from utilities.logger import ThreadLogger

from shared_modules.env_options import (
    EnvOptionsError,
    entitled_environment,
    environment_options,
    options_for,
    profile_may_act_for_group,
    resolve_group,
    service_item_defaults,
)
from shared_modules.tfc_api import TFCError, get_options_client

logger = ThreadLogger(__name__)

ENV_SOURCES = ("resource_group", "subnet", "os_image", "vm_size", "location", "cf")
TFC_VARIABLE_OPTIONS_SOURCE = "tfc_variable_options"


def _respond(options):
    return {"options": options}


def _fail(status, message):
    return {
        "iwh_status_code": status,
        "iwh_embedded_response": {"options": [], "error": message},
    }


def _param(parameters, name):
    value = parameters.get(name) if parameters is not None else None
    return (str(value) if value is not None else "").strip()


def _tfc_variable_options(parameters):
    """Admin-defined allowed values for one variable of the no-code module a
    blueprint is pinned to. The connection and module ID are read from the
    deployment item's parameter_defaults, never from the query string."""
    service_item = _param(parameters, "service_item")
    variable = _param(parameters, "variable")
    if not service_item or not variable:
        return _fail(400, "source=tfc_variable_options requires service_item and variable.")
    defaults = service_item_defaults(service_item)
    connection_info = str(defaults.get("tfc_connection_info") or "").strip()
    module_id = str(defaults.get("tfc_nocode_module_id") or "").strip()
    if not connection_info or not module_id or "FILL-ME" in connection_info or "FILL-ME" in module_id:
        return _fail(
            400,
            "Deployment item {} has no pinned tfc_connection_info / "
            "tfc_nocode_module_id parameter_defaults.".format(service_item),
        )
    client = get_options_client(connection_info)
    all_options = client.get_no_code_variable_options(module_id)
    values = all_options.get(variable, [])
    return _respond([{"value": value, "title": str(value)} for value in values])


def inbound_web_hook_get(*args, parameters=None, profile=None, **kwargs):
    if profile is None:
        return _fail(403, "Authentication required.")
    source = _param(parameters, "source")
    if not source:
        return _fail(400, "source is required.")

    try:
        if source == TFC_VARIABLE_OPTIONS_SOURCE:
            return _tfc_variable_options(parameters)

        group = resolve_group(_param(parameters, "group"))
        if group is None:
            return _fail(400, "group is required and must name an existing group.")
        if not profile_may_act_for_group(profile, group):
            return _fail(403, "You are not a member of group '{}'.".format(group.name))

        if source == "environment":
            return _respond(environment_options(group, profile=profile))

        base_source = "cf" if source.startswith("cf:") else source
        if base_source not in ENV_SOURCES:
            return _fail(400, "Unknown source '{}'.".format(source))
        env_id = _param(parameters, "env_id")
        if not env_id:
            # SurveyJS fires every choicesByUrl on load, before the
            # Environment dropdown has a value: answer with no options (an
            # empty-value hint option renders as "[object Object]").
            return _respond([])
        env = entitled_environment(group, env_id, profile=profile)
        if env is None:
            return _fail(403, "Group '{}' is not entitled to that environment.".format(group.name))
        return _respond(options_for(source, env, cf_name=_param(parameters, "cf_name") or None))

    except (EnvOptionsError, TFCError) as exc:
        return _fail(400, str(exc))
    except Exception as exc:  # noqa: BLE001 -- never leak a traceback into a form
        logger.exception("form-options webhook failed for source '%s'", source)
        return _fail(500, "Option lookup failed: {}".format(exc))
