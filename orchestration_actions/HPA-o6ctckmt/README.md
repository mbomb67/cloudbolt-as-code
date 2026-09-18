# Join Linux Server to AD Domain

Post-Provision orchestration action that joins each Linux server in the job to Active Directory with in-guest `realmd`/`SSSD`/`adcli`. Domain, join account, and password come from the LDAP Utility referenced by the server's `domain_to_join` field; servers without it, and Windows servers, are skipped. Runs at `run_seq` 100 so it follows DNS-configuring steps.

Runbook: [../../docs/linux-ad-domain-join-runbook.md](../../docs/linux-ad-domain-join-runbook.md).

## Contents
| Role | ID | Name |
|---|---|---|
| Plugin | OHK-exqmrl0e | Join Linux Server to AD Domain |

Requires CloudBolt 8.6 or later (`minimum_version_required`).

## Prerequisites
- An LDAP Utility for the target domain with `ldap_domain`, `serviceaccount` (UPN), and `servicepasswd`. The bind account is the join account and needs Create Computer Object (plus reset-password and write account-restrictions) delegated on the target OU. Not Domain Admin.
- Servers must have the seeded LDAP-typed `domain_to_join` field set to that utility (on the server, environment, or blueprint). This is the opt-in.
- Guest OS: RHEL family (including Oracle Linux, Rocky, Alma) or Debian/Ubuntu with base repos reachable; resolver pointing at AD DNS with SRV records resolvable; clock within 5 minutes of the DC; firewall open to DCs on 53, 88, 389/636, 445, 464, 3268/3269; a CloudBolt credential able to sudo (`run_with_sudo=True`).
- Hostnames unique within their first 15 characters (SAM account name truncation).

## Setup
1. Ensure the LDAP Utility exists on the target instance with its password populated; the utility is instance-bound and not stored in the repo. Re-enter the password after every repo sync.
2. Optionally set the `domain_ou` custom field (created by the plugin on first run) to the OU distinguished name for the computer object, e.g. `OU=Linux,OU=Servers,DC=corp,DC=example,DC=com`. Unset places it in the default Computers container.
3. The action imports with `enabled: true` and `continue_on_failure: false`, so a failed join fails the order. Validate against one RHEL-family and one Ubuntu VM (runbook "Validation before fleet rollout") before leaving it enabled fleet-wide.

## Notes
- Idempotent: a host already joined to the domain exits 0 without changes.
- Exit codes surfaced in the job log: 2 unsupported OS, 3 realm discovery/DNS failure, 4 post-join verification failed, 5 package install failed; any other non-zero is a `realm join` enrollment error (credentials, OU rights, DC connectivity).
- The join password is piped to `realm join` via stdin and scrubbed from returned output; it is base64-encoded in the script body for quoting, not secrecy.
- Sets `use_fully_qualified_names = False` in `sssd.conf` and enables home-directory creation on first login. Remote execution timeout is 1800 s.
- Computer-object cleanup on delete is not part of this content; enable CloudBolt's out-of-the-box "Delete Server from LDAP" hook, which keys off the same `domain_to_join`.
