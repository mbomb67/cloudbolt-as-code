"""
CloudBolt orchestration plugin (Pre-Delete): tombstone the AD-integrated DNS A
record CloudBolt registered for each decommissioning server, over LDAP via the
ldap_dns shared module.

Wire at hook point "Pre-Delete" (internal `pre_decom` — the server decommission
hook, NOT the blueprint-Resource `pre_delete_resource`). Servers come from
job.server_set.all() (this hook does NOT pass a `server` kwarg).

Delete authority is the identity stored on the server at create time — there is
no speculative re-derive-and-delete. The record is tombstoned (replication-safe)
only when its live A value still matches the recorded IP, so a name reused by a
later server or a record repointed out-of-band is never clobbered. Idempotent
and PROVFAILED-tolerant: nothing recorded, already gone, or an IP mismatch all
return WARNING.
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


def _resource_gid(job):
    resource = job.resource_set.first()
    return getattr(resource, "global_id", None) if resource else None


def run(job, server=None,*args, **kwargs):
    set_progress("AD DNS: removing A records for decommissioning servers.")

    servers = list(job.server_set.all())
    if not servers:
        if not server:
            msg = "No servers on this job; no A records to remove."
            logger.warning(f"AD DNS delete: {msg}")
            return "SUCCESS", "", ""
        servers = [server]

    removed, skipped, failures = [], [], []
    for server in servers:
        try:
            action, message = _process_server(job, server)
            (removed if action == "tombstoned" else skipped).append(message)
            set_progress("AD DNS: %s" % message)
        except ADDNSError as exc:
            msg = "%s: %s" % (getattr(server, "hostname", "?"), exc)
            logger.error("AD DNS delete failed: %s", msg)
            failures.append(msg)
        except Exception as exc:  # noqa: BLE001 — surface, don't abort other servers
            msg = "%s: unexpected error: %s" % (getattr(server, "hostname", "?"), exc)
            logger.exception("AD DNS delete error for %s", getattr(server, "hostname", "?"))
            failures.append(msg)

    parts = removed + skipped
    summary = "; ".join(parts) if parts else "no AD DNS records to remove"
    if failures:
        return "FAILURE", summary, " | ".join(failures)
    if removed:
        return "SUCCESS", summary, ""
    return "SUCCESS", summary, ""


def _process_server(job, server):
    fqdn = (server.get_value_for_custom_field(CF_FQDN) or "").strip()
    zone = (server.get_value_for_custom_field(CF_ZONE) or "").strip()
    recorded_ip = (server.get_value_for_custom_field(CF_IP) or "").strip()
    utility_ref = (server.get_value_for_custom_field(CF_UTILITY) or "").strip() or None

    # Stored identity is the sole authority — no speculative re-derive delete.
    if not fqdn or not zone or not recorded_ip:
        return "skipped", "%s: no AD DNS record recorded by CloudBolt; nothing to remove" % (
            getattr(server, "hostname", "?")
        )

    # Validate the stored name too — custom fields are mutable by other content,
    # so the delete path enforces the same LDH allowlist as create before any DN.
    record = validate_record_name(relative_record_name(fqdn, zone))
    with get_dns_client(server=server, ldap_utility_ref=utility_ref, zone_override=zone) as client:
        result = client.tombstone(record, expected_ip=recorded_ip)
        verify = client.read_back(record)

    logger.info(
        "AD DNS delete [%s] action=%s dn=%s job=%s resource=%s tombstoned=%s",
        fqdn, result["action"], result["dn"], job.id, _resource_gid(job), verify["tombstoned"],
    )
    if result["action"] == "tombstoned":
        return "tombstoned", "%s tombstoned" % fqdn
    return result["action"], "%s: %s" % (fqdn, result["detail"])
