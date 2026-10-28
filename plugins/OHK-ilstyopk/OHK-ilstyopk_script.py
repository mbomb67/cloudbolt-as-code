"""
POC Onboarding: Configure Custom Server (MCP Tool Action plugin).

Makes the shipped Custom Server blueprint orderable for the POC: recreates
it when it is missing, activates it, opens it to every environment (or the
listed ones), and sets a hostname template on the server tier or on the
listed environments so the order form previews the generated name. Group
entitlement stays with manage_blueprint_permissions. Synchronous. Admin
only.

Every declared input is read from its rendered template token in
_read_inputs(); keep each one referenced that way or CloudBolt's token scan
deletes the input when the script is saved.
"""
import html
import re

from django.conf import settings

GLOBAL_ID_RE = re.compile(r"^[A-Z]{2,4}-[A-Za-z0-9]{6,16}$")


def _text(value):
    return html.unescape(value or "").strip()


def _lines(value):
    return [line.strip() for line in re.split(r"[\r\n,]+", _text(value)) if line.strip()]


def _bool(value, default):
    text = _text(value).lower()
    if not text:
        return default
    return text in ("true", "1", "yes", "y", "on")


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
        "onb_environments": """{{ onb_environments }}""",
        "onb_hostname_template": """{{ onb_hostname_template }}""",
        "onb_all_environments": """{{ onb_all_environments }}""",
    }
    return {
        "environments": _lines(raw["onb_environments"]),
        "hostname_template": _text(raw["onb_hostname_template"]),
        "all_environments": _bool(raw["onb_all_environments"], True),
    }


def _resolve_envs(refs):
    from infrastructure.models import Environment

    envs = []
    for ref in refs:
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
        envs.append(env)
    return envs, None


def _find_or_create_blueprint(name, default_name):
    from servicecatalog.models import ServiceBlueprint

    blueprint = ServiceBlueprint.objects.filter(name=name).order_by("-id").first()
    created = False
    if blueprint is None:
        from c2_wrapper import create_blueprint
        from initialize.cb_minimal import all_blueprints

        for bp_dict in all_blueprints:
            if bp_dict.get("name") == default_name:
                blueprint = create_blueprint(bp_dict)
                blueprint.name = name
                created = True
                break
    return blueprint, created


def _hostname_problem(template):
    """A reason the hostname template would not give each server its own name, or None.

    CloudBolt numbers names with a run of zeros followed by X: poc-00X becomes poc-001, poc-002
    and so on (common.methods, the 0+X substitution). '#' is not part of that syntax and stays
    literal, so every server would get the same name.
    """
    if not template:
        return None
    if "#" in template:
        return "'#' is not CloudBolt hostname syntax and would be used literally; use a run of zeros and X, for example {}".format(
            template.replace("#", "00X"))
    if not re.search(r"0+X", template):
        return "the template has no sequence, so every server would get the same name; add zeros and X, for example {}-00X".format(
            template.rstrip("-"))
    return None


def run(job, *args, **kwargs):
    profile = _caller(job, kwargs)
    if profile is None:
        return _failure("No calling user profile was supplied. Run this tool through the MCP server as an authenticated user.")
    if not profile.is_cbadmin:
        return _failure("Only CloudBolt admins may configure the Custom Server blueprint.")

    inputs = _read_inputs()
    envs, error = _resolve_envs(inputs["environments"])
    if error:
        return _failure(error)
    problem = _hostname_problem(inputs["hostname_template"])
    if problem:
        return _failure("Hostname template '{}' was not applied: {}.".format(inputs["hostname_template"], problem))

    default_name = getattr(settings, "DEFAULT_NEW_SERVER_BLUEPRINT_NAME", "Custom Server")
    name = getattr(settings, "NEW_SERVER_BLUEPRINT_NAME", default_name) or default_name
    try:
        blueprint, created = _find_or_create_blueprint(name, default_name)
    except Exception as exc:
        return _failure("Could not find or recreate the '{}' blueprint: {}: {}".format(name, type(exc).__name__, str(exc)[:400]))
    if blueprint is None:
        return _failure("No '{}' blueprint exists and the shipped definition could not be found.".format(name))

    changes = []
    if created:
        changes.append("recreated from the shipped definition")
    if blueprint.status != "ACTIVE":
        blueprint.status = "ACTIVE"
        changes.append("status set to ACTIVE")
    if not blueprint.is_orderable:
        blueprint.is_orderable = True
        changes.append("made orderable")
    if changes:
        blueprint.save()

    from servicecatalog.models import ProvisionServerServiceItem

    tiers = list(ProvisionServerServiceItem.objects.filter(blueprint=blueprint).order_by("id"))
    tier_rows = []
    for tier in tiers:
        tier_changes = []
        if inputs["all_environments"] and not tier.all_environments_enabled:
            tier.all_environments_enabled = True
            tier_changes.append("all environments enabled")
        if inputs["hostname_template"] and not inputs["environments"]:
            if (tier.hostname_template or "") != inputs["hostname_template"]:
                tier.hostname_template = inputs["hostname_template"]
                tier_changes.append("hostname template set on the server tier")
        if tier_changes:
            tier.save()
        tier_rows.append(
            {
                "id": getattr(tier, "global_id", None),
                "name": tier.name,
                "allEnvironmentsEnabled": bool(tier.all_environments_enabled),
                "hostnameTemplate": tier.hostname_template or None,
                "changes": tier_changes,
            }
        )
        changes.extend(tier_changes)

    env_rows = []
    for env in envs:
        row = {"id": env.global_id, "name": env.name}
        if inputs["hostname_template"]:
            env.set_cfv("hostname_template", inputs["hostname_template"])
            row["hostnameTemplate"] = inputs["hostname_template"]
            changes.append("hostname template set on environment '{}'".format(env.name))
        env_rows.append(row)

    base = _portal_url()
    try:
        url = base + blueprint.get_absolute_url()
    except Exception:
        url = None
    outputs = {
        "blueprint": {
            "id": blueprint.global_id,
            "pk": blueprint.id,
            "name": blueprint.name,
            "status": blueprint.status,
            "isOrderable": bool(blueprint.is_orderable),
            "anyGroupCanDeploy": bool(blueprint.any_group_can_deploy),
            "url": url,
        },
        "serverTiers": tier_rows,
        "environments": env_rows,
        "changes": changes,
        "next": (
            "Order it through MCP: fetch_catalog_items(name='{}'), fetch_orderable_groups, fetch_blueprint_schema, "
            "fetch_orderable_environments, order_catalog_item. If anyGroupCanDeploy is false, grant the POC group with "
            "manage_blueprint_permissions.".format(blueprint.name)
        ),
    }
    if not tiers:
        outputs["note"] = "The blueprint has no server tier; it cannot build a server. Recreate it from the shipped definition."
    message = "'{}' is {} and orderable; {} change(s).".format(blueprint.name, blueprint.status, len(changes))
    return _success(message, outputs)
