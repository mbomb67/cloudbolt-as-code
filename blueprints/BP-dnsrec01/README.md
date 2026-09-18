# DNS A Record

Orderable Active Directory-integrated DNS A record. Build writes the `dnsNode`/`dnsRecord` object directly over LDAPS using the credentials of a chosen LDAP Utility (the zone is that utility's `ldap_domain`); teardown tombstones the record; discovery inventories existing A records from every configured LDAP Utility. No server is provisioned.

Operator runbook: [../../docs/ad-dns-ldap-setup.md](../../docs/ad-dns-ldap-setup.md). Encoder acceptance test: [../../docs/fixtures/README.md](../../docs/fixtures/README.md).

## Contents
| Role | ID | Name |
|---|---|---|
| Build | OHK-dnsbld01 | DNS Record - Build |
| Teardown | OHK-dnstrd01 | DNS Record - Teardown |
| Discovery | OHK-dnsdsc01 | DNS Record - Discover |
| Shared module | SHM-dnsldap1 | ldap_dns (AD DNS over LDAP) |

Order-form inputs (from OHK-dnsbld01): DNS Zone (dropdown of LDAP Utilities), Record (host label, not FQDN), Ip (IPv4).

## Prerequisites
- One or more CloudBolt LDAP Utilities with `ldap_domain` set to the DNS zone, a bind `serviceaccount` in UPN form, `servicepasswd`, and a DC `ip`. Writes always use LDAPS on `LDAPS_PORT` (636), even if the utility is configured for plain LDAP on 389; the appliance must reach the DC on 636/tcp.
- The bind account needs create-child for `dnsNode` on the target zone plus read on the zone (SOA serial). Not DnsAdmins, not domain-wide.
- `ldap3` on the appliance (present; the module targets the shipped ldap3 2.5.1).
- `any_group_can_deploy` is false: grant deployment permissions to groups after import.

## Setup
1. Re-enter each LDAP Utility's bind password in the CloudBolt UI after every repo sync; syncs redact it and the plugins fail fast when it is blank.
2. Review the `OPERATOR CONFIG BLOCK` in `shared_modules/SHM-dnsldap1/SHM-dnsldap1_script.py`: `DEFAULT_TTL` (3600), `ALLOWED_ZONES` (empty = any zone a utility resolves), `ALLOW_OVERWRITE` (False), `ALLOW_SPECIAL_NAMES` (False), `CA_CERTS_FILE` (empty = encrypt-only TLS; set a CA bundle for production). A shared-module change needs a CloudBolt restart.
3. Run the byte-match acceptance test (runbook section 4) once per environment before trusting writes.
4. To use discovery, set `DRY_RUN = False` in `plugins/OHK-dnsdsc01/OHK-dnsdsc01_script.py`; as shipped it logs what it would import and returns nothing.

## Notes
- No-clobber: build fails if an A record for the name exists that CloudBolt did not create, unless `ALLOW_OVERWRITE` is set.
- Teardown tombstones (does not hard-delete) and only when the live A value still matches the IP recorded at build. Missing identity, already-gone node, or IP mismatch return WARNING so the delete is never blocked. The DNS server reaps tombstones on its next zone reload.
- The resource is renamed to the FQDN and carries `addns_fqdn`, `addns_zone`, `addns_ldap_utility`, `addns_record_ip`; discovery keys on `addns_fqdn`.
- Discovery skips, rather than fails on, utilities that are unreachable or whose password was redacted.
- Related lifecycle hooks for provisioned servers: `orchestration_actions/HPA-dnscrt01` (create) and `HPA-dnsdec01` (delete).
