# Azure CMK - Per-VM Disk Encryption Set

Post-Provision orchestration action that gives every Azure VM CloudBolt builds its own customer-managed key and Disk Encryption Set (DES), then re-encrypts the VM's OS and data disks with it. For each Azure server on the job it creates `<vm>-key` in the configured Key Vault, creates `<vm>-DES` in the VM's resource group and region, grants the DES identity key access, deallocates the VM, re-points every managed disk to the DES, optionally enables encryption at host, starts the VM, and records the IDs on the server for the paired Post-Delete action.

## Contents
| Role | ID | Name |
|---|---|---|
| Orchestration action | HPA-w1dmx20b | Azure CMK - Per-VM Disk Encryption Set (Post-Provision, run_seq 2) |
| Plugin | OHK-vklpnqhq | Azure CMK - Per-VM Disk Encryption Set |
| Shared module | SHM-vjwmn6nq | azure_disk_encryption |
| Paired teardown | HPA-h7g0i0dx | Azure CMK - Remove Per-VM Disk Encryption Set (Post-Delete) |

## Prerequisites
- Azure Resource Manager resource handler. The plugin is filtered to `resource_technologies: ["Azure"]` and skips servers with no `azure_cmk_key_vault_id` parameter, plus non-Azure and Confidential VMs.
- A Key Vault per region with soft delete and purge protection enabled, in the same region as the VMs (another subscription is allowed; Managed HSM is not). Premium tier if the key type is `RSA-HSM`.
- Handler service principal: Contributor on the VM resource group (DES, disk and VM operations); on the vault, key create/get/delete, **rotation-policy write and recover** (Key Vault Crypto Officer covers all of them via `keys/*`; on an access-policy vault add the `Rotate`, `Set Rotation Policy`, `Get Rotation Policy` and `Recover` key permissions) and, unless a user-assigned identity is supplied, rights to create role assignments (Key Vault Data Access Administrator) or edit access policies. Full role table in the runbook.
- Encryption at host (default on) requires `Microsoft.Compute/EncryptionAtHost` registered on the subscription and a VM size that supports it.

## Setup
1. Read [../../docs/azure-vm-disk-encryption-setup.md](../../docs/azure-vm-disk-encryption-setup.md) for Azure roles and behaviour.
2. Add the **`azure_cmk_key_vault_id`** parameter wherever CMK encryption should apply, with that region's vault ARM ID as the value: `/subscriptions/<sub>/resourceGroups/<rg>/providers/Microsoft.KeyVault/vaults/<name>`. Environment, Group, blueprint and server all work, resolved by CloudBolt's normal parameter precedence. **This parameter is the on/off switch**: a server with no value is skipped, so the action is safe to enable globally, and multi-region is just a different value per Environment.
3. Optional: set **Azure CMK User-Assigned Identity (Resource ID)** to an existing identity that already holds Key Vault Crypto Service Encryption User on the vault. The per-DES grant is then skipped and the SPN needs no role-assignment rights.
4. Review the key lifetime defaults:
   - **Azure CMK Key Expiration (Days)** (`azure_cmk_key_expiration_days`, default 89) — keys must expire in under 90 days, so values outside 1-89 fail the job.
   - **Azure CMK Key Rotation Policy** (`azure_cmk_key_rotation_policy`, default `TimeBeforeExpiry`) — `TimeBeforeExpiry`, `TimeAfterCreate`, or `Disabled`. Both triggers express the same schedule; pick whichever your auditors read more easily.
   - **Azure CMK Key Rotation Lead (Days)** (`azure_cmk_key_rotation_lead_days`, default 7) — how far ahead of expiry Key Vault mints the new version.
5. Review the rest: key type and size, `-key` and `-des` suffixes, encryption type, encryption at host, **Azure CMK Auto Key Rotation** (leave on — see below).
6. Enable this action and HPA-h7g0i0dx together in Admin > Orchestration Actions. Both ship disabled. Enabling them does nothing on its own — only servers that resolve an `azure_cmk_key_vault_id` are touched.

## Notes
- Adds several minutes per VM: Azure requires disks to be detached from a running VM to change their encryption, so the VM is deallocated and restarted. Set `run_seq` so this runs after other Post-Provision actions that expect a running VM.
- Idempotent: re-runs reuse an existing `<vm>-key`, re-PUT the DES, tolerate an existing grant, and skip disks already on the DES.
- **Rebuilding a VM on a hostname that was used before works.** The teardown soft-deletes `<vm>-key`, and purge protection (mandatory on DES vaults) means the name cannot be freed for the vault's retention window — so a plain create returns `409 ... in a deleted but recoverable state, and its name cannot be reused`. The action recovers the key instead and carries on. Recovery needs the `keys/recover` permission: Key Vault Crypto Officer already has it via `keys/*`, but an access-policy vault needs the `Recover` key permission added.
- A key version is only handed to a DES if it is enabled, has an expiry, and still has more than `azure_cmk_key_rotation_lead_days` of life left; otherwise a fresh version is added under the same name. Azure shuts a VM down when the key behind its disks is disabled or expired, so a stale recovered version must not be reused. The rotation policy is re-applied either way, because it lives on the key rather than on a version.
- **Rotation is two halves and both must be on.** The Key Vault rotation policy mints the new key version; `azure_cmk_auto_key_rotation` (`rotationToLatestKeyVersionEnabled` on the DES) makes disks follow it, [within one hour and without rebooting the VM](https://learn.microsoft.com/en-us/azure/virtual-machines/disk-encryption#automatic-rotation-of-customer-managed-keys). Turn either off and the key eventually expires with nothing to succeed it — Azure then [shuts the VM down, with disk I/O failing about an hour after expiry](https://learn.microsoft.com/en-us/azure/virtual-machines/disk-encryption#full-control-of-your-keys). Setting the policy to `Disabled` is only safe if something else rotates these keys.
- Azure minimums the action enforces before calling Key Vault: the policy `expiryTime` must be at least 28 days, and the rotate trigger at least 7 days from both creation and expiry. So rotation needs `azure_cmk_key_expiration_days` >= 28, and the lead must be 7..(expiration - 7) — 7-82 at the shipped 89-day expiry. A bad combination fails the whole job up front rather than per server.
- The rotation policy is attached to the key, not to a key version, so re-runs re-apply it even when the existing `<vm>-key` is reused. Each scheduled rotation is [separately billed](https://learn.microsoft.com/en-us/azure/key-vault/keys/how-to-configure-key-rotation#pricing).
- Keep the previous key version enabled until re-wrap finishes; Azure re-wraps the disk encryption keys rather than re-encrypting the data.
- Encryption-at-host problems are WARNING (disks stay CMK-encrypted, VM restarted). A vault without purge protection, wrong region, Managed HSM, unmanaged or ephemeral OS disks, or any Azure API error fail the server; the VM is restarted first, and IDs are recorded before disks are touched so Post-Delete can still clean up.
- The shipped default encryption type is `EncryptionAtRestWithPlatformAndCustomerKeys` (double encryption), which Ultra and Premium SSD v2 disks do not support; use `EncryptionAtRestWithCustomerKey` for those. Disks that ever used Azure Disk Encryption cannot use CMK.
- Fields recorded on the server: `azure_cmk_des_id`, `azure_cmk_des_principal_id`, `azure_cmk_key_id`, `azure_cmk_applied_key_vault_id`, `azure_cmk_grant_ref`, `azure_cmk_encrypted_disks`, `azure_cmk_encryption_at_host_applied`.
