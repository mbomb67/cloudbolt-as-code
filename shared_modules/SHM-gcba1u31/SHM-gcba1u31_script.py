"""
Active Directory helpers for the Windows domain join content.

The domain comes from the LDAP Utility that a server's domain_to_join parameter
names. This module binds with that utility's service account, searches the
domain for organizational units, and finds and deletes computer objects. Its
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
"""
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

COMPUTER_ATTRS = ["objectGUID", "sAMAccountName", "dNSHostName", "whenCreated"]


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
