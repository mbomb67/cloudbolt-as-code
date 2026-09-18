"""
CloudBolt teardown plugin for the DNS Record blueprint: tombstone the AD DNS A
record this resource created, over LDAP via the ldap_dns shared module.

Reuses the same ownership-verified tombstone path as the Pre-Delete orchestration
plugin, keyed on the addns_* identity stored on the resource at build time.
Idempotent: missing identity, an already-gone node, or an IP mismatch return
WARNING (not FAILURE), so a delete is never blocked and a record now pointing
elsewhere is never clobbered.
"""
from common.methods import set_progress
from utilities.logger import ThreadLogger

from shared_modules.ldap_dns import (
    get_dns_client,
    relative_record_name,
    validate_record_name,
    ADDNSError,
    CF_FQDN,
    CF_ZONE,
    CF_UTILITY,
    CF_IP,
)

logger = ThreadLogger(__name__)


def run(job, *args, **kwargs):
    set_progress("DNS Record: removing the AD DNS A record for this resource.")

    resource = job.resource_set.first()
    if resource is None:
        return "WARNING", "No resource on this job; nothing to remove.", ""

    fqdn = (resource.get_value_for_custom_field(CF_FQDN) or "").strip()
    zone = (resource.get_value_for_custom_field(CF_ZONE) or "").strip()
    recorded_ip = (resource.get_value_for_custom_field(CF_IP) or "").strip()
    utility_ref = (resource.get_value_for_custom_field(CF_UTILITY) or "").strip() or None

    if not fqdn or not zone or not recorded_ip:
        msg = "No AD DNS record recorded for this resource; nothing to remove."
        logger.warning(msg)
        return "WARNING", msg, ""

    try:
        record = validate_record_name(relative_record_name(fqdn, zone))
        with get_dns_client(ldap_utility_ref=utility_ref, zone_override=zone) as client:
            result = client.tombstone(record, expected_ip=recorded_ip)
            verify = client.read_back(record)
    except ADDNSError as exc:
        logger.error("DNS Record teardown failed for %s: %s", fqdn, exc)
        return "FAILURE", "", str(exc)

    logger.info("DNS Record teardown [%s] action=%s dn=%s tombstoned=%s",
                fqdn, result["action"], result["dn"], verify["tombstoned"])
    if result["action"] == "tombstoned":
        msg = "Tombstoned AD DNS A record %s." % fqdn
        set_progress(msg)
        return "SUCCESS", msg, ""
    msg = "%s: %s" % (fqdn, result["detail"])
    set_progress(msg)
    return "WARNING", msg, ""
