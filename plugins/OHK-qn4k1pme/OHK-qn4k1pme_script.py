"""
POC Onboarding: Trim Environment Order Form (MCP Tool Action plugin).

Shortens an environment's order form to what a POC user needs. CloudBolt
hides a required parameter that has exactly one option, so the tool pins
infrastructure choices (key pair, security group, resource group, storage
type, datastore, disk type) to one value and removes the optional
technology parameters a POC never touches, leaving the size and, where
needed, the network and password visible. Pinned fields are protected from
the handler's sync jobs. The report profile only shows the current form.
Synchronous. Admin only.

Every declared input is read from its rendered template token in
_read_inputs(); keep each one referenced that way or CloudBolt's token scan
deletes the input when the script is saved.
"""
import html
import re

GLOBAL_ID_RE = re.compile(r"^[A-Z]{2,4}-[A-Za-z0-9]{6,16}$")
PROFILES = ("poc_minimal", "report")

# Per technology: fields the user still sees, fields pinned to one value
# (preferred values first; None = first existing option), fields removed.
TRIM = {
    "aws": {
        "show": ["instance_type"],
        "pin": {
            "key_name": [],
            "sec_groups": ["default"],
            "delete_ebs_volumes_on_termination": ["True", "true"],
            "ebs_volume_type": ["gp3", "gp2"],
            "sc_nic_0": [],
        },
        "remove": ["aws_availability_zone", "aws_elastic_ip", "iops", "aws_host", "aws_host_group"],
    },
    "azure_arm": {
        "show": ["node_size", "password_arm", "azure_authentication_type", "azure_ssh_key"],
        "pin": {
            "resource_group_arm": [],
            "storage_account_type_arm": ["Standard_LRS", "StandardSSD_LRS", "Premium_LRS"],
            "create_public_ip": ["False", "false"],
            "availability_set_arm": [],
            "availability_zone_arm": [],
            "sc_nic_0": [],
        },
        "remove": [
            "spot_instance_arm", "eviction_type_arm", "eviction_policy_arm", "max_price_arm",
            "platform_fault_domain_count", "custom_storage_account_arm", "storage_account_arm",
            "encryption_at_host", "security_type_arm", "security_encryption_type_arm", "os_disk_size_arm",
            "resource_group_arm_template", "enable_accelerated_networking",
        ],
    },
    "vmware": {
        "show": ["cpu_cnt", "mem_size"],
        "pin": {"vmware_datastore": [], "vmware_disk_type": ["thin", "Thin"], "sc_nic_0": []},
        "remove": ["vmware_resourcepool", "nsx_scope", "nsx_edge"],
    },
}
TRIM["aws_govcloud"] = TRIM["aws"]
TRIM["aws_china"] = TRIM["aws"]
# Fields other fields regenerate their options from; a single option never hides them.
NEVER_HIDDEN = {"azure_arm": ["node_size", "availability_set_arm", "azure_authentication_type"]}


def _text(value):
    return html.unescape(value or "").strip()


def _lines(value):
    return [line.strip() for line in re.split(r"[\r\n,]+", _text(value)) if line.strip()]


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
        "onb_environment": """{{ onb_environment }}""",
        "onb_profile": """{{ onb_profile }}""",
        "onb_pin": """{{ onb_pin }}""",
        "onb_keep": """{{ onb_keep }}""",
        "onb_remove": """{{ onb_remove }}""",
    }
    pins = {}
    for line in _lines(raw["onb_pin"]):
        name, sep, value = line.partition("=")
        if sep:
            pins[name.strip()] = value.strip()
    return {
        "environment": _text(raw["onb_environment"]),
        "profile": (_text(raw["onb_profile"]) or "poc_minimal").lower(),
        "pins": pins,
        "keep": _lines(raw["onb_keep"]),
        "remove": _lines(raw["onb_remove"]),
    }


def _resolve_env(ref):
    from infrastructure.models import Environment

    env = None
    if GLOBAL_ID_RE.match(ref):
        env = Environment.objects.filter(global_id=ref).first()
    elif ref.isdigit():
        env = Environment.objects.filter(id=int(ref)).first()
    if env is None:
        matches = list(Environment.objects.filter(name__iexact=ref))
        if len(matches) > 1:
            return None, "Several environments are named '{}'; pass the ENV- global id.".format(ref)
        env = matches[0] if matches else None
    if env is None:
        return None, "No environment matches '{}'.".format(ref)
    return env, None


def _option_label(cfv):
    try:
        value = cfv.value
    except Exception:
        value = None
    if value is None:
        return cfv.str_value or ""
    if hasattr(value, "name"):
        return getattr(value, "name")
    return str(value)


def _field_rows(env, handler):
    """One row per parameter that can appear on this environment's order form."""
    from infrastructure.models import CustomField

    special = set(handler.special_fields() or [])
    tech = set(handler.tech_parameter_fields() or [])
    base_names = set(getattr(handler, "base_orders_cf_names", []) or [])
    env_fields = {cf.name: cf for cf in env.custom_fields.all()}
    rh_fields = {cf.name: cf for cf in handler.custom_fields.all()}
    names = set(env_fields) | set(rh_fields)
    rows = {}
    for name in sorted(names):
        cf = env_fields.get(name) or rh_fields.get(name)
        options = list(env.custom_field_options.filter(field=cf).order_by("id"))
        rows[name] = {
            "name": name,
            "label": cf.label,
            "type": cf.type,
            "required": bool(cf.required),
            "onEnvironment": name in env_fields,
            "onHandler": name in rh_fields,
            "technologyParameter": name in tech,
            "special": name in special,
            "baseOrderField": name in base_names,
            "options": [_option_label(o) for o in options][:25],
            "optionCount": len(options),
            "_cf": cf,
            "_cfvs": options,
        }
    return rows


def _hidden(row, slug):
    if row["special"]:
        return True
    if row["name"] in NEVER_HIDDEN.get(slug, []):
        return False
    if row["optionCount"] == 1 and (row["required"] or row["type"] == "BOOL"):
        return True
    return False


def _public(row, slug):
    out = {k: v for k, v in row.items() if not k.startswith("_")}
    out["hiddenOnOrderForm"] = _hidden(row, slug)
    return out


def _choose(row, preferred, wanted):
    """Pick the CFV to pin: the user's value, then a preferred value, then the first option."""
    cfvs = row["_cfvs"]
    labels = [_option_label(c) for c in cfvs]
    if wanted:
        for cfv, label in zip(cfvs, labels):
            if label == wanted or label.lower() == wanted.lower():
                return cfv, None
        if row["_cf"].type in ("STR", "INT", "DEC", "TXT") and row["name"] != "sc_nic_0":
            return wanted, None  # a new option, created by replace_cf_options
        return None, "'{}' is not one of the options {} and a new value cannot be created for this field type.".format(
            wanted, labels
        )
    for pref in preferred:
        for cfv, label in zip(cfvs, labels):
            if label == pref:
                return cfv, None
    if cfvs:
        return cfvs[0], None
    return None, "no options to pin"


def _protect(env, names):
    data = env.modified_parameters or {}
    current = list(data.get("tech_specific_parameters", []) or [])
    changed = False
    for name in names:
        if name not in current:
            current.append(name)
            changed = True
    if changed:
        data["tech_specific_parameters"] = current
        env.modified_parameters = data
        env.save()


def run(job, *args, **kwargs):
    profile = _caller(job, kwargs)
    if profile is None:
        return _failure("No calling user profile was supplied. Run this tool through the MCP server as an authenticated user.")
    if not profile.is_cbadmin:
        return _failure("Only CloudBolt admins may change an environment's parameters.")

    inputs = _read_inputs()
    if not inputs["environment"]:
        return _failure("onb_environment is required: the ENV- global id or the environment name.")
    if inputs["profile"] not in PROFILES:
        return _failure("onb_profile must be one of: {}.".format(", ".join(PROFILES)))
    env, error = _resolve_env(inputs["environment"])
    if error:
        return _failure(error)
    if env.resource_handler is None:
        return _failure("Environment '{}' has no resource handler.".format(env.name))
    handler = env.resource_handler.cast()
    tech = handler.resource_technology
    slug = tech.slug if tech else ""
    base = _portal_url()

    rows = _field_rows(env, handler)
    before = [_public(r, slug) for r in rows.values()]
    outputs = {
        "environment": {"id": env.global_id, "name": env.name, "url": base + "/environments/{}/".format(env.id),
                        "parametersUrl": base + "/environments/{}/#tab-parameters".format(env.id)},
        "handler": {"id": handler.global_id, "name": handler.name, "technologySlug": slug},
        "profile": inputs["profile"],
        "before": before,
    }
    if slug in NEVER_HIDDEN:
        outputs["note"] = "On {} the fields {} drive other fields' options and stay visible however many options they have.".format(
            slug, ", ".join(NEVER_HIDDEN[slug])
        )
    if inputs["profile"] == "report":
        visible = [r["name"] for r in before if not r["hiddenOnOrderForm"]]
        return _success("{}: {} parameter(s) visible on the order form.".format(env.name, len(visible)),
                        dict(outputs, visibleNow=visible))

    table = TRIM.get(slug, {"show": [], "pin": {}, "remove": []})
    keep = set(inputs["keep"]) | set(table["show"])
    pin_plan = {name: prefs for name, prefs in table["pin"].items() if name not in keep}
    for name, value in inputs["pins"].items():
        pin_plan[name] = pin_plan.get(name, [])
    remove_plan = [n for n in list(table["remove"]) + inputs["remove"] if n not in keep and n not in pin_plan]

    actions = []
    protected = []
    for name in remove_plan:
        row = rows.get(name)
        if row is None:
            continue
        if row["onHandler"] and not row["onEnvironment"]:
            actions.append({"field": name, "action": "left", "reason": "defined on the handler, not the environment"})
            continue
        if row["onHandler"]:
            # Removing from the environment leaves the handler's copy; pin instead so it hides.
            choice, err = _choose(row, [], None)
            if choice is None:
                actions.append({"field": name, "action": "left", "reason": err})
                continue
            env.replace_cf_options(name, [choice])
            protected.append(name)
            actions.append({"field": name, "action": "pinned", "value": _option_label(choice) if hasattr(choice, "value") else str(choice),
                            "reason": "handler-level field; pinned instead of removed"})
            continue
        env.clear_cfv(name)
        actions.append({"field": name, "action": "removed"})

    for name, preferred in pin_plan.items():
        row = rows.get(name)
        if row is None:
            if name in inputs["pins"]:
                actions.append({"field": name, "action": "left", "reason": "not a parameter of this environment or handler"})
            continue
        wanted = inputs["pins"].get(name)
        if row["optionCount"] == 0 and not wanted:
            actions.append({"field": name, "action": "left", "reason": "no options to pin; import parameters first or pass onb_pin"})
            continue
        if row["optionCount"] == 1 and not wanted:
            protected.append(name)
            actions.append({"field": name, "action": "already single", "value": row["options"][0]})
            continue
        choice, err = _choose(row, preferred, wanted)
        if choice is None:
            actions.append({"field": name, "action": "left", "reason": err})
            continue
        env.replace_cf_options(name, [choice])
        protected.append(name)
        label = _option_label(choice) if hasattr(choice, "value") else str(choice)
        entry = {"field": name, "action": "pinned", "value": label}
        if not (row["required"] or row["type"] == "BOOL") and name not in NEVER_HIDDEN.get(slug, []):
            entry["note"] = "optional field: a single option still shows with a blank choice; remove it with onb_remove if it should disappear"
        actions.append(entry)

    if protected:
        _protect(env, protected)

    after_rows = _field_rows(env, handler)
    after = [_public(r, slug) for r in after_rows.values()]
    visible = [r["name"] for r in after if not r["hiddenOnOrderForm"]]
    outputs.update({"actions": actions, "protectedFromSync": sorted(set(protected)), "after": after, "visibleNow": visible})
    outputs["next"] = (
        "Order the Custom Server into this environment through MCP and confirm the form shows only {}. "
        "Pass onb_keep to bring a field back, onb_pin field=value to change a pin.".format(", ".join(visible) or "the size")
    )
    message = "{}: {} action(s); visible now: {}.".format(env.name, len(actions), ", ".join(visible) or "none")
    return _success(message, outputs)
