"""
Azure PS Script Panel -- inbound webhook plugin behind webhooks/IWH-vfkvduxm
(uri_path azps-script-panel). Serves the "Run an Azure PowerShell Script"
custom form (forms/FRM-mfveruw6): given the blueprint and the chosen script,
returns the SurveyJS elements for that script's declared action inputs so the
form function can rebuild the Script Parameters Dynamic Panel whenever the
Script dropdown changes.

GET /api/v3/cmp/inboundWebHooks/azps-script-panel/run/
    ?blueprint=BP-...&script=OHK-...&group=<group href, id or name>[&env_id=<id>]

200: {"panel": {"title": ..., "description": ..., "elements": [...]}}
else: the iwh_status_code / iwh_embedded_response pair carrying
      {"panel": null, "error": "..."}.

RBAC: the IWH endpoint enforces nothing itself, so this plugin requires an
authenticated profile, membership of the group, and that the group may deploy
the blueprint. Only scripts that are DISABLED Remote Script build items of
that blueprint are described, the same set the runner will accept.

Input metadata comes from CloudBolt's own order-form builder,
api.parameter_helper_methods.get_runhook_parameter_metadata (internal API,
verified against CloudBolt 2026.1; recheck on upgrade): type, required,
placeholder, options (global ones and plugin-generated ones for inputs with
no controlling field), constraints (minimum, maximum, regex_constraint) and
pinned item defaults. Mapping to SurveyJS:

  options present  -> dropdown (checkbox when the input allows multiple values)
  STR              -> text                 TXT / CODE -> comment
  INT / DEC        -> text, inputType number, numeric validator with min/max
  BOOL             -> boolean              PWD / ETXT -> text, inputType password
  DT               -> text, inputType date DTM        -> text, inputType datetime-local
  anything else    -> text

An input the native form would hide because the build item pins a default
(is_provided) is omitted; the runner applies that default server-side.
Not covered yet: show/hide dependencies between inputs, and options that
depend on another input's value (such an input renders as a text box with a
hint).
"""

import time
from decimal import Decimal

from api.parameter_helper_methods import get_runhook_parameter_metadata
from servicecatalog.models import RunRemoteScriptHookServiceItem, ServiceBlueprint
from utilities.logger import ThreadLogger

from shared_modules.env_options import (
    entitled_environment,
    profile_may_act_for_group,
    resolve_group,
)

logger = ThreadLogger(__name__)


def _fail(status, message):
    # Non-200: return BOTH keys; anything else in the dict is dropped.
    return {"iwh_status_code": status, "iwh_embedded_response": {"panel": None, "error": message}}


def _param(parameters, name):
    value = parameters.get(name) if parameters is not None else None
    return (str(value) if value is not None else "").strip()


def _jsonable(value):
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _choices(options):
    choices = []
    for option in options:
        if isinstance(option, (list, tuple)) and len(option) == 2:
            choices.append({"value": _jsonable(option[0]), "text": str(option[1])})
        else:
            choices.append({"value": _jsonable(option), "text": str(option)})
    return choices


def _element(name, meta, hook_input):
    """One SurveyJS question for one Remote Script action input."""
    element = {
        "name": name,
        "title": (getattr(hook_input, "label", "") or name),
        "isRequired": bool(meta.get("required")),
    }
    description = (getattr(hook_input, "description", "") or "").strip()
    if description:
        element["tooltipText"] = description
    placeholder = meta.get("placeholder")
    if placeholder:
        element["placeholder"] = str(placeholder)

    options = meta.get("options") or []
    if options:
        multiple = bool(getattr(hook_input, "allow_multiple", False))
        element["type"] = "checkbox" if multiple else "dropdown"
        element["choices"] = _choices(options)
        return element

    cf_type = meta.get("type") or "STR"
    constraints = meta.get("constraints") or {}
    validators = []
    if cf_type in ("TXT", "CODE"):
        element["type"] = "comment"
    elif cf_type in ("INT", "DEC"):
        element["type"] = "text"
        element["inputType"] = "number"
        numeric = {"type": "numeric"}
        if constraints.get("minimum") is not None:
            numeric["minValue"] = _jsonable(constraints["minimum"])
        if constraints.get("maximum") is not None:
            numeric["maxValue"] = _jsonable(constraints["maximum"])
        validators.append(numeric)
    elif cf_type == "BOOL":
        element["type"] = "boolean"
    elif cf_type in ("PWD", "ETXT"):
        element["type"] = "text"
        element["inputType"] = "password"
    elif cf_type == "DT":
        element["type"] = "text"
        element["inputType"] = "date"
    elif cf_type == "DTM":
        element["type"] = "text"
        element["inputType"] = "datetime-local"
    else:
        element["type"] = "text"

    regex = constraints.get("regex_constraint")
    if regex:
        validators.append({"type": "regex", "regex": regex, "text": "Value does not match the required pattern."})
    if validators:
        element["validators"] = validators
    if meta.get("has_dynamic_options") and element["type"] == "text":
        element["description"] = "Options for this input depend on another value; enter it by hand."
    return element


class _Trace:
    """Step timing for application.log: grep 'azps-script-panel' to see how far
    a call got and how long each step took."""

    def __init__(self, script_ref):
        self.script_ref = script_ref
        self.started = time.monotonic()
        self.last = self.started

    def step(self, name):
        now = time.monotonic()
        logger.info(
            "azps-script-panel %s: %s (+%.0f ms, %.0f ms total)",
            self.script_ref, name, (now - self.last) * 1000, (now - self.started) * 1000,
        )
        self.last = now


def _panel(item, hook, group, blueprint, profile, env, trace):
    action_context = {"group": group, "blueprint": blueprint}
    if env is not None:
        action_context["environment"] = env
    trace.step("building parameter metadata for {} input(s)".format(hook.input_fields.count()))
    metadata = get_runhook_parameter_metadata(
        hook,
        item.input_mappings,
        action_context=action_context,
        user_profile=profile,
        blueprint=blueprint,
        include_cf=True,
        process_showhide_dep=False,
        execute_gen_options=True,
        include_labels=True,
    )
    trace.step("parameter metadata built for {}".format(", ".join(metadata) or "no inputs"))
    inputs_by_name = {
        hook_input.name.replace("_a{}".format(hook.id), ""): hook_input
        for hook_input in hook.input_fields.all()
    }
    elements = []
    for name, meta in metadata.items():
        if meta.get("is_provided"):
            continue  # pinned on the build item; the runner fills it in
        hook_input = meta.get("field") or inputs_by_name.get(name)
        elements.append(_element(name, meta, hook_input))
    return {
        "title": "{} parameters".format(item.name or hook.name),
        "description": (hook.description or "").strip(),
        "elements": elements,
    }


def inbound_web_hook_get(*args, parameters=None, profile=None, **kwargs):
    """GET: `parameters` is request.GET (strings). `profile` is the caller's
    UserProfile, or None for token-mode/anonymous calls."""
    if profile is None:
        return _fail(403, "Authentication required.")
    blueprint_ref = _param(parameters, "blueprint")
    script_ref = _param(parameters, "script")
    if not blueprint_ref or not script_ref:
        # SurveyJS may call before the dropdowns have values: nothing to build yet.
        return _fail(400, "blueprint and script are required.")
    trace = _Trace(script_ref)
    trace.step("request received for blueprint {}".format(blueprint_ref))
    group = resolve_group(_param(parameters, "group"))
    if group is None:
        return _fail(400, "group is required and must name an existing group.")
    if not profile_may_act_for_group(profile, group):
        return _fail(403, "You are not a member of group '{}'.".format(group.name))
    trace.step("group {} resolved and authorized".format(group.name))

    blueprint = ServiceBlueprint.objects.filter(global_id=blueprint_ref.rstrip("/").rsplit("/", 1)[-1]).first()
    if blueprint is None:
        return _fail(404, "Blueprint '{}' was not found.".format(blueprint_ref))
    # groups_that_can_deploy is a cached_property (a queryset), not a method.
    if not blueprint.groups_that_can_deploy.filter(id=group.id).exists():
        return _fail(403, "Group '{}' may not deploy blueprint '{}'.".format(group.name, blueprint.name))
    trace.step("blueprint {} deployable by group".format(blueprint.name))

    item = (
        RunRemoteScriptHookServiceItem.objects.filter(
            blueprint=blueprint, enabled=False, hook__global_id=script_ref
        )
        .select_related("hook")
        .first()
    )
    if item is None:
        return _fail(404, "'{}' is not a disabled Remote Script item of blueprint '{}'.".format(script_ref, blueprint.name))
    hook = item.hook.cast()
    trace.step("catalog item '{}' found".format(hook.name))

    env = None
    env_id = _param(parameters, "env_id")
    if env_id:
        env = entitled_environment(group, env_id, profile=profile)
        if env is None:
            return _fail(403, "Group '{}' is not entitled to environment id {}.".format(group.name, env_id))
        trace.step("environment {} entitled".format(env.name))

    try:
        panel = _panel(item, hook, group, blueprint, profile, env, trace)
    except Exception as exc:  # noqa: BLE001 -- an uncaught exception is a 500 whose text reaches the browser
        logger.exception("azps-script-panel failed for %s", script_ref)
        return _fail(400, str(exc))
    trace.step("responding with {} element(s)".format(len(panel["elements"])))
    return {"panel": panel}
