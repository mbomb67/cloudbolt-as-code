"""
CloudBolt discovery plugin for the DNS Record blueprint: inventory AD-integrated
DNS A records across all configured LDAPUtilities and sync them into CloudBolt.

For each LDAPUtility, binds over LDAPS (the ldap_dns seam) and enumerates the A
records in the utility's domain zone via ADDNSClient.list_a_records(). Utilities
that can't be reached, aren't AD-DNS zones, or whose bind password was redacted by
a sync are skipped with a warning — never failing the whole discovery run.
"""
from utilities.logger import ThreadLogger
from common.methods import set_progress

from shared_modules.ldap_dns import (
    get_dns_client,
    ADDNSError,
    ADDNSConfigError,
    CF_FQDN,
    CF_ZONE,
    CF_UTILITY,
    CF_IP,
)

logger = ThreadLogger(__name__)

# CloudBolt uses this to know which custom field uniquely identifies the resource.
RESOURCE_IDENTIFIER = CF_FQDN

# Set DRY_RUN to False to actually inventory Records
DRY_RUN = True


def discover_resources(**kwargs):
    """Return one dict per discovered A record (auto-creates the addns_* fields)."""
    from utilities.models import LDAPUtility

    discovered = []
    for util in LDAPUtility.objects.all():
        zone = (getattr(util, "ldap_domain", "") or "").strip()
        if not zone:
            continue
        try:
            with get_dns_client(ldap_utility_ref=util.global_id, zone_override=zone) as client:
                records = client.list_a_records()
        except (ADDNSConfigError, ADDNSError) as exc:
            logger.warning("DNS discovery: skipping LDAP utility %s (%s): %s",
                           zone, util.global_id, exc)
            continue
        except Exception as exc:  # noqa: BLE001 — one bad utility must not abort discovery
            logger.warning("DNS discovery: unexpected error for utility %s: %s", util.global_id, exc)
            continue

        for rec in records:
            discovered.append({
                "name": rec["fqdn"],          # REQUIRED
                CF_FQDN: rec["fqdn"],          # RESOURCE_IDENTIFIER
                CF_ZONE: zone,
                CF_UTILITY: util.global_id,
                CF_IP: rec["ip"],
            })

    logger.info("DNS discovery: found %d A record(s) across LDAP utilities.", len(discovered))
    if DRY_RUN:
        set_progress("DRY_RUN set to True - exiting without updating inventory")
        return []
    return discovered