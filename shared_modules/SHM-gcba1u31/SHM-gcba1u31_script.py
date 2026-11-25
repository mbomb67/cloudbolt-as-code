"""
Active Directory helpers for the Windows domain join content.

The domain comes from the LDAP Utility that a server's domain_to_join parameter
names. This module binds with that utility's service account, searches the
domain for organizational units, finds and deletes computer objects, and
deletes the DNS records a computer registered in AD-integrated zones. Its
callers are the OU suggestions action, the Windows join action, and the
computer object cleanup action.

The bind is the LDAP Utility's own (LDAPUtility._init_ldap_sudo in CloudBolt's
utilities/models/models.py, the call LDAPUtility.runSearch makes), so it uses
the utility's protocol, address, and port, and the appliance's LDAP CA file
setting, exactly as CloudBolt's LDAP sign-in does.

References:
  python-ldap LDAPObject:  https://www.python-ldap.org/en/latest/reference/ldap.html
  Filter escaping:         https://www.python-ldap.org/en/latest/reference/ldap-filter.html
  DN parsing:              https://www.python-ldap.org/en/latest/reference/ldap-dn.html
  Controls:                https://www.python-ldap.org/en/latest/reference/ldap-controls.html
  AD tree delete control:  https://learn.microsoft.com/en-us/previous-versions/windows/desktop/ldap/ldap-server-tree-delete-oid
  AD SD flags control:     https://learn.microsoft.com/en-us/previous-versions/windows/desktop/ldap/ldap-server-sd-flags-oid
  Security descriptor:     https://learn.microsoft.com/en-us/openspecs/windows_protocols/ms-dtyp/7d4dac05-9cef-4563-a058-f108abecce1d
  SID layout:              https://learn.microsoft.com/en-us/openspecs/windows_protocols/ms-dtyp/f992ad60-0fe4-4b87-9fed-beb478836861
  dnsRecord layout:        https://learn.microsoft.com/en-us/openspecs/windows_protocols/ms-dnsp/6912b338-5472-4f59-b912-0edb536b6ed8
  Record types:            https://learn.microsoft.com/en-us/openspecs/windows_protocols/ms-dnsp/ac793981-1c60-43b8-be59-cdbb5c4ecb8a
  Deleting a node's last record (dnsTombstoned):
                           https://learn.microsoft.com/en-us/openspecs/windows_protocols/ms-dnsp/37eea101-00b8-4958-99eb-de8cda4b8417
  DNS_RPC_RECORD_TS:       https://learn.microsoft.com/en-us/openspecs/windows_protocols/ms-dnsp/def7736a-dd09-4b4a-b8d6-6a702a7ecde0
"""
import socket
import struct
import time
import uuid
from datetime import datetime, timezone

import ldap
import ldap.dn
from ldap.controls import LDAPControl
from ldap.filter import escape_filter_chars

from utilities.models import LDAPUtility

# Seconds to wait for one LDAP response before giving up.
LDAP_TIMEOUT = 10

# LDAP_SERVER_TREE_DELETE_OID: deletes an object together with its children,
# such as the BitLocker recovery or service connection point objects AD can
# store under a computer object.
TREE_DELETE_OID = "1.2.840.113556.1.4.805"

# LDAP_SERVER_SD_FLAGS_OID asking for OWNER_SECURITY_INFORMATION (1) only, so
# reading nTSecurityDescriptor needs no right to the SACL. The value is the BER
# encoding of SEQUENCE { INTEGER 1 }.
SD_FLAGS_OID = "1.2.840.113556.1.4.801"
SD_OWNER_ONLY = b"\x30\x03\x02\x01\x01"

# dnsRecord values (MS-DNSP 2.3.2.2): a 24-byte header, then the record data.
DNS_RECORD_HEADER = 24
DNS_RECORD_VERSION = 5
DNS_TYPE_ZERO = 0x0000
DNS_TYPE_A = 0x0001
RANK_ZONE = 0xF0
# 1970-01-01 as a FILETIME: 100-nanosecond intervals since 1601-01-01 UTC.
FILETIME_UNIX_EPOCH = 116444736000000000

COMPUTER_ATTRS = ["objectGUID", "objectSid", "sAMAccountName", "dNSHostName", "whenCreated"]


def utilities():
    """Every LDAP Utility on the instance, the list the domain_to_join dropdown offers."""
    return list(LDAPUtility.objects.all())


def naming_context(conn):
    """
    The domain naming context of the directory conn is bound to: the
    defaultNamingContext attribute of its root DSE, e.g. DC=corp,DC=example,DC=com.
    Empty when the server does not publish one.
    https://learn.microsoft.com/en-us/windows/win32/adschema/rootdse
    """
    try:
        entries = conn.search_ext_s("", ldap.SCOPE_BASE, "(objectClass=*)", ["defaultNamingContext"])
    except ldap.LDAPError:
        return ""
    for _, entry in entries:
        values = (entry or {}).get("defaultNamingContext")
        if values:
            return values[0].decode("utf-8")
    return ""


# search_base per LDAP Utility for the life of the process (a job, or a web worker).
_BASES = {}


def search_base(ldap_utility, conn=None):
    """
    Where to search: the root of the domain the utility's directory server
    holds, read from the server itself, so OUs and computers outside a narrower
    Base DN set for user sign-in are still found. The utility's Domain field is
    not used: it is often a sign-in or e-mail domain, not the AD domain name.
    Falls back to the Base DN when the server publishes no naming context.
    """
    key = (ldap_utility.id, ldap_utility.ip, ldap_utility.port, ldap_utility.base_dn)
    if key not in _BASES:
        own = conn or connect(ldap_utility)
        try:
            base = naming_context(own)
        finally:
            if conn is None:
                own.unbind_s()
        _BASES[key] = base or ldap_utility.base_dn
    return _BASES[key]


def dn_domain(dn):
    """The DNS domain a DN lives in, from its DC components, lowercased."""
    try:
        rdns = ldap.dn.str2dn(dn)
    except ldap.DECODING_ERROR:
        return ""
    return ".".join(value for rdn in rdns for (attr, value, _) in rdn if attr.lower() == "dc").lower()


def canonical_name(dn):
    """A readable path for a DN: OU=Web,OU=Servers,DC=corp,DC=example,DC=com -> corp.example.com/Servers/Web."""
    try:
        rdns = ldap.dn.str2dn(dn)
    except ldap.DECODING_ERROR:
        return dn
    domain = ".".join(value for rdn in rdns for (attr, value, _) in rdn if attr.lower() == "dc")
    path = [value for rdn in rdns for (attr, value, _) in rdn if attr.lower() != "dc"]
    return "/".join([domain] + list(reversed(path)))


def is_dn(text):
    """True when text parses as a distinguished name with at least one DC component."""
    try:
        rdns = ldap.dn.str2dn(text)
    except ldap.DECODING_ERROR:
        return False
    return any(attr.lower() == "dc" for rdn in rdns for (attr, _, _) in rdn)


def connect(ldap_utility):
    """A bound python-ldap connection for the utility. The caller unbinds it."""
    if not ldap_utility.servicepasswd:
        raise RuntimeError(
            "LDAP Utility '%s' has no password. Enter it in Admin > LDAP Authentication."
            % ldap_utility.ldap_domain
        )
    conn = ldap_utility._init_ldap_sudo()
    conn.set_option(ldap.OPT_TIMEOUT, LDAP_TIMEOUT)
    return conn


def _search(conn, base, filterstr, attrs, limit):
    """
    Up to limit entries under base. Results are read one at a time
    (result with all=0), so reaching the size limit keeps the entries already
    returned instead of raising them away. Referrals have no DN and are skipped.
    """
    msgid = conn.search_ext(base, ldap.SCOPE_SUBTREE, filterstr, attrs, sizelimit=limit)
    entries = []
    try:
        while True:
            _, data = conn.result(msgid, 0, LDAP_TIMEOUT)
            if not data:
                break
            entries.extend((dn, entry) for dn, entry in data if dn)
    except ldap.SIZELIMIT_EXCEEDED:
        pass
    return entries


def utility_domain(ldap_utility):
    """
    The DNS domain an LDAP Utility's directory holds, from its naming context.
    When the directory cannot be reached, the Base DN's DC components, then the
    Domain field.
    """
    try:
        domain = dn_domain(search_base(ldap_utility))
    except Exception:
        domain = dn_domain(ldap_utility.base_dn or "")
    return domain or (ldap_utility.ldap_domain or "").strip().strip(".").lower()


def resolve(server, dn=None):
    """
    (LDAP Utility, domain, OU DN) for a server, or (None, "", "") when it has
    neither domain_ou nor domain_to_join.

    The domain comes from the DC components of dn when given (the cleanup
    action passes the recorded computer object DN), else of domain_ou, and the
    LDAP Utility is the one serving that domain. domain_to_join is needed only
    with no OU (the domain's default Computers container) or to choose between
    several utilities for one domain; when set it must agree with the OU.
    Raises ValueError when it does not, or when no single utility serves the
    domain.
    """
    ou = str(server.get_value_for_custom_field("domain_ou") or "").strip()
    chosen = server.get_value_for_custom_field("domain_to_join")
    source = dn or ou
    if not source:
        if chosen is None:
            return None, "", ""
        return chosen, utility_domain(chosen), ""

    domain = dn_domain(source)
    if not domain:
        raise ValueError("%s is not a distinguished name with DC components" % source)
    if chosen is not None:
        if utility_domain(chosen) != domain:
            raise ValueError("%s is in %s, but domain_to_join is %s" % (source, domain, utility_domain(chosen)))
        return chosen, domain, ou
    matches = [u for u in utilities() if utility_domain(u) == domain]
    if not matches:
        raise ValueError("no LDAP Utility serves %s, the domain of %s" % (domain, source))
    if len(matches) > 1:
        raise ValueError("%d LDAP Utilities serve %s; set domain_to_join to choose one" % (len(matches), domain))
    return matches[0], domain, ou


def guid_text(raw):
    """The objectGUID bytes as the GUID string AD tools show."""
    return str(uuid.UUID(bytes_le=raw))


def _guid_filter(guid):
    """An objectGUID equality filter: each byte of the GUID as an escaped hex pair."""
    return "(objectGUID=%s)" % "".join("\\%02x" % byte for byte in uuid.UUID(guid).bytes_le)


def _first(entry, attr):
    values = entry.get(attr) or [b""]
    return values[0]


def _computer(dn, entry):
    created = _first(entry, "whenCreated").decode("ascii", "ignore")
    try:
        created = datetime.strptime(created[:14], "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
    except ValueError:
        created = None
    raw_guid = _first(entry, "objectGUID")
    return {
        "dn": dn,
        "guid": guid_text(raw_guid) if len(raw_guid) == 16 else "",
        "sid": _first(entry, "objectSid"),
        "sam_account_name": _first(entry, "sAMAccountName").decode("utf-8", "ignore"),
        "dns_host_name": _first(entry, "dNSHostName").decode("utf-8", "ignore"),
        "created": created,
    }


def find_computers(conn, base, name=None, guid=None):
    """
    Computer objects by objectGUID or, without one, by computer name
    (sAMAccountName is the name with a trailing $). Returns at most two, so a
    caller can tell one match from several.
    """
    if guid:
        match = _guid_filter(guid)
    else:
        match = "(sAMAccountName=%s$)" % escape_filter_chars(name)
    filterstr = "(&(objectCategory=computer)%s)" % match
    return [_computer(dn, entry) for dn, entry in _search(conn, base, filterstr, COMPUTER_ATTRS, 2)]


def delete_entry(conn, dn):
    """Delete dn; when it has child objects, delete it with them through the tree delete control."""
    try:
        conn.delete_ext_s(dn)
    except ldap.NOT_ALLOWED_ON_NONLEAF:
        conn.delete_ext_s(dn, serverctrls=[LDAPControl(TREE_DELETE_OID, True)])


def computer_name(hostname):
    """The Windows computer name for a host name: its first label, cut to the 15-character NetBIOS limit."""
    return str(hostname).split(".")[0][:15]


def dns_bases(conn):
    """
    The containers of the AD-integrated DNS zones on the directory conn is
    bound to: CN=MicrosoftDNS in each DomainDnsZones or ForestDnsZones
    application partition its root DSE lists in namingContexts, and
    CN=MicrosoftDNS,CN=System of the domain partition, for zones stored there.
    """
    entries = conn.search_ext_s("", ldap.SCOPE_BASE, "(objectClass=*)", ["namingContexts", "defaultNamingContext"])
    contexts, default = [], ""
    for _, entry in entries:
        entry = entry or {}
        contexts += [value.decode("utf-8") for value in entry.get("namingContexts") or []]
        default = default or _first(entry, "defaultNamingContext").decode("utf-8")
    bases = ["CN=MicrosoftDNS,%s" % context for context in contexts
             if context.split(",", 1)[0].upper() in ("DC=DOMAINDNSZONES", "DC=FORESTDNSZONES")]
    if default:
        bases.append("CN=MicrosoftDNS,CN=System,%s" % default)
    return bases


def find_dns_nodes(conn, fqdn):
    """
    The dnsNode objects that hold fqdn's records, as (dn, entry) with
    dnsRecord and dNSTombstoned. A node's DN is DC=<name in the zone>,
    DC=<zone>,CN=MicrosoftDNS,..., so its two first DC values joined by a dot
    are fqdn; the name in the zone can have dots itself.
    """
    fqdn = fqdn.strip(".").lower()
    labels = fqdn.split(".")
    relative = [".".join(labels[:i]) for i in range(1, len(labels))]
    if not relative:
        return []
    filterstr = "(&(objectClass=dnsNode)(|%s))" % "".join("(dc=%s)" % escape_filter_chars(name) for name in relative)
    nodes = []
    for base in dns_bases(conn):
        try:
            entries = _search(conn, base, filterstr, ["dnsRecord", "dNSTombstoned"], 20)
        except (ldap.NO_SUCH_OBJECT, ldap.REFERRAL):
            continue
        for dn, entry in entries:
            rdns = ldap.dn.str2dn(dn)
            if len(rdns) > 1 and ("%s.%s" % (rdns[0][0][1], rdns[1][0][1])).lower() == fqdn:
                nodes.append((dn, entry))
    return nodes


def dns_records(entry):
    """(type, serial, data) of each record in a dnsNode's dnsRecord values."""
    records = []
    for raw in entry.get("dnsRecord") or []:
        if len(raw) < DNS_RECORD_HEADER:
            continue
        length, rtype = struct.unpack_from("<HH", raw, 0)
        serial = struct.unpack_from("<I", raw, 8)[0]
        records.append((rtype, serial, raw[DNS_RECORD_HEADER:DNS_RECORD_HEADER + length]))
    return records


def a_addresses(entry):
    """The IPv4 addresses of a dnsNode's A records."""
    return [socket.inet_ntoa(data) for rtype, _, data in dns_records(entry) if rtype == DNS_TYPE_A and len(data) == 4]


def is_tombstoned(entry):
    return _first(entry, "dNSTombstoned").upper() == b"TRUE"


def owner_sid(conn, dn):
    """The owner SID of an object's security descriptor, in objectSid's binary form; b"" when unreadable."""
    control = LDAPControl(SD_FLAGS_OID, True, encodedControlValue=SD_OWNER_ONLY)
    entries = conn.search_ext_s(dn, ldap.SCOPE_BASE, "(objectClass=*)", ["nTSecurityDescriptor"],
                                serverctrls=[control])
    for _, entry in entries:
        descriptor = _first(entry or {}, "nTSecurityDescriptor")
        if len(descriptor) < 20:
            continue
        # Self-relative form: OffsetOwner is the second 32-bit field. A SID is
        # 8 bytes plus 4 per subauthority, the count being its second byte.
        offset = struct.unpack_from("<I", descriptor, 4)[0]
        if offset and len(descriptor) >= offset + 8:
            return descriptor[offset:offset + 8 + 4 * descriptor[offset + 1]]
    return b""


def tombstone_dns_node(conn, dn, entry):
    """
    Delete every record of a dnsNode the way a DNS server does when it deletes
    the last record at a node: dNSTombstoned TRUE, and dnsRecord a single
    DNS_TYPE_ZERO record whose data is the deletion time (EntombedTime). DNS
    servers read the change at their next directory poll and drop the name, as
    they do for a deletion another DNS server made, and their tombstone cleanup
    removes the object later. The name is free to register again.
    """
    serial = max([serial for _, serial, _ in dns_records(entry)] or [0])
    entombed = int(time.time() * 10 ** 7) + FILETIME_UNIX_EPOCH
    # DataLength, Type, Version, Rank, Flags, Serial; TtlSeconds is big-endian;
    # Reserved, TimeStamp; then the 8-byte EntombedTime.
    record = (struct.pack("<HHBBHI", 8, DNS_TYPE_ZERO, DNS_RECORD_VERSION, RANK_ZONE, 0, serial)
              + struct.pack(">I", 0) + struct.pack("<II", 0, 0) + struct.pack("<Q", entombed))
    conn.modify_ext_s(dn, [(ldap.MOD_REPLACE, "dnsRecord", [record]),
                           (ldap.MOD_REPLACE, "dNSTombstoned", [b"TRUE"])])
