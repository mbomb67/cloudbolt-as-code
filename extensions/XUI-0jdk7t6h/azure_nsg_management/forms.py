"""
Form for creating and editing an Azure NSG security rule, with the fields in
the order of the Azure portal's Add security rule pane (Service omitted).

Field names and values follow the azure.mgmt.network SecurityRule model, and
the rules for names, priorities, addresses and ports follow Microsoft's NSG
overview:
https://learn.microsoft.com/en-us/python/api/azure-mgmt-network/azure.mgmt.network.models.securityrule
https://learn.microsoft.com/en-us/azure/virtual-network/network-security-groups-overview#security-rules
https://learn.microsoft.com/en-us/azure/azure-resource-manager/management/resource-name-rules#microsoftnetwork

Azure takes either the singular (source_address_prefix) or the plural
(source_address_prefixes) form of an address or port field. The form accepts
a comma-separated list and sends the singular form for one value and the
plural list for several (an augmented rule); see to_rule().
"""
import ipaddress
import re

from azure.mgmt.network.models import SecurityRule
from django import forms
from django.core.exceptions import ValidationError

from common.forms import C2Form


ACCESS_CHOICES = [("Allow", "Allow"), ("Deny", "Deny")]
PROTOCOL_CHOICES = [
    ("*", "Any"),
    ("Tcp", "TCP"),
    ("Udp", "UDP"),
    ("Icmp", "ICMP"),
    ("Esp", "ESP"),
    ("Ah", "AH"),
]

# Rule names: 1-80 letters, digits, underscores, periods and hyphens, starting
# with a letter or digit and ending with a letter, digit or underscore.
RULE_NAME_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9_.-]{0,78}[A-Za-z0-9_])?$")
PORT_RE = re.compile(r"^(\d{1,5})(?:-(\d{1,5}))?$")
ANY_VALUES = ("*", "any")


def _split(raw):
    return [value.strip() for value in (raw or "").split(",") if value.strip()]


def _is_address(value):
    """An IP address, CIDR block or IP range (first-last), as opposed to a service tag."""
    try:
        ipaddress.ip_network(value, strict=False)
        return True
    except ValueError:
        pass
    first, _, last = value.partition("-")
    try:
        ipaddress.ip_address(first.strip())
        ipaddress.ip_address(last.strip())
        return True
    except ValueError:
        return False


def _clean_any(values, what):
    """['*'] for an empty field or Any, which cannot be combined with other values."""
    if not values or any(value.lower() in ANY_VALUES for value in values):
        if len(values) > 1:
            raise ValidationError(f"Any (*) cannot be combined with other {what}.")
        return ["*"]
    return None


def clean_addresses(raw):
    values = _split(raw)
    any_value = _clean_any(values, "addresses")
    if any_value:
        return any_value
    tags = [value for value in values if not _is_address(value)]
    if tags and len(values) > 1:
        raise ValidationError(
            f"A service tag ({tags[0]}) must be the only value. List several IP addresses or CIDR "
            "blocks, or one service tag."
        )
    return values


def clean_ports(raw):
    values = _split(raw)
    any_value = _clean_any(values, "ports")
    if any_value:
        return any_value
    for value in values:
        match = PORT_RE.match(value)
        low = int(match.group(1)) if match else None
        high = int(match.group(2) or match.group(1)) if match else None
        if not match or high > 65535 or low > high:
            raise ValidationError(
                f"'{value}' is not a port or port range. Use ports from 0 to 65535, ranges such as "
                "1024-2048, or * for any."
            )
    return values


def _single_or_plural(values, single_key, plural_key):
    if len(values) == 1:
        return {single_key: values[0]}
    return {plural_key: values}


class SecurityRuleForm(C2Form):
    source_address_prefix = forms.CharField(
        label="Source",
        initial="*",
        help_text=(
            "An IP address, CIDR block (10.0.0.0/24) or service tag (VirtualNetwork, Internet, "
            "AzureLoadBalancer), or * for any. Separate several addresses with commas."
        ),
    )
    source_port_range = forms.CharField(
        label="Source port ranges",
        initial="*",
        help_text="A port, a range such as 1024-2048, or * for any. Separate several with commas.",
    )
    destination_address_prefix = forms.CharField(
        label="Destination",
        initial="*",
        help_text="An IP address, CIDR block or service tag, or * for any. Separate several addresses with commas.",
    )
    destination_port_range = forms.CharField(
        label="Destination port ranges",
        initial="*",
        help_text="A port, a range, or * for any. Separate several with commas, for example 80, 443, 8000-8080.",
    )
    protocol = forms.ChoiceField(label="Protocol", choices=PROTOCOL_CHOICES, initial="*")
    access = forms.ChoiceField(label="Action", choices=ACCESS_CHOICES, initial="Allow")
    priority = forms.IntegerField(
        label="Priority",
        min_value=100,
        max_value=4096,
        help_text="100-4096, unique per direction. Lower numbers are evaluated first.",
    )
    name = forms.CharField(
        label="Name",
        max_length=80,
        help_text="Up to 80 letters, digits, underscores, periods and hyphens.",
    )
    description = forms.CharField(
        label="Description",
        required=False,
        max_length=140,
        widget=forms.Textarea(attrs={"rows": 2}),
    )

    def __init__(self, *args, **kwargs):
        # Direction is fixed by the table the dialog was opened from, or by
        # the rule being edited. Editing locks the name, Azure's key for it.
        self.direction = kwargs.pop("direction", "Inbound")
        editing = kwargs.pop("editing", False)
        super().__init__(*args, **kwargs)
        if editing:
            self.fields["name"].disabled = True
            self.fields["name"].help_text = "The name identifies the rule in Azure and cannot be changed."

    def clean_name(self):
        name = self.cleaned_data["name"].strip()
        if not RULE_NAME_RE.match(name):
            raise ValidationError(
                "Use letters, digits, underscores, periods and hyphens, starting with a letter or digit "
                "and ending with a letter, digit or underscore."
            )
        return name

    def clean_source_address_prefix(self):
        return clean_addresses(self.cleaned_data["source_address_prefix"])

    def clean_destination_address_prefix(self):
        return clean_addresses(self.cleaned_data["destination_address_prefix"])

    def clean_source_port_range(self):
        return clean_ports(self.cleaned_data["source_port_range"])

    def clean_destination_port_range(self):
        return clean_ports(self.cleaned_data["destination_port_range"])

    @staticmethod
    def initial_from_rule(rule):
        """Initial values from a live azure.mgmt.network SecurityRule."""

        def join(single, plural):
            if plural:
                return ", ".join(plural)
            return single or "*"

        return {
            "name": rule.name,
            "priority": rule.priority,
            "access": rule.access,
            "protocol": rule.protocol,
            "source_address_prefix": join(rule.source_address_prefix, rule.source_address_prefixes),
            "source_port_range": join(rule.source_port_range, rule.source_port_ranges),
            "destination_address_prefix": join(
                rule.destination_address_prefix, rule.destination_address_prefixes
            ),
            "destination_port_range": join(rule.destination_port_range, rule.destination_port_ranges),
            "description": rule.description or "",
        }

    def to_rule(self):
        """The SecurityRule for security_rules.begin_create_or_update, which
        replaces the whole rule, so fields left out are cleared."""
        data = self.cleaned_data
        params = {
            "priority": data["priority"],
            "direction": self.direction,
            "access": data["access"],
            "protocol": data["protocol"],
            "description": data.get("description") or None,
        }
        params.update(_single_or_plural(
            data["source_address_prefix"], "source_address_prefix", "source_address_prefixes"
        ))
        params.update(_single_or_plural(data["source_port_range"], "source_port_range", "source_port_ranges"))
        params.update(_single_or_plural(
            data["destination_address_prefix"], "destination_address_prefix", "destination_address_prefixes"
        ))
        params.update(_single_or_plural(
            data["destination_port_range"], "destination_port_range", "destination_port_ranges"
        ))
        return SecurityRule(**params)


class ConfirmForm(C2Form):
    """No fields: submitting the dialog confirms."""
