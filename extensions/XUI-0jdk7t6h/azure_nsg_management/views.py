"""
Azure NSG Management XUI

Adds a Security Rules tab to the resources of the Azure Network Security
Group blueprint (BP-3fdhnw54): any resource carrying the
azure_network_security_group_id custom field, which the blueprint's build
(OHK-7987st2p) and discovery (OHK-kdne1t5s) plugins record together with
azure_network_security_group (the NSG name), resource_group_name,
azure_location and azure_rh_id (the AzureARMHandler behind it).

The tab loosely mirrors the Azure portal's NSG Overview: the essentials
(resource group, location, subscription, provisioning state, custom rule
counts, associated subnets and network interfaces, tags), then Inbound and
Outbound security rule tables sorted by priority with the portal's columns
(Priority, Name, Port, Protocol, Source, Destination, Action). Each table has
a filter and an Add rule button; custom rules have Edit and Delete, Azure's
default rules are read-only, and a rule that uses application security
groups can be deleted but is edited in the Azure portal, since this form
cannot express it.

The tab is built like CloudBolt's own Bootstrap 5 pages: theme tokens,
Bootstrap Icons, and CloudBolt's dialogs (dialog_view and .open-dialog), so
Branded Themes re-theme it. The rules panel loads asynchronously, so a slow
Azure call never stalls the resource page.

Azure is read and written through the handler's API wrapper
(handler.get_api_wrapper().network_client), which carries CloudBolt's proxy,
SSL verification and sovereign cloud settings; the handler is never shown.
The wrapper signs in as the handler's service principal, not as the viewer,
so every view checks the viewer's CloudBolt permission on the resource:
VIEW_PERMISSION to see the rules, MANAGE_PERMISSION to add, edit or delete
them. Changes are written to Azure directly, without a CloudBolt job, and
each one is recorded in the resource's History.

Azure SDK (azure-mgmt-network) and service references:
    https://learn.microsoft.com/en-us/python/api/azure-mgmt-network/azure.mgmt.network.operations.networksecuritygroupsoperations
    https://learn.microsoft.com/en-us/python/api/azure-mgmt-network/azure.mgmt.network.operations.securityrulesoperations
    https://learn.microsoft.com/en-us/python/api/azure-mgmt-network/azure.mgmt.network.models.securityrule
    https://learn.microsoft.com/en-us/azure/virtual-network/network-security-groups-overview
    https://learn.microsoft.com/en-us/azure/virtual-network/manage-network-security-group
"""
from azure.core.exceptions import HttpResponseError
from django.contrib import messages
from django.http import HttpResponseRedirect
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.utils.html import format_html

from extensions.views import tab_extension, TabExtensionDelegate
from resourcehandlers.azure_arm.models import AzureARMHandler
from resources.models import Resource
from utilities.decorators import dialog_view
from utilities.events import add_resource_event
from utilities.logger import ThreadLogger

from xui.azure_nsg_management.forms import PROTOCOL_CHOICES, ConfirmForm, SecurityRuleForm

logger = ThreadLogger(__name__)

TEMPLATES = "azure_nsg_management/templates/"

# Seeing the tab needs VIEW_PERMISSION on the resource; adding, editing and
# deleting rules needs MANAGE_PERMISSION, the permission CloudBolt's own
# Kubernetes object editor requires to change the cloud object behind a
# resource.
VIEW_PERMISSION = "resource.view"
MANAGE_PERMISSION = "resource.change_attributes"

# The custom field that marks an NSG resource.
MARKER_FIELD = "azure_network_security_group_id"

DIRECTIONS = {"inbound": "Inbound", "outbound": "Outbound"}

# Protocols as the portal names them (Azure stores Tcp, Udp, ...).
PROTOCOL_LABELS = dict(PROTOCOL_CHOICES)

# Custom rule priorities; Azure's default rules use 65000 and above.
# https://learn.microsoft.com/en-us/azure/virtual-network/network-security-groups-overview#security-rules
MIN_PRIORITY = 100
MAX_PRIORITY = 4096

# Azure portal per handler cloud (AzureARMHandler.cloud_environment), as the
# azure_aks shared module (SHM-ptvp0km4) maps them.
# https://learn.microsoft.com/en-us/azure/azure-government/documentation-government-get-started-connect-with-portal
# https://learn.microsoft.com/en-us/azure/china/resources-developer-guide
PORTALS = {
    "PUBLIC": "https://portal.azure.com",
    "US_GOV": "https://portal.azure.us",
    "CHINA": "https://portal.azure.cn",
    "GERMAN": "https://portal.microsoftazure.de",
}


class NSGError(Exception):
    """A readable problem shown instead of a traceback."""


# ---------------------------------------------------------------------------
# Resource, permission and Azure helpers
# ---------------------------------------------------------------------------
def _attr(resource, name):
    """Value of a custom field on the resource (exposed as an attribute), or None."""
    try:
        value = getattr(resource, name)
    except AttributeError:
        return None
    if value in ("", None):
        return None
    return value


def _has_permission(profile, permission, resource):
    if getattr(profile, "is_cbadmin", False):
        return True
    try:
        return bool(profile.has_permission(permission, resource))
    except Exception:
        logger.exception("Permission check '%s' failed for resource %s", permission, resource.id)
        return False


def _azure_message(exc):
    """Azure's error message and code from the ARM error body, else the SDK's text."""
    error = getattr(exc, "error", None)
    if error is not None and getattr(error, "message", None):
        return f"{error.message} ({error.code})" if getattr(error, "code", None) else error.message
    return getattr(exc, "message", None) or str(exc)


def _handler(resource):
    handler = AzureARMHandler.objects.filter(id=_attr(resource, "azure_rh_id")).first()
    if handler is None:
        raise NSGError(
            "The Azure resource handler recorded on this resource (azure_rh_id) no longer exists. "
            "Run discovery (Sync) on the Azure Network Security Group blueprint to record the current one."
        )
    return handler


def _network_client(resource):
    return _handler(resource).get_api_wrapper().network_client


def _coordinates(resource):
    """(resource group, NSG name) recorded on the resource."""
    resource_group = _attr(resource, "resource_group_name")
    name = _attr(resource, "azure_network_security_group")
    if not resource_group or not name:
        raise NSGError(
            "This resource does not record the NSG's resource group and name. Run discovery (Sync) on "
            "the Azure Network Security Group blueprint to record them."
        )
    return resource_group, name


def _get_nsg(resource):
    """The live NSG, carrying its custom and default rules and its associations.
    https://learn.microsoft.com/en-us/python/api/azure-mgmt-network/azure.mgmt.network.operations.networksecuritygroupsoperations#azure-mgmt-network-operations-networksecuritygroupsoperations-get
    """
    resource_group, name = _coordinates(resource)
    try:
        return _network_client(resource).network_security_groups.get(resource_group, name)
    except HttpResponseError as exc:
        raise NSGError(f"Azure could not read network security group '{name}': {_azure_message(exc)}")


def _find_rule(nsg, rule_name):
    """The custom rule of that name (case-insensitive, as Azure names are), or None."""
    for rule in nsg.security_rules or []:
        if (rule.name or "").lower() == (rule_name or "").lower():
            return rule
    return None


def _portal_url(resource, resource_id):
    """Azure portal link to a resource: {portal}/#@{tenant}/resource{id}."""
    try:
        handler = _handler(resource)
    except NSGError:
        return ""
    portal = PORTALS.get(getattr(handler, "cloud_environment", "") or "PUBLIC", PORTALS["PUBLIC"])
    tenant = getattr(handler, "azure_tenant_id", "") or ""
    return f"{portal}/#{'@' + tenant if tenant else ''}/resource{resource_id}"


def _id_parts(resource_id):
    """{lower-cased segment name: value} for an ARM resource ID."""
    parts = str(resource_id or "").strip("/").split("/")
    return {parts[i].lower(): parts[i + 1] for i in range(0, len(parts) - 1, 2)}


def _record(resource, profile, message):
    """Add the change to the resource's History tab; never fails the change."""
    try:
        add_resource_event("INFO", resource, message, profile=profile)
    except Exception:
        logger.exception("Could not record NSG change on resource %s", resource.id)


# ---------------------------------------------------------------------------
# Rule display (the Azure portal's columns)
# ---------------------------------------------------------------------------
def _any(value):
    """Azure uses '*' for 'match anything'; the portal shows it as Any."""
    if value in (None, "", "*"):
        return "Any"
    return value


def _values(single, plural):
    if plural:
        return ", ".join(plural)
    return _any(single)


def _asg_names(groups):
    return ", ".join(str(group.id).rstrip("/").split("/")[-1] for group in groups or [] if group.id)


def _rule_row(resource, rule, is_default, can_manage):
    source_asgs = _asg_names(rule.source_application_security_groups)
    destination_asgs = _asg_names(rule.destination_application_security_groups)
    source_ports = _values(rule.source_port_range, rule.source_port_ranges)
    row = {
        "name": rule.name,
        "priority": rule.priority,
        "access": rule.access or "",
        "allow": (rule.access or "").lower() == "allow",
        "protocol": PROTOCOL_LABELS.get(rule.protocol or "*", rule.protocol),
        "port": _values(rule.destination_port_range, rule.destination_port_ranges),
        "source_ports": "" if source_ports == "Any" else source_ports,
        "source": source_asgs or _values(rule.source_address_prefix, rule.source_address_prefixes),
        "destination": destination_asgs
        or _values(rule.destination_address_prefix, rule.destination_address_prefixes),
        "source_is_asg": bool(source_asgs),
        "destination_is_asg": bool(destination_asgs),
        "description": rule.description or "",
        "is_default": is_default,
        "edit_url": "",
        "delete_url": "",
    }
    if can_manage and not is_default:
        if not (source_asgs or destination_asgs):
            row["edit_url"] = reverse("azure_nsg_edit_rule", args=[resource.id, rule.name])
        row["delete_url"] = reverse("azure_nsg_delete_rule", args=[resource.id, rule.name])
    return row


def _rules(resource, nsg, direction, can_manage):
    """Rows for one direction sorted by priority: custom rules, then Azure's defaults."""
    rows = [
        _rule_row(resource, rule, False, can_manage)
        for rule in nsg.security_rules or []
        if (rule.direction or "").lower() == direction.lower()
    ]
    rows += [
        _rule_row(resource, rule, True, can_manage)
        for rule in nsg.default_security_rules or []
        if (rule.direction or "").lower() == direction.lower()
    ]
    rows.sort(key=lambda row: (row["priority"] is None, row["priority"] or 0))
    return rows


def _priorities(nsg, direction, exclude=None):
    """{priority: rule name} of the custom rules in one direction."""
    return {
        rule.priority: rule.name
        for rule in nsg.security_rules or []
        if (rule.direction or "").lower() == direction.lower()
        and (rule.name or "").lower() != (exclude or "").lower()
    }


def _suggest_priority(nsg, direction):
    """Ten above the highest custom priority, else the lowest free one."""
    taken = _priorities(nsg, direction)
    if not taken:
        return MIN_PRIORITY
    candidate = max(taken) + 10
    if candidate <= MAX_PRIORITY:
        return candidate
    return next((p for p in range(MIN_PRIORITY, MAX_PRIORITY + 1) if p not in taken), MAX_PRIORITY)


def _association(resource, resource_id, kind):
    parts = _id_parts(resource_id)
    if kind == "subnet":
        name = "{} / {}".format(parts.get("virtualnetworks", ""), parts.get("subnets", ""))
    else:
        name = parts.get("networkinterfaces", "")
    return {
        "name": name,
        "resource_group": parts.get("resourcegroups", ""),
        "url": _portal_url(resource, resource_id),
    }


def _panel_context(resource, profile):
    nsg = _get_nsg(resource)
    can_manage = _has_permission(profile, MANAGE_PERMISSION, resource)
    inbound = _rules(resource, nsg, "Inbound", can_manage)
    outbound = _rules(resource, nsg, "Outbound", can_manage)
    subnets = [_association(resource, s.id, "subnet") for s in nsg.subnets or [] if s.id]
    interfaces = [_association(resource, n.id, "nic") for n in nsg.network_interfaces or [] if n.id]
    parts = _id_parts(nsg.id)
    return {
        "nsg": {
            "name": nsg.name,
            "id": nsg.id,
            "resource_group": _attr(resource, "resource_group_name") or parts.get("resourcegroups", ""),
            "location": nsg.location,
            "subscription_id": parts.get("subscriptions", ""),
            "provisioning_state": nsg.provisioning_state or "",
            "tags": sorted((nsg.tags or {}).items()),
        },
        "inbound": inbound,
        "outbound": outbound,
        "inbound_custom": sum(1 for row in inbound if not row["is_default"]),
        "outbound_custom": sum(1 for row in outbound if not row["is_default"]),
        "subnets": subnets,
        "interfaces": interfaces,
        "can_manage": can_manage,
        "add_inbound_url": reverse("azure_nsg_add_rule", args=[resource.id, "inbound"]) if can_manage else "",
        "add_outbound_url": reverse("azure_nsg_add_rule", args=[resource.id, "outbound"]) if can_manage else "",
    }


# ---------------------------------------------------------------------------
# Tab and panel
# ---------------------------------------------------------------------------
class NSGTabDelegate(TabExtensionDelegate):
    """Resources of the Azure Network Security Group blueprint."""

    def should_display(self):
        return bool(_attr(self.instance, MARKER_FIELD))


@tab_extension(
    model=Resource,
    title="Security Rules",
    delegate=NSGTabDelegate,
    description="Azure network security group essentials and inbound and outbound security rules",
)
def nsg_rules_tab(request, obj_id):
    """Identity from the resource's custom fields (no Azure call); the panel loads the rest."""
    resource = get_object_or_404(Resource, pk=obj_id)
    return render(request, TEMPLATES + "nsg_rules_tab.html", {
        "resource": resource,
        "nsg_name": _attr(resource, "azure_network_security_group") or resource.name,
        "resource_group": _attr(resource, "resource_group_name") or "",
        "location": _attr(resource, "azure_location") or "",
        "portal_url": _portal_url(resource, _attr(resource, MARKER_FIELD)),
        "panel_url": reverse("azure_nsg_rules_panel", args=[resource.id]),
    })


def nsg_rules_panel(request, resource_id):
    """Essentials, associations and both rule tables, as an HTML fragment."""
    resource = get_object_or_404(Resource, pk=resource_id)
    profile = request.get_user_profile()
    if not _has_permission(profile, VIEW_PERMISSION, resource):
        context = {"error": "You do not have permission to view this resource."}
    elif not _attr(resource, MARKER_FIELD):
        context = {"error": "This resource is not an Azure network security group."}
    else:
        try:
            context = _panel_context(resource, profile)
        except NSGError as exc:
            context = {"error": str(exc)}
        except Exception as exc:
            logger.exception("Security Rules panel failed for resource %s", resource.id)
            context = {"error": f"Could not load the security rules: {exc}"}
    context["resource"] = resource
    return render(request, TEMPLATES + "nsg_rules_panel.html", context)


# ---------------------------------------------------------------------------
# Add / Edit / Delete dialogs
# ---------------------------------------------------------------------------
def _dialog_message(title, text, css="text-danger"):
    return {"title": title, "content": format_html('<p class="{}">{}</p>', css, text)}


def _dialog_guard(request, resource_id, title):
    """The viewer may change the rules of this NSG resource.
    Returns (resource, profile, refusal), refusal being a dialog or None."""
    resource = get_object_or_404(Resource, pk=resource_id)
    profile = request.get_user_profile()
    if not _attr(resource, MARKER_FIELD):
        return resource, profile, _dialog_message(title, "This resource is not an Azure network security group.")
    if not (
        _has_permission(profile, VIEW_PERMISSION, resource)
        and _has_permission(profile, MANAGE_PERMISSION, resource)
    ):
        return resource, profile, _dialog_message(
            title, "You do not have permission to change the security rules of this resource."
        )
    return resource, profile, None


def _redirect_back(request, resource):
    return HttpResponseRedirect(request.META.get("HTTP_REFERER") or resource.get_absolute_url())


def _save_rule(request, resource, profile, nsg, form, rule_name, verb):
    """Write the form's rule to Azure. Returns a redirect on success, or None
    with the problem added to the form so the dialog shows it.
    https://learn.microsoft.com/en-us/python/api/azure-mgmt-network/azure.mgmt.network.operations.securityrulesoperations#azure-mgmt-network-operations-securityrulesoperations-begin-create-or-update
    """
    priority = form.cleaned_data["priority"]
    taken = _priorities(nsg, form.direction, exclude=rule_name).get(priority)
    if taken:
        form.add_error(
            "priority", f"Rule '{taken}' already uses priority {priority} for {form.direction.lower()} traffic."
        )
        return None
    resource_group, nsg_name = _coordinates(resource)
    try:
        _network_client(resource).security_rules.begin_create_or_update(
            resource_group, nsg_name, rule_name, form.to_rule()
        ).result()
    except HttpResponseError as exc:
        form.add_error(None, f"Azure rejected the rule: {_azure_message(exc)}")
        return None
    except NSGError as exc:
        form.add_error(None, str(exc))
        return None
    except Exception as exc:
        logger.exception("Could not save NSG rule %s on resource %s", rule_name, resource.id)
        form.add_error(None, f"The rule could not be saved: {exc}")
        return None
    _record(resource, profile, f"Security rule '{rule_name}' {verb} ({form.direction}, priority {priority}).")
    messages.success(request, f"Security rule '{rule_name}' {verb}.")
    return _redirect_back(request, resource)


@dialog_view
def add_rule(request, resource_id, direction):
    direction = DIRECTIONS.get((direction or "").lower())
    title = f"Add {direction.lower()} security rule" if direction else "Add security rule"
    resource, profile, refusal = _dialog_guard(request, resource_id, title)
    if refusal:
        return refusal
    if not direction:
        return _dialog_message(title, "The direction must be inbound or outbound.")
    try:
        nsg = _get_nsg(resource)
    except NSGError as exc:
        return _dialog_message(title, str(exc))

    if request.method == "POST":
        form = SecurityRuleForm(request.POST, direction=direction)
        if form.is_valid():
            name = form.cleaned_data["name"]
            existing = _find_rule(nsg, name)
            if existing is not None:
                # create_or_update would silently replace it.
                form.add_error(
                    "name", f"A {(existing.direction or '').lower()} rule named '{existing.name}' already exists."
                )
            else:
                done = _save_rule(request, resource, profile, nsg, form, name, "added")
                if done:
                    return done
    else:
        form = SecurityRuleForm(direction=direction, initial={"priority": _suggest_priority(nsg, direction)})

    return {
        "title": title,
        "content": format_html(
            "<p>The rule is added to network security group <b>{}</b> in Azure as soon as you save it.</p>",
            nsg.name,
        ),
        "form": form,
        "use_ajax": True,
        "action_url": reverse("azure_nsg_add_rule", args=[resource.id, direction.lower()]),
        "submit": "Add",
    }


@dialog_view
def edit_rule(request, resource_id, rule_name):
    title = f"Edit security rule {rule_name}"
    resource, profile, refusal = _dialog_guard(request, resource_id, title)
    if refusal:
        return refusal
    try:
        nsg = _get_nsg(resource)
    except NSGError as exc:
        return _dialog_message(title, str(exc))
    rule = _find_rule(nsg, rule_name)
    if rule is None:
        return _dialog_message(
            title, f"Network security group '{nsg.name}' has no custom rule named '{rule_name}'. Default rules cannot be edited."
        )
    if rule.source_application_security_groups or rule.destination_application_security_groups:
        return _dialog_message(
            title, "This rule uses application security groups, which this form cannot edit. Edit it in the Azure portal."
        )

    direction = rule.direction or "Inbound"
    initial = SecurityRuleForm.initial_from_rule(rule)
    if request.method == "POST":
        form = SecurityRuleForm(request.POST, initial=initial, direction=direction, editing=True)
        if form.is_valid():
            done = _save_rule(request, resource, profile, nsg, form, rule.name, "updated")
            if done:
                return done
    else:
        form = SecurityRuleForm(initial=initial, direction=direction, editing=True)

    return {
        "title": title,
        "content": format_html(
            "<p>{} rule of network security group <b>{}</b>. Changes apply in Azure as soon as you save them.</p>",
            direction, nsg.name,
        ),
        "form": form,
        "use_ajax": True,
        "action_url": reverse("azure_nsg_edit_rule", args=[resource.id, rule.name]),
        "submit": "Save",
    }


@dialog_view
def delete_rule(request, resource_id, rule_name):
    title = "Delete security rule"
    resource, profile, refusal = _dialog_guard(request, resource_id, title)
    if refusal:
        return refusal
    try:
        nsg = _get_nsg(resource)
    except NSGError as exc:
        return _dialog_message(title, str(exc))
    rule = _find_rule(nsg, rule_name)
    if rule is None:
        return _dialog_message(
            title, f"Network security group '{nsg.name}' has no custom rule named '{rule_name}'. Default rules cannot be deleted."
        )

    if request.method == "POST":
        form = ConfirmForm(request.POST)
        if form.is_valid():
            resource_group, nsg_name = _coordinates(resource)
            # https://learn.microsoft.com/en-us/python/api/azure-mgmt-network/azure.mgmt.network.operations.securityrulesoperations#azure-mgmt-network-operations-securityrulesoperations-begin-delete
            try:
                _network_client(resource).security_rules.begin_delete(resource_group, nsg_name, rule.name).result()
            except HttpResponseError as exc:
                form.add_error(None, f"Azure could not delete the rule: {_azure_message(exc)}")
            except Exception as exc:
                logger.exception("Could not delete NSG rule %s on resource %s", rule.name, resource.id)
                form.add_error(None, f"The rule could not be deleted: {exc}")
            else:
                _record(resource, profile, f"Security rule '{rule.name}' deleted ({rule.direction}, priority {rule.priority}).")
                messages.success(request, f"Security rule '{rule.name}' deleted.")
                return _redirect_back(request, resource)
    else:
        form = ConfirmForm()

    return {
        "title": title,
        "content": format_html(
            "<p>Delete the {} rule <b>{}</b> (priority {}) from network security group <b>{}</b>? "
            "Azure applies this at once and it cannot be undone. Existing connections are not interrupted.</p>",
            (rule.direction or "").lower(), rule.name, rule.priority, nsg.name,
        ),
        "form": form,
        "use_ajax": True,
        "action_url": reverse("azure_nsg_delete_rule", args=[resource.id, rule.name]),
        "submit": "Delete",
    }
