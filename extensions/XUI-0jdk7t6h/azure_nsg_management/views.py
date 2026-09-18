"""
Azure NSG Management XUI

Adds a "Security Rules" tab to Azure Network Security Group resources that
loosely mirrors the Azure portal NSG overview: two DataTables (Inbound / Outbound
security rules, sorted by priority) with per-rule Edit/Delete actions and an
"Add Security Rule" button per table.

Companion to the "Azure Network Security Group" blueprint (BP-3fdhnw54). That
blueprint's build/discovery plugins stamp these custom fields on the Resource:
    - azure_network_security_group_id  (full ARM resource id -- the tab gate)
    - azure_network_security_group     (NSG name)
    - resource_group_name              (Azure resource group)
    - azure_rh_id                      (AzureARMHandler id backing the resource)

Azure SDK reference (azure-mgmt-network, track2 -- same SDK the blueprint's
build plugin uses via handler.get_api_wrapper().network_client):
    - SecurityRulesOperations:
      https://learn.microsoft.com/en-us/python/api/azure-mgmt-network/azure.mgmt.network.operations.securityrulesoperations
    - SecurityRule model:
      https://learn.microsoft.com/en-us/python/api/azure-mgmt-network/azure.mgmt.network.models.securityrule
"""
from django.contrib import messages
from django.http import HttpResponseRedirect
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.utils.html import escape, format_html, mark_safe

from extensions.views import tab_extension, TabExtensionDelegate
from resourcehandlers.azure_arm.models import AzureARMHandler
from resources.models import Resource
from utilities.decorators import dialog_view, json_view_no_escape
from utilities.logger import ThreadLogger

from xui.azure_nsg_management.forms import SecurityRuleForm

logger = ThreadLogger(__name__)


# ---------------------------------------------------------------------------
# Azure connection helpers
# ---------------------------------------------------------------------------
def _get_network_client(resource):
    """Return an azure.mgmt.network NetworkManagementClient for this resource.

    Uses the AzureARMHandler stored on the resource (azure_rh_id) and its
    api wrapper -- the same path the NSG discovery plugin (OHK-kdne1t5s) uses.
    """
    handler = AzureARMHandler.objects.get(id=resource.azure_rh_id)
    wrapper = handler.get_api_wrapper()
    return wrapper.network_client


def _get_nsg(resource):
    """Fetch the live NSG object (carries both custom and default rules)."""
    network_client = _get_network_client(resource)
    # network_security_groups.get(resource_group_name, network_security_group_name)
    return network_client.network_security_groups.get(
        resource.resource_group_name,
        resource.azure_network_security_group,
    )


# ---------------------------------------------------------------------------
# Rule formatting helpers (mirror the Azure portal columns)
# ---------------------------------------------------------------------------
def _any(value):
    """Azure uses '*' for 'match anything'; the portal renders that as 'Any'."""
    if value in (None, "", "*"):
        return "Any"
    return value


def _prefixes(single, plural):
    """A rule carries either a single *_prefix/*_range or a plural list."""
    if plural:
        return ", ".join(plural)
    return _any(single)


def _rule_to_dict(rule, is_default):
    """Flatten an azure.mgmt.network SecurityRule into display fields."""
    return {
        "name": rule.name,
        "priority": rule.priority,
        "direction": (rule.direction or ""),
        "access": (rule.access or ""),
        "protocol": _any(rule.protocol),
        "port": _prefixes(rule.destination_port_range, rule.destination_port_ranges),
        "source": _prefixes(rule.source_address_prefix, rule.source_address_prefixes),
        "destination": _prefixes(
            rule.destination_address_prefix, rule.destination_address_prefixes
        ),
        "is_default": is_default,
    }


def _list_rules(resource, direction):
    """Return display dicts for all rules in the requested direction.

    Custom rules (editable) first, then Azure's built-in default rules
    (read-only), each tagged with is_default.
    """
    nsg = _get_nsg(resource)
    want = (direction or "").lower()
    rules = []
    for rule in (nsg.security_rules or []):
        row = _rule_to_dict(rule, is_default=False)
        if row["direction"].lower() == want:
            rules.append(row)
    for rule in (nsg.default_security_rules or []):
        row = _rule_to_dict(rule, is_default=True)
        if row["direction"].lower() == want:
            rules.append(row)
    return rules


def _access_cell(access):
    if access.lower() == "allow":
        return mark_safe(
            '<span class="text-success"><i class="fa fa-check-circle"></i> Allow</span>'
        )
    return mark_safe(
        '<span class="text-danger"><i class="fa fa-times-circle"></i> Deny</span>'
    )


def _actions_cell(resource, rule):
    """Edit/Delete links for custom rules; a read-only marker for defaults."""
    if rule["is_default"]:
        return mark_safe(
            '<span class="text-muted" title="Azure default rule (read-only)">'
            '<i class="fa fa-lock"></i> Default</span>'
        )
    edit_url = reverse("azure_nsg_edit_rule", args=[resource.id, rule["name"]])
    delete_url = reverse("azure_nsg_delete_rule", args=[resource.id, rule["name"]])
    return format_html(
        '<a class="open-dialog" href="{}" title="Edit rule">'
        '<i class="fa fa-pencil"></i> Edit</a>'
        '&nbsp;&nbsp;'
        '<a class="open-dialog text-danger" href="{}" title="Delete rule">'
        '<i class="fa fa-trash"></i> Delete</a>',
        edit_url,
        delete_url,
    )


def _render_row(resource, rule):
    """One DataTables row: Priority, Name, Port, Protocol, Source, Destination,
    Action, Actions -- matching the Azure portal column order."""
    return [
        rule["priority"],
        escape(rule["name"]),
        escape(rule["port"]),
        escape(rule["protocol"]),
        escape(rule["source"]),
        escape(rule["destination"]),
        _access_cell(rule["access"]),
        _actions_cell(resource, rule),
    ]


def _suggest_priority(resource, direction):
    """Suggest the next free custom priority for the Add dialog."""
    try:
        customs = [r for r in _list_rules(resource, direction) if not r["is_default"]]
        if not customs:
            return 100
        return min(4096, max(r["priority"] for r in customs) + 10)
    except Exception:
        return 100


# ---------------------------------------------------------------------------
# Tab extension
# ---------------------------------------------------------------------------
class NSGTabDelegate(TabExtensionDelegate):
    def should_display(self):
        try:
            return bool(self.instance.azure_network_security_group_id)
        except AttributeError:
            # Resource has no NSG id custom field -- not an Azure NSG resource.
            return False


@tab_extension(
    model=Resource,
    title="Security Rules",
    delegate=NSGTabDelegate,
    description="Azure Network Security Group inbound/outbound rules",
)
def nsg_overview_tab(request, obj_id):
    resource = get_object_or_404(Resource, pk=obj_id)
    return render(
        request,
        "azure_nsg_management/templates/nsg_overview_tab.html",
        {
            "resource": resource,
            "inbound_source": reverse(
                "azure_nsg_rules_json", args=[resource.id, "inbound"]
            ),
            "outbound_source": reverse(
                "azure_nsg_rules_json", args=[resource.id, "outbound"]
            ),
            "inbound_add_url": reverse(
                "azure_nsg_add_rule", args=[resource.id, "inbound"]
            ),
            "outbound_add_url": reverse(
                "azure_nsg_add_rule", args=[resource.id, "outbound"]
            ),
        },
    )


# ---------------------------------------------------------------------------
# DataTables JSON source (one endpoint, direction in the URL)
#
# json_view_no_escape is required: the Action/Actions cells contain HTML and
# the default json_view would HTML-escape every value.
# ---------------------------------------------------------------------------
@json_view_no_escape
def nsg_rules_json(request, resource_id, direction):
    resource = get_object_or_404(Resource, pk=resource_id)

    try:
        rules = _list_rules(resource, direction)
    except Exception as err:
        logger.exception("Failed to list Azure NSG rules")
        return {
            "sEcho": int(request.GET.get("sEcho", 1)),
            "iTotalRecords": 0,
            "iTotalDisplayRecords": 0,
            "aaData": [],
            "error": "Unable to load security rules from Azure: {}".format(err),
        }

    # Optional DataTables free-text search across the display fields.
    search = (request.GET.get("sSearch", "") or "").lower()
    if search:
        rules = [
            r
            for r in rules
            if search
            in " ".join(
                [
                    str(r["priority"]),
                    r["name"],
                    r["port"],
                    r["protocol"],
                    r["source"],
                    r["destination"],
                    r["access"],
                ]
            ).lower()
        ]

    # Always present sorted by priority, like the Azure portal.
    rules.sort(key=lambda r: (r["priority"] is None, r["priority"]))

    total = len(rules)
    start = int(request.GET.get("iDisplayStart", 0))
    length = int(request.GET.get("iDisplayLength", 25))
    page = rules[start : start + length] if length and length > 0 else rules

    return {
        "sEcho": int(request.GET.get("sEcho", 1)),
        "iTotalRecords": total,
        "iTotalDisplayRecords": total,
        "aaData": [_render_row(resource, r) for r in page],
    }


# ---------------------------------------------------------------------------
# Add / Edit / Delete dialogs
# ---------------------------------------------------------------------------
def _create_or_update_rule(resource, rule_name, params):
    """Create or update a security rule and block until Azure completes it.

    SecurityRulesOperations.begin_create_or_update(
        resource_group_name, network_security_group_name,
        security_rule_name, security_rule_parameters)
    """
    network_client = _get_network_client(resource)
    poller = network_client.security_rules.begin_create_or_update(
        resource.resource_group_name,
        resource.azure_network_security_group,
        rule_name,
        params,
    )
    return poller.result()


def _delete_rule(resource, rule_name):
    """SecurityRulesOperations.begin_delete(rg, nsg, rule_name)."""
    network_client = _get_network_client(resource)
    poller = network_client.security_rules.begin_delete(
        resource.resource_group_name,
        resource.azure_network_security_group,
        rule_name,
    )
    return poller.result()


def _dir_label(direction):
    return "Outbound" if (direction or "").lower() == "outbound" else "Inbound"


@dialog_view
def add_rule(request, resource_id, direction):
    resource = get_object_or_404(Resource, pk=resource_id)
    dir_label = _dir_label(direction)
    action_url = reverse("azure_nsg_add_rule", args=[resource_id, direction])

    if request.method == "POST":
        form = SecurityRuleForm(request.POST, direction=dir_label)
        if form.is_valid():
            name = form.cleaned_data["name"]
            try:
                _create_or_update_rule(resource, name, form.to_params())
                messages.success(
                    request, "Security rule '{}' saved.".format(name)
                )
            except Exception as err:
                logger.exception("Failed to add Azure NSG rule")
                messages.error(request, "Failed to add rule: {}".format(err))
            return HttpResponseRedirect(request.META["HTTP_REFERER"])
    else:
        form = SecurityRuleForm(
            direction=dir_label,
            initial={"priority": _suggest_priority(resource, dir_label)},
        )

    return {
        "title": "Add {} Security Rule".format(dir_label),
        "form": form,
        "use_ajax": True,
        "action_url": action_url,
        "submit": "Add",
    }


@dialog_view
def edit_rule(request, resource_id, rule_name):
    resource = get_object_or_404(Resource, pk=resource_id)
    action_url = reverse("azure_nsg_edit_rule", args=[resource_id, rule_name])

    if request.method == "POST":
        form = SecurityRuleForm(request.POST, editing=True)
        if form.is_valid():
            try:
                _create_or_update_rule(resource, rule_name, form.to_params())
                messages.success(
                    request, "Security rule '{}' updated.".format(rule_name)
                )
            except Exception as err:
                logger.exception("Failed to update Azure NSG rule")
                messages.error(request, "Failed to update rule: {}".format(err))
            return HttpResponseRedirect(request.META["HTTP_REFERER"])
    else:
        try:
            network_client = _get_network_client(resource)
            rule = network_client.security_rules.get(
                resource.resource_group_name,
                resource.azure_network_security_group,
                rule_name,
            )
        except Exception as err:
            logger.exception("Failed to load Azure NSG rule for edit")
            return {
                "title": "Edit Security Rule",
                "content": mark_safe(
                    "<p class='text-danger'>Unable to load rule "
                    "<b>{}</b>: {}</p>".format(escape(rule_name), escape(str(err)))
                ),
            }
        form = SecurityRuleForm(
            direction=(rule.direction or "Inbound"),
            editing=True,
            initial=SecurityRuleForm.initial_from_rule(rule),
        )

    return {
        "title": "Edit Security Rule: {}".format(rule_name),
        "form": form,
        "use_ajax": True,
        "action_url": action_url,
        "submit": "Save",
    }


@dialog_view
def delete_rule(request, resource_id, rule_name):
    resource = get_object_or_404(Resource, pk=resource_id)
    action_url = reverse("azure_nsg_delete_rule", args=[resource_id, rule_name])

    if request.method == "POST":
        try:
            _delete_rule(resource, rule_name)
            messages.success(request, "Security rule '{}' deleted.".format(rule_name))
        except Exception as err:
            logger.exception("Failed to delete Azure NSG rule")
            messages.error(request, "Failed to delete rule: {}".format(err))
        return HttpResponseRedirect(request.META["HTTP_REFERER"])

    return {
        "title": "Delete Security Rule",
        "content": mark_safe(
            "<p>Are you sure you want to delete the security rule "
            "<b>{}</b>? This change is applied directly in Azure and cannot "
            "be undone.</p>".format(escape(rule_name))
        ),
        "action_url": action_url,
        "submit": "Delete",
    }
