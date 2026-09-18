# Linux Active Directory Domain Join — Operations Runbook

A Post-Provision orchestration action joins Linux VMs to Active Directory.
Computer-object cleanup on delete is handled separately by CloudBolt's
out-of-the-box "Delete Server from LDAP" hook (see Decommission cleanup below),
not by this content. This runbook covers per-server setup, the AD/network
prerequisites, and the post-sync secret step.

## What ships

| Action | Hook point | Plugin | Failure posture |
|---|---|---|---|
| Join Linux Server to AD Domain | `Post-Provision` | `plugins/OHK-exqmrl0e` | `continue_on_failure: false` (a failed join fails the order) |

The join action iterates `job.server_set.all()`, skips Windows servers, runs
OS-native tooling (`realmd`/`SSSD`/`adcli`) in-guest via `server.execute_script`,
and resolves all domain config from the server's `domain_to_join`.

## Decommission cleanup (use the OOB plugin, not an in-guest leave)

Computer-object cleanup on delete is handled by CloudBolt's out-of-the-box
**Delete Server from LDAP** hook (`cbhooks/hookmodules/delete_server_from_ldap.py`),
not by this content. It removes the AD computer object **over the LDAP wire**, so
it works even when the server is powered off or was deleted prematurely — an
in-guest `realm leave` cannot. It resolves the target via the same
`domain_to_join` → `LDAPUtility` convention the join action uses, so the two
compose cleanly: set `domain_to_join` on the server, and both join and cleanup
key off it. Enable the OOB hook on the instance (it ships with CloudBolt and is
not part of this repo).

## Per-server setup

- **`domain_to_join`** (seeded, LDAP-typed custom field) — set it to the
  `LDAPUtility` for the target domain. Its value *is* the `LDAPUtility`; the
  join action reads the domain, bind account, and bind password from it. A
  server with no `domain_to_join` is skipped (clean no-op) — this is the opt-in.
- **`domain_ou`** (STR custom field, created automatically by the join plugin) —
  optional. Set to the OU distinguished name for the computer object, e.g.
  `OU=Linux,OU=Servers,DC=corp,DC=example,DC=com`. When unset, the object lands
  in the domain's default Computers container.

Set these on the server, its environment, or the blueprint as fits your model.

## AD-side prerequisites

- **Bind-account delegation.** The join uses the `LDAPUtility`'s bind account
  (`serviceaccount`, stored as a `user@domain` UPN) as the join account. It must
  have **Create Computer Object** rights (plus reset-password / write
  account-restrictions on objects it creates) delegated on the target OU. Never
  Domain Admin. The leave operation uses the host's own machine keytab and needs
  no credentials.
- **OU placement is in-guest.** `realm join --computer-ou=<domain_ou>` creates
  the object directly in that OU — no pre-staging required, given the delegation
  above. If your model forbids in-guest object creation, pre-stage the object and
  leave `domain_ou` unset (the join binds to the existing object).

## Network / OS prerequisites

The join fails without these — they are environmental, not the plugin's job:

- **DNS** — the VM's resolver must point at AD DNS (or DNS hosting the
  `_ldap._tcp.<domain>` / `_kerberos._tcp.<domain>` SRV records). `realm discover`
  failure exits 3. If a provisioning step sets DNS, the join must run after it —
  the join action is set to `run_seq: 100` to bias late; raise it further if a
  later step configures the resolver.
- **Time** — clock skew to the DC must be < 5 minutes (Kerberos). The script
  nudges `chrony`; ensure NTP/chrony is actually configured.
- **Firewall to DCs** — TCP/UDP 53 (DNS), 88 (Kerberos), 389 + 636 (LDAP/LDAPS),
  445 (SMB), 464 (kpasswd), 3268 + 3269 (Global Catalog).
- **Hostname** — the SAM computer name is truncated to 15 characters; ensure the
  hostname is unique within its first 15 characters or two hosts collide on one
  AD computer account.
- **Base-repo reachability** — packages install at provision time from base repos
  (RHEL BaseOS/AppStream; Ubuntu `main` + `universe`). Verify reachability on
  minimal images; the script retries installs up to 3 times before failing (exit 5).

## After every Source Control Repo sync

The actions and plugins sync as code, but the `LDAPUtility` (and its encrypted
`servicepasswd`) is **instance-bound and not stored in the repo**. It must
already exist on the target CloudBolt instance, with the bind account and
password populated, before the actions run.

## Exit codes (join script → job diagnostics)

| Exit | Meaning |
|---|---|
| 2 | Unsupported OS family (not RHEL- or Debian-family) |
| 3 | DNS / realm-discovery failure (resolver/SRV) |
| 4 | Post-join verification failed |
| 5 | Package install from base repos failed |
| other non-zero | `realm join` enrollment failure (creds, OU rights, or DC connectivity) |

The plugin reads these from `CommandExecutionException.rv` and surfaces a
matching diagnostic in the job log. The join password is fed to `realm join` via
stdin (never on argv) and scrubbed from the job output.

## Validation before fleet rollout

1. **Standalone first.** Wire the join script body into a throwaway Server Action
   and run it against a manually provisioned RHEL-family and Ubuntu VM before
   enabling the Post-Provision trigger.
2. **Per OS:** confirm `realm list` shows `configured: kerberos-member`, the
   computer object appears in the target OU, `id <aduser>` resolves, and an AD
   user can SSH in (home dir created).
3. **Idempotency:** re-run the join → clean no-op, no duplicate computer object.
4. **Negative:** wrong DNS → exit 3; bad bind password → enrollment failure;
   Windows server → skipped.

### Items to confirm on the actual fleet images

- **Join-user form.** `serviceaccount` is stored as a UPN (`user@domain`).
  Confirm the fleet's `realm`/`adcli` versions accept the UPN with
  `realm join --user=`; some setups expect the bare sAMAccountName.
- **Stdin password.** Confirm the fleet's `realm join` reads the password from
  piped stdin (baseline used here); some versions prefer `--stdin-password`.
