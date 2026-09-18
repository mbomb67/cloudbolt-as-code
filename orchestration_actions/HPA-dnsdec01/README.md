# AD DNS - Delete A Record

Pre-Delete (server decommission) orchestration action that tombstones the AD-integrated DNS A record HPA-dnscrt01 registered for each server being deleted. It acts only on the identity stored on the server at create time, and only when the live A value still matches the recorded IP.

Prerequisites, LDAP Utility setup, and the shared-module config block are the same as the paired create action: see [../HPA-dnscrt01/README.md](../HPA-dnscrt01/README.md) and the runbook [../../docs/ad-dns-ldap-setup.md](../../docs/ad-dns-ldap-setup.md).

## Contents
| Role | ID | Name |
|---|---|---|
| Plugin | OHK-dnsdel01 | AD DNS - Delete A Record |
| Shared module | SHM-dnsldap1 | ldap_dns (AD DNS over LDAP) |
| Paired action | HPA-dnscrt01 | AD DNS - Create A Record (Post-Provision) |

## Setup
1. Ships disabled. Enable alongside HPA-dnscrt01 once its setup is complete.
2. Re-enter the LDAP Utility bind password after every repo sync.

## Notes
- Tombstones (sets `dNSTombstoned`; does not hard-delete). The DNS server reaps the node on its next zone reload (about 180 s). Idempotent.
- Nothing recorded on the server, node already gone, or a record now pointing at a different IP all return WARNING, never FAILURE, so a decommission is never blocked and a reused name is never clobbered.
- If the LDAP Utility recorded at create time no longer exists, the plugin re-derives the directory from the server (runbook section 7, R1).
- Fires at server decommission (`pre_decom`), not at blueprint resource deletion; the DNS A Record blueprint (BP-dnsrec01) has its own teardown plugin.
