# Azure per-VM disk encryption (customer-managed keys) — setup guide

Two orchestration actions give every Azure VM CloudBolt builds its own
customer-managed key (CMK) and Disk Encryption Set (DES), and clean both up
when the VM is deleted.

| Content | ID | Hook point | What it does |
|---|---|---|---|
| Orchestration action | `orchestration_actions/HPA-w1dmx20b` | Post-Provision | Per Azure VM: create `<vm>-key` in the configured Key Vault, create `<vm>-DES` in the VM's resource group/region, grant the DES identity key access, deallocate, re-encrypt OS + data disks, enable encryption at host, start, record IDs on the server |
| Plug-in | `plugins/OHK-vklpnqhq` | (paired) | Build logic + declared inputs |
| Orchestration action | `orchestration_actions/HPA-h7g0i0dx` | Post-Delete | Delete the DES, revoke the grant, soft-delete the key, using only the IDs recorded on the server |
| Plug-in | `plugins/OHK-2vpg4pff` | (paired) | Teardown logic |
| Shared module | `shared_modules/SHM-vjwmn6nq` (`azure_disk_encryption`) | — | REST client, Azure rules, custom-field definitions |

Both actions ship **disabled**. Enable them together once the prerequisites below are met.

**Execution filter.** Both plug-ins carry `"resource_technologies": ["Azure"]`, so CloudBolt only runs the actions for servers whose Resource Handler technology is Azure (the filter lives on the plug-in, `OrchestrationHook.resource_technologies`, not on the hook-point action). The plug-ins additionally guard in code by casting to `AzureARMHandler`, so an Azure Stack or non-Azure server is skipped even if the filter is widened.

## How this differs from the original outline (validated against CloudBolt + Azure docs)

| Outline step | Verdict | What actually happens |
|---|---|---|
| Create a DES per VM, same RG/region | ✅ as written | `<vm>` + `azure_cmk_des_suffix` (default `-DES`), VM's RG and region |
| Unique key per VM in a shared vault | ✅ as written | `<vm>` + `azure_cmk_key_suffix` (default `-key`), RSA 2048/3072/4096 or RSA-HSM, ops wrapKey/unwrapKey |
| Create key → DES → grant DES identity | ✅ order confirmed by Microsoft's CLI walkthrough | Grant is an RBAC role assignment on RBAC-mode vaults or an access policy on legacy vaults |
| **"Provision the VM with the DES attached"** | ❌ not possible from a hook | CloudBolt's Azure handler (`TechnologyWrapper.create_node`) has `encryption_at_host` and `security_type` parameters but **no DES parameter**, so the DES cannot be injected into the create call. Azure also requires disks to be detached from a running VM to change encryption. The action therefore runs at **Post-Provision** and does deallocate → PATCH disks → start (adds a few minutes per VM). |
| Encryption at host | ✅ but conditional | Enabled while the VM is deallocated **if** `Microsoft.Compute/EncryptionAtHost` is registered on the subscription and the size supports it; otherwise reported as WARNING and the VM is still restarted. CloudBolt can also set it natively at create time. |
| Store key/DES IDs on the server | ✅ | Custom fields listed below; the teardown trusts only these |
| Teardown "when the VM is deleted" | ✅ at **Post-Delete**, not Pre-Delete | Azure refuses to delete a DES while disks reference it, so the DES can only go after CloudBolt has destroyed the VM and its disks. The key is soft-deleted; purge is impossible by design (see below). |

**Why everything runs in one Post-Provision action.** Azure creates VMs running (an ARM VM PUT has no create-stopped option), so no "VM exists but not yet booted" hook window exists: `Pre-Network Configuration` is template-build only and `Pre-Create Resource` fires before the VM and its disks exist. A split design (key/DES/grant at Pre-Create Resource, disk cycle at Post-Provision) was considered and rejected in favour of a single, self-contained action; the only cost is that RBAC propagation is waited on inside the disk-PATCH retry instead of overlapping the VM build.

## Azure prerequisites

1. **Key Vault** (one per region you deploy into, shareable across VMs):
   - Soft delete **and** purge protection enabled — Azure: "These settings are mandatory when using a Key Vault for encrypting managed disks."
   - Same region as the VMs / DES. A different subscription is allowed; Managed HSM is not supported by this action.
   - Premium tier if you choose `RSA-HSM`.
2. **Encryption at host** (optional, default on): register the feature once per subscription:
   ```bash
   az feature register --name EncryptionAtHost --namespace Microsoft.Compute
   ```
   and use a VM size that supports it. VMs that ever had Azure Disk Encryption (ADE) cannot use it.
3. **Handler service principal permissions** (the Azure Resource Handler's `client_id`/`secret`):

| Scope | Needed for | Minimal built-in role |
|---|---|---|
| VM resource group (or subscription) | DES create/delete, disk PATCH, VM deallocate/start/PATCH | Contributor (what the handler normally has) |
| Key Vault resource | read vault properties | Reader (included in Contributor) |
| Key Vault resource, **RBAC-mode vault** | create/delete the DES role assignment | **Key Vault Data Access Administrator** (`8b54135c-b56d-4d72-a534-26097cfdc8d8`) or User Access Administrator |
| Key Vault resource, **access-policy vault** | add/remove the DES access policy | Contributor on the vault (`Microsoft.KeyVault/vaults/accessPolicies/write`) |
| Key Vault data plane, RBAC-mode vault | create/get/delete keys | **Key Vault Crypto Officer** (`14b46e9e-c2b7-41b4-b07b-48a6ebf60603`) |
| Key Vault data plane, access-policy vault | same | Access policy: keys `get`, `create`, `delete` |

The DES identity itself receives only `get`/`wrapKey`/`unwrapKey` (role **Key Vault Crypto Service Encryption User**, `e147488a-f6f5-4113-8e2d-b22465e65bf6`). When the optional user-assigned identity input is set, that identity must hold this role already and the two "role assignment / access policy" rows above are not needed by the SPN.

Sovereign clouds are supported via the handler's `cloud_environment` (login/ARM/vault endpoints for US Gov, China, Germany).

## CloudBolt configuration

1. Sync the repo; the five content units above appear.
2. On **Admin → Orchestration Actions → Post-Provision → Azure CMK - Per-VM Disk Encryption Set**, set the default value of **Azure CMK Key Vault (Resource ID)** to the vault's ARM ID:
   `/subscriptions/<sub>/resourceGroups/<rg>/providers/Microsoft.KeyVault/vaults/<name>`
   Adjust the other defaults if needed (key type/size, suffixes, encryption type, encryption at host, auto rotation).
3. Multi-region / multi-vault: add a parameter named `azure_cmk_key_vault_id` to each Environment (or Group) with that region's vault ID. A value on the server overrides the action default.
3a. (Optional) **Azure CMK User-Assigned Identity (Resource ID)** — leave empty for the default behaviour (each DES gets a system-assigned identity that the action grants key access per DES). Set it to an existing user-assigned managed identity's ARM ID to have every DES use that shared identity instead:
   - The identity must already hold *Key Vault Crypto Service Encryption User* (or access-policy keys Get/Wrap Key/Unwrap Key) on the vault; the action does not grant or revoke anything for it.
   - The handler SPN then no longer needs *Key Vault Data Access Administrator* / role-assignment rights on the vault, and there is no RBAC-propagation wait.
   - This is the identity mode Azure requires for cross-tenant vaults and (preview) Ultra / Premium SSD v2 disks.
   - Trade-off: one identity across all DESes instead of per-DES isolation.
4. Enable **both** actions (Post-Provision and Post-Delete).
5. (Optional) Set `run_seq` so this runs after other Post-Provision actions that expect the VM to be running; it deallocates and restarts the VM.

## What gets recorded on the server

| Custom field | Content |
|---|---|
| `azure_cmk_des_id` | DES ARM resource ID |
| `azure_cmk_des_principal_id` | DES system-assigned identity object ID |
| `azure_cmk_key_id` | Versioned Key Vault key URL (kid) |
| `azure_cmk_applied_key_vault_id` | Vault the key was created in |
| `azure_cmk_grant_ref` | Role-assignment ID, `accessPolicy:<objectId>`, or `userAssignedIdentity:<resourceId>` (shared identity; teardown revokes nothing) |
| `azure_cmk_encrypted_disks` | Comma-separated disk names |
| `azure_cmk_encryption_at_host_applied` | True/False |

## Behaviour details and limitations

- **Idempotent**: re-running reuses an existing `<vm>-key` (no new version), re-PUTs the DES, tolerates an existing role assignment/access policy, and skips disks already on the DES.
- **Skips** (SUCCESS, noted in output): non-Azure servers; Confidential VMs (they need `ConfidentialVmEncryptedWithCustomerKey` and a different flow).
- **Fails** the server (job FAILURE): vault without purge protection / wrong region / Managed HSM; unmanaged (VHD) or ephemeral OS disk; any Azure API failure. The VM is always restarted first when it was running. Identifiers are recorded before disks are touched so the Post-Delete action can still clean up a failed build.
- **Encryption at host** failures are WARNING (feature not registered / unsupported size); disks stay CMK-encrypted.
- **Auto key rotation**: the DES is created with `rotationToLatestKeyVersionEnabled` (default on) and the mandatory *versioned* key URL; Azure moves disks to a new key version within about an hour, without a reboot.
- **Key deletion is soft**: purge protection (required for DES vaults) blocks a hard purge, so the key becomes recoverable for the vault's retention window (7–90 days) and Azure purges it afterwards. Disabling or deleting a key that is still in use would shut the VMs down, which is why the teardown never touches a key while its DES exists.
- **Post-Delete ordering**: the action retries the DES delete for up to 5 minutes while Azure still reports disks detaching. If CloudBolt's own delete job fails before destroying the VM, Post-Delete does not run and the resources remain for a manual retry.
- Double encryption (`EncryptionAtRestWithPlatformAndCustomerKeys`) is not supported on Ultra / Premium SSD v2 disks. Disks previously encrypted with ADE cannot use CMK.

## Microsoft references used

- [Server-side encryption of Azure managed disks](https://learn.microsoft.com/en-us/azure/virtual-machines/disk-encryption) (restrictions, auto rotation, encryption at host)
- [Enable customer-managed keys with the Azure CLI](https://learn.microsoft.com/en-us/azure/virtual-machines/linux/disks-enable-customer-managed-keys-cli) (order of operations, "must not be attached to a running VM")
- [Enable encryption at host](https://learn.microsoft.com/en-us/azure/virtual-machines/disks-enable-host-based-encryption-portal) (feature registration, deallocation)
- REST: [Disk Encryption Sets create/delete](https://learn.microsoft.com/en-us/rest/api/compute/disk-encryption-sets), [Disks update](https://learn.microsoft.com/en-us/rest/api/compute/disks/update), [Virtual Machines deallocate/start/update](https://learn.microsoft.com/en-us/rest/api/compute/virtual-machines), [Key Vault keys create/delete](https://learn.microsoft.com/en-us/rest/api/keyvault/keys), [Vaults get / update access policy](https://learn.microsoft.com/en-us/rest/api/keyvault/keyvault/vaults), [Role assignments create](https://learn.microsoft.com/en-us/rest/api/authorization/role-assignments/create), [Built-in roles: security](https://learn.microsoft.com/en-us/azure/role-based-access-control/built-in-roles/security)
