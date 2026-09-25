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
                location | cf:<custom_field_name> | tfc_variable_options |
                day2_panel
  group         the form's group value -- the relative href the standard
                group dropdown submits ('/api/v3/cmp/groups/GRP-xxxxxxxx/'),
                or a group global ID / name. Required for every env source.
  env_id        the selected Environment id. Required for every env source
                except 'environment'.
  cf_name       with source=cf: the custom field whose env options to list.
  service_item  with source=tfc_variable_options: the build deployment item's
                global ID (BDI-...). The 'tf-cloud' ConnectionInfo and the
                nocode-* module ID are read server-side from that item's
                pins (env_options.service_item_defaults: the blueprint's
                custom form hidden plugin-bdi-<id>.* defaults, else the
                item's parameter_defaults), never from the query string.
  variable      with source=tfc_variable_options: the Terraform variable
                whose admin-defined options to return.
  resource      a deployed Resource's numeric pk (the object_id a custom
                ACTION form carries) or global ID; object_id and
                resource_id are accepted as aliases. Day-2 forms pass this
                INSTEAD of group and env_id: the group and the Environment
                the resource was ordered into are derived server-side
                (env_options.resource_environment_id), so every env source
                lists what that original environment offers. Required for
                source=day2_panel, which returns the resource's Terraform
                Update variables panel (tfc_api.day2_form_panel) as
                {"panel": {title, description, elements}}.

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
    resolve_resource,
    resource_environment_id,
    service_item_defaults,
)
from shared_modules.tfc_api import TFCError, day2_form_panel, get_options_client

logger = ThreadLogger(__name__)

ENV_SOURCES = ("resource_group", "subnet", "os_image", "vm_size", "location", "cf")
TFC_VARIABLE_OPTIONS_SOURCE = "tfc_variable_options"
DAY2_PANEL_SOURCE = "day2_panel"


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
    blueprint is pinned to. The connection and module ID are resolved
    server-side from the deployment item's pins (its blueprint's custom form
    hidden fields, else its parameter_defaults), never from the query
    string."""
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
            "tfc_nocode_module_id (hidden fields in its blueprint's custom "
            "form, or parameter_defaults).".format(service_item),
        )
    client = get_options_client(connection_info)
    all_options = client.get_no_code_variable_options(module_id)
    values = all_options.get(variable, [])
    return _respond([{"value": value, "title": str(value)} for value in values])


def _resource_context(parameters, profile):
    """(resource, group, env_id) for a day-2 call that passes resource=..., or
    (None, None, None) when the parameter is absent. Raises EnvOptionsError
    for an unknown resource; the caller turns a non-member into a 403."""
    resource_ref = (
        _param(parameters, "resource")
        or _param(parameters, "object_id")
        or _param(parameters, "resource_id")
    )
    if not resource_ref:
        return None, None, None
    resource = resolve_resource(resource_ref)
    if resource is None:
        raise EnvOptionsError("No resource '{}' exists.".format(resource_ref))
    return resource, resource.group, resource_environment_id(resource)


def inbound_web_hook_get(*args, parameters=None, profile=None, **kwargs):
    if profile is None:
        return _fail(403, "Authentication required.")
    source = _param(parameters, "source")
    if not source:
        return _fail(400, "source is required.")

    try:
        if source == TFC_VARIABLE_OPTIONS_SOURCE:
            return _tfc_variable_options(parameters)

        # A day-2 form names the deployed resource; the ordering group and
        # the Environment it was ordered into come from the resource itself,
        # never from the query string. An order form passes group (+ env_id).
        resource, group, env_id = _resource_context(parameters, profile)
        if resource is None:
            group = resolve_group(_param(parameters, "group"))
            env_id = _param(parameters, "env_id")
        if group is None:
            # Name what did arrive: a day-2 form that lands here sent no
            # usable resource reference, and the keys show whether the
            # parameter was dropped on the way in.
            received = sorted(str(key) for key in (parameters.keys() if parameters is not None else []))
            return _fail(
                400,
                "group is required and must name an existing group, or pass "
                "resource=<id> for a deployed resource (query parameters "
                "received: {}).".format(", ".join(received) or "none"),
            )
        if not profile_may_act_for_group(profile, group):
            return _fail(403, "You are not a member of group '{}'.".format(group.name))

        if source == DAY2_PANEL_SOURCE:
            if resource is None:
                return _fail(400, "source=day2_panel requires resource.")
            return {"panel": day2_form_panel(resource)}

        if source == "environment":
            return _respond(environment_options(group, profile=profile))

        base_source = "cf" if source.startswith("cf:") else source
        if base_source not in ENV_SOURCES:
            return _fail(400, "Unknown source '{}'.".format(source))
        if not env_id:
            # SurveyJS fires every choicesByUrl on load, before the
            # Environment dropdown has a value: answer with no options (an
            # empty-value hint option renders as "[object Object]"). A
            # resource with no recorded environment gets the same answer.
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
