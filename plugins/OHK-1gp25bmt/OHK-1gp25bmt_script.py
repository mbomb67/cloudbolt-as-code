"""
POC Onboarding: UI Link (MCP Tool Action plugin).

Turns a CloudBolt global id (RH-, ENV-, GRP-, USR-, BP-, ORD-, JOB-, SVR-,
RSC-, MTA-, ...) or a named admin page into an absolute UI link, so the agent
can send the user to the exact page where a secret must be entered or a
handler, environment, group or parameter is edited. Forms that CloudBolt
opens as a dialog (edit a user, SMTP settings, handler credentials) render
without any page around them when opened directly, so their links go to the
page that holds the button, and the result names the button in click.
Named pages take an optional argument after a colon, for example
new_resource_handler:aws or group_quotas:GRP-abc12345. Read-only,
synchronous. Admin only.

Every declared input is read from its rendered template token in
_read_inputs(); keep each one referenced that way or CloudBolt's token scan
deletes the input when the script is saved.
"""
import html
import importlib
import re

GLOBAL_ID_RE = re.compile(r"^[A-Z]{2,4}-[A-Za-z0-9]{6,16}$")

# prefix -> [(module, class)]; the first model holding the global id wins.
REGISTRY = {
    "SVR": [("infrastructure.models", "Server")],
    "ENV": [("infrastructure.models", "Environment")],
    "GRP": [("accounts.models", "Group")],
    "USR": [("accounts.models", "UserProfile")],
    "JOB": [("jobs.models", "Job")],
    "ORD": [("orders.models", "Order")],
    "BP": [("servicecatalog.models", "ServiceBlueprint")],
    "RSC": [("resources.models", "Resource")],
    "RES": [("resources.models", "Resource")],
    "RH": [("resourcehandlers.models", "ResourceHandler")],
    "NET": [("resourcehandlers.models", "ResourceNetwork")],
    "OSB": [("externalcontent.models", "OSBuild")],
    "APP": [("externalcontent.models", "Application")],
    "IMG": [("externalcontent.models", "OSBuildAttribute")],
    "RJB": [("jobs.models", "RecurringJob")],
    "SVA": [("cbhooks.models", "ServerAction")],
    "RSA": [("cbhooks.models", "ResourceAction")],
    "OHK": [("cbhooks.models", "OrchestrationHook")],
    "MTA": [("cbhooks.models", "MCPToolAction")],
    "CIT": [("cscv.models", "CITTest")],
    "SCR": [("source_code_repos.models", "SourceCodeRepo")],
    "IDD": [("utilities.models", "LDAPUtility")],
    "CON": [("utilities.models", "ConnectionInfo")],
    "CF": [("infrastructure.models", "CustomField")],
}

# Fallback paths for models whose get_absolute_url is missing or not a detail page.
FALLBACK_PATHS = {
    "ResourceHandler": "/admin/resourcehandlers/{pk}/",
    "Environment": "/environments/{pk}/",
    "Group": "/groups/{pk}/",
    "UserProfile": "/users/{pk}/",
    "Job": "/jobs/{pk}/",
    "Order": "/orders/{pk}/",
    "ServiceBlueprint": "/catalog/{pk}/",
    "Server": "/servers/{pk}/",
    "Resource": "/resources/{pk}/",
    "LDAPUtility": "/admin/ldap_settings/{pk}/",
    "MCPToolAction": "/actions/mcp_tool_action/{pk}/",
    "CustomField": "/admin/customfields/{pk}/",
    "SourceCodeRepo": "/source_code_repos/{pk}/",
    "ConnectionInfo": "/admin/connectioninfo/{pk}/",
}

# The Setup Wizard's Add infrastructure page: the resource handler list with the technology
# chooser opened on load. The list page's add_by_tech/<slug>/ URL is meant to open the
# technology's form directly, but its auto-open script does not fire, so new handlers start
# here and the user clicks the technology. Older releases without the Setup Wizard page get
# the add_by_tech URL instead (_handler_chooser).
SETUP_ADD_HANDLER = "/setup/add-resource-handler/"

# Named pages. {arg} is the text after the colon in the target.
PAGES = {
    "dashboard": ("/dashboard/", "CloudBolt dashboard"),
    "setup_wizard": ("/setup/", "First-run Setup Wizard"),
    "resource_handlers": ("/admin/resourcehandlers/", "Resource handler list"),
    "new_resource_handler": (SETUP_ADD_HANDLER, "Resource handler list with the technology chooser open"),
    "new_resource_handler:{arg}": (
        SETUP_ADD_HANDLER,
        "Technology chooser for a new handler; the user clicks the technology in clickTechnology "
        "(aws, azure_arm, vmware, scvmm, gcp, acropolis, openstack)",
    ),
    "handler_verify:{arg}": ("/admin/resourcehandlers/{pk}/", "A handler's page, to run Verify Connection (RH- id)"),
    "handler_edit:{arg}": ("/admin/resourcehandlers/{pk}/", "A handler's page, to edit its credentials (RH- id)"),
    "handler_import_locations:{arg}": ("/admin/resourcehandlers/{pk}/#tab-locations", "A handler's Locations tab, to import locations or clusters as environments (RH- id)"),
    "environments": ("/environments/", "Environment list"),
    "new_ipam": ("/setup/add-ipam/", "Add an IPAM endpoint (Infoblox, BlueCat, phpIPAM, SolarWinds); credentials are entered there"),
    "ipam_list": ("/ipam/", "IPAM endpoints"),
    "ipam:{arg}": ("/ipam/{pk}/#tab-networks", "An IPAM endpoint's Networks tab, to import and map networks (numeric IPAM id)"),
    "network:{arg}": ("/networks/{pk}/", "A network's page with its addressing and IP Pool tab (NET- id or numeric id)"),
    "environment_parameters:{arg}": ("/environments/{pk}/#tab-parameters", "An environment's Parameters tab (ENV- id)"),
    "ldap": ("/admin/ldap_settings/", "LDAP utilities"),
    "new_ldap": ("/admin/ldap_settings/", "LDAP utilities, to add one (the service account password is entered there)"),
    "ldap_verify:{arg}": ("/admin/ldap_settings/{pk}/", "An LDAP utility's page, to verify it (IDD- id or pk)"),
    "ldap_mappings:{arg}": ("/admin/ldap_settings/{pk}/#tab-group-mappings", "An LDAP utility's Group Mappings tab (IDD- id or pk)"),
    "sso": ("/admin/sso/", "SSO providers"),
    "email_settings": ("/admin/email_settings/", "SMTP settings (password is entered there)"),
    "email_test": ("/admin/email_settings/", "SMTP settings, to send a test email"),
    "email_templates": ("/admin/email/", "Email templates"),
    "proxy": ("/admin/misc_settings/#web-proxy-pane", "Outbound web proxy (Miscellaneous settings, Web Proxy tab)"),
    "misc_settings": ("/admin/misc_settings/", "Miscellaneous settings"),
    "ssl_certificates": ("/admin/ssl_certificates/", "Trusted SSL certificates"),
    "parameters": ("/admin/customfields/", "Parameters (custom fields)"),
    "new_parameter": ("/admin/customfields/", "Parameters, to add one"),
    "os_builds": ("/externalcontent/osbuilds/", "OS builds"),
    "groups": ("/groups/", "Groups (add a group from here)"),
    "group_quotas:{arg}": ("/groups/{pk}/#tab-quotas", "A group's Quotas tab (GRP- id)"),
    "users": ("/users/", "Users"),
    "new_user": ("/users/", "Users, to create a local user"),
    "user_edit:{arg}": ("/users/{pk}/", "A user's page, to edit the user and set a password (USR- id)"),
    "roles": ("/roles/", "Roles"),
    "portals": ("/portals/", "Branded portals"),
    "catalog": ("/catalog/", "Service catalog"),
    "content_library": ("/library/", "Content Library"),
    "source_code_repos": ("/source_code_repos/", "Source Control Repositories"),
    "mcp_tool_actions": ("/actions/mcp_tool_actions/", "MCP Tool Actions"),
    "server_actions": ("/actions/server_actions/", "Server actions"),
    "resource_actions": ("/actions/resource_actions/", "Resource actions"),
    "orchestration_actions": ("/actions/orchestration_actions/", "Orchestration actions"),
    "rules": ("/actions/rules/", "Rules"),
    "recurring_jobs": ("/recurring_jobs/", "Recurring jobs"),
    "cit_tests": ("/cit/", "Continuous Infrastructure Testing"),
    "inbound_webhooks": ("/actions/inbound_web_hooks/", "Inbound webhooks"),
    "ui_extensions": ("/admin/extensions/", "UI extensions"),
    "application_log": ("/admin/view_application_log", "Last 10 KB of application.log"),
    "jobs": ("/jobs/", "Job list"),
    "orders": ("/orders/", "Order list"),
}

# The button that opens the page's form as a dialog, by page key.
CLICKS = {
    "handler_verify": "Verify Connection on the Overview tab",
    "handler_edit": "Edit Credentials on the Overview tab",
    "handler_import_locations": "Import Locations on the Locations tab",
    "new_ldap": "Add",
    "ldap_verify": "Verify Connection on the Overview tab",
    "ldap_mappings": "New LDAP Mapping on the Group Mappings tab",
    "email_settings": "Edit on the Overview tab",
    "email_test": "Send Test Email on the Overview tab",
    "proxy": "Edit next to Settings on the Web Proxy tab",
    "new_parameter": "Add",
    "group_quotas": "Edit on the Quotas tab",
    "new_user": "Add",
    "user_edit": "Edit on the Overview tab",
}


def _text(value):
    return html.unescape(value or "").strip()


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


def _read_inputs():
    raw = {"onb_link_target": """{{ onb_link_target }}"""}
    return {"target": _text(raw["onb_link_target"])}


def _resolve_object(global_id):
    prefix = global_id.split("-", 1)[0].upper()
    candidates = REGISTRY.get(prefix)
    if not candidates:
        return None, None, "Unsupported global id prefix '{}'. Supported: {}.".format(prefix, ", ".join(sorted(REGISTRY)))
    for module_path, class_name in candidates:
        try:
            model = getattr(importlib.import_module(module_path), class_name)
        except Exception:
            continue
        obj = model.objects.filter(global_id=global_id).first()
        if obj is not None:
            return obj, class_name, None
    return None, None, "No object with global id '{}' exists on this appliance.".format(global_id)


def _object_path(obj, class_name):
    fallback = FALLBACK_PATHS.get(class_name)
    if fallback:
        return fallback.format(pk=obj.pk)
    try:
        return obj.get_absolute_url()
    except Exception:
        return None


def _pk_for(arg):
    """A page argument may be a global id (resolved to its pk) or a bare pk."""
    arg = _text(arg)
    if arg.isdigit():
        return int(arg), None
    if GLOBAL_ID_RE.match(arg):
        obj, _class_name, error = _resolve_object(arg)
        if error:
            return None, error
        return obj.pk, None
    return None, "'{}' is neither a global id nor a numeric id.".format(arg)


def _handler_chooser(arg, outputs):
    """Point a new-handler link at the Setup Wizard chooser, or at add_by_tech on older releases.

    Returns the path to use. With a technology slug, adds clickTechnology: the name the
    chooser shows for that technology, so the agent can tell the user which tile to click.
    """
    from django.urls import NoReverseMatch, reverse
    from resourcehandlers.models import ResourceTechnology

    try:
        path = reverse("quick_setup_add_resource_handler")
    except NoReverseMatch:
        path = "/admin/resourcehandlers/add_by_tech/{}/".format(arg) if arg else "/admin/resourcehandlers/add/"
        outputs["note"] = "This release has no Setup Wizard handler page; the link goes to the older add page."
    if not arg:
        return path
    name = None
    for tech in ResourceTechnology.objects.all():
        try:
            if tech.type_slug == arg:
                name = tech.name
                break
        except Exception:
            continue
    if name:
        outputs["clickTechnology"] = name
        outputs.setdefault("note", "The link opens the technology chooser; click {}.".format(name))
    else:
        outputs.setdefault("note", "No technology has the slug '{}'; the user picks it in the chooser.".format(arg))
    return path


def _pages_help():
    rows = []
    for key, (_path, meaning) in PAGES.items():
        row = {"page": key, "meaning": meaning}
        click = CLICKS.get(key.split(":")[0])
        if click:
            row["click"] = click
        rows.append(row)
    return rows


def run(job, *args, **kwargs):
    profile = _caller(job, kwargs)
    if profile is None:
        return _failure("No calling user profile was supplied. Run this tool through the MCP server as an authenticated user.")
    if not profile.is_cbadmin:
        return _failure("Only CloudBolt admins may resolve admin UI links.")

    target = _read_inputs()["target"]
    base = _portal_url()
    if not target:
        return _failure("onb_link_target is required: a global id such as RH-abc12345, or a page key. Pages: {}.".format(
            ", ".join(PAGES)))

    outputs = {"target": target, "portalUrl": base or None}
    if GLOBAL_ID_RE.match(target):
        obj, class_name, error = _resolve_object(target)
        if error:
            return _failure(error)
        path = _object_path(obj, class_name)
        if not path:
            return _failure("Found {} {} but it has no UI page.".format(class_name, target))
        outputs.update(
            {
                "model": class_name,
                "pk": obj.pk,
                "label": getattr(obj, "name", None) or getattr(obj, "label", None) or getattr(obj, "hostname", None),
                "url": base + path,
            }
        )
        return _success("{} {} -> {}".format(class_name, target, outputs["url"]), outputs)

    key, _sep, arg = target.partition(":")
    key = key.strip().lower()
    arg = arg.strip()
    if arg and (key + ":{arg}") in PAGES:
        path, meaning = PAGES[key + ":{arg}"]
        if "{pk}" in path:
            pk, error = _pk_for(arg)
            if error:
                return _failure(error)
            path = path.format(pk=pk)
        else:
            if not re.match(r"^[a-z0-9_]+$", arg):
                return _failure("'{}' is not a valid argument for {}.".format(arg, key))
            path = path.format(arg=arg)
    elif not arg and key in PAGES:
        path, meaning = PAGES[key]
    else:
        return _failure(
            "Unknown page '{}'. Pass a global id or one of: {}.".format(target, ", ".join(PAGES))
        )
    if key == "new_resource_handler":
        path = _handler_chooser(arg, outputs)
    outputs.update({"page": key, "argument": arg or None, "meaning": meaning, "url": base + path})
    if key in CLICKS:
        outputs["click"] = CLICKS[key]
        outputs["note"] = "The form opens as a dialog over this page: tell the user to click {}.".format(CLICKS[key])
    if not base:
        outputs["relativeNote"] = "The default portal has no domain, so the URL is relative; prefix it with the appliance's https address."
    return _success("{} -> {}".format(target, outputs["url"]), outputs)
