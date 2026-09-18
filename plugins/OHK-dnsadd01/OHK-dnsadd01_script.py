"""
CloudBolt orchestration plugin (Post-Provision): ensure an AD-integrated DNS A
record for each provisioned server, over LDAP via the ldap_dns shared module.

Wire at hook point "Post-Provision". Servers come from job.server_set.all() —
this hook does NOT pass a `server` kwarg (mirrors the OOTB
cbhooks/hookmodules/delete_server_from_ldap.py pattern). The record name derives
from the server hostname (zone-relative); the IPv4 from the server NIC. The zone
and target LDAPUtility are resolved from the server, with optional per-deployment
overrides (action-input default values; the stored input name carries the
_a<hookid> suffix, the template var is unsuffixed). The registered identity is
stored on the server so the paired Pre-Delete plugin can tombstone exactly what
was created.

Optional input `wait_for_resolution` (BOOL, default False): when True, after
writing each record the plugin blocks until the CloudBolt appliance's own
resolver returns the record — polling every DNS_RESOLUTION_POLL_SECS up to
DNS_RESOLUTION_TIMEOUT_SECS (configured in the ldap_dns shared module). A record
that never becomes resolvable within the timeout fails the action for that server.
"""
import ast

from common.methods import set_progress
from utilities.logger import ThreadLogger

from shared_modules.ldap_dns import (
    get_dns_client,
    ensure_custom_fields,
    relative_record_name,
    validate_record_name,
    validate_zone,
    validate_ipv4,
    wait_for_dns_resolution,
    ADDNSError,
    CF_FQDN,
    CF_ZONE,
    CF_UTILITY,
    CF_IP,
    DEFAULT_TTL,
)

logger = ThreadLogger(__name__)

DNS_RESOLUTION_TIMEOUT_SECS = 60 * 15  # 15 minutes
DNS_RESOLUTION_POLL_SECS = 15 # 15 seconds

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


def _opt(rendered):
    """Return a rendered value, or None if it is empty or left unrendered."""
    value = (rendered or "").strip()
    if not value:
        return None
    return value


def _server_ip(server):
    return (getattr(server, "ip", None) or getattr(server, "sc_nic_0_ip", None) or "").strip()


def _resource_gid(job):
    resource = job.resource_set.first()
    return getattr(resource, "global_id", None) if resource else None


def run(job, *args, **kwargs):
    set_progress("AD DNS: ensuring A records for provisioned servers.")
    ensure_custom_fields()

    ldap_utility = "{{ ldap_utility }}".strip()
    # Optional per-deployment overrides from HPA action-input defaults.
    ttl = DEFAULT_TTL
    allow_overwrite = False
    # Declared BOOL input; the HPA default renders "True"/"False". Fall back to
    # False if the action was left unconfigured (template renders empty/unparsable).
    try:
        wait_for_resolution = ast.literal_eval("{{ wait_for_resolution }}")
    except (ValueError, SyntaxError):
        wait_for_resolution = False

    servers = list(job.server_set.all())
    if not servers:
        return "WARNING", "No servers on this job; no A records to register.", ""

    results, failures = [], []
    for server in servers:
        try:
            results.append(
                _process_server(
                    job, ldap_utility, server, ttl, allow_overwrite, wait_for_resolution,
                )
            )
        except ADDNSError as exc:
            msg = "%s: %s" % (getattr(server, "hostname", "?"), exc)
            logger.error("AD DNS create failed: %s", msg)
            set_progress(msg)
            failures.append(msg)
        except Exception as exc:  # noqa: BLE001 — surface, don't abort other servers
            msg = "%s: unexpected error: %s" % (getattr(server, "hostname", "?"), exc)
            logger.exception("AD DNS create error for %s", getattr(server, "hostname", "?"))
            failures.append(msg)

    summary = "; ".join(results) if results else "no A records changed"
    if failures:
        return "FAILURE", summary, " | ".join(failures)
    return "SUCCESS", summary, ""


def _process_server(job, ldap_utility, server, ttl, allow_overwrite, wait_for_resolution):
    # Validate the IP before opening a connection (fail fast, no bind on bad input).
    ip = validate_ipv4(_server_ip(server))
    if not ip:
        raise ADDNSError("No valid IPv4 address found for server.")
    raw_name = server.hostname

    with get_dns_client(ldap_utility_ref=ldap_utility) as client:
        zone = validate_zone(client.zone)
        if not zone:
            raise ADDNSError("DNS zone is not configured for this server or deployment.")
        record = validate_record_name(relative_record_name(raw_name, zone))
        if not record:
            raise ADDNSError(
                "Derived record name '%s' from hostname '%s' is not valid for zone '%s'."
                % (raw_name, server.hostname, zone)
            )
        # Reduce both the derived hostname AND an operator override to a
        # zone-relative label, so a full-FQDN override doesn't double the suffix.
        fqdn = zone if record == "@" else "%s.%s" % (record, zone)

        # We "own" the name if the server already records this exact FQDN — then a
        # replace is a safe re-provision; otherwise no-clobber applies.
        owned = (server.get_value_for_custom_field(CF_FQDN) or "") == fqdn
        result = client.ensure_a(
            record, ip, ttl, owned=owned, allow_overwrite=allow_overwrite
        )
        if result["action"] == "not_owned":
            raise ADDNSError(result["detail"])
        verify = client.read_back(record)
        utility_ref = client.utility_ref or ""

    # Persist identity on the SERVER (guaranteed present in both hooks) for teardown.
    server.set_value_for_custom_field(CF_FQDN, fqdn)
    server.set_value_for_custom_field(CF_ZONE, zone)
    server.set_value_for_custom_field(CF_UTILITY, utility_ref)
    server.set_value_for_custom_field(CF_IP, ip)

    logger.info(
        "AD DNS create [%s -> %s] action=%s dn=%s job=%s resource=%s verified=%s",
        fqdn, ip, result["action"], result["dn"], job.id, _resource_gid(job),
        [r.get("ip") for r in verify["records"] if r.get("ip")],
    )
    set_progress("AD DNS: %s -> %s (%s)." % (fqdn, ip, result["action"]))
    summary = "%s -> %s (%s)" % (fqdn, ip, result["action"])

    # Optional: block until the record actually resolves from the appliance.
    # Runs AFTER the LDAP connection is released (see the `with` above) so a long
    # wait never pins the bind open. Poll interval and timeout come from the
    # ldap_dns config globals.
    if wait_for_resolution:
        set_progress(
            "AD DNS: waiting for %s to resolve to %s from the appliance "
            "(polling every %ds, up to %ds)."
            % (fqdn, ip, DNS_RESOLUTION_POLL_SECS, DNS_RESOLUTION_TIMEOUT_SECS)
        )
        outcome = wait_for_dns_resolution(
            fqdn, 
            expected_ip=ip, 
            progress=set_progress, 
            poll_secs=DNS_RESOLUTION_POLL_SECS, 
            timeout_secs=DNS_RESOLUTION_TIMEOUT_SECS
        )
        if not outcome["resolved"]:
            # Record was written; it just never became resolvable in time. Surface
            # as a failure for this server so the operator who opted into waiting
            # sees the propagation problem.
            raise ADDNSError(
                "A record %s -> %s was written but did not become resolvable by the "
                "appliance within %ds (%d attempts; last seen: %s)."
                % (fqdn, ip, DNS_RESOLUTION_TIMEOUT_SECS, outcome["attempts"],
                   ", ".join(outcome["ips"]) or "nothing")
            )
        set_progress(
            "AD DNS: %s resolved to %s after %ds (%d attempts)."
            % (fqdn, ip, outcome["elapsed"], outcome["attempts"])
        )
        summary += " [resolved in %ds]" % outcome["elapsed"]

    return summary
