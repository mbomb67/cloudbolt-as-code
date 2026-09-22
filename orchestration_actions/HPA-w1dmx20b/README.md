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
- Azure Resource Manager resource handler. The plugin is filtered to `resource_technologies: ["Azure"]` and skips non-Azure and Confidential VMs.
- A Key Vault per region with soft delete and purge protection enabled, in the same region as the VMs (another subscription is allowed; Managed HSM is not). Premium tier if the key type is `RSA-HSM`.
- Handler service principal: Contributor on the VM resource group (DES, disk and VM operations); on the vault, key create/get/delete **and rotation-policy write** (Key Vault Crypto Officer covers both; on an access-policy vault add the `Rotate`, `Set Rotation Policy` and `Get Rotation Policy` key permissions) and, unless a user-assigned identity is supplied, rights to create role assignments (Key Vault Data Access Administrator) or edit access policies. Full role table in the runbook.
- Encryption at host (default on) requires `Microsoft.Compute/EncryptionAtHost` registered on the subscription and a VM size that supports it.

## Setup
1. Read [../../docs/azure-vm-disk-encryption-setup.md](../../docs/azure-vm-disk-encryption-setup.md) for Azure roles and behaviour.
2. Set the default of **Azure CMK Key Vault (Resource ID)** (`azure_cmk_key_vault_id`). It ships as a `<placeholder>` path and every Azure order fails until it names a real vault: `/subscriptions/<sub>/resourceGroups/<rg>/providers/Microsoft.KeyVault/vaults/<name>`.
3. Multi-region: add a parameter named `azure_cmk_key_vault_id` on each Environment or Group; a server-level value overrides the action default.
4. Optional: set **Azure CMK User-Assigned Identity (Resource ID)** to an existing identity that already holds Key Vault Crypto Service Encryption User on the vault. The per-DES grant is then skipped and the SPN needs no role-assignment rights.
5. Review the key lifetime defaults:
   - **Azure CMK Key Expiration (Days)** (`azure_cmk_key_expiration_days`, default 89) — keys must expire in under 90 days, so values outside 1-89 fail the job.
   - **Azure CMK Key Rotation Policy** (`azure_cmk_key_rotation_policy`, default `TimeBeforeExpiry`) — `TimeBeforeExpiry`, `TimeAfterCreate`, or `Disabled`. Both triggers express the same schedule; pick whichever your auditors read more easily.
   - **Azure CMK Key Rotation Lead (Days)** (`azure_cmk_key_rotation_lead_days`, default 7) — how far ahead of expiry Key Vault mints the new version.
6. Review the rest: key type and size, `-key` and `-des` suffixes, encryption type, encryption at host, **Azure CMK Auto Key Rotation** (leave on — see below).
7. Enable this action and HPA-h7g0i0dx together in Admin > Orchestration Actions. Both ship disabled.

## Notes
- Adds several minutes per VM: Azure requires disks to be detached from a running VM to change their encryption, so the VM is deallocated and restarted. Set `run_seq` so this runs after other Post-Provision actions that expect a running VM.
- Idempotent: re-runs reuse an existing `<vm>-key` (no new version, so it keeps its original expiry), re-PUT the DES, tolerate an existing grant, and skip disks already on the DES.
- **Rotation is two halves and both must be on.** The Key Vault rotation policy mints the new key version; `azure_cmk_auto_key_rotation` (`rotationToLatestKeyVersionEnabled` on the DES) makes disks follow it, [within one hour and without rebooting the VM](https://learn.microsoft.com/en-us/azure/virtual-machines/disk-encryption#automatic-rotation-of-customer-managed-keys). Turn either off and the key eventually expires with nothing to succeed it — Azure then [shuts the VM down, with disk I/O failing about an hour after expiry](https://learn.microsoft.com/en-us/azure/virtual-machines/disk-encryption#full-control-of-your-keys). Setting the policy to `Disabled` is only safe if something else rotates these keys.
- Azure minimums the action enforces before calling Key Vault: the policy `expiryTime` must be at least 28 days, and the rotate trigger at least 7 days from both creation and expiry. So rotation needs `azure_cmk_key_expiration_days` >= 28, and the lead must be 7..(expiration - 7) — 7-82 at the shipped 89-day expiry. A bad combination fails the whole job up front rather than per server.
- The rotation policy is attached to the key, not to a key version, so re-runs re-apply it even when the existing `<vm>-key` is reused. Each scheduled rotation is [separately billed](https://learn.microsoft.com/en-us/azure/key-vault/keys/how-to-configure-key-rotation#pricing).
- Keep the previous key version enabled until re-wrap finishes; Azure re-wraps the disk encryption keys rather than re-encrypting the data.
- Encryption-at-host problems are WARNING (disks stay CMK-encrypted, VM restarted). A vault without purge protection, wrong region, Managed HSM, unmanaged or ephemeral OS disks, or any Azure API error fail the server; the VM is restarted first, and IDs are recorded before disks are touched so Post-Delete can still clean up.
- The shipped default encryption type is `EncryptionAtRestWithPlatformAndCustomerKeys` (double encryption), which Ultra and Premium SSD v2 disks do not support; use `EncryptionAtRestWithCustomerKey` for those. Disks that ever used Azure Disk Encryption cannot use CMK.
- Fields recorded on the server: `azure_cmk_des_id`, `azure_cmk_des_principal_id`, `azure_cmk_key_id`, `azure_cmk_applied_key_vault_id`, `azure_cmk_grant_ref`, `azure_cmk_encrypted_disks`, `azure_cmk_encryption_at_host_applied`.
