# AD DNS - Create A Record

Post-Provision orchestration action that registers an Active Directory-integrated DNS A record for every server in the job, writing the `dnsNode`/`dnsRecord` object over LDAPS with the server's LDAP Utility credentials. The record name comes from the hostname, the IP from the server NIC, and the zone from the utility's `ldap_domain`. The registered identity is stored on the server so HPA-dnsdec01 can remove exactly that record at decommission.

Operator runbook: [../../docs/ad-dns-ldap-setup.md](../../docs/ad-dns-ldap-setup.md).

## Contents
| Role | ID | Name |
|---|---|---|
| Plugin | OHK-dnsadd01 | AD DNS - Create A Record |
| Shared module | SHM-dnsldap1 | ldap_dns (AD DNS over LDAP) |
| Paired action | HPA-dnsdec01 | AD DNS - Delete A Record (Pre-Delete) |

Action input: Wait for DNS Resolution (BOOL, default `False` via `action_input_default_values`).

## Prerequisites
- Each server must resolve to an LDAP Utility: its `domain_to_join` field, a utility whose `ldap_domain` matches the NIC DNS domain, or the single configured utility. Set `domain_to_join` on servers, environments, or blueprints to make this deterministic.
- The utility needs `serviceaccount` (UPN), `servicepasswd`, `ip`, and `ldap_domain`; the bind account needs create-child for `dnsNode` on the zone plus read. The appliance must reach the DC on 636/tcp; writes force LDAPS even when the utility is configured for plain LDAP.
- OHK-dnsadd01 declares `target_os_families: ["Linux"]`; clear that restriction on the plugin if Windows servers should also get records.

## Setup
1. Re-enter the LDAP Utility bind password in the UI after every repo sync; the plugin fails fast when it is blank.
2. Review the `OPERATOR CONFIG BLOCK` in `shared_modules/SHM-dnsldap1/SHM-dnsldap1_script.py` (`DEFAULT_TTL`, `ALLOWED_ZONES`, `ALLOW_OVERWRITE`, `CA_CERTS_FILE`); restart CloudBolt after changing it.
3. Run the byte-match acceptance test (runbook section 4) once per environment.
4. Ships disabled. Enable it in Admin > Orchestration Actions after steps 1-3 are complete.
5. To wait for propagation, set the Wait for DNS Resolution default to `True` in the UI after import (it ships `False`).

## Notes
- Idempotent and no-clobber: re-provisioning a name CloudBolt owns replaces only the A value; a pre-existing foreign A record fails that server unless `ALLOW_OVERWRITE` is set.
- With Wait for DNS Resolution on, the plugin polls the appliance resolver every 15 s for up to 15 min (constants in `OHK-dnsadd01_script.py`) and fails that server if the record never resolves.
- One server's failure is reported without aborting the others; `continue_on_failure` is false, so any FAILURE fails the provisioning job.
- Stored identity on the server: `addns_fqdn`, `addns_zone`, `addns_ldap_utility`, `addns_record_ip`.
