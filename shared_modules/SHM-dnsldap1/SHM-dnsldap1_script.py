"""
CloudBolt shared module: Active Directory–integrated DNS A-record management over LDAP.

Creates and removes A records by writing the AD ``dnsNode``/``dnsRecord`` object
directly over ``ldap3`` (no Kerberos, no dynamic DNS). Imported by the
Post-Provision create plugin and the Pre-Delete delete plugin as::

    from shared_modules.ldap_dns import (
        get_dns_client,
        relative_record_name,
        validate_record_name,
        validate_ipv4,
        ADDNSError,
        ADDNSConfigError,
    )

Layering (strict — keeps the encoder/client unit-testable off-platform):

- The encoder/decoder functions and ``ADDNSClient`` carry ZERO CloudBolt
  references: plain values plus an injected, already-bound ``ldap3.Connection``.
- ``get_dns_client()`` is the ONLY CloudBolt-importing seam: it resolves the
  ``LDAPUtility`` for a server, reads the (decrypted) bind password into a local,
  builds the bound connection, and returns an ``ADDNSClient``. The bind password,
  the ``LDAPUtility`` instance via ``vars()``/``__dict__``/``%r``, and the
  ``ldap3`` Server/Connection objects are NEVER logged, returned, or interpolated
  into errors (the model's ``__str__`` returns only ``ldap_domain``, so plain
  ``repr``/``str`` are safe).

Wire format — every record blob follows Microsoft [MS-DNSP] (cite at call sites):
- dnsRecord / DNS_RPC_RECORD layout:
  https://learn.microsoft.com/en-us/openspecs/windows_protocols/ms-dnsp/6912b338-5472-4f59-b912-0edb536b6ed8
- DNS_RPC_RECORD (RANK_ZONE=0xF0, version 5):
  https://learn.microsoft.com/en-us/openspecs/windows_protocols/ms-dnsp/ac793981-1c60-43b8-be59-cdbb5c4ecb8a
- DNS_RPC_RECORD_A (IPv4 in network byte order):
  https://learn.microsoft.com/en-us/openspecs/windows_protocols/ms-dnsp/117c2ff9-9094-45b2-83c2-5e44518e0bac
- Reference implementation mirrored (non-Kerberos path only):
  dirkjanm/krbrelayx dnstool.py — record structs, get_next_serial, tombstone.

LAB VALIDATION REQUIRED (this module is authored against the spec + dnstool but
has not run against a live DC):
- The encoder is byte-match validated against a Windows DNS console record per
  docs/ad-dns-ldap-setup.md before the write path is trusted (the encoder LOGIC
  is unit-checked: 28-byte blob, big-endian TtlSeconds, RANK_ZONE, round-trip).
- The appliance runs **ldap3 2.5.1** (old; current is 2.9.x). Every ldap3 call
  below mirrors dnstool's shapes — verify ``Server``/``Connection``/``Tls`` kwargs,
  ``MODIFY_REPLACE``/``MODIFY_ADD`` semantics, raw binary value handling, and the
  ``dNSTombstoned`` boolean serialization against the 2.5 docs before shipping.
"""

import datetime
import ipaddress
import logging
import re
import socket
import struct
import ssl
import time

from ldap3 import (
    ALL,
    BASE,
    Connection,
    MODIFY_REPLACE,
    Server,
    SIMPLE,
    SUBTREE,
    Tls,
)
from ldap3.core.exceptions import LDAPException

# Common CloudBolt utilities (always present in the venv). Model/service imports
# specific to LDAP resolution are deferred into the seam functions below.
from common.methods import set_progress
from utilities.logger import ThreadLogger

logger = ThreadLogger(__name__)


# =============================================================================
# == OPERATOR CONFIG BLOCK — EDIT ME ==========================================
# =============================================================================
# Non-secret, instance-wide policy. This repo is config-as-code: these values are
# reviewable and versioned. The ONLY secret — the bind password — lives on the
# LDAPUtility (re-entered in the CloudBolt UI after every sync). Per-deployment
# overrides (zone, ttl) may also be supplied as HPA action-input defaults; these
# are the fallback/policy defaults and the security floor.

# Default TTL (seconds) for created A records when no {{ ttl }} override is given.
DEFAULT_TTL = 3600

# Allowed DNS zones this content may write into. Empty list = allow any zone the
# server's LDAPUtility resolves to (POC-permissive). Populate to hard-restrict.
ALLOWED_ZONES = []  # e.g. ["corp.example.com", "lab.example.com"]

# Record-name policy. A single LDH DNS label (letters/digits/hyphen, not
# leading/trailing hyphen). Multi-label names (sub.host) are allowed as
# dot-separated LDH labels. '*' (wildcard) and '@' (apex) and leading '_' are
# rejected unless ALLOW_SPECIAL_NAMES is True.
RECORD_LABEL_RE = re.compile(r"^(?=.{1,63}$)[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?$")
ALLOW_SPECIAL_NAMES = False

# Overwrite policy. False = refuse to replace an A record this content cannot
# prove it created (no-clobber). True = replace any existing A value for the name.
ALLOW_OVERWRITE = False

# LDAPS port. DNS writes always use LDAPS (SSL-on-connect). When the resolved
# LDAPUtility is configured for plain "ldap" (commonly port 389), the write
# connection uses THIS port instead of the utility's — SSL-wrapping a plain-LDAP
# 389 listener fails with "connection reset by peer". Only when the utility is
# itself "ldaps" is its own port used as-is. Override for a non-standard LDAPS port.
LDAPS_PORT = 636

# TLS posture for the LDAPS write connection. When CA_CERTS_FILE is set, the DC
# certificate is validated (CERT_REQUIRED). When empty, TLS is encrypt-only
# (CERT_NONE) — matching the platform's own tolerant self-signed posture, but
# vulnerable to active MITM of the SIMPLE-bind password. Set a CA bundle for
# production. See docs/ad-dns-ldap-setup.md.
CA_CERTS_FILE = ""  # e.g. "/etc/cloudbolt/ad-dc-ca-bundle.pem"

# LDAP connect/receive timeouts (seconds). Bound hangs against an unresponsive DC
# so a single slow server cannot block the hook thread (and every server after it
# in the serial loop) indefinitely.
LDAP_CONNECT_TIMEOUT_SECS = 10
LDAP_RECEIVE_TIMEOUT_SECS = 30

# Post-write DNS-resolution wait. When the Post-Provision create plugin's
# `wait_for_resolution` input is True, it polls the CloudBolt appliance's own
# resolver after writing each A record until the record resolves to the
# registered IP — proving the record is live end-to-end (AD replication +
# resolver cache) before provisioning continues, not merely written to the DC.
# DNS_RESOLUTION_POLL_SECS is the interval between lookups; the wait gives up
# after DNS_RESOLUTION_TIMEOUT_SECS (a hard ceiling so a record that never
# propagates cannot hang the provision job — and, in the serial hook loop, every
# server queued behind it — indefinitely). Both are operator-tunable here.
DNS_RESOLUTION_TIMEOUT_SECS = 900  # 15 minutes
DNS_RESOLUTION_POLL_SECS = 10

# Namespaced custom fields the plugins store on the server for deterministic
# teardown. Centralized here so create/delete agree.
CF_FQDN = "addns_fqdn"
CF_ZONE = "addns_zone"
CF_UTILITY = "addns_ldap_utility"
CF_IP = "addns_record_ip"


def _validate_operator_config():
    """Fail fast on a malformed config block (operator-actionable)."""
    if not isinstance(DEFAULT_TTL, int) or DEFAULT_TTL <= 0:
        raise ADDNSConfigError("DEFAULT_TTL must be a positive integer.")
    if not isinstance(ALLOWED_ZONES, (list, tuple)):
        raise ADDNSConfigError("ALLOWED_ZONES must be a list of zone names.")
    if not isinstance(DNS_RESOLUTION_TIMEOUT_SECS, int) or DNS_RESOLUTION_TIMEOUT_SECS <= 0:
        raise ADDNSConfigError("DNS_RESOLUTION_TIMEOUT_SECS must be a positive integer (seconds).")
    if not isinstance(DNS_RESOLUTION_POLL_SECS, int) or DNS_RESOLUTION_POLL_SECS <= 0:
        raise ADDNSConfigError("DNS_RESOLUTION_POLL_SECS must be a positive integer (seconds).")


# =============================================================================
# == CONSTANTS (MS-DNSP) ======================================================
# =============================================================================
DNS_TYPE_ZERO = 0x0000  # tombstone record carrier
DNS_TYPE_A = 0x0001
DNS_TYPE_SOA = 0x0006
DNS_RECORD_VERSION = 0x05
RANK_ZONE = 0xF0  # authoritative zone record (MS-DNSP DNS_RPC_RECORD flags table)
# 100ns intervals between 1601-01-01 (FILETIME epoch) and the Unix epoch.
_FILETIME_EPOCH = datetime.datetime(1601, 1, 1, tzinfo=datetime.timezone.utc)


# =============================================================================
# == EXCEPTIONS ===============================================================
# =============================================================================
class ADDNSError(Exception):
    """Operational error talking to AD DNS. Messages are scalar-only — never a
    password, never a raw ldap3 exception/connection object."""


class ADDNSConfigError(ADDNSError):
    """Misconfiguration (config block, missing utility, blank password)."""


# =============================================================================
# == PURE ENCODER / DECODER (zero CloudBolt deps) — MS-DNSP 2.3.2.2 ===========
# =============================================================================
# Byte layout (validated field-by-field; the byte-match vs a console record in
# docs/ad-dns-ldap-setup.md is the authoritative acceptance gate):
#   off size endian field
#    0   2   LE     DataLength (len of Data)
#    2   2   LE     Type (0x0001 = A)
#    4   1   --     Version (0x05)
#    5   1   --     Rank (0xF0 = RANK_ZONE)
#    6   2   LE     Flags (0x0000)
#    8   4   LE     Serial (zone SOA serial + 1)
#   12   4   BIG    TtlSeconds  <-- the ONLY big-endian field (classic bug)
#   16   4   LE     Reserved (0)
#   20   4   LE     Timestamp (0 = static/non-aging)
#   24   N   net    Data (A: 4-byte IPv4 in network byte order)


def encode_a_record(ip, ttl, serial):
    """Encode an A record dnsRecord blob. ``ip`` must be a valid dotted-quad IPv4."""
    data = socket.inet_aton(ip)  # 4 bytes, network byte order; raises OSError if bad
    return _encode_record(DNS_TYPE_A, ttl, serial, data)


def encode_tombstone_record(serial, entombed_filetime):
    """Encode a DNS_TYPE_ZERO tombstone record carrying the entombed FILETIME
    (mirrors dnstool's DNS_RPC_RECORD_TS: 8-byte little-endian timestamp)."""
    data = struct.pack("<Q", int(entombed_filetime))
    return _encode_record(DNS_TYPE_ZERO, 0, serial, data)


def _encode_record(rtype, ttl, serial, data):
    # LE prefix: DataLength, Type, Version, Rank, Flags, Serial
    prefix = struct.pack("<HHBBHI", len(data), rtype, DNS_RECORD_VERSION, RANK_ZONE, 0x0000, int(serial))
    ttl_be = struct.pack(">I", int(ttl))          # TtlSeconds is BIG-endian per MS-DNSP
    tail = struct.pack("<II", 0, 0)               # Reserved, Timestamp
    return prefix + ttl_be + tail + data


def decode_record(blob):
    """Decode a dnsRecord blob into its header fields plus the raw Data section."""
    if len(blob) < 24:
        raise ValueError("dnsRecord blob shorter than the 24-byte header")
    data_len, rtype, version, rank, flags, serial = struct.unpack("<HHBBHI", blob[:12])
    (ttl,) = struct.unpack(">I", blob[12:16])
    data = blob[24:24 + data_len]
    return {
        "type": rtype, "version": version, "rank": rank, "flags": flags,
        "serial": serial, "ttl": ttl, "data": data,
    }


def decode_a_value(blob):
    """Decode an A record's IP (or ``None`` if the blob is not an A record)."""
    rec = decode_record(blob)
    rec["ip"] = socket.inet_ntoa(rec["data"]) if (rec["type"] == DNS_TYPE_A and len(rec["data"]) == 4) else None
    return rec


def parse_soa_serial(blob):
    """Return the SOA zone serial (first big-endian ULONG of the SOA Data), or
    ``None`` if the blob is not an SOA record."""
    rec = decode_record(blob)
    if rec["type"] != DNS_TYPE_SOA or len(rec["data"]) < 4:
        return None
    (soa_serial,) = struct.unpack(">I", rec["data"][0:4])  # SOA Data fields are big-endian
    return soa_serial


def _filetime_now():
    """Windows FILETIME (100ns intervals since 1601-01-01) for an entombed time."""
    delta = datetime.datetime.now(datetime.timezone.utc) - _FILETIME_EPOCH
    return int(delta.total_seconds() * 10_000_000)


# =============================================================================
# == PURE HELPERS / VALIDATION (zero CloudBolt deps) ==========================
# =============================================================================
def relative_record_name(hostname, zone):
    """Reduce a server hostname to the zone-relative record label.

    ``host1.corp.example.com`` in zone ``corp.example.com`` -> ``host1``; a name equal
    to the zone -> ``@`` (apex). Trailing dots are stripped; comparison is
    case-insensitive.
    """
    name = (hostname or "").strip().rstrip(".").lower()
    zone = (zone or "").strip().rstrip(".").lower()
    if not name:
        raise ADDNSError("Server has no hostname to derive a DNS record name from.")
    if name == zone:
        return "@"
    suffix = "." + zone
    if zone and name.endswith(suffix):
        name = name[: -len(suffix)]
    return name


def validate_record_name(record):
    """Validate a zone-relative record name against the LDH allowlist policy.

    Rejects ``*``/``@``/leading-``_`` and DN/filter metacharacters unless
    ALLOW_SPECIAL_NAMES is set. Returns the validated name or raises ADDNSError.
    """
    if record == "@" or "*" in record:
        if not ALLOW_SPECIAL_NAMES:
            raise ADDNSError(
                "Record name %r is a wildcard/apex; refused by policy "
                "(set ALLOW_SPECIAL_NAMES to permit)." % record
            )
        return record
    labels = record.split(".")
    for label in labels:
        if not RECORD_LABEL_RE.match(label) or (label.startswith("_") and not ALLOW_SPECIAL_NAMES):
            raise ADDNSError(
                "Record name %r is not a valid DNS label set (LDH only; no "
                "wildcards, DN/filter metacharacters, or leading underscores)." % record
            )
    return record


def validate_zone(zone):
    """Validate the zone against ALLOWED_ZONES (when configured) and basic shape."""
    z = (zone or "").strip().rstrip(".").lower()
    if not z or "/" in z or "," in z:
        raise ADDNSError("Zone %r is empty or malformed." % zone)
    if ALLOWED_ZONES and z not in [a.strip().rstrip(".").lower() for a in ALLOWED_ZONES]:
        raise ADDNSError("Zone %r is not in the operator ALLOWED_ZONES allowlist." % zone)
    return z


def validate_ipv4(ip):
    """Validate a real, routable-ish unicast IPv4 (reject empty/IPv6/0.0.0.0/
    loopback/multicast/broadcast). Returns the dotted-quad string."""
    try:
        addr = ipaddress.ip_address((ip or "").strip())
    except ValueError:
        raise ADDNSError("IP %r is not a valid IP address." % ip)
    if addr.version != 4:
        raise ADDNSError("IP %r is not IPv4 (A records are IPv4-only)." % ip)
    if addr.is_unspecified or addr.is_loopback or addr.is_multicast or addr.is_reserved:
        raise ADDNSError("IP %r is not a usable unicast address." % ip)
    return str(addr)


def resolve_a_records(fqdn):
    """Return the set of IPv4 addresses the local resolver currently returns for
    ``fqdn`` (empty set if it does not resolve). Uses ``getaddrinfo`` — the same
    resolver stack (``/etc/resolv.conf``) any process on the CloudBolt appliance
    uses, so a hit here IS "resolvable by the appliance", the ``nslookup``
    equivalent. Never raises: any lookup failure (NXDOMAIN, SERVFAIL, timeout,
    no resolver) is reported as an empty set for the caller to retry."""
    try:
        infos = socket.getaddrinfo(fqdn, None, family=socket.AF_INET, type=socket.SOCK_STREAM)
    except (socket.gaierror, OSError, UnicodeError):
        return set()
    return {info[4][0] for info in infos if info[4] and info[4][0]}


def wait_for_dns_resolution(fqdn, expected_ip=None, timeout_secs=None,
                            poll_secs=None, progress=None):
    """Block until ``fqdn`` is resolvable by the appliance resolver, polling every
    ``poll_secs`` seconds up to ``timeout_secs`` (both default to the operator
    config globals ``DNS_RESOLUTION_POLL_SECS`` / ``DNS_RESOLUTION_TIMEOUT_SECS``).

    Success = the name resolves and, when ``expected_ip`` is given, that IP is
    among the returned addresses (confirms the appliance sees the record we just
    wrote, not a stale/other A value). Always attempts at least once and honors
    the timeout as a hard ceiling. Returns
    ``{"resolved": bool, "elapsed": int, "attempts": int, "ips": [str, ...]}`` and
    never raises on non-resolution — the caller decides whether a timeout is fatal.
    ``progress`` is an optional callable (e.g. ``set_progress``) invoked with a
    status line between attempts.
    """
    timeout_secs = DNS_RESOLUTION_TIMEOUT_SECS if timeout_secs is None else int(timeout_secs)
    poll_secs = DNS_RESOLUTION_POLL_SECS if poll_secs is None else int(poll_secs)
    poll_secs = max(poll_secs, 1)
    start = time.monotonic()
    deadline = start + max(timeout_secs, 0)
    attempts = 0
    ips = []
    while True:
        attempts += 1
        found = resolve_a_records(fqdn)
        ips = sorted(found)
        if found and (expected_ip is None or expected_ip in found):
            return {"resolved": True, "elapsed": int(time.monotonic() - start),
                    "attempts": attempts, "ips": ips}
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return {"resolved": False, "elapsed": int(time.monotonic() - start),
                    "attempts": attempts, "ips": ips}
        if progress:
            progress("AD DNS: %s not yet resolvable by the appliance (seen: %s); "
                     "retrying in %ds (%ds until timeout)."
                     % (fqdn, ", ".join(ips) or "nothing", poll_secs, int(remaining)))
        time.sleep(min(poll_secs, remaining))  # never sleep past the hard ceiling


def _escape_dn_value(value):
    """Minimal RFC 4514 escaping for a DN attribute value (ldap3 2.5 has no
    public ``escape_dn_chars``). Validated record names need none in practice;
    this is unconditional defense-in-depth."""
    if value == "@":
        return value  # the apex RDN value is written literally
    out = []
    for i, ch in enumerate(value):
        if ch in ',+"\\<>;=' or ch == "\x00":
            out.append("\\" + ch)
        elif ch == " " and (i == 0 or i == len(value) - 1):
            out.append("\\ ")
        elif ch == "#" and i == 0:
            out.append("\\#")
        else:
            out.append(ch)
    return "".join(out)


# =============================================================================
# == PURE AD-DNS LDAP CLIENT (zero CloudBolt deps) ============================
# =============================================================================
class ADDNSClient:
    """AD-integrated DNS operations over an injected, already-bound ldap3
    connection. The connection MUST have been built with ``get_info=ALL`` so
    RootDSE (defaultNamingContext / rootDomainNamingContext) is populated.

    All write methods return a small result dict: ``{"action": <code>, "dn": ...,
    "detail": ...}`` where action is one of: created, replaced, no_change,
    not_owned, tombstoned, already_gone, ip_mismatch.
    """

    def __init__(self, connection, zone, owns_connection=False, utility_ref=None):
        self._conn = connection
        self._zone = (zone or "").strip().rstrip(".").lower()
        self._owns_connection = owns_connection
        self._dnsroot = None  # cached partition base once resolved
        # Public outputs of resolution, for the calling plugin to persist:
        self.utility_ref = utility_ref  # LDAPUtility global_id (for deterministic teardown)

    @property
    def zone(self):
        """The normalized zone this client writes to (resolution output)."""
        return self._zone

    def __repr__(self):
        # NEVER include the connection (its repr can carry bound credentials).
        return "<ADDNSClient zone=%s>" % self._zone

    # -- context manager: own the connection lifecycle if we built it ----------
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if self._owns_connection and self._conn is not None:
            try:
                self._conn.unbind()
            except Exception:  # never mask the real error with a teardown failure
                logger.debug("ADDNSClient: connection unbind raised on exit.")
        return False

    # -- partition / DN --------------------------------------------------------
    def resolve_partition(self):
        """Locate the DNS partition containing this zone by probing, in order,
        DomainDnsZones -> ForestDnsZones -> System (mirrors adidnsdump/dnstool)."""
        if self._dnsroot:
            return self._dnsroot
        info = getattr(self._conn.server, "info", None)
        other = getattr(info, "other", None) if info else None
        if not other:
            raise ADDNSError(
                "RootDSE is unavailable; the connection must be built with "
                "get_info=ALL to resolve the DNS partition."
            )
        domain_root = _first(other.get("defaultNamingContext"))
        forest_root = _first(other.get("rootDomainNamingContext")) or domain_root
        if not domain_root:
            raise ADDNSError("RootDSE has no defaultNamingContext; cannot locate the zone.")
        candidates = [
            "CN=MicrosoftDNS,DC=DomainDnsZones,%s" % domain_root,
            "CN=MicrosoftDNS,DC=ForestDnsZones,%s" % forest_root,
            "CN=MicrosoftDNS,CN=System,%s" % domain_root,
        ]
        for dnsroot in candidates:
            zone_dn = "DC=%s,%s" % (_escape_dn_value(self._zone), dnsroot)
            # BASE search on the zone container; a missing base returns result 32
            # and search() False (try the next candidate). Convert socket/TLS
            # errors to a typed ADDNSError instead of letting LDAPException escape.
            try:
                found = self._conn.search(zone_dn, "(objectClass=*)", search_scope=BASE, attributes=["name"])
            except LDAPException as exc:
                raise ADDNSError("LDAP search for the zone partition failed: %s" % type(exc).__name__)
            result_code = (getattr(self._conn, "result", {}) or {}).get("result")
            if result_code in (1, 50):  # operationsError / insufficientAccessRights — don't silently skip
                raise ADDNSError(
                    "Insufficient access reading DNS partition '%s' (LDAP result %s)." % (dnsroot, result_code)
                )
            # Check entries (searchResEntry only) — conn.response also carries
            # referral (searchResRef) records, which would false-positive here.
            if found and self._conn.entries:
                self._dnsroot = dnsroot
                return dnsroot
        raise ADDNSError(
            "Zone %r not found under any DNS partition. Probed: %s"
            % (self._zone, "; ".join("DC=%s,%s" % (self._zone, c) for c in candidates))
        )

    def node_dn(self, record):
        dnsroot = self.resolve_partition()
        return "DC=%s,DC=%s,%s" % (_escape_dn_value(record), _escape_dn_value(self._zone), dnsroot)

    # -- reads -----------------------------------------------------------------
    def _read_node(self, record):
        """Return (exists, list_of_record_blobs, tombstoned_bool) for a node."""
        dn = self.node_dn(record)
        if not self._conn.search(dn, "(objectClass=dnsNode)", search_scope=BASE,
                                 attributes=["dnsRecord", "dNSTombstoned"]):
            return False, [], False
        if not self._conn.entries:
            return False, [], False
        entry = self._conn.entries[0]
        # Guard the attribute access: a dnsNode can be returned without a
        # dnsRecord attribute (empty/just-tombstoned node, or read-restricted
        # attribute). entry["dnsRecord"] would raise LDAPKeyError otherwise.
        blobs = list(getattr(entry["dnsRecord"], "raw_values", []) or []) if "dnsRecord" in entry else []
        tomb_attr = entry["dNSTombstoned"].value if "dNSTombstoned" in entry else None
        tombstoned = str(tomb_attr).upper() == "TRUE"
        return True, blobs, tombstoned

    def _next_serial(self):
        """Read the zone SOA serial from the apex (@) node and return serial + 1.
        Raises if the SOA cannot be read (never returns 0 — AD treats 0 as stale)."""
        exists, blobs, _ = self._read_node("@")
        if exists:
            for blob in blobs:
                serial = parse_soa_serial(bytes(blob))
                if serial is not None:
                    return serial + 1
        raise ADDNSError(
            "Could not read the SOA serial from the zone apex (DC=@,%s); "
            "refusing to write a record with an unknown serial." % self._zone
        )

    def read_back(self, record):
        """Re-read a node and return decoded A values plus the tombstoned flag."""
        exists, blobs, tombstoned = self._read_node(record)
        records = []
        for blob in blobs:
            try:
                records.append(decode_a_value(bytes(blob)))
            except ValueError:
                continue
        return {"exists": exists, "tombstoned": tombstoned, "records": records}

    def list_a_records(self):
        """Enumerate A records in the zone for discovery. Paged SUBTREE search for
        dnsNode objects under the zone container; returns
        ``[{"record", "fqdn", "ip", "ttl"}, ...]``. Skips the apex (`@`),
        tombstoned nodes, and any non-A record values."""
        dnsroot = self.resolve_partition()
        zone_container = "DC=%s,%s" % (_escape_dn_value(self._zone), dnsroot)
        try:
            entries = self._conn.extend.standard.paged_search(
                search_base=zone_container,
                search_filter="(objectClass=dnsNode)",
                search_scope=SUBTREE,
                attributes=["name", "dnsRecord", "dNSTombstoned"],
                paged_size=500,
                generator=True,
            )
        except LDAPException as exc:
            raise ADDNSError("LDAP enumeration of zone %s failed: %s" % (self._zone, type(exc).__name__))

        out = []
        for entry in entries:
            if entry.get("type") != "searchResEntry":
                continue
            attrs = entry.get("attributes", {}) or {}
            raw = entry.get("raw_attributes", {}) or {}
            name = _first(attrs.get("name"))
            if not name or name == "@":
                continue
            if str(_first(attrs.get("dNSTombstoned"))).upper() == "TRUE":
                continue
            for blob in raw.get("dnsRecord", []) or []:
                try:
                    rec = decode_a_value(bytes(blob))
                except ValueError:
                    continue
                if rec["type"] == DNS_TYPE_A and rec["ip"]:
                    out.append({
                        "record": name,
                        "fqdn": "%s.%s" % (name, self._zone),
                        "ip": rec["ip"],
                        "ttl": rec["ttl"],
                    })
        return out

    # -- writes ----------------------------------------------------------------
    def ensure_a(self, record, ip, ttl, owned=False, allow_overwrite=None):
        """Idempotently ensure ``record -> ip`` as an A record.

        - absent node            -> add ['top','dnsNode'] with the A value
        - node has co-located non-A records -> preserved
        - existing A we own (or allow_overwrite) -> replace; identical -> no_change
        - existing A we do NOT own and no overwrite -> not_owned (no write)
        """
        if allow_overwrite is None:
            allow_overwrite = ALLOW_OVERWRITE
        dn = self.node_dn(record)
        exists, blobs, tombstoned = self._read_node(record)
        serial = self._next_serial()
        new_a = encode_a_record(ip, ttl, serial)

        if not exists or tombstoned:
            # dnstool/MS-DNSP: a fresh dnsNode carries object classes top+dnsNode,
            # the record blob, and dNSTombstoned=False. A tombstoned node is
            # revived by replacing its records and clearing the flag.
            if not exists:
                added = self._conn.add(dn, ["top", "dnsNode"],
                                       {"dnsRecord": [new_a], "dNSTombstoned": False})
                self._raise_on_fail(added, "add dnsNode %s" % dn)
                return {"action": "created", "dn": dn, "detail": "added A %s" % ip}
            self._modify(dn, {"dnsRecord": [(MODIFY_REPLACE, [new_a])],
                              "dNSTombstoned": [(MODIFY_REPLACE, [False])]})
            return {"action": "created", "dn": dn, "detail": "revived tombstoned node, A %s" % ip}

        a_blobs, non_a_blobs, existing_ips = [], [], []
        for blob in blobs:
            rec = decode_a_value(bytes(blob))
            if rec["type"] == DNS_TYPE_A:
                a_blobs.append(blob)
                if rec["ip"]:
                    existing_ips.append(rec["ip"])
            else:
                non_a_blobs.append(blob)

        if a_blobs and not owned and not allow_overwrite:
            return {"action": "not_owned", "dn": dn,
                    "detail": "existing A %s not created by CloudBolt; refusing to overwrite "
                              "(set allow_overwrite)" % ",".join(existing_ips)}

        if a_blobs and existing_ips == [ip]:
            return {"action": "no_change", "dn": dn, "detail": "A already %s" % ip}

        # Replace the A value(s) with exactly our one, preserving non-A records.
        new_values = [bytes(b) for b in non_a_blobs] + [new_a]
        self._modify(dn, {"dnsRecord": [(MODIFY_REPLACE, new_values)]})
        return {"action": "replaced", "dn": dn, "detail": "A -> %s (kept %d non-A record(s))"
                % (ip, len(non_a_blobs))}

    def tombstone(self, record, expected_ip):
        """Tombstone the node ONLY if its current A value matches ``expected_ip``
        (ownership-verified). Replication-safe vs a hard delete."""
        dn = self.node_dn(record)
        exists, blobs, tombstoned = self._read_node(record)
        if not exists or tombstoned:
            return {"action": "already_gone", "dn": dn, "detail": "node absent or already tombstoned"}
        current_ips = []
        for blob in blobs:
            rec = decode_a_value(bytes(blob))
            if rec["type"] == DNS_TYPE_A and rec["ip"]:
                current_ips.append(rec["ip"])
        if expected_ip not in current_ips:
            return {"action": "ip_mismatch", "dn": dn,
                    "detail": "current A %s != recorded %s; refusing to tombstone"
                              % (",".join(current_ips) or "none", expected_ip)}
        ts = encode_tombstone_record(self._next_serial(), _filetime_now())
        self._modify(dn, {"dnsRecord": [(MODIFY_REPLACE, [ts])],
                          "dNSTombstoned": [(MODIFY_REPLACE, [True])]})
        return {"action": "tombstoned", "dn": dn, "detail": "tombstoned A %s" % expected_ip}

    # -- ldap3 result handling -------------------------------------------------
    def _modify(self, dn, changes):
        ok = self._conn.modify(dn, changes)
        self._raise_on_fail(ok, "modify %s" % dn)

    def _raise_on_fail(self, ok, what):
        if not ok:
            # conn.result is a scalar dict (description/message) — no credentials.
            result = getattr(self._conn, "result", {}) or {}
            raise ADDNSError("LDAP %s failed: %s" % (what, result.get("description") or result))


def _first(values):
    if isinstance(values, (list, tuple)):
        return values[0] if values else None
    return values


# =============================================================================
# == CloudBolt SEAM — the ONLY CloudBolt-importing surface ====================
# =============================================================================
def ensure_custom_fields():
    """Idempotently create the namespaced custom fields used to record what was
    written. Shared by the orchestration create, blueprint build, and teardown
    plugins so the addns_* field set lives in exactly one place (works on both
    Server and Resource — show_on_servers stays False either way)."""
    from infrastructure.models import CustomField
    for name, label, desc, attr in [
        (CF_FQDN, "AD DNS FQDN", "Fully-qualified A record name CloudBolt registered in AD DNS.", True),
        (CF_ZONE, "AD DNS Zone", "DNS zone the A record was written to.", True),
        (CF_UTILITY, "AD DNS LDAP Utility", "global_id of the LDAPUtility used to write the record.", False),
        (CF_IP, "AD DNS Record IP", "IPv4 address CloudBolt registered for the A record.", True),
    ]:
        CustomField.objects.get_or_create(
            name=name,
            defaults=dict(label=label, description=desc, type="STR", show_as_attribute=attr),
        )


def get_dns_client(server=None, ldap_utility_ref=None, zone_override=None):
    """Resolve the target LDAPUtility (from the server, or an explicit ref),
    build a bound LDAPS+SIMPLE ldap3 connection, and return a context-managed
    ADDNSClient. Forces LDAPS regardless of the utility's stored protocol — AD
    rejects clear-LDAP writes to integrated DNS.

    Usage:
        with get_dns_client(server=server) as client:
            client.ensure_a(record, ip, ttl, owned=..., allow_overwrite=...)
    """
    _validate_operator_config()
    util = _resolve_utility(server=server, ref=ldap_utility_ref)
    if util is None:
        raise ADDNSConfigError(
            "No LDAPUtility could be resolved for this server (no domain_to_join, "
            "no matching ldap_domain, and not a single configured utility). "
            "Set domain_to_join on the server or pass an ldap_utility override."
        )

    zone = validate_zone(zone_override or getattr(util, "ldap_domain", "") or "")
    _assert_zone_consistent(util, zone)

    # servicepasswd decrypts on plain attribute access (vault mixin). Read once
    # into a local; never store on self, never log the model/vars()/connection.
    password = (getattr(util, "servicepasswd", "") or "").strip()
    user = (getattr(util, "serviceaccount", "") or "").strip()  # UPN user@domain
    host = (getattr(util, "ip", "") or "").strip()
    # Writes always use LDAPS. If the utility is plain "ldap" (e.g. port 389),
    # connect on the LDAPS port (636) — SSL-wrapping a plain-LDAP port is reset by
    # the DC. Use the utility's own port only when it is already configured ldaps.
    protocol = (getattr(util, "protocol", "") or "").strip().lower()
    port = int(getattr(util, "port", None) or LDAPS_PORT) if protocol == "ldaps" else LDAPS_PORT
    if not password:
        raise ADDNSConfigError(
            "LDAPUtility '%s' has no bind password. Repo syncs redact secrets, so "
            "it must be re-entered in the CloudBolt UI after every sync." % zone
        )
    if not user or not host:
        raise ADDNSConfigError("LDAPUtility '%s' is missing a service account or host." % zone)

    server_obj = Server(host, port=port, use_ssl=True, get_info=ALL, tls=_build_tls(),
                        connect_timeout=LDAP_CONNECT_TIMEOUT_SECS)
    conn = Connection(server_obj, user=user, password=password,
                      authentication=SIMPLE, auto_bind=False,
                      receive_timeout=LDAP_RECEIVE_TIMEOUT_SECS)
    try:
        bound = conn.bind()
    except LDAPException as exc:
        # auto_bind=False, so credential failures return False (handled below) and
        # never raise here — this path is connection/TLS/socket errors, whose
        # messages carry no credentials. Surface str(exc) for operator actionability.
        raise ADDNSError("LDAP connection to %s:%d failed: %s" % (host, port, exc))
    if not bound:
        result = getattr(conn, "result", {}) or {}
        raise ADDNSError("LDAP bind to %s:%d as utility '%s' was refused: %s"
                         % (host, port, zone, result.get("description") or "bind failed"))

    set_progress("Bound to %s:%d over LDAPS via LDAP utility '%s'." % (host, port, zone))
    return ADDNSClient(conn, zone, owns_connection=True, utility_ref=getattr(util, "global_id", None))


def _build_tls():
    """Build the ldap3 Tls context. CA bundle -> validated; else encrypt-only
    (platform-parity self-signed tolerance; MITM caveat documented in U7)."""
    if CA_CERTS_FILE:
        return Tls(validate=ssl.CERT_REQUIRED, ca_certs_file=CA_CERTS_FILE)
    return Tls(validate=ssl.CERT_NONE)


def _resolve_utility(server=None, ref=None):
    """Resolve an LDAPUtility from an explicit ref (global_id/pk/name) or, failing
    that, from the server via the platform's get_ldaputility heuristic."""
    if ref:
        util = _resolve_utility_by_ref(ref)
        if util:
            return util
    if server is not None:
        # Native precedent: cbhooks/hookmodules/delete_server_from_ldap.get_ldaputility
        # tries server.domain_to_join, then ldap_domain == NIC dns_domain, then the
        # sole utility. Returns None if nothing matches.
        try:
            from cbhooks.hookmodules.delete_server_from_ldap import get_ldaputility
            return get_ldaputility(server)
        except Exception as exc:
            logger.warning("get_ldaputility(server) failed: %s", exc)
    return None


def _resolve_utility_by_ref(ref):
    from utilities.models import LDAPUtility
    ref = str(ref).strip()
    # Try global_id, then numeric pk, then exact ldap_domain.
    util = LDAPUtility.objects.filter(global_id=ref).first()
    if util:
        return util
    if ref.isdigit():
        util = LDAPUtility.objects.filter(pk=int(ref)).first()
        if util:
            return util
    return LDAPUtility.objects.filter(ldap_domain__iexact=ref).first()


def _assert_zone_consistent(util, zone):
    """Refuse to write into a zone unrelated to the resolved utility's domain."""
    domain = (getattr(util, "ldap_domain", "") or "").strip().rstrip(".").lower()
    if domain and not (zone == domain or zone.endswith("." + domain) or domain.endswith("." + zone)):
        raise ADDNSConfigError(
            "Zone %r is not consistent with the resolved LDAP utility domain %r; "
            "refusing to bind to the wrong directory." % (zone, domain)
        )
