"""
Form for creating/editing an Azure NSG security rule.

Field names and accepted values follow the azure.mgmt.network SecurityRule
model:
https://learn.microsoft.com/en-us/python/api/azure-mgmt-network/azure.mgmt.network.models.securityrule

Azure requires exactly one of the singular (source_address_prefix) or plural
(source_address_prefixes) form for addresses and ports. This form accepts a
comma-separated string and emits the singular form for one value or the plural
list for several -- see to_params().
"""
from django import forms

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


class SecurityRuleForm(C2Form):
    def __init__(self, *args, **kwargs):
        # direction is fixed per table (Inbound/Outbound); editing locks the name.
        self.direction = kwargs.pop("direction", "Inbound")
        self.editing = kwargs.pop("editing", False)
        super(SecurityRuleForm, self).__init__(*args, **kwargs)
        initial = kwargs.get("initial", {}) or {}

        self.fields["name"] = forms.CharField(
            label="Name",
            required=True,
            initial=initial.get("name"),
            help_text="Unique name for this security rule within the NSG.",
        )
        if self.editing:
            # The rule name is the Azure key; do not allow renaming on edit.
            self.fields["name"].widget.attrs["readonly"] = True

        self.fields["priority"] = forms.IntegerField(
            label="Priority",
            required=True,
            min_value=100,
            max_value=4096,
            initial=initial.get("priority"),
            help_text="100-4096. Lower numbers are evaluated first.",
        )
        self.fields["access"] = forms.ChoiceField(
            label="Action",
            required=True,
            choices=ACCESS_CHOICES,
            initial=initial.get("access", "Allow"),
            widget=forms.Select(),
        )
        self.fields["protocol"] = forms.ChoiceField(
            label="Protocol",
            required=True,
            choices=PROTOCOL_CHOICES,
            initial=initial.get("protocol", "*"),
            widget=forms.Select(),
        )
        self.fields["source_address_prefix"] = forms.CharField(
            label="Source",
            required=True,
            initial=initial.get("source_address_prefix", "*"),
            help_text=(
                "CIDR, IP, service tag (e.g. VirtualNetwork, Internet, "
                "AzureLoadBalancer) or * for Any. Comma-separate for multiple."
            ),
        )
        self.fields["source_port_range"] = forms.CharField(
            label="Source port ranges",
            required=True,
            initial=initial.get("source_port_range", "*"),
            help_text="A port, range (e.g. 1024-2048) or * for Any.",
        )
        self.fields["destination_address_prefix"] = forms.CharField(
            label="Destination",
            required=True,
            initial=initial.get("destination_address_prefix", "*"),
            help_text=(
                "CIDR, IP, service tag or * for Any. Comma-separate for multiple."
            ),
        )
        self.fields["destination_port_range"] = forms.CharField(
            label="Destination port ranges",
            required=True,
            initial=initial.get("destination_port_range", "*"),
            help_text="A port, range (e.g. 80,443 -> use comma) or * for Any.",
        )
        self.fields["description"] = forms.CharField(
            label="Description",
            required=False,
            initial=initial.get("description", ""),
            widget=forms.Textarea(attrs={"rows": 2}),
        )

    # -- helpers -----------------------------------------------------------
    @staticmethod
    def initial_from_rule(rule):
        """Build an initial dict from a live azure.mgmt.network SecurityRule."""

        def join(single, plural):
            if plural:
                return ", ".join(plural)
            return single or "*"

        return {
            "name": rule.name,
            "priority": rule.priority,
            "access": rule.access,
            "protocol": rule.protocol,
            "source_address_prefix": join(
                rule.source_address_prefix, rule.source_address_prefixes
            ),
            "source_port_range": join(
                rule.source_port_range, rule.source_port_ranges
            ),
            "destination_address_prefix": join(
                rule.destination_address_prefix, rule.destination_address_prefixes
            ),
            "destination_port_range": join(
                rule.destination_port_range, rule.destination_port_ranges
            ),
            "description": rule.description or "",
        }

    @staticmethod
    def _single_or_plural(raw, single_key, plural_key):
        """Emit the singular Azure key for one value, the plural for several."""
        values = [v.strip() for v in (raw or "").split(",") if v.strip()]
        if not values:
            return {single_key: "*"}
        if len(values) == 1:
            return {single_key: values[0]}
        return {plural_key: values}

    def to_params(self):
        """Assemble the SecurityRule payload for begin_create_or_update."""
        cd = self.cleaned_data
        params = {
            "priority": cd["priority"],
            "direction": self.direction,
            "access": cd["access"],
            "protocol": cd["protocol"],
            "description": cd.get("description") or "",
        }
        params.update(
            self._single_or_plural(
                cd["source_address_prefix"],
                "source_address_prefix",
                "source_address_prefixes",
            )
        )
        params.update(
            self._single_or_plural(
                cd["source_port_range"], "source_port_range", "source_port_ranges"
            )
        )
        params.update(
            self._single_or_plural(
                cd["destination_address_prefix"],
                "destination_address_prefix",
                "destination_address_prefixes",
            )
        )
        params.update(
            self._single_or_plural(
                cd["destination_port_range"],
                "destination_port_range",
                "destination_port_ranges",
            )
        )
        return params

    def clean(self):
        return super(SecurityRuleForm, self).clean()
