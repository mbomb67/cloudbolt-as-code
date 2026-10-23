"""
AWS Security Group Management XUI

Adds a Security Rules tab to the resources of the AWS Security Group
blueprint (BP-ofwowhqd): any resource carrying the aws_security_group_id
custom field, which the blueprint's build (OHK-jb16kobi) and discovery
(OHK-62awc6bl) plugins record together with aws_security_group_name,
aws_vpc_id, aws_region and aws_rh_id (the AWSHandler behind it). Azure
network security groups share the Network resource type and get their own
Security Rules tab from XUI-0jdk7t6h.

The tab follows the AWS console's security group page: the details (name,
ID, description, VPC, owner, rule counts, tags), the network interfaces the
group is attached to, then Inbound and Outbound rule tables with the
console's columns (Name, Security group rule ID, IP version, Type, Protocol,
Port range, Source or Destination, Description). Each table has a filter and
an Add rule button; every rule has Edit and Delete. The rule dialog offers
the console's Type presets (SSH, HTTPS, All traffic, Custom TCP, ...), and
choosing one fills and locks the protocol and port range. A rule that
references a security group in another account is deleted here but edited
in the AWS console.

The tab is built like CloudBolt's own Bootstrap 5 pages: theme tokens,
Bootstrap Icons, and CloudBolt's dialogs (dialog_view and .open-dialog), so
Branded Themes re-theme it. The rules panel loads asynchronously, so a slow
AWS call never stalls the resource page.

AWS is read and written with the handler's credentials
(handler.get_boto3_client), not the viewer's, so every view checks the
viewer's CloudBolt permission on the resource: VIEW_PERMISSION to see the
rules, MANAGE_PERMISSION to add, edit or delete them. Changes are written to
AWS directly, without a CloudBolt job, and each one is recorded in the
resource's History.

EC2 API references (each call site cites its own page):
    https://docs.aws.amazon.com/AWSEC2/latest/APIReference/API_DescribeSecurityGroups.html
    https://docs.aws.amazon.com/AWSEC2/latest/APIReference/API_DescribeSecurityGroupRules.html
    https://docs.aws.amazon.com/AWSEC2/latest/APIReference/API_AuthorizeSecurityGroupIngress.html
    https://docs.aws.amazon.com/AWSEC2/latest/APIReference/API_ModifySecurityGroupRules.html
    https://docs.aws.amazon.com/AWSEC2/latest/APIReference/API_RevokeSecurityGroupIngress.html
    https://docs.aws.amazon.com/vpc/latest/userguide/security-group-rules.html
"""
import json
import uuid

from botocore.exceptions import BotoCoreError, ClientError
from django.contrib import messages
from django.http import HttpResponseRedirect
from django.shortcuts import get_object_or_404, render
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils.html import format_html

from extensions.views import tab_extension, TabExtensionDelegate
from resourcehandlers.aws.models import AWSHandler
from resources.models import Resource
from utilities.decorators import dialog_view
from utilities.events import add_resource_event
from utilities.logger import ThreadLogger

from xui.aws_sg_management.forms import (
    ANYWHERE,
    CUSTOM_ICMP_TYPES,
    PROTOCOL_LABELS,
    RULE_TYPES,
    TYPES,
    ConfirmForm,
    SecurityGroupRuleForm,
    name_tag,
    peer_of,
    protocol_name,
    rule_type_key,
)

logger = ThreadLogger(__name__)

TEMPLATES = "aws_sg_management/templates/"

# Seeing the tab needs VIEW_PERMISSION on the resource; adding, editing and
# deleting rules needs MANAGE_PERMISSION, as on the Azure NSG tab.
VIEW_PERMISSION = "resource.view"
MANAGE_PERMISSION = "resource.change_attributes"

# The custom field that marks a security group resource.
MARKER_FIELD = "aws_security_group_id"

DIRECTIONS = {"inbound": "Inbound", "outbound": "Outbound"}


class SGError(Exception):
    """A readable problem shown instead of a traceback."""


# ---------------------------------------------------------------------------
# Resource, permission and AWS helpers
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


def _error_code(exc):
    return exc.response.get("Error", {}).get("Code", "") if isinstance(exc, ClientError) else ""


def _aws_message(exc):
    """AWS's error message and code, else the SDK's text."""
    if isinstance(exc, ClientError):
        error = exc.response.get("Error", {})
        return "{} ({})".format(error.get("Message", exc), error.get("Code", "ClientError"))
    return str(exc)


def _client(resource):
    """EC2 client for the handler and region recorded on the resource."""
    handler = AWSHandler.objects.filter(id=_attr(resource, "aws_rh_id")).first()
    region = _attr(resource, "aws_region")
    if handler is None or not region:
        raise SGError(
            "This resource does not record a current AWS resource handler and region (aws_rh_id, aws_region). "
            "Run discovery (Sync) on the AWS Security Group blueprint to record them."
        )
    try:
        return handler.get_boto3_client(region, "ec2")
    except (BotoCoreError, ClientError) as exc:
        raise SGError(f"Could not connect to AWS {region}: {_aws_message(exc)}")


def _get_group(client, group_id):
    """https://docs.aws.amazon.com/AWSEC2/latest/APIReference/API_DescribeSecurityGroups.html"""
    try:
        groups = client.describe_security_groups(GroupIds=[group_id]).get("SecurityGroups", [])
    except (BotoCoreError, ClientError) as exc:
        if _error_code(exc) == "InvalidGroup.NotFound":
            raise SGError(f"Security group {group_id} no longer exists in AWS.")
        raise SGError(f"AWS could not read security group {group_id}: {_aws_message(exc)}")
    if not groups:
        raise SGError(f"Security group {group_id} no longer exists in AWS.")
    return groups[0]


def _get_rules(client, group_id, rule_id=None):
    """The group's rules, or the one with rule_id.
    https://docs.aws.amazon.com/AWSEC2/latest/APIReference/API_DescribeSecurityGroupRules.html
    """
    filters = [{"Name": "group-id", "Values": [group_id]}]
    if rule_id:
        filters.append({"Name": "security-group-rule-id", "Values": [rule_id]})
    rules = []
    try:
        for page in client.get_paginator("describe_security_group_rules").paginate(Filters=filters):
            rules.extend(page.get("SecurityGroupRules", []))
    except (BotoCoreError, ClientError) as exc:
        raise SGError(f"AWS could not read the rules of security group {group_id}: {_aws_message(exc)}")
    return rules


def _group_names(client, group_ids):
    """{group ID: name} for referenced groups AWS lets this account see; {} on failure."""
    if not group_ids:
        return {}
    try:
        groups = client.describe_security_groups(
            Filters=[{"Name": "group-id", "Values": sorted(group_ids)}]
        ).get("SecurityGroups", [])
    except (BotoCoreError, ClientError):
        return {}
    return {group["GroupId"]: group["GroupName"] for group in groups}


def _prefix_list_names(client, prefix_list_ids):
    """{prefix list ID: name}; {} on failure.
    https://docs.aws.amazon.com/AWSEC2/latest/APIReference/API_DescribeManagedPrefixLists.html
    """
    if not prefix_list_ids:
        return {}
    try:
        lists = client.describe_managed_prefix_lists(
            Filters=[{"Name": "prefix-list-id", "Values": sorted(prefix_list_ids)}]
        ).get("PrefixLists", [])
    except (BotoCoreError, ClientError):
        return {}
    return {item["PrefixListId"]: item.get("PrefixListName", "") for item in lists}


def _console_url(region, fragment, service="ec2"):
    """AWS console link for the region's partition."""
    if not region or not fragment:
        return ""
    if region.startswith("us-gov-"):
        host = "console.amazonaws-us-gov.com"
    elif region.startswith("cn-"):
        host = "console.amazonaws.cn"
    else:
        host = f"{region}.console.aws.amazon.com"
    return f"https://{host}/{service}/home?region={region}#{fragment}"


def _record(resource, profile, message):
    """Add the change to the resource's History tab; never fails the change."""
    try:
        add_resource_event("INFO", resource, message, profile=profile)
    except Exception:
        logger.exception("Could not record security group change on resource %s", resource.id)


# ---------------------------------------------------------------------------
# Rule display (the AWS console's columns)
# ---------------------------------------------------------------------------
def _port_range(protocol, key, low, high):
    if protocol == "-1" or key == "custom-protocol":
        return "All"
    if protocol in ("icmp", "icmpv6"):
        if low in (None, -1):
            return "All"
        return f"Type {low}" if high in (None, -1) else f"Type {low}, code {high}"
    if low is None:
        return "All"
    return str(low) if low == high else f"{low} - {high}"


def _peer_cell(rule, region, group_names, prefix_names):
    """(text, hint, url) for the Source or Destination column."""
    kind, value = peer_of(rule)
    if kind == "group":
        info = rule.get("ReferencedGroupInfo") or {}
        hint = group_names.get(value, "")
        owner = info.get("UserId")
        if owner and owner != rule.get("GroupOwnerId"):
            hint = " ".join(filter(None, [hint, f"account {owner}"]))
        if info.get("VpcPeeringConnectionId"):
            hint = " ".join(filter(None, [hint, f"via {info['VpcPeeringConnectionId']}"]))
        return value, hint, _console_url(region, f"SecurityGroup:groupId={value}")
    if kind == "prefix":
        return value, prefix_names.get(value, ""), _console_url(region, f"PrefixListDetails:prefixListId={value}", "vpcconsole")
    return value, "", ""


def _editable(rule):
    """The dialog can express the rule: not a group in another account or behind a peering connection."""
    kind, _ = peer_of(rule)
    if kind != "group":
        return True
    info = rule.get("ReferencedGroupInfo") or {}
    owner = info.get("UserId")
    return not info.get("VpcPeeringConnectionId") and (not owner or owner == rule.get("GroupOwnerId"))


def _rule_row(resource, rule, region, group_names, prefix_names, can_manage):
    protocol = protocol_name(rule.get("IpProtocol"))
    low, high = rule.get("FromPort"), rule.get("ToPort")
    key = rule_type_key(protocol, low, high)
    kind, _ = peer_of(rule)
    peer, peer_hint, peer_url = _peer_cell(rule, region, group_names, prefix_names)
    row = {
        "id": rule["SecurityGroupRuleId"],
        "egress": bool(rule.get("IsEgress")),
        "name": name_tag(rule),
        "ip_version": {"ipv4": "IPv4", "ipv6": "IPv6"}.get(kind, ""),
        "type": TYPES[key][0],
        "protocol": PROTOCOL_LABELS.get(protocol, protocol),
        "port": _port_range(protocol, key, low, high),
        "peer": peer,
        "peer_hint": peer_hint,
        "peer_url": peer_url,
        "description": rule.get("Description") or "",
        "edit_url": "",
        "delete_url": "",
        "sort": (-2 if low is None else low, protocol, peer),
    }
    if can_manage:
        if _editable(rule):
            row["edit_url"] = reverse("aws_sg_edit_rule", args=[resource.id, rule["SecurityGroupRuleId"]])
        row["delete_url"] = reverse("aws_sg_delete_rule", args=[resource.id, rule["SecurityGroupRuleId"]])
    return row


def _interfaces(client, group_id, region):
    """Network interfaces the group is attached to, and an error message when they cannot be listed.
    https://docs.aws.amazon.com/AWSEC2/latest/APIReference/API_DescribeNetworkInterfaces.html
    """
    rows = []
    try:
        pages = client.get_paginator("describe_network_interfaces").paginate(
            Filters=[{"Name": "group-id", "Values": [group_id]}]
        )
        for page in pages:
            for eni in page.get("NetworkInterfaces", []):
                instance_id = (eni.get("Attachment") or {}).get("InstanceId", "")
                rows.append({
                    "id": eni["NetworkInterfaceId"],
                    "url": _console_url(region, f"NetworkInterface:networkInterfaceId={eni['NetworkInterfaceId']}"),
                    "description": eni.get("Description") or "",
                    "type": eni.get("InterfaceType") or "",
                    "status": eni.get("Status") or "",
                    "private_ip": eni.get("PrivateIpAddress") or "",
                    "subnet": eni.get("SubnetId") or "",
                    "instance_id": instance_id,
                    "instance_url": _console_url(region, f"InstanceDetails:instanceId={instance_id}") if instance_id else "",
                })
    except (BotoCoreError, ClientError) as exc:
        return rows, f"AWS could not list the network interfaces: {_aws_message(exc)}"
    return rows, ""


def _panel_context(resource, profile):
    group_id = _attr(resource, MARKER_FIELD)
    region = _attr(resource, "aws_region")
    client = _client(resource)
    group = _get_group(client, group_id)
    rules = _get_rules(client, group_id)
    can_manage = _has_permission(profile, MANAGE_PERMISSION, resource)

    peers = [peer_of(rule) for rule in rules]
    group_names = _group_names(client, {value for kind, value in peers if kind == "group" and value})
    prefix_names = _prefix_list_names(client, {value for kind, value in peers if kind == "prefix"})
    rows = [_rule_row(resource, rule, region, group_names, prefix_names, can_manage) for rule in rules]
    rows.sort(key=lambda row: row["sort"])
    inbound = [row for row in rows if not row["egress"]]
    outbound = [row for row in rows if row["egress"]]
    interfaces, interfaces_error = _interfaces(client, group_id, region)
    return {
        "group": {
            "name": group.get("GroupName", ""),
            "id": group_id,
            "description": group.get("Description", ""),
            "vpc_id": group.get("VpcId", ""),
            "vpc_url": _console_url(region, f"VpcDetails:VpcId={group.get('VpcId', '')}", "vpcconsole")
            if group.get("VpcId") else "",
            "owner": group.get("OwnerId", ""),
            "region": region,
            "arn": group.get("SecurityGroupArn", ""),
            "tags": sorted((tag["Key"], tag.get("Value", "")) for tag in group.get("Tags", [])),
            "is_default": group.get("GroupName") == "default",
        },
        "inbound": inbound,
        "outbound": outbound,
        "interfaces": interfaces,
        "interfaces_error": interfaces_error,
        "can_manage": can_manage,
        "add_inbound_url": reverse("aws_sg_add_rule", args=[resource.id, "inbound"]) if can_manage else "",
        "add_outbound_url": reverse("aws_sg_add_rule", args=[resource.id, "outbound"]) if can_manage else "",
    }


# ---------------------------------------------------------------------------
# Tab and panel
# ---------------------------------------------------------------------------
class SecurityGroupTabDelegate(TabExtensionDelegate):
    """Resources of the AWS Security Group blueprint."""

    def should_display(self):
        return bool(_attr(self.instance, MARKER_FIELD))


@tab_extension(
    model=Resource,
    title="Security Rules",
    delegate=SecurityGroupTabDelegate,
    description="AWS security group details and inbound and outbound rules",
)
def sg_rules_tab(request, obj_id):
    """Identity from the resource's custom fields (no AWS call); the panel loads the rest."""
    resource = get_object_or_404(Resource, pk=obj_id)
    group_id = _attr(resource, MARKER_FIELD) or ""
    region = _attr(resource, "aws_region") or ""
    return render(request, TEMPLATES + "sg_rules_tab.html", {
        "resource": resource,
        "group_name": _attr(resource, "aws_security_group_name") or resource.name,
        "group_id": group_id,
        "vpc_id": _attr(resource, "aws_vpc_id") or "",
        "region": region,
        "console_url": _console_url(region, f"SecurityGroup:groupId={group_id}"),
        "panel_url": reverse("aws_sg_rules_panel", args=[resource.id]),
    })


def sg_rules_panel(request, resource_id):
    """Details, network interfaces and both rule tables, as an HTML fragment."""
    resource = get_object_or_404(Resource, pk=resource_id)
    profile = request.get_user_profile()
    if not _has_permission(profile, VIEW_PERMISSION, resource):
        context = {"error": "You do not have permission to view this resource."}
    elif not _attr(resource, MARKER_FIELD):
        context = {"error": "This resource is not an AWS security group."}
    else:
        try:
            context = _panel_context(resource, profile)
        except SGError as exc:
            context = {"error": str(exc)}
        except Exception:
            logger.exception("Security Rules panel failed for resource %s", resource.id)
            context = {"error": "Could not load the security group rules. The CloudBolt application log has the details."}
    context["resource"] = resource
    return render(request, TEMPLATES + "sg_rules_panel.html", context)


# ---------------------------------------------------------------------------
# Add / Edit / Delete dialogs
# ---------------------------------------------------------------------------
def _dialog_message(title, text, css="text-danger"):
    return {"title": title, "content": format_html('<p class="{}">{}</p>', css, text)}


def _dialog_guard(request, resource_id, title):
    """The viewer may change the rules of this security group resource.
    Returns (resource, profile, refusal), refusal being a dialog or None."""
    resource = get_object_or_404(Resource, pk=resource_id)
    profile = request.get_user_profile()
    if not _attr(resource, MARKER_FIELD):
        return resource, profile, _dialog_message(title, "This resource is not an AWS security group.")
    if not (
        _has_permission(profile, VIEW_PERMISSION, resource)
        and _has_permission(profile, MANAGE_PERMISSION, resource)
    ):
        return resource, profile, _dialog_message(
            title, "You do not have permission to change the rules of this security group."
        )
    return resource, profile, None


def _redirect_back(request, resource):
    return HttpResponseRedirect(request.META.get("HTTP_REFERER") or resource.get_absolute_url())


def _peer_options(client, group):
    """Datalist suggestions for the Source / Destination field: the security
    groups in the group's VPC and the region's prefix lists. [] on failure.
    https://docs.aws.amazon.com/AWSEC2/latest/APIReference/API_DescribeSecurityGroups.html
    https://docs.aws.amazon.com/AWSEC2/latest/APIReference/API_DescribeManagedPrefixLists.html
    """
    options = []
    try:
        pages = client.get_paginator("describe_security_groups").paginate(
            Filters=[{"Name": "vpc-id", "Values": [group.get("VpcId", "")]}]
        )
        for page in pages:
            options += [
                (item["GroupId"], f"Security group: {item['GroupName']}")
                for item in page.get("SecurityGroups", [])
            ]
    except (BotoCoreError, ClientError):
        logger.info("Could not list the security groups of %s for suggestions", group.get("VpcId"))
    try:
        for page in client.get_paginator("describe_managed_prefix_lists").paginate():
            options += [
                (item["PrefixListId"], f"Prefix list: {item.get('PrefixListName', '')}")
                for item in page.get("PrefixLists", [])
            ]
    except (BotoCoreError, ClientError):
        logger.info("Could not list the prefix lists for suggestions")
    return sorted(options, key=lambda option: option[1].lower())


def _form_extras(client, group, list_id):
    """The datalist and the script that makes the rule form follow the chosen Type."""
    presets = {}
    for key, _, protocol, low, high in RULE_TYPES:
        if key in ("custom-tcp", "custom-udp") or key in CUSTOM_ICMP_TYPES:
            ports = None
        else:
            ports = _port_range(protocol or "", key, low, high)
        presets[key] = ports
    return render_to_string(TEMPLATES + "_sg_rule_form_script.html", {
        "root_id": f"sgx-form-{uuid.uuid4().hex[:8]}",
        "list_id": list_id,
        "peer_options": _peer_options(client, group),
        "presets_json": json.dumps(presets),
        "anywhere_json": json.dumps(ANYWHERE),
    })


def _summary(form, direction):
    data = form.cleaned_data
    key = data["rule_type"]
    port = _port_range(protocol_name(data["protocol"]), key, data["from_port"], data["to_port"])
    peers = ", ".join(value for _, value in data["peers"])
    return f"{TYPES[key][0]}, port {port}, {'from' if direction == 'Inbound' else 'to'} {peers}"


def _authorize(client, group_id, form):
    """Add the form's rules; returns the new rule IDs.
    https://docs.aws.amazon.com/AWSEC2/latest/APIReference/API_AuthorizeSecurityGroupIngress.html
    https://docs.aws.amazon.com/AWSEC2/latest/APIReference/API_AuthorizeSecurityGroupEgress.html
    """
    kwargs = {"GroupId": group_id, "IpPermissions": [form.to_ip_permission()]}
    if form.cleaned_data["name"]:
        kwargs["TagSpecifications"] = [{
            "ResourceType": "security-group-rule",
            "Tags": [{"Key": "Name", "Value": form.cleaned_data["name"]}],
        }]
    call = client.authorize_security_group_egress if form.outbound else client.authorize_security_group_ingress
    response = call(**kwargs)
    return [rule["SecurityGroupRuleId"] for rule in response.get("SecurityGroupRules", [])]


def _revoke(client, group_id, rule_id, outbound):
    """https://docs.aws.amazon.com/AWSEC2/latest/APIReference/API_RevokeSecurityGroupIngress.html
    https://docs.aws.amazon.com/AWSEC2/latest/APIReference/API_RevokeSecurityGroupEgress.html
    """
    call = client.revoke_security_group_egress if outbound else client.revoke_security_group_ingress
    call(GroupId=group_id, SecurityGroupRuleIds=[rule_id])


@dialog_view
def add_rule(request, resource_id, direction):
    direction = DIRECTIONS.get((direction or "").lower())
    title = f"Add {direction.lower()} rule" if direction else "Add rule"
    resource, profile, refusal = _dialog_guard(request, resource_id, title)
    if refusal:
        return refusal
    if not direction:
        return _dialog_message(title, "The direction must be inbound or outbound.")
    group_id = _attr(resource, MARKER_FIELD)
    try:
        client = _client(resource)
        group = _get_group(client, group_id)
    except SGError as exc:
        return _dialog_message(title, str(exc))

    list_id = f"sgx-peers-{uuid.uuid4().hex[:8]}"
    if request.method == "POST":
        form = SecurityGroupRuleForm(request.POST, direction=direction, peer_list=list_id)
        if form.is_valid():
            try:
                created = _authorize(client, group_id, form)
            except (BotoCoreError, ClientError) as exc:
                form.add_error(None, f"AWS rejected the rule: {_aws_message(exc)}")
            else:
                ids = ", ".join(created) or "rule"
                _record(resource, profile, f"{direction} rule {ids} added ({_summary(form, direction)}).")
                messages.success(request, f"{direction} rule {ids} added.")
                return _redirect_back(request, resource)
    else:
        form = SecurityGroupRuleForm(direction=direction, peer_list=list_id)

    return {
        "title": title,
        "content": format_html(
            "<p>The rule is added to security group <b>{}</b> ({}) in AWS as soon as you save it.</p>",
            group.get("GroupName", ""), group_id,
        ),
        "form": form,
        "bottom_content": _form_extras(client, group, list_id),
        "use_ajax": True,
        "action_url": reverse("aws_sg_add_rule", args=[resource.id, direction.lower()]),
        "submit": "Add",
    }


def _save_edit(request, resource, profile, client, group_id, rule, form):
    """Write the edited rule to AWS. Returns a redirect, or None with the
    problem added to the form so the dialog shows it.

    ModifySecurityGroupRules cannot change a rule's peer kind (an IPv4 CIDR
    stays an IPv4 CIDR), so a rule whose kind changes is replaced: the new
    rule is added first, then the old one is removed.
    https://docs.aws.amazon.com/AWSEC2/latest/APIReference/API_ModifySecurityGroupRules.html
    https://docs.aws.amazon.com/AWSEC2/latest/APIReference/API_SecurityGroupRuleRequest.html
    https://docs.aws.amazon.com/AWSEC2/latest/APIReference/API_CreateTags.html
    """
    rule_id = rule["SecurityGroupRuleId"]
    direction = form.direction
    summary = _summary(form, direction)
    old_kind, _ = peer_of(rule)
    new_kind, _ = form.cleaned_data["peers"][0]

    if new_kind != old_kind:
        try:
            created = _authorize(client, group_id, form)
        except (BotoCoreError, ClientError) as exc:
            form.add_error(None, f"AWS rejected the rule: {_aws_message(exc)}")
            return None
        new_id = ", ".join(created) or "a new rule"
        try:
            _revoke(client, group_id, rule_id, form.outbound)
        except (BotoCoreError, ClientError) as exc:
            _record(resource, profile, f"{direction} rule {new_id} added to replace {rule_id} ({summary}); "
                                       f"removing {rule_id} failed: {_aws_message(exc)}")
            messages.warning(request, f"Added {new_id}, but AWS could not remove {rule_id}: {_aws_message(exc)}. "
                                      "Delete it from the list.")
            return _redirect_back(request, resource)
        _record(resource, profile, f"{direction} rule {rule_id} replaced by {new_id} ({summary}).")
        messages.success(request, f"{direction} rule {rule_id} replaced by {new_id}.")
        return _redirect_back(request, resource)

    try:
        client.modify_security_group_rules(
            GroupId=group_id,
            SecurityGroupRules=[{"SecurityGroupRuleId": rule_id, "SecurityGroupRule": form.to_rule_request()}],
        )
    except (BotoCoreError, ClientError) as exc:
        form.add_error(None, f"AWS rejected the rule: {_aws_message(exc)}")
        return None

    new_name, old_name = form.cleaned_data["name"], name_tag(rule)
    if new_name != old_name:
        try:
            if new_name:
                client.create_tags(Resources=[rule_id], Tags=[{"Key": "Name", "Value": new_name}])
            else:
                client.delete_tags(Resources=[rule_id], Tags=[{"Key": "Name"}])
        except (BotoCoreError, ClientError) as exc:
            messages.warning(request, f"Rule {rule_id} updated, but AWS could not change its Name: {_aws_message(exc)}")

    _record(resource, profile, f"{direction} rule {rule_id} updated ({summary}).")
    messages.success(request, f"{direction} rule {rule_id} updated.")
    return _redirect_back(request, resource)


@dialog_view
def edit_rule(request, resource_id, rule_id):
    title = f"Edit rule {rule_id}"
    resource, profile, refusal = _dialog_guard(request, resource_id, title)
    if refusal:
        return refusal
    group_id = _attr(resource, MARKER_FIELD)
    try:
        client = _client(resource)
        group = _get_group(client, group_id)
        rules = _get_rules(client, group_id, rule_id)
    except SGError as exc:
        return _dialog_message(title, str(exc))
    if not rules:
        return _dialog_message(title, f"Security group {group_id} has no rule {rule_id}.")
    rule = rules[0]
    if not _editable(rule):
        return _dialog_message(
            title, "This rule references a security group in another account or through a VPC peering "
                   "connection, which this form cannot edit. Edit it in the AWS console."
        )

    direction = "Outbound" if rule.get("IsEgress") else "Inbound"
    list_id = f"sgx-peers-{uuid.uuid4().hex[:8]}"
    initial = SecurityGroupRuleForm.initial_from_rule(rule)
    if request.method == "POST":
        form = SecurityGroupRuleForm(request.POST, initial=initial, direction=direction, editing=True, peer_list=list_id)
        if form.is_valid():
            done = _save_edit(request, resource, profile, client, group_id, rule, form)
            if done:
                return done
    else:
        form = SecurityGroupRuleForm(initial=initial, direction=direction, editing=True, peer_list=list_id)

    return {
        "title": title,
        "content": format_html(
            "<p>{} rule of security group <b>{}</b> ({}). Changes apply in AWS as soon as you save them.</p>",
            direction, group.get("GroupName", ""), group_id,
        ),
        "form": form,
        "bottom_content": _form_extras(client, group, list_id),
        "use_ajax": True,
        "action_url": reverse("aws_sg_edit_rule", args=[resource.id, rule_id]),
        "submit": "Save",
    }


@dialog_view
def delete_rule(request, resource_id, rule_id):
    title = "Delete rule"
    resource, profile, refusal = _dialog_guard(request, resource_id, title)
    if refusal:
        return refusal
    group_id = _attr(resource, MARKER_FIELD)
    try:
        client = _client(resource)
        group = _get_group(client, group_id)
        rules = _get_rules(client, group_id, rule_id)
    except SGError as exc:
        return _dialog_message(title, str(exc))
    if not rules:
        return _dialog_message(title, f"Security group {group_id} has no rule {rule_id}.")
    rule = rules[0]
    outbound = bool(rule.get("IsEgress"))
    direction = "Outbound" if outbound else "Inbound"
    key = rule_type_key(rule.get("IpProtocol"), rule.get("FromPort"), rule.get("ToPort"))
    port = _port_range(protocol_name(rule.get("IpProtocol")), key, rule.get("FromPort"), rule.get("ToPort"))
    _, peer = peer_of(rule)

    if request.method == "POST":
        form = ConfirmForm(request.POST)
        if form.is_valid():
            try:
                _revoke(client, group_id, rule_id, outbound)
            except (BotoCoreError, ClientError) as exc:
                form.add_error(None, f"AWS could not delete the rule: {_aws_message(exc)}")
            else:
                _record(resource, profile, f"{direction} rule {rule_id} deleted ({TYPES[key][0]}, port {port}, "
                                           f"{'from' if direction == 'Inbound' else 'to'} {peer}).")
                messages.success(request, f"{direction} rule {rule_id} deleted.")
                return _redirect_back(request, resource)
    else:
        form = ConfirmForm()

    return {
        "title": title,
        "content": format_html(
            "<p>Delete the {} rule <b>{}</b> ({}, port {}, {} {}) from security group <b>{}</b>? "
            "AWS applies this at once and it cannot be undone.</p>",
            direction.lower(), rule_id, TYPES[key][0], port,
            "from" if direction == "Inbound" else "to", peer, group.get("GroupName", ""),
        ),
        "form": form,
        "use_ajax": True,
        "action_url": reverse("aws_sg_delete_rule", args=[resource.id, rule_id]),
        "submit": "Delete",
    }
