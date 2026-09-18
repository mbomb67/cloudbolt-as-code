# AD-Integrated DNS A-Record content — operator setup

This content registers and de-registers **Active Directory–integrated DNS A
records** automatically over the server lifecycle, by writing the AD
`dnsNode`/`dnsRecord` object directly over **LDAP (`ldap3`)** — no Kerberos, no
dynamic DNS, no Windows-side PowerShell.

| Piece | ID | Role |
|---|---|---|
| Shared module | `shared_modules/SHM-dnsldap1` (`ldap_dns`) | MS-DNSP encoder + AD-DNS LDAP client + the `LDAPUtility` seam |
| Create plugin | `plugins/OHK-dnsadd01` | Ensures the A record at provision |
| Delete plugin | `plugins/OHK-dnsdel01` | Tombstones the A record at decommission |
| Create hook | `orchestration_actions/HPA-dnscrt01` | Fires `OHK-dnsadd01` at **Post-Provision** |
| Delete hook | `orchestration_actions/HPA-dnsdec01` | Fires `OHK-dnsdel01` at **Pre-Delete** (decommission) |

Both orchestration actions ship **disabled**. Complete the prerequisites and the
byte-match acceptance test below, then enable them.

---

## 1. Prerequisites

### Directory permissions (least privilege)

The bind service account needs **create-child for the `dnsNode` class on the
target zone object only** — not domain-wide, not `DnsAdmins`. Scope the ACL to
the one zone you intend CloudBolt to manage. The account also needs read on the
zone (to resolve the partition and read the SOA serial).

### Network

- The appliance must reach each writable DC on **`636/tcp` (LDAPS)** — this is
  **required for writes**. Modern AD rejects clear-LDAP writes to integrated DNS
  under default `LdapEnforceChannelBinding` / `LDAPServerIntegrity` policy, so
  this content **forces LDAPS (`use_ssl=True`) even if the LDAPUtility is
  configured for plain `ldap` on 389**.
- **No Kerberos ports** are needed.

### LDAPUtility

The content resolves the directory **from the server** (it does not hardcode
one), using the platform's own `get_ldaputility(server)` logic, which tries, in
order:

1. the server's `domain_to_join` custom field (an LDAP-typed field whose value
   *is* an `LDAPUtility`);
2. an `LDAPUtility` whose `ldap_domain` matches the server's NIC DNS domain;
3. the single configured `LDAPUtility`, if exactly one exists.

So: make sure the servers that should get DNS records resolve to the right
`LDAPUtility` (typically by setting `domain_to_join`). The utility must have a
valid `serviceaccount` (UPN form `user@domain`), `servicepasswd`, `ip`, `port`,
`ldap_domain`, and be **reachable over LDAPS**. If resolution is wrong or
ambiguous, set the optional `ldap_utility` override (see §3).

> **Secrets after sync:** repo syncs redact secrets. After every sync, re-enter
> the LDAPUtility bind password in the CloudBolt UI. The create/delete plugins
> fail fast with a clear "re-enter credentials" message when the password is blank.

---

## 2. Configure the shared-module config block

Edit the `OPERATOR CONFIG BLOCK` at the top of
`shared_modules/SHM-dnsldap1/SHM-dnsldap1_script.py`. This is the **primary,
versioned configuration surface** (config-as-code):

| Setting | Meaning | Default |
|---|---|---|
| `DEFAULT_TTL` | A-record TTL (seconds) when no override is given | `3600` |
| `ALLOWED_ZONES` | Hard allowlist of writable zones; empty = allow whatever the server's utility resolves to | `[]` |
| `ALLOW_SPECIAL_NAMES` | Permit `*`/`@`/leading-`_` record names | `False` |
| `ALLOW_OVERWRITE` | Replace an A record CloudBolt did **not** create (no-clobber off) | `False` |
| `CA_CERTS_FILE` | CA bundle path for LDAPS certificate validation | `""` |

> A shared-module change requires a CloudBolt restart to take effect (the module
> is cached in the running process), not just a sync.

### TLS / certificate validation (read this)

- With **`CA_CERTS_FILE` set**, the DC certificate is validated
  (`CERT_REQUIRED`) — the recommended production posture.
- With **`CA_CERTS_FILE` empty**, the LDAPS connection is **encrypt-only**
  (`CERT_NONE`), matching the platform's own tolerant self-signed posture. This
  protects the bind password from passive sniffing but **not from an active
  man-in-the-middle**, because a SIMPLE bind sends the password inside the TLS
  channel. Use a CA bundle in production; the unvalidated mode is for labs with
  self-signed DC certificates.

---

## 3. Enable

1. Confirm §1 and §2.
2. Run the **byte-match acceptance test** (§4) at least once per environment.
3. Enable `orchestration_actions/HPA-dnscrt01` and `orchestration_actions/HPA-dnsdec01`.
4. Provision a test server and confirm the record (see §4); decommission it and
   confirm the tombstone.

### Optional: per-deployment `{{ }}` overrides (advanced)

The plugins read optional overrides — `{{ zone }}`, `{{ ttl }}`,
`{{ ldap_utility }}`, `{{ record_name }}`, `{{ ip_address }}`,
`{{ allow_overwrite }}` — from the HPA's `action_input_default_values`. These are
**not required** (the config block + server-derivation cover the common case).

If you do add them, note the export/import quirk: each default-value entry's
`name` must carry the action's **`_a<hookid>` suffix** (where `<hookid>` is the
HookPointAction's id on *this* instance), while the plugin template references
the **unsuffixed** name (`{{ zone }}`). The cleanest way to get the suffix right
is to add the inputs through the CloudBolt UI on the action and re-export, rather
than hand-editing the JSON. If a `{{ }}` value renders empty at runtime, the
suffix is almost always the cause.

---

## 4. Byte-match acceptance test (required before trusting writes)

The encoder hand-builds the MS-DNSP `dnsRecord` blob. Its logic is unit-checked,
but validate it byte-for-byte against a record the Windows DNS console creates,
once per environment, before enabling in production. Full procedure and the
encoder's candidate bytes are in [`docs/fixtures/README.md`](fixtures/README.md):

1. Create an A record in the **DNS Manager console** (e.g. `host1` → `10.0.0.50`,
   TTL 3600).
2. Hex-dump that node's raw `dnsRecord` attribute over LDAP.
3. Diff against the encoder's output for the same input, ignoring the `Serial`
   bytes (offset 8..11). Every other byte must match — pay attention to the
   big-endian `TtlSeconds` (offset 12..15), the classic bug.

After provisioning a server with the create action enabled, also verify live:

```powershell
Resolve-DnsName host1.corp.example.com
Get-DnsServerResourceRecord -ZoneName corp.example.com -Name host1
```

After decommission, the record is **tombstoned** (not hard-deleted) and the DNS
server reaps it on its next zone reload (~180 s); re-running is idempotent.

---

## 5. Behavior & safety

- **Idempotent create.** Re-provisioning replaces only the A value for a name
  CloudBolt owns and **preserves any co-located non-A records** on the node.
- **No-clobber by default.** The create plugin refuses to overwrite a
  pre-existing A record CloudBolt did not create, unless `ALLOW_OVERWRITE` is set
  (or a `{{ allow_overwrite }}` override). This prevents a hostname collision
  from hijacking an existing record.
- **Ownership-verified delete.** The delete plugin tombstones a record **only
  when its live A value still matches the IP recorded at create time**, using the
  identity stored on the server. A record now pointing elsewhere, a name with no
  recorded identity, or an already-absent node all return WARNING — never a blind
  delete.
- **Input validation.** Record names are validated against an LDH allowlist
  (wildcards/apex/underscore-prefixed/DN-metacharacter names rejected unless
  `ALLOW_SPECIAL_NAMES`); IPs must be unicast IPv4.
- **Secret hygiene.** The bind password is read once into a local and never
  logged, returned, or interpolated into errors; the `LDAPUtility` instance is
  never logged via `vars()`/`%r` (which would leak the decrypted password).
- **Per server.** Both plugins iterate every server on the job; one server's
  failure is reported without aborting the others.

---

## 6. Troubleshooting

| Symptom | Likely cause / fix |
|---|---|
| `socket ssl wrapping error` / `Connection reset by peer` | The utility is plain `ldap` on 389; LDAPS is on **636**. The content already connects on `LDAPS_PORT` (636) for plain-`ldap` utilities — this error means 636 isn't serving LDAPS: ensure 636/tcp is open and the DC has a certificate (LDAPS enabled). If your DCs only expose StartTLS on 389, ask for the StartTLS variant. |
| Bind refused / timeout | LDAPS not reachable on 636; firewall; utility `ip` wrong. Writes use LDAPS on `LDAPS_PORT` (636) even when the utility is `protocol="ldap"`/389. |
| "Zone not found under any DNS partition" | Zone name wrong, or the account lacks read on the DNS partition. The error lists the probed DNs. |
| "could not read the SOA serial" | The account can't read the zone apex (`DC=@,…`), or the zone has no SOA. |
| `{{ }}` override renders empty | Missing `_a<hookid>` suffix on the action-input default-value name (§3). Prefer the config block. |
| "no LDAPUtility could be resolved" | Set `domain_to_join` on the server, or pass an `ldap_utility` override. |
| "refusing to overwrite" on create | A foreign A record exists for that name; set `ALLOW_OVERWRITE` only if intended. |
| Record not removed on delete | Live A no longer matches the recorded IP (it was repointed), or nothing was recorded — both are intentional WARNINGs. |
| Stale behavior after editing the SHM | Restart CloudBolt — shared-module code is process-cached. |

---

## 7. Known residuals (from code review)

A Tier-2 code review applied safe fixes (attribute guards, referral-safe partition
resolution, LDAP timeouts, delete-path validation, cred-free error messages). The
following were reviewed and **accepted as known residuals** — each is inherent,
tied to deferred scope, or gated on lab validation. None block until the lab
acceptance test (§4) runs.

- **R1 — Stale stored utility on delete.** If the `LDAPUtility` recorded at
  create time is later deleted in the UI, the delete plugin re-derives the
  directory from the server (`get_ldaputility`). The zone↔directory consistency
  check still guards against an unrelated domain, but a same-domain/different-DC
  resolution is possible. Mitigation: keep the LDAPUtility stable for the life of
  any server that registered a record; re-provision to refresh stored identity.
- **R2 — Write-then-persist orphan window.** `ensure_a` commits the AD record
  before the plugin stores the `addns_*` identity on the server. If a custom-field
  write fails in between, the record exists but the teardown identity does not, so
  Pre-Delete cannot reap it. There is no atomic LDAP+custom-field transaction;
  watch for create FAILUREs whose job log shows the A record was written, and
  remove such records manually if the server is discarded.
- **R3 — Single A value per name.** `ensure_a` ensures exactly one A value and
  collapses any co-located A records on replace. Managing multiple A records per
  name (`allow_multiple`) is deferred (see the plan's Scope Boundaries).
- **R4 — Reviving a tombstoned node.** A re-provision onto a name currently
  tombstoned (by any party) revives it without the no-clobber ownership check.
  A tombstoned node holds no live A record, so nothing live is overwritten; still,
  set `ALLOW_SPECIAL_NAMES`/`ALLOW_OVERWRITE` deliberately and scope the service
  account's ACL to the intended zone.
- **R5 — `dNSTombstoned` serialization (lab-verify).** The flag is written as a
  Python bool, matching the dnstool reference. Confirm ldap3 2.5.1 serializes it
  to the LDAP `TRUE`/`FALSE` literal against your DC during the §4 validation;
  if a tombstone fails to take, this is the first thing to check.

Standing lab gates (also §4): the encoder **byte-match** against a Windows DNS
console record, and the ldap3 2.5.1 raw-binary `dnsRecord` value handling on
`add`/`modify` — both must pass on a lab DC before enabling in production.

---

## References

- Encoder fixtures + byte-match: [`docs/fixtures/README.md`](fixtures/README.md)
- MS-DNSP dnsRecord layout: https://learn.microsoft.com/en-us/openspecs/windows_protocols/ms-dnsp/6912b338-5472-4f59-b912-0edb536b6ed8
- Reference implementation (non-Kerberos path mirrored): `dirkjanm/krbrelayx` dnstool.py
