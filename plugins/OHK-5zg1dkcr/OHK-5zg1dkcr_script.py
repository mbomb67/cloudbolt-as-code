"""
POC Onboarding: Status (MCP Tool Action plugin).

Reports what the appliance has configured for a proof of concept in one
read: licence, portal URL, SMTP, proxy, Content Library reachability, the
Setup Wizard's derived step states, every resource handler with its
environments (OS builds, networks, groups, lifecycle parameters, last
verify result), the Custom Server blueprint, groups and users, LDAP and SSO,
which onboarding tools are installed and synchronous, the POC items the
agent has recorded as done or not applicable, and the next steps with the
UI link for each. Read-only, no inputs, synchronous. Admin only.

The only network call is a HEAD against the Content Library host with a
five-second timeout; everything else is the ORM.
"""
import json
from datetime import date, datetime, timezone

from django.conf import settings

TOOLKIT_VERSION = "0.1.0"
STATE_NAMESPACE = "poc_onboarding"
STATE_FIELD = "poc_onboarding_state"
TOOLKIT_TOOLS = [
    "onb_status",
    "onb_ui_link",
    "onb_recent_errors",
    "onb_failure_trace",
    "onb_verify_resource_handler",
    "onb_playbook",
    "onb_install",
    "onb_set_item_status",
    "sync_from_source_control_repo",
    "fetch_job_log",
    "manage_blueprint_permissions",
]
ENV_PARAM_FLAGS = ["hostname_template", "expiration_date", "power_schedule"]
MAX_GROUPS = 25


def _failure(message):
    return {
        "status": "FAILURE",
        "output_message": "",
        "error_message": message,
        "outputs": {"error": message},
    }


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


def _iso(value):
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return value


def _load_state():
    try:
        from infrastructure.models import CustomField
        from orders.models import CustomFieldValue

        field = CustomField.objects.filter(name=STATE_FIELD).first()
        if field is None:
            return {}
        cfv = CustomFieldValue.objects.filter(field=field).order_by("id").first()
        if cfv is None:
            return {}
        data = json.loads(cfv.txt_value or "")
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _appliance(base):
    info = getattr(settings, "VERSION_INFO", {}) or {}
    return {
        "portalUrl": base or None,
        "version": info.get("VERSION"),
        "build": info.get("BUILD"),
        "toolkitVersion": TOOLKIT_VERSION,
    }


def _license():
    try:
        from product_license.models import ProductLicense
    except Exception:
        return {"known": False}
    try:
        lic = ProductLicense.objects.order_by("-expiration_date", "-id").first()
    except Exception as exc:
        return {"known": False, "error": str(exc)[:200]}
    if lic is None:
        return {"known": True, "installed": False}
    days_left = None
    if lic.expiration_date:
        days_left = (lic.expiration_date - date.today()).days
    return {
        "known": True,
        "installed": True,
        "company": lic.company,
        "productType": getattr(lic, "product_type", None),
        "licenseType": getattr(lic, "license_type", None),
        "expirationDate": _iso(lic.expiration_date),
        "daysLeft": days_left,
    }


def _preferences(base):
    from utilities.models import GlobalPreferences

    gp = GlobalPreferences.get()
    smtp_host = (gp.smtp_host or "").strip()
    return {
        "smtp": {
            "configured": bool(smtp_host),
            "host": smtp_host or None,
            "editUrl": base + "/admin/email_settings/edit/",
            "testUrl": base + "/admin/email_test/",
        },
        "proxy": {
            "configured": gp.web_proxy_id is not None,
            "editUrl": base + "/admin/edit_web_proxy/",
        },
    }


def _content_library():
    try:
        from utilities.cb_http import get_content_library_connection
        from utilities.helpers import cb_request
    except Exception as exc:
        return {"reachable": None, "error": str(exc)[:200]}
    try:
        ci = get_content_library_connection("blueprints", raise_error=False)
    except Exception as exc:
        return {"reachable": None, "error": str(exc)[:200]}
    if ci is None:
        return {"reachable": None, "note": "No Content Library connection is defined on this appliance."}
    url = "{}://{}:{}/".format(ci.protocol or "https", ci.ip, ci.port or 443)
    try:
        response = cb_request("HEAD", url, timeout=5)
        return {"reachable": True, "url": url, "httpStatus": response.status_code}
    except Exception as exc:
        return {
            "reachable": False,
            "url": url,
            "error": str(exc)[:300],
            "hint": (
                "The appliance needs outbound HTTPS to content.cloudbolt.app. Configure the web "
                "proxy under Admin > Miscellaneous Settings if one is required, or add the "
                "inspecting CA under Admin > SSL Certificates."
            ),
        }


def _setup_wizard(base):
    try:
        from quick_setup.models import QuickSetup
    except Exception:
        return {"available": False}
    try:
        wizard = QuickSetup.get()
        optional = {}
        for key, _field in wizard.OPTIONAL_KEYS:
            optional[key] = wizard.effective_status(key)
        return {
            "available": True,
            "url": base + "/setup/",
            "hasResourceHandler": bool(wizard.has_resource_handler),
            "hasContentBlueprint": bool(wizard.has_content_blueprint),
            "optional": optional,
        }
    except Exception as exc:
        return {"available": False, "error": str(exc)[:200]}


def _environment_row(env, base):
    try:
        network_count = len(env.networks())
    except Exception:
        network_count = None
    flags = list(env.custom_fields.filter(name__in=ENV_PARAM_FLAGS).values_list("name", flat=True))
    return {
        "id": env.global_id,
        "pk": env.id,
        "name": env.name,
        "osBuilds": env.os_builds.count(),
        "networks": network_count,
        "groups": env.groups_served.count(),
        "lifecycleParameters": sorted(flags),
        "url": base + "/environments/{}/".format(env.id),
        "parametersUrl": base + "/environments/{}/#tab-parameters".format(env.id),
    }


def _handlers(base, state):
    from resourcehandlers.models import ResourceHandler

    verify_state = state.get("handlers") or {}
    rows = []
    for handler in ResourceHandler.objects.all().order_by("id"):
        try:
            handler = handler.cast()
        except Exception:
            pass
        tech = handler.resource_technology
        envs = [_environment_row(env, base) for env in handler.environment_set.all().order_by("id")]
        rows.append(
            {
                "id": handler.global_id,
                "pk": handler.id,
                "name": handler.name,
                "technology": tech.name if tech else None,
                "technologySlug": tech.slug if tech else None,
                "endpoint": handler.ip,
                "environments": envs,
                "lastVerify": (verify_state.get(handler.global_id) or {}).get("lastVerify"),
                "url": base + "/admin/resourcehandlers/{}/".format(handler.id),
                "verifyUrl": base + "/admin/resourcehandlers/{}/verify_connection/".format(handler.id),
                "importLocationsUrl": base + "/admin/resourcehandlers/{}/import_locations/".format(handler.id),
            }
        )
    return rows


def _custom_server(base):
    from orders.models import BlueprintOrderItem
    from servicecatalog.models import ServiceBlueprint

    name = getattr(settings, "DEFAULT_NEW_SERVER_BLUEPRINT_NAME", "Custom Server")
    blueprint = ServiceBlueprint.objects.filter(name=name).order_by("id").first()
    if blueprint is None:
        return {
            "name": name,
            "exists": False,
            "restoreUrl": base + "/recreate_new_server/",
            "hint": "The shipped Custom Server blueprint is missing; the restore URL recreates it.",
        }
    successful = (
        BlueprintOrderItem.objects.filter(blueprint=blueprint, order__status="SUCCESS")
        .values("order")
        .distinct()
        .count()
    )
    failed = (
        BlueprintOrderItem.objects.filter(blueprint=blueprint, order__status="FAILURE")
        .values("order")
        .distinct()
        .count()
    )
    try:
        url = base + blueprint.get_absolute_url()
    except Exception:
        url = None
    return {
        "name": name,
        "exists": True,
        "id": blueprint.global_id,
        "pk": blueprint.id,
        "status": blueprint.status,
        "isOrderable": bool(blueprint.is_orderable),
        "anyGroupCanDeploy": bool(blueprint.any_group_can_deploy),
        "successfulOrders": successful,
        "failedOrders": failed,
        "url": url,
    }


def _groups_users(base):
    from accounts.models import Group, UserProfile

    try:
        groups_qs = Group.without_unassigned.all()
    except AttributeError:
        groups_qs = Group.objects.exclude(name="Unassigned")
    groups = [
        {"id": g.global_id, "pk": g.id, "name": g.name, "url": base + "/groups/{}/".format(g.id)}
        for g in groups_qs.order_by("name")[:MAX_GROUPS]
    ]
    total_groups = groups_qs.count()
    users = UserProfile.objects.count()
    admins = UserProfile.objects.filter(super_admin=True).count()
    return {
        "groups": groups,
        "groupCount": total_groups,
        "groupsUrl": base + "/groups/",
        "userCount": users,
        "superAdminCount": admins,
        "usersUrl": base + "/users/",
        "newUserUrl": base + "/users/create/",
    }


def _auth(base):
    from utilities.models import LDAPUtility

    ldap_rows = []
    for ldap in LDAPUtility.objects.all().order_by("id"):
        ldap_rows.append(
            {
                "id": getattr(ldap, "global_id", None),
                "pk": ldap.id,
                "name": getattr(ldap, "name", None) or getattr(ldap, "ldap_domain", None),
                "server": ldap.ip,
                "port": ldap.port,
                "protocol": ldap.protocol,
                "url": base + "/admin/ldap_settings/{}/".format(ldap.id),
                "verifyUrl": base + "/admin/ldap_settings/{}/verify_connection/".format(ldap.id),
            }
        )
    sso_count = None
    try:
        from authentication.sso.models import BaseSSOProvider

        sso_count = BaseSSOProvider.objects.count()
    except Exception:
        pass
    return {
        "ldapUtilities": ldap_rows,
        "newLdapUrl": base + "/admin/ldap_settings/add/",
        "ssoProviders": sso_count,
    }


def _toolkit(base):
    from cbhooks.models import MCPToolAction

    present = {}
    for action in MCPToolAction.objects.filter(mcp_tool_name__in=TOOLKIT_TOOLS):
        present[action.mcp_tool_name] = {
            "id": action.global_id,
            "enabled": bool(action.enabled),
            "isSynchronous": bool(action.is_synchronous),
            "editUrl": base + "/actions/mcp_tool_actions/{}/edit/".format(action.id),
        }
    missing = [name for name in TOOLKIT_TOOLS if name not in present]
    return {"tools": present, "missing": missing, "listUrl": base + "/actions/mcp_tool_actions/"}


def _next_steps(report, base):
    steps = []
    items = report["pocItems"]

    def skipped(item_id):
        return (items.get(item_id) or {}).get("status") in ("skipped", "not_applicable", "done")

    if report["toolkit"]["missing"]:
        steps.append(
            {
                "step": "Sync the missing onboarding tools, then run onb_install and reconnect the MCP client.",
                "tools": report["toolkit"]["missing"],
                "url": report["toolkit"]["listUrl"],
            }
        )
    handlers = report["resourceHandlers"]
    if not handlers:
        steps.append(
            {
                "step": "Create the first resource handler in the UI (credentials are entered there), then run onb_verify_resource_handler.",
                "item": "P3",
                "url": base + "/admin/resourcehandlers/add/",
            }
        )
    for handler in handlers:
        verify = handler.get("lastVerify") or {}
        if verify and not verify.get("ok"):
            steps.append(
                {
                    "step": "Fix the connection of resource handler '{}': the last verify failed with '{}'. Re-run onb_verify_resource_handler afterwards.".format(
                        handler["name"], (verify.get("error") or "")[:160]
                    ),
                    "item": "P3",
                    "handler": handler["id"],
                    "url": handler["url"] + "edit/",
                }
            )
        elif not verify:
            steps.append(
                {
                    "step": "Verify the connection of resource handler '{}' with onb_verify_resource_handler.".format(
                        handler["name"]
                    ),
                    "item": "P3",
                    "handler": handler["id"],
                    "url": handler["url"],
                }
            )
        if not handler["environments"]:
            steps.append(
                {
                    "step": "Import one or two environments for '{}' (Import locations on the handler page).".format(
                        handler["name"]
                    ),
                    "item": "P3",
                    "handler": handler["id"],
                    "url": handler["importLocationsUrl"],
                }
            )
        for env in handler["environments"]:
            if env["osBuilds"] == 0:
                steps.append(
                    {
                        "step": "Add an OS build with an image for environment '{}'.".format(env["name"]),
                        "item": "P4",
                        "environment": env["id"],
                        "url": handler["url"],
                    }
                )
            if env["networks"] == 0:
                steps.append(
                    {
                        "step": "Attach a network (sc_nic_0) to environment '{}'.".format(env["name"]),
                        "item": "P4",
                        "environment": env["id"],
                        "url": env["url"],
                    }
                )
    custom = report["customServerBlueprint"]
    if not custom.get("exists"):
        steps.append({"step": "Restore the Custom Server blueprint.", "item": "P4", "url": custom.get("restoreUrl")})
    elif not custom.get("isOrderable") or custom.get("status") != "ACTIVE":
        steps.append({"step": "Enable the Custom Server blueprint so it can be ordered.", "item": "P4", "url": custom.get("url")})
    elif handlers and any(h["environments"] for h in handlers) and custom.get("successfulOrders", 0) == 0:
        steps.append(
            {
                "step": "Order the Custom Server blueprint through MCP: fetch_catalog_items, fetch_orderable_groups, fetch_blueprint_schema, order_catalog_item.",
                "item": "P4",
                "url": custom.get("url"),
            }
        )
    if report["groupsAndUsers"]["groupCount"] == 0:
        steps.append({"step": "Create a POC group and a non-admin test user.", "item": "2.3.1", "url": base + "/groups/"})
    if not report["preferences"]["smtp"]["configured"] and not skipped("P7"):
        steps.append(
            {
                "step": "Configure SMTP (needed for approval and completion emails), or record P7 as not applicable with onb_set_item_status.",
                "item": "P7",
                "url": report["preferences"]["smtp"]["editUrl"],
            }
        )
    if report["contentLibrary"].get("reachable") is False and not skipped("P5"):
        steps.append(
            {
                "step": "Restore outbound access to the Content Library (proxy or SSL inspection), or record P5 as not applicable.",
                "item": "P5",
                "url": report["preferences"]["proxy"]["editUrl"],
            }
        )
    return steps[:10]


def run(job, *args, **kwargs):
    profile = _caller(job, kwargs)
    if profile is None:
        return _failure("No calling user profile was supplied. Run this tool through the MCP server as an authenticated user.")
    if not profile.is_cbadmin:
        return _failure("Only CloudBolt admins may read the onboarding status.")

    base = _portal_url()
    state = _load_state()
    report = {
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "appliance": _appliance(base),
        "license": _license(),
        "preferences": _preferences(base),
        "contentLibrary": _content_library(),
        "setupWizard": _setup_wizard(base),
        "resourceHandlers": _handlers(base, state),
        "customServerBlueprint": _custom_server(base),
        "groupsAndUsers": _groups_users(base),
        "authentication": _auth(base),
        "toolkit": _toolkit(base),
        "pocItems": state.get("items") or {},
        "install": state.get("install"),
    }
    report["nextSteps"] = _next_steps(report, base)
    if not base:
        report["note"] = (
            "The default portal has no domain, so URLs are relative; prefix them with the appliance's https address."
        )
    handler_count = len(report["resourceHandlers"])
    env_count = sum(len(h["environments"]) for h in report["resourceHandlers"])
    message = "{} resource handler(s), {} environment(s), {} next step(s).".format(
        handler_count, env_count, len(report["nextSteps"])
    )
    return _success(message, report)
