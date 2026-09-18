# Azure CMK - Remove Per-VM Disk Encryption Set

Post-Delete orchestration action that removes the per-VM customer-managed-key resources created by [Azure CMK - Per-VM Disk Encryption Set](../HPA-w1dmx20b/README.md). After CloudBolt's Azure handler has destroyed the VM and its disks, it deletes the VM's Disk Encryption Set, revokes the DES identity's Key Vault grant, and soft-deletes the per-VM key.

## Contents
| Role | ID | Name |
|---|---|---|
| Orchestration action | HPA-h7g0i0dx | Azure CMK - Remove Per-VM Disk Encryption Set (Post-Delete, run_seq 1) |
| Plugin | OHK-2vpg4pff | Azure CMK - Remove Per-VM Disk Encryption Set |
| Shared module | SHM-vjwmn6nq | azure_disk_encryption |
| Paired build | HPA-w1dmx20b | Azure CMK - Per-VM Disk Encryption Set (Post-Provision) |

## Prerequisites
- Same handler, Key Vault and service-principal roles as HPA-w1dmx20b; see its README and [../../docs/azure-vm-disk-encryption-setup.md](../../docs/azure-vm-disk-encryption-setup.md). Deleting keys needs Key Vault Crypto Officer (or access-policy keys `delete`); revoking the grant needs the same role-assignment or access-policy rights the build used.

## Setup
1. Complete the HPA-w1dmx20b setup first.
2. This action ships with `enabled: false`. Enable it together with HPA-w1dmx20b: the build action records the identifiers this one relies on, so enabling only one side leaves resources orphaned.

## Notes
- Acts only on identifiers recorded on the server (`azure_cmk_des_id`, `azure_cmk_key_id`, `azure_cmk_applied_key_vault_id`, `azure_cmk_grant_ref`); nothing is re-derived. Nothing recorded, already gone, or a DES that no longer uses the recorded key is a skip (WARNING at most), never FAILURE. `continue_on_failure` is true.
- The DES delete is retried for up to 5 minutes while Azure still reports disks detaching.
- The key is soft-deleted only. Purge protection (required for DES vaults) blocks a hard purge; Azure purges it after the vault's retention window (7-90 days). When a user-assigned identity was used, no grant is revoked.
- If CloudBolt's own delete job fails before the VM is destroyed, Post-Delete does not run and the key and DES remain for manual cleanup.
