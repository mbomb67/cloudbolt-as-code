# AD-DNS encoder byte-match fixtures

The `ldap_dns` shared module hand-encodes the MS-DNSP `dnsRecord` blob. The
encoder's **logic** is unit-checked (28-byte blob, big-endian `TtlSeconds`,
`RANK_ZONE`, round-trip) but the **authoritative** acceptance gate is a
byte-for-byte match against a record created by the Windows DNS console. Capture
that here and diff before trusting the write path in production.

## Acceptance procedure

1. On the lab DC, create an A record through the **DNS Manager console** for a
   known name/IP/TTL (e.g. `host1` → `10.0.0.50`, TTL 3600) in the target zone.
2. Read the node's raw `dnsRecord` attribute over LDAP and hex-dump it, e.g.:

   ```powershell
   # ADSI / PowerShell — dump the dnsRecord bytes for the node
   Get-ADObject -SearchBase "DC=host1,DC=corp.example.com,CN=MicrosoftDNS,DC=DomainDnsZones,DC=corp,DC=example,DC=com" `
     -LDAPFilter "(objectClass=dnsNode)" -Properties dnsRecord |
     ForEach-Object { ($_.dnsRecord | ForEach-Object { '{0:x2}' -f $_ }) -join '' }
   ```

   Save the output as `dnsrecord_a_reference.hex` in this folder.
3. Compare against the encoder's output for the **same** input. Ignore bytes
   `8..11` (Serial — set to the live zone SOA serial + 1, so it legitimately
   differs run to run). Every other byte must match.

## Candidate bytes (this module's encoder)

For `host1 → 10.0.0.50`, `TTL = 3600`, `Serial = 1` (Serial bytes vary in real
records — compare everything *except* offset 8..11):

```
04 00            DataLength = 4            (LE)
01 00            Type = A (0x0001)         (LE)
05               Version = 0x05
f0               Rank = 0xF0 (RANK_ZONE)
00 00            Flags = 0x0000            (LE)
01 00 00 00      Serial = 1                (LE)   <-- ignore in the diff
00 00 0e 10      TtlSeconds = 3600         (BIG-endian)  <-- the classic-bug field
00 00 00 00      Reserved = 0              (LE)
00 00 00 00      Timestamp = 0 (static)    (LE)
0a 00 00 32      Data = 10.0.0.50          (network order)
```

Contiguous: `04000100 05f00000 01000000 00000e10 00000000 00000000 0a000032`
(28 bytes total).

## SOA serial read

`dnsrecord_soa_reference.hex` (optional): the apex (`DC=@,…`) node's SOA
`dnsRecord` blob. The module reads the zone serial from the **first 4 bytes of
the SOA Data section, big-endian** (`parse_soa_serial`) and writes new records
with `serial + 1`. Capture a real SOA blob here to confirm the parse offset
against your DC.
