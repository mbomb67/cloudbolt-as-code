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

TOOLKIT_VERSION = "1.0.0"
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
    "onb_handler_locations",
    "onb_import_environments",
    "onb_trim_environment",
    "onb_import_os_build",
    "onb_configure_custom_server",
    "onb_remove_environment",
    "onb_failed_servers",
    "onb_parameter",
    "onb_group",
    "onb_user",
    "onb_action_approval",
    "onb_tag_mapping",
    "onb_ldap_mapping",
    "onb_verify_ldap",
    "onb_smtp",
    "onb_smtp_test",
    "onb_install_content_library_item",
    "onb_cit_from_order",
    "onb_scorecard",
    "onb_sync_images",
    "onb_environment_networks",
    "onb_network_addressing",
    "sync_from_source_control_repo",
    "fetch_job_log",
    "manage_blueprint_permissions",
    "run_recurring_job",
    "run_cit_tests",
]
ENV_PARAM_FLAGS = ["hostname_template", "expiration_date", "power_schedule"]
MAX_GROUPS = 25
MAX_FAILED_SERVERS = 10


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


# Technologies whose networks carry addressing CloudBolt sends to the guest (the others let the cloud assign IPs).
ADDRESSING_TECH = {"vmware", "vmc_aws", "scvmm", "hyperv", "acropolis", "h3ccas", "vcloud_director", "azurelocal"}
NO_NETWORK_TECH = {"proxmox", "metalsoft", "orka", "ipmi"}


def _network_problems(env):
    """What would make an order's NIC 1 fail: no network, too many, static without gateway and netmask, a full pool."""
    problems = []
    handler = env.resource_handler
    tech = getattr(handler, "resource_technology", None) if handler else None
    slug = (getattr(tech, "slug", None) or "") if tech else ""
    if slug in NO_NETWORK_TECH:
        return problems
    nets = []
    for cfv in env.custom_field_options.filter(field__name="sc_nic_0").select_related("network_value"):
        if cfv.network_value is not None:
            nets.append(cfv.network_value)
    if not nets:
        problems.append("no network on NIC 1")
        return problems
    if len(nets) > 5:
        problems.append("{} networks on NIC 1; choose up to 5".format(len(nets)))
    if env.custom_field_options.filter(field__name__regex=r"^sc_nic_([1-9]|[1-9][0-9])$").exists():
        problems.append("networks on more than one NIC")
    if slug not in ADDRESSING_TECH:
        return problems
    for net in nets:
        try:
            provider = net.determine_ip_provider()
        except Exception:
            provider = None
        if provider in ("user_static", "user_static_or_dhcp", "pool", "ipam") and not (net.gateway and net.netmask):
            problems.append("network '{}' is static without gateway and netmask".format(net.name))
        if provider == "pool":
            try:
                rp = net.get_resource_pool()
                if rp is not None and not rp.get_values().filter(server=None, resource=None).exists():
                    problems.append("network '{}' has a full IP pool".format(net.name))
            except Exception:
                pass
        if provider == "ipam":
            try:
                if net.ipam_network.ipam_set.first() is None:
                    problems.append("network '{}' is mapped to an IPAM network with no IPAM endpoint".format(net.name))
            except Exception:
                pass
    return problems


def _environment_row(env, base):
    try:
        network_count = len(env.networks())
    except Exception:
        network_count = None
    flags = list(env.custom_fields.filter(name__in=ENV_PARAM_FLAGS).values_list("name", flat=True))
    problems = []
    try:
        handler = env.resource_handler.cast() if env.resource_handler else None
        for osb in env.os_builds.all():
            try:
                if handler is not None and osb.osba_for_resource_handler(handler, environment=env) is None:
                    problems.append({"osBuild": osb.name, "problem": "no image of this OS build for the environment"})
            except Exception as exc:
                problems.append({"osBuild": osb.name, "problem": str(exc)[:300]})
    except Exception:
        pass
    return {
        "id": env.global_id,
        "pk": env.id,
        "name": env.name,
        "osBuilds": env.os_builds.count(),
        "osBuildProblems": problems,
        "networkProblems": _network_problems(env),
        "networks": network_count,
        "groups": env.groups_served.count(),
        "lifecycleParameters": sorted(flags),
        "url": base + "/environments/{}/".format(env.id),
        "parametersUrl": base + "/environments/{}/#tab-parameters".format(env.id),
    }


def _cached_images(handler, slug):
    """How many images the handler's cache holds; None when the technology keeps none."""
    try:
        if (slug or "").startswith("aws"):
            from resourcehandlers.aws.models import AwsCachedAMI

            return AwsCachedAMI.objects.for_resource_handler(resource_handler=handler).distinct().count()
        if slug == "azure_arm":
            from resourcehandlers.azure_arm.models import AzureARMAvailableImage

            return AzureARMAvailableImage.objects.for_resource_handler(handler).distinct().count()
        if slug == "vmware":
            from resourcehandlers.vmware.models import VMWareAvailableImage

            return VMWareAvailableImage.objects.filter(resource_handler=handler).count()
    except Exception:
        return None
    return None


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
        slug = tech.slug if tech else None
        envs = [_environment_row(env, base) for env in handler.environment_set.all().order_by("id")]
        rows.append(
            {
                "id": handler.global_id,
                "pk": handler.id,
                "name": handler.name,
                "technology": tech.name if tech else None,
                "technologySlug": slug,
                "endpoint": handler.ip,
                "environments": envs,
                "cachedImages": _cached_images(handler, slug),
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


def _failed_servers(base):
    """Servers a failed order left in Provision Failed state; onb_failed_servers lists and deletes them."""
    from infrastructure.models import Server

    failed = Server.objects.filter(status="PROVFAILED").select_related("environment")
    rows = [
        {
            "id": s.global_id,
            "hostname": s.hostname,
            "environment": s.environment.name if s.environment else None,
            "created": _iso(s.add_date),
            "url": base + "/servers/{}/".format(s.id),
        }
        for s in failed.order_by("-add_date")[:MAX_FAILED_SERVERS]
    ]
    return {"count": failed.count(), "servers": rows}


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
                "step": "Create the first resource handler in the UI (credentials are entered there): the link opens the technology chooser. Then run onb_verify_resource_handler.",
                "item": "P3",
                "url": base + "/setup/add-resource-handler/",
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
        if verify.get("ok") and handler.get("cachedImages") == 0:
            steps.append(
                {
                    "step": "Fill the image cache of '{}' with onb_sync_images (no whitelist; also enables the cache jobs); it runs for a while.".format(
                        handler["name"]
                    ),
                    "item": "P4",
                    "handler": handler["id"],
                    "url": handler["url"],
                }
            )
        if not handler["environments"]:
            steps.append(
                {
                    "step": "Import the one environment the POC needs for '{}' with onb_handler_locations and onb_import_environments (AWS: the one VPC the user picks).".format(
                        handler["name"]
                    ),
                    "item": "P3",
                    "handler": handler["id"],
                    "url": handler["importLocationsUrl"],
                }
            )
        for env in handler["environments"]:
            for problem in env.get("osBuildProblems") or []:
                steps.append(
                    {
                        "step": "Orders in '{}' with OS build '{}' would fail: {}. Fix it with onb_import_os_build (onb_replace_existing=true replaces a duplicate image after the user agrees).".format(
                            env["name"], problem["osBuild"], problem["problem"]),
                        "item": "P4",
                        "environment": env["id"],
                        "url": handler["url"],
                    }
                )
            for problem in env.get("networkProblems") or []:
                steps.append(
                    {
                        "step": "Environment '{}': {}. Choose its networks with onb_environment_networks and set their addressing with onb_network_addressing.".format(
                            env["name"], problem),
                        "item": "P4",
                        "environment": env["id"],
                        "url": env["url"],
                    }
                )
            if env["osBuilds"] == 0:
                steps.append(
                    {
                        "step": "Add an OS build with an image for environment '{}' (onb_import_os_build; empty onb_image lists the cache).".format(env["name"]),
                        "item": "P4",
                        "environment": env["id"],
                        "url": handler["url"],
                    }
                )
    failed = report["failedServers"]
    if failed["count"]:
        steps.append(
            {
                "step": "{} server(s) are left in Provision Failed state by failed orders. Once the cause is known, list them with "
                "onb_failed_servers and offer the user to delete them (it removes their VMs from the cloud too).".format(failed["count"]),
                "url": failed["servers"][0]["url"],
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
        "failedServers": _failed_servers(base),
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
