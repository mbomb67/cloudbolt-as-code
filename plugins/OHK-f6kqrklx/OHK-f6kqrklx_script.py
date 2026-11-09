"""
Onboarding: Parameter (MCP Tool Action plugin).

Creates or updates a parameter (CustomField) and attaches it to
environments and groups with options and constraints, the way the
Parameters admin page and an environment's Parameters tab do. Covers the
test plan's user-defined parameter (1.1.2), cost center tag source (2.5.1),
power schedule (3.1.1) and expiration date with a maximum lease (3.1.2).
Synchronous. Admin only.

Required is one flag on the parameter itself: the order form and the API
(api/parameter_helper_methods.py) read CustomField.required and ignore the
per-environment mapping's flag. So onb_param_required changes it for every
environment and group that offers the parameter, and asks for
onb_param_confirm_global=true when that reaches beyond the targets.

A parameter name is global too, so a name that already exists and is not the
toolkit's own (created here, agreed to with onb_param_use_existing, or one
of CloudBolt's expiration_date and power_schedule) changes nothing until the
user agrees. Names this tool creates are recorded in the onboarding state.

Every declared input is read from its rendered template token in
_read_inputs(); keep each one referenced that way or CloudBolt's token scan
deletes the input when the script is saved.
"""
import html
import json
import re

GLOBAL_ID_RE = re.compile(r"^[A-Z]{2,4}-[A-Za-z0-9]{6,16}$")
NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,99}$")
TYPES = ("STR", "INT", "DEC", "BOOL", "TXT", "DT", "DTM", "IP", "URL", "EMAIL", "PWD", "CODE", "ETXT")
MAX_OPTIONS = 100
MAX_LISTED = 20
STATE_NAMESPACE = "poc_onboarding"
STATE_FIELD = "poc_onboarding_state"
BUILTIN_REUSE = ("expiration_date", "power_schedule")
# Parameters the Standard Server blueprint brings; a sync stamps these global ids only on parameters it creates.
CONTENT_PARAMETERS = {"application": "CF-15yw228m", "maintenance_window": "CF-cei72k8x"}


def _text(value):
    return html.unescape(value or "").strip()


def _lines(value):
    return [line.strip() for line in re.split(r"[\r\n]+", _text(value)) if line.strip()]


def _bool(value, default):
    text = _text(value).lower()
    if not text:
        return default
    return text in ("true", "1", "yes", "y", "on")


def _int(value):
    text = _text(value)
    if not text:
        return None
    try:
        return int(float(text))
    except ValueError:
        return None


def _failure(message):
    return {"status": "FAILURE", "output_message": "", "error_message": message, "outputs": {"error": message}}


def _success(message, outputs):
    return {"status": "SUCCESS", "output_message": message, "error_message": "", "outputs": outputs}


def _caller(job, kwargs):
    profile = kwargs.get("profile")
    if profile is None and job is not None:
        profile = job.owner
    return profile


def _portal_url():
    try:
        from portals.models import PortalConfig

        portal = PortalConfig.get_current_portal()
        url = portal.site_url if portal else None
    except Exception:
        url = None
    return (url or "").rstrip("/")


def _read_inputs():
    raw = {
        "onb_param_name": """{{ onb_param_name }}""",
        "onb_param_label": """{{ onb_param_label }}""",
        "onb_param_type": """{{ onb_param_type }}""",
        "onb_param_options": """{{ onb_param_options }}""",
        "onb_param_required": """{{ onb_param_required }}""",
        "onb_param_targets": """{{ onb_param_targets }}""",
        "onb_param_description": """{{ onb_param_description }}""",
        "onb_param_max": """{{ onb_param_max }}""",
        "onb_param_min": """{{ onb_param_min }}""",
        "onb_param_show_on_servers": """{{ onb_param_show_on_servers }}""",
        "onb_param_confirm_global": """{{ onb_param_confirm_global }}""",
        "onb_param_use_existing": """{{ onb_param_use_existing }}""",
    }
    return {
        "name": _text(raw["onb_param_name"]),
        "label": _text(raw["onb_param_label"]),
        "type": (_text(raw["onb_param_type"]) or "").upper(),
        "options": _lines(raw["onb_param_options"])[:MAX_OPTIONS],
        "required": _text(raw["onb_param_required"]),
        "targets": _lines(raw["onb_param_targets"]),
        "description": _text(raw["onb_param_description"]),
        "maximum": _int(raw["onb_param_max"]),
        "minimum": _int(raw["onb_param_min"]),
        "show_on_servers": _text(raw["onb_param_show_on_servers"]),
        "confirm_global": _bool(raw["onb_param_confirm_global"], False),
        "use_existing": _bool(raw["onb_param_use_existing"], False),
    }


def _state_field(create=False):
    from infrastructure.models import CustomField, Namespace

    if not create:
        return CustomField.objects.filter(name=STATE_FIELD).first()
    namespace, _ = Namespace.objects.get_or_create(name=STATE_NAMESPACE)
    field, _ = CustomField.objects.get_or_create(
        name=STATE_FIELD,
        defaults=dict(
            label="Onboarding State",
            description="State recorded by the onboarding MCP tools: item statuses, handler verify results, install details.",
            type="TXT",
            namespace=namespace,
        ),
    )
    return field


def _load_state():
    try:
        from orders.models import CustomFieldValue

        field = _state_field()
        cfv = CustomFieldValue.objects.filter(field=field).order_by("id").first() if field else None
        data = json.loads(cfv.txt_value or "") if cfv else {}
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _record_owned(name):
    """Remember that the toolkit created this parameter, or that the user agreed to use it."""
    from django.db import transaction
    from infrastructure.models import CustomField
    from orders.models import CustomFieldValue

    field = _state_field(create=True)
    with transaction.atomic():
        list(CustomField.objects.select_for_update().filter(pk=field.pk))
        cfv = CustomFieldValue.objects.filter(field=field).order_by("id").first()
        try:
            state = json.loads(cfv.txt_value or "") if cfv else {}
        except ValueError:
            state = {}
        if not isinstance(state, dict):
            state = {}
        owned = set(state.get("toolkitParameters") or [])
        if name in owned:
            return
        state["toolkitParameters"] = sorted(owned | {name})
        cfv = cfv or CustomFieldValue(field=field)
        cfv.value = json.dumps(state, indent=2)
        cfv.save()


def _owned(cf):
    if cf.name in BUILTIN_REUSE or cf.name in (_load_state().get("toolkitParameters") or []):
        return True
    return CONTENT_PARAMETERS.get(cf.name) == getattr(cf, "global_id", None)


def _resolve_target(ref):
    from accounts.models import Group
    from infrastructure.models import Environment

    if ref.upper().startswith("ENV-"):
        obj = Environment.objects.filter(global_id=ref).first()
        return ("environment", obj) if obj else (None, None)
    if ref.upper().startswith("GRP-"):
        obj = Group.objects.filter(global_id=ref).first()
        return ("group", obj) if obj else (None, None)
    env = Environment.objects.filter(name__iexact=ref).first()
    if env is not None:
        return "environment", env
    group = Group.objects.filter(name__iexact=ref).first()
    if group is not None:
        return "group", group
    return None, None


def _coerce(cf, option):
    if cf.type in ("INT",):
        return int(float(option))
    if cf.type in ("DEC",):
        return float(option)
    if cf.type == "BOOL":
        return option.strip().lower() in ("true", "1", "yes", "y", "on")
    return option


def _attach_group(group, cf, options):
    from orders.models import CustomFieldValue

    group.custom_fields.add(cf)
    if options:
        for cfv in group.custom_field_options.filter(field=cf):
            group.custom_field_options.remove(cfv)
        for option in options:
            cfv, _ = CustomFieldValue.objects.get_or_create(field=cf, value=_coerce(cf, option))
            group.custom_field_options.add(cfv)


def _other_uses(cf, targets):
    """Environments and groups offering the parameter besides the targets; a required change reaches them too."""
    from accounts.models import Group
    from django.db.models import Q
    from infrastructure.models import Environment

    env_ids = [obj.id for _ref, kind, obj in targets if kind == "environment"]
    group_ids = [obj.id for _ref, kind, obj in targets if kind == "group"]
    envs = Environment.objects.filter(Q(custom_fields=cf) | Q(custom_field_options__field=cf)).exclude(id__in=env_ids).distinct()
    groups = Group.objects.filter(Q(custom_fields=cf) | Q(custom_field_options__field=cf)).exclude(id__in=group_ids).distinct()
    rows = [{"kind": "environment", "id": e.global_id, "name": e.name} for e in envs.order_by("name")[:MAX_LISTED]]
    rows += [{"kind": "group", "id": g.global_id, "name": g.name} for g in groups.order_by("name")[:MAX_LISTED]]
    return envs.count() + groups.count(), rows


def _constraints(cf, inputs, env=None, group=None):
    if inputs["maximum"] is None and inputs["minimum"] is None:
        return None
    from behavior_mapping.models import CustomFieldMapping

    cfm, _ = CustomFieldMapping.objects.get_or_create(
        custom_field=cf, environment=env, group=group, blueprint=None, service_item=None,
        network=None, resource_handler=None, os_family=None,
    )
    changed = []
    if inputs["maximum"] is not None:
        cfm.maximum = inputs["maximum"]
        changed.append("maximum={}".format(inputs["maximum"]))
    if inputs["minimum"] is not None:
        cfm.minimum = inputs["minimum"]
        changed.append("minimum={}".format(inputs["minimum"]))
    cfm.save()
    return changed


def run(job, *args, **kwargs):
    profile = _caller(job, kwargs)
    if profile is None:
        return _failure("No calling user profile was supplied. Run this tool through the MCP server as an authenticated user.")
    if not profile.is_cbadmin:
        return _failure("Only CloudBolt admins may manage parameters.")

    inputs = _read_inputs()
    if not inputs["name"]:
        return _failure("onb_param_name is required: the parameter's internal name, for example backup_policy.")
    if not NAME_RE.match(inputs["name"]):
        return _failure("onb_param_name must be lowercase letters, digits and underscores, starting with a letter.")
    if inputs["name"] == "hostname_template":
        return _failure(
            "Set hostname templates with onb_configure_custom_server (onb_environments for per-environment ones): "
            "it checks the template has CloudBolt's zeros-and-X sequence, for example poc-00X."
        )
    if inputs["type"] and inputs["type"] not in TYPES:
        return _failure("onb_param_type must be one of: {}.".format(", ".join(TYPES)))

    from infrastructure.models import CustomField

    cf = CustomField.objects.filter(name=inputs["name"]).first()
    targets = [(ref,) + _resolve_target(ref) for ref in inputs["targets"]]
    wanted_required = _bool(inputs["required"], False) if inputs["required"] else None
    base = _portal_url()
    if cf is not None and not inputs["use_existing"] and not _owned(cf):
        count, others = _other_uses(cf, [])
        outputs = {
            "parameter": {"id": getattr(cf, "global_id", None), "name": cf.name, "label": cf.label, "type": cf.type,
                          "url": base + "/admin/customfields/{}/".format(cf.id)},
            "existingParameter": True,
            "usedByCount": count,
            "usedBy": others,
            "next": (
                "Nothing changed. This appliance already has a parameter named '{}' (label '{}', type {}) that the "
                "onboarding toolkit did not create. Show the user its label and usedBy and ask one question: use it as "
                "is for this step, or pick another name. Use it: call again with onb_param_use_existing=true."
            ).format(cf.name, cf.label, cf.type),
        }
        return _success("Confirmation needed: parameter '{}' already exists and is not the toolkit's own.".format(cf.name), outputs)
    if cf is not None and wanted_required is not None and bool(cf.required) != wanted_required:
        count, others = _other_uses(cf, targets)
        if count and not inputs["confirm_global"]:
            outputs = {
                "parameter": {"name": cf.name, "label": cf.label, "required": bool(cf.required),
                              "url": base + "/admin/customfields/{}/".format(cf.id)},
                "confirmationRequired": True,
                "requiredWanted": wanted_required,
                "alsoAffectsCount": count,
                "alsoAffects": others,
                "next": (
                    "Nothing changed. CloudBolt has one required flag per parameter, so making '{}' {} applies to every "
                    "order form, API and MCP order that offers it, including the {} other environment(s) and group(s) in "
                    "alsoAffects. Ask the user whether to do that. Yes: call again with onb_param_confirm_global=true."
                ).format(cf.name, "required" if wanted_required else "optional", count),
            }
            return _success(
                "Confirmation needed: required on '{}' is global and also reaches {} other environment(s) or group(s).".format(
                    cf.name, count),
                outputs,
            )
    created = False
    changes = []
    if cf is None:
        cf = CustomField.objects.create(
            name=inputs["name"],
            label=inputs["label"] or inputs["name"].replace("_", " ").title(),
            type=inputs["type"] or "STR",
            description=inputs["description"],
            required=bool(wanted_required),
            show_on_servers=_bool(inputs["show_on_servers"], True),
            show_as_attribute=True,
        )
        created = True
        changes.append("created")
    if created or inputs["use_existing"]:
        try:
            _record_owned(cf.name)
        except Exception as exc:
            changes.append("not recorded as the toolkit's own: {}".format(str(exc)[:160]))
    else:
        if inputs["label"] and cf.label != inputs["label"]:
            cf.label = inputs["label"]
            changes.append("label")
        if inputs["description"] and cf.description != inputs["description"]:
            cf.description = inputs["description"]
            changes.append("description")
        if inputs["show_on_servers"]:
            wanted = _bool(inputs["show_on_servers"], True)
            if bool(cf.show_on_servers) != wanted:
                cf.show_on_servers = wanted
                changes.append("show_on_servers")
        if wanted_required is not None and bool(cf.required) != wanted_required:
            cf.required = wanted_required
            changes.append("required={} (global)".format(wanted_required))
        if changes:
            cf.save()
        if inputs["type"] and cf.type != inputs["type"]:
            changes.append("type left as {} (changing a parameter's type is a UI operation)".format(cf.type))

    attached = []
    for ref, kind, obj in targets:
        if obj is None:
            attached.append({"target": ref, "status": "failed", "error": "no environment or group matches"})
            continue
        try:
            if kind == "environment":
                if inputs["options"]:
                    obj.replace_cf_options(cf.name, [_coerce(cf, o) for o in inputs["options"]])
                else:
                    obj.custom_fields.add(cf)
                constraints = _constraints(cf, inputs, env=obj)
                url = base + "/environments/{}/#tab-parameters".format(obj.id)
            else:
                _attach_group(obj, cf, inputs["options"])
                constraints = _constraints(cf, inputs, group=obj)
                url = base + "/groups/{}/".format(obj.id)
            attached.append(
                {"target": ref, "kind": kind, "id": obj.global_id, "name": obj.name, "status": "attached",
                 "options": inputs["options"], "constraints": constraints, "url": url}
            )
        except Exception as exc:
            attached.append({"target": ref, "kind": kind, "status": "failed", "error": "{}: {}".format(type(exc).__name__, str(exc)[:400])})

    outputs = {
        "parameter": {
            "id": getattr(cf, "global_id", None),
            "name": cf.name,
            "label": cf.label,
            "type": cf.type,
            "required": bool(cf.required),
            "requiredScope": "global",
            "showOnServers": bool(cf.show_on_servers),
            "created": created,
            "changes": changes,
            "url": base + "/admin/customfields/{}/".format(cf.id),
        },
        "attached": attached,
    }
    notes = []
    if cf.required:
        notes.append(
            "Required applies wherever this parameter is offered. Where an environment or group offers exactly one "
            "option, API and MCP orders fill it in automatically; with several, the order must pick one."
        )
    if cf.type in ("DT", "DTM") and inputs["maximum"] is not None:
        notes.append("For a date parameter the maximum is the number of days from today a user may pick.")
    if cf.name == "expiration_date":
        notes.append("Expiration needs the 'Expire Servers' recurring job enabled (Admin > Recurring Jobs) to act on the date.")
    if cf.name == "power_schedule":
        notes.append("Servers ordered with a schedule are powered by the scheduler every 15 minutes; set_server_power_schedule reads it back.")
    if inputs["options"] and not inputs["targets"]:
        notes.append("Options apply per environment or group; pass onb_param_targets to attach them.")
    if notes:
        outputs["notes"] = notes
    message = "Parameter '{}' {}; attached to {} target(s).".format(cf.name, "created" if created else "updated", sum(1 for a in attached if a["status"] == "attached"))
    return _success(message, outputs)
