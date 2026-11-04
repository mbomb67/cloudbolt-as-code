"""
Form for adding and editing an AWS security group rule, with the fields of
the AWS console's Edit inbound rules page: Type, Protocol, Port range,
Source (or Destination) type, Source, Description, plus the rule's Name tag.

Type is the console's preset list. Choosing one fixes the protocol and port
range (SSH is TCP 22); the Custom types leave them open. The dialog's script
(_sg_rule_form_script.html) shows and locks the matching fields as the type
changes, and clean() applies the preset again on the server, so the preset
wins over whatever the browser sent.

Rule fields follow the EC2 API, which takes exactly one peer per rule: an
IPv4 CIDR, an IPv6 CIDR, a prefix list ID or a security group ID. Adding
accepts several peers separated by commas, one rule each, as the console
does; editing changes one rule.
https://docs.aws.amazon.com/AWSEC2/latest/APIReference/API_SecurityGroupRuleRequest.html
https://docs.aws.amazon.com/AWSEC2/latest/APIReference/API_IpPermission.html
https://docs.aws.amazon.com/AWSEC2/latest/APIReference/API_IpRange.html
https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/security-group-rules-reference.html
"""
import ipaddress
import re

from django import forms
from django.core.exceptions import ValidationError

from common.forms import C2Form


# (key, console label, protocol, from port, to port). A None port is open
# for the user; the Custom types and Custom protocol take theirs from the form.
RULE_TYPES = [
    ("custom-tcp", "Custom TCP", "tcp", None, None),
    ("custom-udp", "Custom UDP", "udp", None, None),
    ("custom-icmp", "Custom ICMP - IPv4", "icmp", None, None),
    ("custom-icmpv6", "Custom ICMP - IPv6", "icmpv6", None, None),
    ("custom-protocol", "Custom protocol", None, None, None),
    ("all-tcp", "All TCP", "tcp", 0, 65535),
    ("all-udp", "All UDP", "udp", 0, 65535),
    ("all-icmp", "All ICMP - IPv4", "icmp", -1, -1),
    ("all-icmpv6", "All ICMP - IPv6", "icmpv6", -1, -1),
    ("all", "All traffic", "-1", None, None),
    ("ssh", "SSH", "tcp", 22, 22),
    ("smtp", "SMTP", "tcp", 25, 25),
    ("dns-udp", "DNS (UDP)", "udp", 53, 53),
    ("dns-tcp", "DNS (TCP)", "tcp", 53, 53),
    ("http", "HTTP", "tcp", 80, 80),
    ("pop3", "POP3", "tcp", 110, 110),
    ("imap", "IMAP", "tcp", 143, 143),
    ("ldap", "LDAP", "tcp", 389, 389),
    ("https", "HTTPS", "tcp", 443, 443),
    ("smb", "SMB", "tcp", 445, 445),
    ("smtps", "SMTPS", "tcp", 465, 465),
    ("imaps", "IMAPS", "tcp", 993, 993),
    ("pop3s", "POP3S", "tcp", 995, 995),
    ("mssql", "MSSQL", "tcp", 1433, 1433),
    ("oracle-rds", "Oracle-RDS", "tcp", 1521, 1521),
    ("nfs", "NFS", "tcp", 2049, 2049),
    ("mysql", "MYSQL/Aurora", "tcp", 3306, 3306),
    ("rdp", "RDP", "tcp", 3389, 3389),
    ("postgresql", "PostgreSQL", "tcp", 5432, 5432),
    ("redshift", "Redshift", "tcp", 5439, 5439),
    ("winrm-http", "WinRM-HTTP", "tcp", 5985, 5985),
    ("winrm-https", "WinRM-HTTPS", "tcp", 5986, 5986),
]
TYPES = {key: (label, protocol, low, high) for key, label, protocol, low, high in RULE_TYPES}
TYPE_CHOICES = [(key, label) for key, label, _, _, _ in RULE_TYPES]
CUSTOM_PORT_TYPES = ("custom-tcp", "custom-udp")
CUSTOM_ICMP_TYPES = ("custom-icmp", "custom-icmpv6")

# Protocol numbers the API also reports by name.
# https://www.iana.org/assignments/protocol-numbers/protocol-numbers.xhtml
PROTOCOL_NAMES = {"6": "tcp", "17": "udp", "1": "icmp", "58": "icmpv6"}
PROTOCOL_LABELS = {"tcp": "TCP", "udp": "UDP", "icmp": "ICMP", "icmpv6": "ICMPv6", "-1": "All"}

PEER_TYPE_CHOICES = [
    ("custom", "Custom"),
    ("anywhere-ipv4", "Anywhere-IPv4"),
    ("anywhere-ipv6", "Anywhere-IPv6"),
]
ANYWHERE = {"anywhere-ipv4": "0.0.0.0/0", "anywhere-ipv6": "::/0"}

# Peer kinds and their keys in SecurityGroupRuleRequest and IpPermission.
PEER_KINDS = {
    "ipv4": ("CidrIpv4", "IpRanges", "CidrIp"),
    "ipv6": ("CidrIpv6", "Ipv6Ranges", "CidrIpv6"),
    "prefix": ("PrefixListId", "PrefixListIds", "PrefixListId"),
    "group": ("ReferencedGroupId", "UserIdGroupPairs", "GroupId"),
}
GROUP_ID_RE = re.compile(r"^sg-[0-9a-f]{8}(?:[0-9a-f]{9})?$")
PREFIX_LIST_ID_RE = re.compile(r"^pl-[0-9a-f]{8}(?:[0-9a-f]{9})?$")
PORT_RANGE_RE = re.compile(r"^(\d{1,5})(?:\s*-\s*(\d{1,5}))?$")

# Descriptions and Name tags: up to 255 letters, digits, spaces and
# ._-:/()#,@[]+=&;{}!$* (descriptions); tag values allow any characters.
DESCRIPTION_RE = re.compile(r"^[A-Za-z0-9 ._\-:/()#,@\[\]+=&;{}!$*]*$")


def protocol_name(protocol):
    protocol = str(protocol if protocol is not None else "")
    return PROTOCOL_NAMES.get(protocol, protocol)


def rule_type_key(protocol, low, high):
    """The console type of a rule's protocol and ports."""
    protocol = protocol_name(protocol)
    for key, _, preset_protocol, preset_low, preset_high in RULE_TYPES:
        if preset_protocol == protocol and preset_low is not None and (low, high) == (preset_low, preset_high):
            return key
    if protocol == "-1":
        return "all"
    if protocol == "tcp":
        return "custom-tcp"
    if protocol == "udp":
        return "custom-udp"
    if protocol == "icmp":
        return "custom-icmp"
    if protocol == "icmpv6":
        return "custom-icmpv6"
    return "custom-protocol"


def parse_peer(raw):
    """(kind, value) for one source or destination, canonicalized as AWS stores it."""
    value = raw.strip()
    if GROUP_ID_RE.match(value.lower()):
        return "group", value.lower()
    if PREFIX_LIST_ID_RE.match(value.lower()):
        return "prefix", value.lower()
    try:
        network = ipaddress.ip_network(value, strict=False)
    except ValueError:
        raise ValidationError(
            f"'{value}' is not a CIDR block (10.0.0.0/16, 2001:db8::/32), an IP address, a security group "
            "ID (sg-...) or a prefix list ID (pl-...)."
        )
    return ("ipv4" if network.version == 4 else "ipv6"), str(network)


def peer_of(rule):
    """(kind, value) of a SecurityGroupRule from DescribeSecurityGroupRules."""
    if rule.get("CidrIpv4"):
        return "ipv4", rule["CidrIpv4"]
    if rule.get("CidrIpv6"):
        return "ipv6", rule["CidrIpv6"]
    if rule.get("PrefixListId"):
        return "prefix", rule["PrefixListId"]
    group = rule.get("ReferencedGroupInfo") or {}
    return "group", group.get("GroupId", "")


def name_tag(rule):
    return next((tag["Value"] for tag in rule.get("Tags", []) if tag.get("Key") == "Name"), "")


class SecurityGroupRuleForm(C2Form):
    rule_type = forms.ChoiceField(label="Type", choices=TYPE_CHOICES, initial="custom-tcp")
    protocol_number = forms.IntegerField(
        label="Protocol",
        required=False,
        min_value=0,
        max_value=255,
        help_text="An IP protocol number, for example 47 for GRE or 50 for ESP. The rule covers all ports.",
    )
    port_range = forms.CharField(
        label="Port range",
        required=False,
        help_text="A port such as 8080 or a range such as 8000-8100.",
    )
    icmp_type = forms.IntegerField(
        label="ICMP type",
        required=False,
        min_value=-1,
        max_value=255,
        initial=-1,
        help_text="-1 for all types, for example 8 for echo request.",
    )
    icmp_code = forms.IntegerField(
        label="ICMP code",
        required=False,
        min_value=-1,
        max_value=255,
        initial=-1,
        help_text="-1 for all codes.",
    )
    peer_type = forms.ChoiceField(label="Source type", choices=PEER_TYPE_CHOICES, initial="custom")
    peer = forms.CharField(
        label="Source",
        required=False,
        help_text=(
            "A CIDR block (10.0.0.0/16), an IP address, a security group ID (sg-...) or a prefix list ID "
            "(pl-...). Separate several with commas to add one rule for each."
        ),
    )
    description = forms.CharField(label="Description", required=False, max_length=255)
    name = forms.CharField(
        label="Name",
        required=False,
        max_length=255,
        help_text="Optional Name tag shown in the rule list.",
    )

    def __init__(self, *args, **kwargs):
        # Direction is fixed by the table the dialog was opened from, or by
        # the rule being edited. Editing changes one rule, so one peer.
        self.direction = kwargs.pop("direction", "Inbound")
        self.editing = kwargs.pop("editing", False)
        peer_list = kwargs.pop("peer_list", "")
        super().__init__(*args, **kwargs)
        self.outbound = self.direction == "Outbound"
        if self.outbound:
            self.fields["peer_type"].label = "Destination type"
            self.fields["peer"].label = "Destination"
        if self.editing:
            self.fields["peer"].help_text = (
                "A CIDR block, an IP address, a security group ID (sg-...) or a prefix list ID (pl-...)."
            )
        if peer_list:
            self.fields["peer"].widget.attrs["list"] = peer_list
        self.fields["peer"].widget.attrs["autocomplete"] = "off"

    def clean_description(self):
        description = self.cleaned_data["description"].strip()
        if not DESCRIPTION_RE.match(description):
            raise ValidationError("Use letters, digits, spaces and ._-:/()#,@[]+=&;{}!$* only.")
        return description

    def clean_name(self):
        return self.cleaned_data["name"].strip()

    def _ports(self, data):
        """(protocol, from port, to port) for the chosen type."""
        key = data.get("rule_type")
        _, protocol, low, high = TYPES[key]
        if key in CUSTOM_PORT_TYPES:
            match = PORT_RANGE_RE.match((data.get("port_range") or "").strip())
            low = int(match.group(1)) if match else None
            high = int(match.group(2) or match.group(1)) if match else None
            if not match or high > 65535 or low > high:
                self.add_error("port_range", "Enter a port from 0 to 65535 or a range such as 8000-8100.")
        elif key in CUSTOM_ICMP_TYPES:
            low = data.get("icmp_type")
            high = data.get("icmp_code")
            low = -1 if low is None else low
            high = -1 if high is None else high
            if low == -1 and high != -1:
                self.add_error("icmp_code", "All ICMP types (-1) covers all codes, so the code must be -1 too.")
        elif key == "custom-protocol":
            number = data.get("protocol_number")
            if number is None:
                self.add_error("protocol_number", "Enter an IP protocol number from 0 to 255.")
            elif str(number) in PROTOCOL_NAMES:
                self.add_error(
                    "protocol_number",
                    f"Protocol {number} is {PROTOCOL_NAMES[str(number)].upper()}; choose that type instead.",
                )
            protocol = str(number)
        return protocol, low, high

    def clean(self):
        data = super().clean()
        if data.get("rule_type") not in TYPES:
            return data
        data["protocol"], data["from_port"], data["to_port"] = self._ports(data)

        raw = ANYWHERE.get(data.get("peer_type")) or data.get("peer") or ""
        values = [value for value in (part.strip() for part in raw.split(",")) if value]
        if not values:
            self.add_error("peer", f"Enter a {self.fields['peer'].label.lower()}.")
            return data
        if self.editing and len(values) > 1:
            self.add_error("peer", "A rule has one peer. Add a rule for each additional one.")
            return data
        peers = []
        for value in values:
            try:
                peer = parse_peer(value)
            except ValidationError as exc:
                self.add_error("peer", exc)
                return data
            if peer not in peers:
                peers.append(peer)
        data["peers"] = peers
        return data

    @staticmethod
    def initial_from_rule(rule):
        """Initial values from a SecurityGroupRule (DescribeSecurityGroupRules)."""
        low, high = rule.get("FromPort"), rule.get("ToPort")
        key = rule_type_key(rule.get("IpProtocol"), low, high)
        _, peer = peer_of(rule)
        peer_type = next((choice for choice, cidr in ANYWHERE.items() if cidr == peer), "custom")
        initial = {
            "rule_type": key,
            "peer_type": peer_type,
            "peer": peer,
            "description": rule.get("Description") or "",
            "name": name_tag(rule),
        }
        if key in CUSTOM_PORT_TYPES:
            initial["port_range"] = str(low) if low == high else f"{low}-{high}"
        elif key in CUSTOM_ICMP_TYPES:
            initial["icmp_type"], initial["icmp_code"] = low, high
        elif key == "custom-protocol":
            initial["protocol_number"] = rule.get("IpProtocol")
        return initial

    def _ports_request(self):
        data = self.cleaned_data
        request = {"IpProtocol": data["protocol"]}
        if data["from_port"] is not None:
            request["FromPort"] = data["from_port"]
            request["ToPort"] = data["to_port"]
        return request

    def to_rule_request(self):
        """The SecurityGroupRuleRequest for ModifySecurityGroupRules (one peer).
        https://docs.aws.amazon.com/AWSEC2/latest/APIReference/API_SecurityGroupRuleRequest.html
        """
        kind, value = self.cleaned_data["peers"][0]
        request = self._ports_request()
        request[PEER_KINDS[kind][0]] = value
        request["Description"] = self.cleaned_data["description"]
        return request

    def to_ip_permission(self):
        """The IpPermission for Authorize*: one rule per peer.
        https://docs.aws.amazon.com/AWSEC2/latest/APIReference/API_IpPermission.html
        """
        permission = self._ports_request()
        description = self.cleaned_data["description"]
        for kind, value in self.cleaned_data["peers"]:
            _, list_key, value_key = PEER_KINDS[kind]
            entry = {value_key: value}
            if description:
                entry["Description"] = description
            permission.setdefault(list_key, []).append(entry)
        return permission


class ConfirmForm(C2Form):
    """No fields: submitting the dialog confirms."""
