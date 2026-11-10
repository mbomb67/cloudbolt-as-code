"""
Onboarding: Status (MCP Tool Action plugin).

Reports what the appliance has configured for a proof of concept in one
read: licence, portal URL, SMTP, proxy, Content Library reachability, the
Setup Wizard's derived step states, every resource handler with its
environments (OS builds, image guest credentials, networks, groups,
lifecycle parameters, key pairs, last verify result), the Expire Servers
job, the Custom Server blueprint, groups and users, LDAP and SSO, a summary
of the onboarding tools, the POC item statuses, the install record, and the
next steps with the UI link for each. Read-only, no inputs, synchronous.
Admin only.

The report is a summary: tools are counted with only the missing, disabled
and not-synchronous ones named, and POC items carry their status only
(onb_scorecard and onb_set_item_status return the notes). Credentials are
reported as booleans; no password, username or key material is returned.

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
# Tools that run as jobs; onb_install turns Synchronous Action on for every other toolkit tool (its TOOLS map).
ASYNC_TOOLS = {
    "onb_verify_resource_handler",
    "onb_install",
    "onb_handler_locations",
    "onb_import_environments",
    "onb_import_os_build",
    "onb_verify_ldap",
    "onb_smtp_test",
    "onb_install_content_library_item",
    "onb_sync_images",
    "onb_environment_networks",
    "onb_network_addressing",
}
INSTALL_STEP = "Run onb_install (asynchronous; poll the job), then reconnect the MCP client."
ENV_PARAM_FLAGS = ["hostname_template", "expiration_date", "power_schedule"]
EXPIRE_JOB_NAME = "Expire Servers"
MAX_GROUPS = 25
MAX_FAILED_SERVERS = 5
MAX_USERS = 20
MAX_KEYS = 10
# Guest credentials. A server's password comes from these parameters (ResourceHandler.get_server_credentials);
# provisioning copies the order's values and then the image's username, password and key_name onto the server.
GUEST_PASSWORD_PARAMS = ["password", "initial_password", "initial_linux_password", "initial_windows_password", "template_root_password"]
# The order form carries the guest password on these technologies, so an image without one is fine there.
ORDER_PASSWORD_TECH = {"azure_arm", "alibaba"}
# vCenter: new_password is the Windows guest password that guest customization sets. On "vmware" handlers
# scripts also run through VMware Tools (Server.use_tech_specific_script_execution), which needs a password:
# a key alone fails with "Cannot run script, server's password is unknown".
VCENTER_TECH = {"vmware", "vmc_aws"}
TOOLS_SCRIPT_TECH = {"vmware"}
# The handler page's tab that lists images, by handler type (resourcehandlers/<type>/views.py); the rest use "images".
IMAGES_TAB = {"vmware": "Templates", "proxmox": "Templates", "aws": "amis"}
SSH_KEY_STEPS = "Click the pencil in the Actions column of the key's row, paste the private key (PEM) into Key material, then Save."
SSH_KEY_WHY = (
    "CloudBolt needs it to sign in to Linux servers over SSH (remote scripts such as Standard Server's Hello World) and to read the Administrator password of Windows servers on AWS."
)
# Parameters the onboarding content creates: the Standard Server blueprint's (with the global id its sync stamps)
# and the ones the skill makes with onb_parameter (recorded in the state as toolkitParameters).
CONTENT_PARAMETERS = {"application": "CF-15yw228m", "maintenance_window": "CF-cei72k8x"}
PLAN_PARAMETERS = ("backup_policy", "cost_center")
PASSWORD_FIX_STEPS = (
    "CloudBolt asks for the user's current password before it changes one, and this user has none. On the user's page click Delete and confirm, then on the Users list click Add, enter the user again with Password and Confirm password, and click Create."
)


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


def _local_to_utc(value):
    """A naive database datetime as aware UTC. CloudBolt runs with USE_TZ=False, so it is appliance-local time."""
    if value is None:
        return None
    if value.tzinfo is not None:
        return value.astimezone(timezone.utc)
    try:
        from zoneinfo import ZoneInfo

        return value.replace(tzinfo=ZoneInfo(settings.TIME_ZONE)).astimezone(timezone.utc)
    except Exception:
        return value.astimezone(timezone.utc)  # a naive datetime is read as the process's local time


def _parse_utc(text):
    """An ISO string the toolkit stored with datetime.now(timezone.utc), as aware UTC; None when unreadable."""
    try:
        value = datetime.fromisoformat(str(text))
    except (TypeError, ValueError):
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


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
            "editUrl": base + "/admin/email_settings/",
            "editClick": "Edit on the Overview tab",
        },
        "proxy": {
            "configured": gp.web_proxy_id is not None,
            "editUrl": base + "/admin/misc_settings/#web-proxy-pane",
            "editClick": "Edit next to Settings on the Web Proxy tab",
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


def _ssh_keys(env, handler):
    """Key pairs the environment offers and whether CloudBolt holds each one's private key (the handler's SSH Keys tab)."""
    from common.methods import find_key_material

    field = getattr(handler, "ssh_keyname_field_name", None) if handler else None
    if not field:
        return []
    names = env.custom_field_options.filter(field__name=field).values_list("str_value", flat=True).distinct()
    rows = []
    for name in list(names)[:MAX_KEYS]:
        if name:
            have, _material = find_key_material(name, handler)  # the material itself is never returned
            rows.append({"name": name, "materialUploaded": bool(have)})
    return rows


def _image_credentials(osb, osba, slug):
    """Which guest credentials the image (OSBuildAttribute) carries, as booleans; the values are never returned."""
    creds = osba.get_credentials()  # username, password and keyfile parameters on the image
    row = {"osBuild": osb.name, "image": getattr(osba, "global_id", None) or osba.template_name}
    if osb.is_windows():
        row["windows"] = True
    row["username"] = bool(creds.get("username"))
    if not slug.startswith("aws"):  # on AWS the key pair is the environment's (sshKeys)
        row["password"] = bool(creds.get("password"))
        row["key"] = bool(creds.get("keyfile"))
    # Not output: the template name, and whether a server without a username would sign in as root
    # (get_server_credentials defaults to Administrator on Windows, ubuntu on the Ubuntu family, else root).
    row["_template"] = osba.template_name or row["image"]
    row["_rootDefault"] = not osb.is_windows() and getattr(osb.os_family, "name", None) != "Ubuntu"
    return row


def _new_password_set(env):
    """Whether the environment offers New Password (new_password) with a value; the value is only tested, never returned."""
    if not env.custom_fields.filter(name="new_password").exists():
        return False
    for cfv in env.custom_field_options.filter(field__name="new_password"):
        if cfv.value:
            return True
    return False


def _script_path(env):
    """How scripts reach a vCenter server: VMware Tools (tech-specific) or SSH/WinRM, and the WinRM protocol."""
    tech = None
    for cfv in env.custom_field_options.filter(field__name="tech_specific_script_execution"):
        tech = bool(cfv.value)
    if tech is None:
        tech = bool(getattr(settings, "TECH_SPECIFIC_SCRIPT_EXECUTION", True))
    path = {"techSpecific": tech}
    if not tech:
        protocol = None
        for cfv in env.custom_field_options.filter(field__name="winrm_protocol"):
            protocol = str(cfv.value or "").lower() or None
        path["winrmProtocol"] = protocol or "https"
        path["ports"] = "SSH 22 (Linux) and WinRM {} (Windows) from the appliance to the servers".format(
            5985 if protocol == "http" else 5986)
    return path


def _environment_row(env, base, handler, slug):
    try:
        network_count = len(env.networks())
    except Exception:
        network_count = None
    flags = list(env.custom_fields.filter(name__in=ENV_PARAM_FLAGS).values_list("name", flat=True))
    problems = []
    guest = []
    try:
        for osb in env.os_builds.all():
            try:
                osba = osb.osba_for_resource_handler(handler, environment=env) if handler is not None else None
                if handler is not None and osba is None:
                    problems.append({"osBuild": osb.name, "problem": "no image of this OS build for the environment"})
            except Exception as exc:
                problems.append({"osBuild": osb.name, "problem": str(exc)[:300]})
                continue
            if osba is not None:
                try:
                    guest.append(_image_credentials(osb, osba, slug))
                except Exception:
                    pass
    except Exception:
        pass
    row = {
        "id": env.global_id,
        "name": env.name,
        "osBuilds": env.os_builds.count(),
        "osBuildProblems": problems,
        "guestCredentials": guest,
        "networkProblems": _network_problems(env),
        "networks": network_count,
        # Group.environments (Environment.groups_served is deprecated); Unassigned is the placeholder for restricted new environments.
        "groups": env.group_set.exclude(name="Unassigned").count(),
        "lifecycleParameters": sorted(flags),
        "sshKeys": _ssh_keys(env, handler),
        "url": base + "/environments/{}/".format(env.id),
        "parametersUrl": base + "/environments/{}/#tab-parameters".format(env.id),
    }
    try:
        # Not output: whether servers get a guest password from the order instead of the image.
        row["_orderPassword"] = slug in ORDER_PASSWORD_TECH or env.custom_fields.filter(name__in=GUEST_PASSWORD_PARAMS).exists()
        if slug in VCENTER_TECH:
            row["newPasswordSet"] = _new_password_set(env)
            row["scriptPath"] = _script_path(env)
    except Exception:
        pass
    return row


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


def _images_tab(handler):
    """(fragment, label) of the handler page's image tab: tabs/views.py makes the pane id 'tab-' + the tab's slug."""
    try:
        type_slug = handler.type_slug
    except Exception:
        type_slug = ""
    fragment = IMAGES_TAB.get(type_slug, "images")
    if fragment == "amis":
        return fragment, "AMIs"
    return fragment, "{}s".format(getattr(handler, "template_label", None) or "image").title()


def _last_verify(entry):
    """The verify result onb_verify_resource_handler recorded, with the error cut to 300 characters."""
    verify = (entry or {}).get("lastVerify")
    if not verify:
        return verify
    error = verify.get("error")
    return {"ok": verify.get("ok"), "when": verify.get("when"), "error": error[:300] if error else None}


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
        envs = [_environment_row(env, base, handler, slug or "") for env in handler.environment_set.all().order_by("id")]
        fragment, images_label = _images_tab(handler)
        rows.append(
            {
                "id": handler.global_id,
                "name": handler.name,
                "technology": tech.name if tech else None,
                "technologySlug": slug,
                "endpoint": handler.ip,
                "environments": envs,
                "cachedImages": _cached_images(handler, slug),
                "lastVerify": _last_verify(verify_state.get(handler.global_id)),
                "url": base + "/admin/resourcehandlers/{}/".format(handler.id),
                "editClick": "Edit Credentials on the Overview tab",
                "importLocationsUrl": base + "/admin/resourcehandlers/{}/#tab-locations".format(handler.id),
                "sshKeysUrl": base + "/admin/resourcehandlers/{}/#tab-ssh-keys".format(handler.id),
                "imagesUrl": base + "/admin/resourcehandlers/{}/#tab-{}".format(handler.id, fragment),
                # Not output: the tab's label and what the handler calls an image, for the step text.
                "_imagesTab": images_label,
                "_imageLabel": "AMI" if fragment == "amis" else (getattr(handler, "template_label", None) or "image").lower(),
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


def _users(base):
    """Local users, newest first, each with its own page; passwordNeeded marks one an earlier onb_user created without a password."""
    from accounts.models import GroupRoleMembership, UserProfile

    rows = []
    for up in UserProfile.objects.filter(ldap__isnull=True).select_related("user").order_by("-id")[:MAX_USERS]:
        user = up.user
        usable = user.has_usable_password()
        rows.append(
            {
                "id": up.global_id,
                "username": user.username,
                "name": "{} {}".format(user.first_name, user.last_name).strip(),
                "superAdmin": bool(up.super_admin),
                "roles": [
                    "{}: {}".format(m.group.name, m.role.name)
                    for m in GroupRoleMembership.objects.filter(profile=up).select_related("group", "role")
                ],
                "passwordSet": usable,
                # SSO users have no usable password either, but they have logged in.
                "passwordNeeded": not usable and user.last_login is None,
                "url": base + "/users/{}/".format(up.id),
            }
        )
    return rows


def _parameter_conflicts(base, state):
    """Parameters the onboarding content would use that already exist and are not the toolkit's own."""
    from infrastructure.models import CustomField

    owned = set(state.get("toolkitParameters") or [])
    rows = []
    for name in list(CONTENT_PARAMETERS) + list(PLAN_PARAMETERS):
        cf = CustomField.objects.filter(name=name).first()
        if cf is None or name in owned or CONTENT_PARAMETERS.get(name) == cf.global_id:
            continue
        rows.append(
            {
                "name": name,
                "label": cf.label,
                "type": cf.type,
                "id": cf.global_id,
                "usedFor": "Standard Server blueprint sync" if name in CONTENT_PARAMETERS else "onb_parameter",
                "url": base + "/admin/customfields/{}/".format(cf.id),
            }
        )
    return rows


def _groups_users(base):
    from accounts.models import Group, UserProfile

    try:
        groups_qs = Group.without_unassigned.all()
    except AttributeError:
        groups_qs = Group.objects.exclude(name="Unassigned")
    groups = [
        {"id": g.global_id, "name": g.name, "url": base + "/groups/{}/".format(g.id)}
        for g in groups_qs.order_by("name")[:MAX_GROUPS]
    ]
    total_groups = groups_qs.count()
    users = UserProfile.objects.count()
    admins = UserProfile.objects.filter(super_admin=True).count()
    local_users = _users(base)
    result = {
        "groups": groups,
        "groupCount": total_groups,
        "groupsUrl": base + "/groups/",
        "userCount": users,
        "superAdminCount": admins,
        "users": local_users,
        "usersUrl": base + "/users/",
    }
    if any(u["passwordNeeded"] for u in local_users):
        result["passwordFixSteps"] = PASSWORD_FIX_STEPS
    return result


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
                "editClick": "Credentials for the bind password, Settings for the rest",
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
        "newLdapUrl": base + "/admin/ldap_settings/",
        "newLdapClick": "Add",
        "ssoProviders": sso_count,
    }


def _toolkit(base):
    """Counts the toolkit's tools and names only the missing, disabled and not-synchronous ones.

    lastSync is the newest last_cached of the tools and their plugins: SourceCodeRepoMixin.refresh_from_remote_source
    stamps it on every refresh from a repository (a first import leaves it empty).
    """
    from cbhooks.models import MCPToolAction

    present, disabled, not_sync, synced = set(), [], [], []
    rows = MCPToolAction.objects.filter(mcp_tool_name__in=TOOLKIT_TOOLS).values_list(
        "mcp_tool_name", "enabled", "is_synchronous", "last_cached", "hook__last_cached"
    )
    for name, enabled, is_sync, cached, hook_cached in rows:
        present.add(name)
        if not enabled:
            disabled.append(name)
        if name not in ASYNC_TOOLS and not is_sync:
            not_sync.append(name)
        synced.extend(t for t in (_local_to_utc(cached), _local_to_utc(hook_cached)) if t is not None)
    return {
        "count": len(present),
        "missing": [name for name in TOOLKIT_TOOLS if name not in present],
        "disabled": sorted(disabled),
        "notSynchronous": sorted(not_sync),
        "lastSync": max(synced).isoformat() if synced else None,
        "listUrl": base + "/actions/mcp_tool_actions/",
    }


def _install(state, toolkit):
    """The record onb_install saved, compacted, with whether a toolkit sync came after it."""
    record = state.get("install")
    if not isinstance(record, dict):
        return None
    egress = record.get("egress") or {}
    result = {
        "toolkitVersion": record.get("toolkitVersion"),
        "when": record.get("when"),
        "by": record.get("by"),
        "missing": record.get("missing") or [],
        "egress": {name: (check or {}).get("reachable") for name, check in egress.items() if isinstance(check, dict)},
    }
    installed = _parse_utc(record.get("when"))
    synced = _parse_utc(toolkit.get("lastSync"))
    result["olderThanToolkitSync"] = bool(installed and synced and synced > installed)
    return result


def _lifecycle_jobs(base):
    """The Expire Servers recurring job, which acts on expiration_date (seeded disabled)."""
    from jobs.models import RecurringJob

    job = RecurringJob.objects.filter(name=EXPIRE_JOB_NAME).first()
    if job is None:
        expire = {"exists": False, "listUrl": base + "/recurring_jobs/"}
    else:
        expire = {"enabled": bool(job.enabled), "url": base + job.get_absolute_url()}
    return {"expireServers": expire}


def _next_steps(report, base):
    steps = []
    items = report["pocItems"]

    def skipped(item_id):
        return items.get(item_id) in ("skipped", "not_applicable", "done")

    toolkit = report["toolkit"]
    install = report["install"] or {}
    if toolkit["missing"]:
        steps.append(
            {
                "step": "Sync the missing onboarding tools, then run onb_install and reconnect the MCP client.",
                "tools": toolkit["missing"],
                "url": toolkit["listUrl"],
            }
        )
    elif toolkit["disabled"] or toolkit["notSynchronous"] or install.get("olderThanToolkitSync"):
        reasons = []
        if toolkit["disabled"]:
            reasons.append("disabled: " + ", ".join(toolkit["disabled"]))
        if toolkit["notSynchronous"]:
            reasons.append("not synchronous: " + ", ".join(toolkit["notSynchronous"]))
        if install.get("olderThanToolkitSync"):
            reasons.append("the install record ({}) is older than the last toolkit sync ({})".format(install.get("when"), toolkit["lastSync"]))
        steps.append({"step": INSTALL_STEP + " Why: " + "; ".join(reasons) + ".", "url": toolkit["listUrl"]})
    handlers = report["resourceHandlers"]
    if not handlers:
        steps.append(
            {
                "step": "Create the first resource handler in the UI (credentials are entered there): the link opens the technology chooser. Then run onb_verify_resource_handler.",
                "item": "P3",
                "url": base + "/setup/add-resource-handler/",
            }
        )
    guest_seen = set()  # one step per image, however many environments offer it
    advice = []  # steps that keep things tidy but block nothing, listed last
    for handler in handlers:
        verify = handler.get("lastVerify") or {}
        if verify and not verify.get("ok"):
            steps.append(
                {
                    "step": "Fix the connection of resource handler '{}': the last verify failed with '{}'. On the handler's page, "
                    "click Edit Credentials on the Overview tab. Re-run onb_verify_resource_handler afterwards.".format(
                        handler["name"], (verify.get("error") or "")[:160]
                    ),
                    "item": "P3",
                    "handler": handler["id"],
                    "url": handler["url"],
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
            for key in env.get("sshKeys") or []:
                if not key["materialUploaded"]:
                    steps.append(
                        {
                            "step": "Have the admin upload the private key of key pair '{}', which '{}' offers: on the handler's SSH Keys tab, {} {}".format(
                                key["name"], env["name"], SSH_KEY_STEPS[0].lower() + SSH_KEY_STEPS[1:], SSH_KEY_WHY),
                            "item": "P4",
                            "environment": env["id"],
                            "url": handler["sshKeysUrl"],
                        }
                    )
            for image in env.get("guestCredentials") or []:
                if image["image"] in guest_seen:
                    continue
                if "password" not in image:  # AWS: the key is the environment's, the username is the image's
                    if not image["username"] and image.get("_rootDefault"):
                        guest_seen.add(image["image"])
                        steps.append(
                            {
                                "step": "SSH scripts on servers built from OS build '{}' would sign in as root, which AWS images normally refuse: "
                                "AMI '{}' has no username. Have the admin set it on the handler's AMIs tab: in that AMI's row, "
                                "Credentials column, click the + (Add credentials) next to None configured, enter the image's login "
                                "user as Username (ec2-user on Amazon Linux), then Save.".format(image["osBuild"], image["_template"]),
                                "item": "P4",
                                "environment": env["id"],
                                "url": handler["imagesUrl"],
                            }
                        )
                    continue
                if env.get("_orderPassword"):
                    continue
                if image.get("windows") and env.get("newPasswordSet"):
                    continue  # vCenter guest customization saves new_password as the Windows server's password
                # VMware Tools needs a password; SSH also takes a key, which a Windows image cannot carry.
                password_only = handler["technologySlug"] in TOOLS_SCRIPT_TECH or image.get("windows")
                if image["password"] or (image["key"] and not password_only):
                    continue
                guest_seen.add(image["image"])
                steps.append(
                    {
                        "step": "Remote scripts on servers built from OS build '{}' would fail{}: {} '{}' has no guest {}. "
                        "Have the admin enter it on the handler's {} tab: in that {}'s row, Credentials column, click the + (Add "
                        "credentials) next to None configured, or the pencil (Edit credentials) when a username or key is set; enter Username and Password{}, then Save. "
                        "Never ask for the password in chat.".format(
                            image["osBuild"],
                            " with 'Cannot run script, server's password is unknown'"
                            if handler["technologySlug"] in TOOLS_SCRIPT_TECH else "",
                            handler["_imageLabel"],
                            image["_template"],
                            "password" if password_only else "password or SSH key",
                            handler["_imagesTab"],
                            handler["_imageLabel"],
                            "" if password_only else " (or choose an SSH key)",
                        ),
                        "item": "P4",
                        "environment": env["id"],
                        "url": handler["imagesUrl"],
                    }
                )
            if env.get("newPasswordSet") is False:
                windows = [i["osBuild"] for i in env.get("guestCredentials") or [] if i.get("windows")]
                if windows:
                    advice.append(
                        {
                            "step": "Advice, not a blocker: environment '{}' offers Windows OS build '{}' without New Password. Builds "
                            "pass on the {}'s password, but New Password is the Windows guest password CloudBolt sets at build and "
                            "stores for the server, so setting it keeps the two in agreement. Have the admin open the environment's "
                            "Parameters tab; unless New Password is listed, click Add, choose New Password and click Add; then click "
                            "the pencil in its Options column, enter the password and Save. Use the {}'s password unless the customer "
                            "wants it changed at build. Never ask for it in chat.".format(
                                env["name"], windows[0], handler["_imageLabel"], handler["_imageLabel"]),
                            "item": "P4",
                            "environment": env["id"],
                            "url": env["parametersUrl"],
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
    for user in reversed(report["groupsAndUsers"]["users"]):  # oldest first: the requestor is created before the approver
        if user["passwordNeeded"]:
            steps.append(
                {
                    "step": "User '{}'{} has no password: {} Then run onb_user again with its group and roles to "
                    "restore them. One user per message; check passwordSet here before sending the next.".format(
                        user["username"],
                        " ({})".format(", ".join(user["roles"])) if user["roles"] else "",
                        PASSWORD_FIX_STEPS,
                    ),
                    "item": "2.3.1",
                    "user": user["id"],
                    "url": user["url"],
                }
            )
    expire = (report["lifecycleJobs"] or {}).get("expireServers") or {}
    uses_expiration = any(
        "expiration_date" in env["lifecycleParameters"] for handler in handlers for env in handler["environments"]
    )
    if uses_expiration and expire.get("enabled") is False:
        steps.append(
            {
                "step": "The Expire Servers recurring job is disabled, so expiration_date never acts on expired servers. It must be "
                "enabled for expiration to work: enable it (onb_parameter does this when it attaches expiration_date), or have "
                "the admin turn on the Enabled switch at the top right of the job's Overview tab.",
                "item": "3.1.2",
                "url": expire.get("url"),
            }
        )
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
    return (steps + advice)[:10]


def _strip_private(handlers):
    """Drop the underscore keys the next steps read (template names, tab labels) from the output."""
    for handler in handlers:
        for key in [k for k in handler if k.startswith("_")]:
            del handler[key]
        for env in handler["environments"]:
            for key in [k for k in env if k.startswith("_")]:
                del env[key]
            for image in env.get("guestCredentials") or []:
                image.pop("_template", None)
                image.pop("_rootDefault", None)


def _poc_items(state):
    """Each recorded POC item's status; the notes stay in the state."""
    items = state.get("items") or {}
    return {item_id: (entry.get("status") if isinstance(entry, dict) else entry) for item_id, entry in items.items()}


def run(job, *args, **kwargs):
    profile = _caller(job, kwargs)
    if profile is None:
        return _failure("No calling user profile was supplied. Run this tool through the MCP server as an authenticated user.")
    if not profile.is_cbadmin:
        return _failure("Only CloudBolt admins may read the onboarding status.")

    base = _portal_url()
    state = _load_state()
    toolkit = _toolkit(base)
    try:
        lifecycle_jobs = _lifecycle_jobs(base)
    except Exception as exc:
        lifecycle_jobs = {"error": str(exc)[:200]}
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
        "parameterConflicts": _parameter_conflicts(base, state),
        "groupsAndUsers": _groups_users(base),
        "authentication": _auth(base),
        "lifecycleJobs": lifecycle_jobs,
        "toolkit": toolkit,
        "pocItems": _poc_items(state),
        "pocItemsNote": "Statuses only; onb_scorecard or onb_set_item_status returns each item's note.",
        "install": _install(state, toolkit),
    }
    report["nextSteps"] = _next_steps(report, base)
    _strip_private(report["resourceHandlers"])
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
