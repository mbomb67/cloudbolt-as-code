"""
CloudBolt build plugin for the DNS Record blueprint: create an AD-integrated DNS
A record from an order, over LDAP via the ldap_dns shared module.

Order-form inputs (action inputs auto-discovered from the below):
- ldap_utility (required) — chosen via generate_options_for_ldap_utility
- record (required)       — the record name (host label, zone-relative)
- ip (required)           — the IPv4 address

The DNS zone is NOT prompted: it is intrinsic to the chosen LDAP Utility
(its ldap_domain), which the seam resolves into client.zone.

Reuses get_dns_client / ensure_a / the validators / ensure_custom_fields from the
shared module — the same write path the Post-Provision orchestration plugin uses.
The provisioned resource is named after the FQDN and carries the addns_* identity
for discovery and teardown.
"""
from common.methods import set_progress
from utilities.logger import ThreadLogger

from shared_modules.ldap_dns import (
    get_dns_client,
    ensure_custom_fields,
    relative_record_name,
    validate_record_name,
    validate_zone,
    validate_ipv4,
    ADDNSError,
    ADDNSConfigError,
    CF_FQDN,
    CF_ZONE,
    CF_UTILITY,
    CF_IP,
    DEFAULT_TTL,
)

logger = ThreadLogger(__name__)


def generate_options_for_ldap_utility(field=None, **kwargs):
    """List the CloudBolt LDAPUtilities the order can target. The option value is
    the utility's global_id, which get_dns_client resolves back to the utility."""
    from utilities.models import LDAPUtility

    options = []
    for util in LDAPUtility.objects.all():
        label = (getattr(util, "ldap_domain", None) or str(util) or "LDAP Utility").strip()
        options.append((util.global_id, label))
    options.sort(key=lambda opt: opt[1].lower())
    if not options:
        return [("", "------ No LDAP Utilities configured ------")]
    return options


def run(job, *args, **kwargs):
    set_progress("DNS Record: creating an AD DNS A record from the order inputs.")
    ensure_custom_fields()

    ldap_utility = "{{ ldap_utility }}".strip()
    record_in = "{{ record }}".strip()
    ip_in = "{{ ip }}".strip()

    if not ldap_utility:
        return "FAILURE", "", "An LDAP Utility is required."
    if not record_in:
        return "FAILURE", "", "A record name is required."

    try:
        ip = validate_ipv4(ip_in)
        # Zone is derived from the LDAP Utility (its ldap_domain) — not prompted.
        with get_dns_client(ldap_utility_ref=ldap_utility) as client:
            zone = validate_zone(client.zone)
            record = validate_record_name(relative_record_name(record_in, zone))
            fqdn = zone if record == "@" else "%s.%s" % (record, zone)
            result = client.ensure_a(record, ip, DEFAULT_TTL)
            if result["action"] == "not_owned":
                return "FAILURE", "", (
                    "An A record for %s already exists and was not created by CloudBolt; "
                    "refusing to overwrite. Set ALLOW_OVERWRITE in the ldap_dns config "
                    "block to replace it." % fqdn
                )
            verify = client.read_back(record)
            utility_ref = client.utility_ref or ldap_utility
    except (ADDNSConfigError, ADDNSError) as exc:
        logger.error("DNS Record build failed: %s", exc)
        return "FAILURE", "", str(exc)

    resource = job.resource_set.first()
    if resource:
        resource.name = fqdn
        resource.set_value_for_custom_field(CF_FQDN, fqdn)
        resource.set_value_for_custom_field(CF_ZONE, zone)
        resource.set_value_for_custom_field(CF_UTILITY, utility_ref)
        resource.set_value_for_custom_field(CF_IP, ip)
        resource.save()

    msg = "AD DNS A record %s -> %s (%s)." % (fqdn, ip, result["action"])
    logger.info("DNS Record build: %s dn=%s verified=%s", msg, result["dn"],
                [r.get("ip") for r in verify["records"] if r.get("ip")])
    set_progress(msg)
    return "SUCCESS", msg, ""