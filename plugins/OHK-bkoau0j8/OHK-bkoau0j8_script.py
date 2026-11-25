"""
Delete Windows Computer Object from AD

Pre-Delete orchestration action. For each Windows server being deleted that
has a domain_ou or domain_to_join, it deletes the server's computer object
from Active Directory over LDAP, bound as the service account of the LDAP
Utility that serves the domain. The domain comes from the computer object DN
the join action recorded (ad_computer_object_dn), else from domain_ou or
domain_to_join, the same way the join action finds it (ad_domain.resolve).
The guest is not contacted: the VM is about to be deleted, so an in-guest
unjoin would only add a restart.

Which object is deleted:
  - The one whose objectGUID the join action recorded in
    ad_computer_object_guid. A GUID survives renames and OU moves and never
    matches another machine's object.
  - Without a recorded GUID (for example, a server CloudBolt's vCenter guest
    customization joined while the join action was disabled), the object named
    after the computer (sAMAccountName is the name plus $), but only when
    exactly one matches and AD created it after CloudBolt added the server, so
    an older object that reuses the name is never deleted.

An object with children, such as BitLocker recovery information, is deleted
together with them through AD's tree delete control. An object that is already
gone is reported and counts as success.

With the computer object, it deletes the DNS records the computer registered
in AD-integrated zones: the node for its DNS host name and the reverse (PTR)
node for each IPv4 address that node holds. A node is deleted only when the
computer object owns it, as the node a secure dynamic update creates is, so a
record an administrator made is left alone. AD-integrated zones let only a
record's owner update it, so a record left behind would keep the next server
with the same name from registering its own. Nodes are tombstoned the way the
DNS server deletes them (ad_domain.tombstone_dns_node).

The action's OS family filter (Windows) keeps Linux servers out; the check in
_delete_one keeps them out if the filter is edited. The orchestration action
continues on failure, so an AD problem is reported in the job without
stopping the server's deletion.
"""
from datetime import timedelta, timezone
from zoneinfo import ZoneInfo

from django.conf import settings

from common.methods import set_progress
from utilities.logger import ThreadLogger

from shared_modules import ad_domain

logger = ThreadLogger(__name__)

GUID_FIELD = "ad_computer_object_guid"
DN_FIELD = "ad_computer_object_dn"

# Allowed clock difference between the appliance and the domain controllers
# when comparing an object's whenCreated with the server's add date.
CLOCK_SLACK = timedelta(minutes=10)


def _added_utc(server):
    """When CloudBolt added the server, in UTC. Its dates are naive appliance-local time."""
    added = getattr(server, "add_date", None)
    if added is None:
        return None
    if added.tzinfo is None:
        added = added.replace(tzinfo=ZoneInfo(settings.TIME_ZONE))
    return added.astimezone(timezone.utc)


def _find_target(conn, base, server):
    """
    (computer object, "", "") when there is one to delete, else (None, status,
    reason): SKIPPED when there is nothing to delete, FAILURE when deleting by
    name would not be safe.
    """
    guid = str(server.get_value_for_custom_field(GUID_FIELD) or "").strip()
    if guid:
        matches = ad_domain.find_computers(conn, base, guid=guid)
        if not matches:
            return None, "SKIPPED", "computer object %s is already gone" % guid
        return matches[0], "", ""

    name = ad_domain.computer_name(server.hostname)
    matches = ad_domain.find_computers(conn, base, name=name)
    if not matches:
        return None, "SKIPPED", "no computer object named %s" % name
    if len(matches) > 1:
        return None, "FAILURE", "more than one computer object is named %s; delete the right one by hand" % name
    target = matches[0]
    added = _added_utc(server)
    if target["created"] is None or added is None or target["created"] < added - CLOCK_SLACK:
        return None, "FAILURE", (
            "computer object %s was created before CloudBolt added this server, so it may belong to "
            "another machine; it was not deleted" % target["dn"])
    return target, "", ""


def _reverse_name(address):
    return "%s.in-addr.arpa" % ".".join(reversed(address.split(".")))


def _delete_dns(conn, target, fqdn):
    """
    Tombstone the DNS nodes the computer object owns for fqdn and for the
    reverse names of its addresses. Returns a description of what was done.
    """
    sid = target["sid"]
    if not sid:
        return "its DNS records were left alone: the computer object's SID could not be read"
    deleted, foreign = [], []
    names = [fqdn]
    while names:
        name = names.pop(0)
        for dn, entry in ad_domain.find_dns_nodes(conn, name):
            if ad_domain.is_tombstoned(entry):
                continue
            if ad_domain.owner_sid(conn, dn) != sid:
                foreign.append(name)
                continue
            addresses = ad_domain.a_addresses(entry)
            ad_domain.tombstone_dns_node(conn, dn, entry)
            deleted.append("%s (%s)" % (name, ", ".join(addresses)) if addresses else name)
            if name == fqdn:
                names += [_reverse_name(address) for address in addresses]
    parts = []
    if deleted:
        parts.append("deleted DNS records %s" % "; ".join(deleted))
    if foreign:
        parts.append("left DNS records %s alone: another account owns them" % ", ".join(foreign))
    return ", ".join(parts) or "found no DNS records of its own for %s" % fqdn


def _delete_one(server):
    """Returns (status, message), status SUCCESS, FAILURE, or SKIPPED."""
    hostname = server.hostname
    if not server.is_windows():
        return "SKIPPED", "%s: not Windows, skipped." % hostname
    recorded_dn = str(server.get_value_for_custom_field(DN_FIELD) or "").strip()
    try:
        ldap_utility, domain, _ = ad_domain.resolve(server, dn=recorded_dn or None)
    except ValueError as err:
        return "FAILURE", "%s: cannot tell which domain its computer object is in: %s." % (hostname, err)
    if ldap_utility is None:
        return "SKIPPED", "%s: no domain_ou or domain_to_join, skipped." % hostname

    try:
        conn = ad_domain.connect(ldap_utility)
        try:
            target, status, reason = _find_target(conn, ad_domain.search_base(ldap_utility, conn), server)
            if target is None:
                return status, "%s: %s in %s." % (hostname, reason, ldap_utility.ldap_domain)
            ad_domain.delete_entry(conn, target["dn"])
            fqdn = target["dns_host_name"] or "%s.%s" % (ad_domain.computer_name(hostname), domain)
            try:
                dns = _delete_dns(conn, target, fqdn)
            except Exception as err:
                message = "%s: deleted computer object %s, but could not delete its DNS records: %s" % (
                    hostname, target["dn"], err)
                logger.error(message)
                return "FAILURE", message
        finally:
            conn.unbind_s()
    except Exception as err:
        message = "%s: could not delete its computer object from %s: %s" % (
            hostname, ldap_utility.ldap_domain, err)
        logger.error(message)
        return "FAILURE", message
    return "SUCCESS", "%s: deleted computer object %s; %s." % (hostname, target["dn"], dns)


def run(job, *args, **kwargs):
    """Pre-Delete entry point."""
    # With an OS family filter CloudBolt passes only the matching servers.
    servers = kwargs.get("servers") or list(job.server_set.all())
    results = []
    for server in servers:
        status, message = _delete_one(server)
        set_progress(message)
        results.append((status, message))

    failures = [message for status, message in results if status == "FAILURE"]
    done = [message for status, message in results if status != "FAILURE"]
    if failures:
        return "FAILURE", "\n".join(done), "\n".join(failures)
    return "SUCCESS", "\n".join(done), ""
