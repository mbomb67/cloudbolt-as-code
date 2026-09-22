"""
CloudBolt orchestration plugin (Post-Provision): give every newly built Azure
VM its own customer-managed disk-encryption key and Disk Encryption Set (DES),
and re-encrypt the VM's OS and data disks with it.

Per server on the job (Post-Provision passes NO `server` kwarg; iterate
job.server_set.all() like the OOTB hooks):

  1. Resolve the Azure coordinates (handler, subscription, resource group,
     VM name == hostname) and read the VM (region, security type, disks).
  2. Resolve the Key Vault: the server's azure_cmk_key_vault_id custom field
     (Environment/Group override) or this action's default input. Validate
     soft delete + purge protection (mandatory for DES) and same region.
  3. Create the per-VM key  <vm>-key  (RSA 2048/3072/4096 or RSA-HSM, wrap/unwrap)
     with an expiry azure_cmk_key_expiration_days from now — the policy ceiling
     is under 90 days, so the input is capped at 89.
     Re-uses an existing key of that name instead of minting a new version.
  4. Create the per-VM DES  <vm>-DES  in the VM's resource group/region with a
     system-assigned identity, the *versioned* key URL, and optional
     auto-rotation to the latest key version.
  5. Grant the DES identity get/wrapKey/unwrapKey on the vault (RBAC role
     "Key Vault Crypto Service Encryption User", or an access policy).
     Optional alternative: when azure_cmk_user_assigned_identity_id is set,
     the DES is created with that pre-existing user-assigned managed identity
     instead of a system-assigned one and step 5 is skipped entirely — the
     identity must already hold get/wrapKey/unwrapKey on the vault. The
     handler SPN then needs no role-assignment rights on the vault.
  6. Record key/DES/grant identifiers on the Server (traceability + teardown).
  7. Deallocate the VM, PATCH every managed disk to the DES, optionally enable
     encryption at host, and start the VM again (always, even on failure).

Why Post-Provision and not "provision the VM with the DES": CloudBolt's Azure
handler (TechnologyWrapper.create_node) exposes encryption_at_host and
security_type but has no disk-encryption-set parameter, so the DES cannot be
injected into the create call. Azure requires disks to be detached from a
running VM to change their encryption, hence the deallocate/start cycle.
This adds a few minutes to provisioning. Encryption at host can alternatively
be set natively at create time by CloudBolt; this plugin only ensures it.

Inputs (declared on this plug-in; defaults supplied by the orchestration
action HPA-w1dmx20b): azure_cmk_key_vault_id, azure_cmk_key_type,
azure_cmk_key_size, azure_cmk_key_expiration_days, azure_cmk_des_suffix,
azure_cmk_key_suffix, azure_cmk_encryption_type, azure_cmk_encryption_at_host,
azure_cmk_auto_key_rotation, azure_cmk_user_assigned_identity_id (optional).

Return contract: (status, output, error). Non-Azure servers are skipped
(SUCCESS). Any per-server hard failure -> FAILURE for the job with the other
servers still processed. Encryption-at-host problems are WARNING (the disks
are still CMK-encrypted and the VM is restarted).

Vendor APIs are wrapped in shared_modules/azure_disk_encryption, with the
Microsoft REST reference cited at each call site.
"""
import time

from common.methods import set_progress
from utilities.logger import ThreadLogger

from shared_modules.azure_disk_encryption import (
    AzureCMKClient,
    AzureCMKError,
    AzureCMKSkip,
    CF_APPLIED_VAULT_ID,
    CF_DES_ID,
    CF_DES_PRINCIPAL_ID,
    CF_EAH_APPLIED,
    CF_ENCRYPTED_DISKS,
    CF_GRANT_REF,
    CF_KEY_ID,
    CF_KEY_VAULT_ID,
    ENCRYPTION_TYPES,
    GRANT_REF_UAMI_PREFIX,
    KEY_EXPIRATION_DAYS_DEFAULT,
    KEY_EXPIRATION_DAYS_MAX,
    ensure_custom_fields,
    sanitize_name,
    server_azure_coords,
    user_assigned_principal_id,
)

logger = ThreadLogger(__name__)


def _str(raw, default=""):
    raw = (raw or "").strip()
    return raw if raw else default


def _bool(raw, default):
    raw = (raw or "").strip().lower()
    if not raw:
        return default
    return raw in ("true", "1", "yes", "on")


def _int(raw, default, name):
    raw = (raw or "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        raise AzureCMKError(f"{name} must be a whole number (got '{raw}')")


def _config():
    """Read this action's inputs. Every value is quoted and cast (never eval'd)."""
    key_size_raw = _str("{{ azure_cmk_key_size }}", "2048")
    try:
        key_size = int(key_size_raw)
    except ValueError:
        raise AzureCMKError(f"azure_cmk_key_size must be 2048, 3072 or 4096 (got '{key_size_raw}')")
    # Key lifetime. The shared module re-validates and rejects anything that is
    # not 1..KEY_EXPIRATION_DAYS_MAX-1, so an out-of-policy value never reaches Azure.
    expiration_days = _int(
        "{{ azure_cmk_key_expiration_days }}", KEY_EXPIRATION_DAYS_DEFAULT, "azure_cmk_key_expiration_days"
    )
    if expiration_days < 1 or expiration_days >= KEY_EXPIRATION_DAYS_MAX:
        raise AzureCMKError(
            f"azure_cmk_key_expiration_days must be between 1 and {KEY_EXPIRATION_DAYS_MAX - 1} "
            f"(keys must expire in less than {KEY_EXPIRATION_DAYS_MAX} days); got {expiration_days}"
        )
    return {
        "key_vault_id": _str("{{ azure_cmk_key_vault_id }}"),
        "key_type": _str("{{ azure_cmk_key_type }}", "RSA"),
        "key_size": key_size,
        "key_expiration_days": expiration_days,
        "des_suffix": _str("{{ azure_cmk_des_suffix }}", "-DES"),
        "key_suffix": _str("{{ azure_cmk_key_suffix }}", "-key"),
        "encryption_type": _str("{{ azure_cmk_encryption_type }}", ENCRYPTION_TYPES[0]),
        "encryption_at_host": _bool("{{ azure_cmk_encryption_at_host }}", True),
        "auto_key_rotation": _bool("{{ azure_cmk_auto_key_rotation }}", True),
        # Optional. Empty = system-assigned DES identity + per-DES grant (default path).
        "user_assigned_identity_id": _str("{{ azure_cmk_user_assigned_identity_id }}"),
    }


def run(job, *args, **kwargs):
    servers = list(job.server_set.all())
    if not servers:
        return "SUCCESS", "No servers on this job; nothing to encrypt.", ""

    ensure_custom_fields()
    cfg = _config()

    applied, skipped, warnings, failures = [], [], [], []
    for server in servers:
        host = getattr(server, "hostname", "?")
        try:
            message, warning = _process_server(job, server, cfg)
            applied.append(message)
            if warning:
                warnings.append(warning)
            set_progress(f"Azure CMK: {message}")
        except AzureCMKSkip as exc:
            skipped.append(f"{host}: skipped ({exc})")
            set_progress(f"Azure CMK: {host}: skipped ({exc})")
        except AzureCMKError as exc:
            failures.append(f"{host}: {exc}")
            logger.error("Azure CMK failed for %s: %s", host, exc)
            set_progress(f"Azure CMK: {host}: FAILED: {exc}")
        except Exception as exc:  # noqa: BLE001 — surface, don't abort other servers
            failures.append(f"{host}: unexpected error: {exc}")
            logger.exception("Azure CMK unexpected error for %s", host)

    summary = "; ".join(applied + skipped) or "nothing to do"
    if failures:
        return "FAILURE", summary, " | ".join(failures)
    if warnings:
        return "WARNING", summary, " | ".join(warnings)
    return "SUCCESS", summary, ""


def _process_server(job, server, cfg):
    rh, subscription_id, resource_group, vm_name = server_azure_coords(server)
    if not subscription_id:
        raise AzureCMKError("the Azure handler has no subscription id (serviceaccount)")
    client = AzureCMKClient(rh)

    # --- Key Vault (per-server override wins over the action default) ----------
    vault_id = (server.get_value_for_custom_field(CF_KEY_VAULT_ID) or "").strip() or cfg["key_vault_id"]
    if not vault_id:
        raise AzureCMKError(
            "no Key Vault configured: set the azure_cmk_key_vault_id default on the "
            "'Azure CMK - Per-VM Disk Encryption Set' orchestration action, or as a parameter "
            "on the Environment/Group"
        )

    set_progress(f"Azure CMK: {vm_name}: reading VM and Key Vault")
    vm = client.get_vm(subscription_id, resource_group, vm_name, instance_view=True)
    location = vm.get("location")
    security_type = ((vm.get("properties") or {}).get("securityProfile") or {}).get("securityType")
    if security_type == "ConfidentialVM":
        # Confidential VMs need a DES of type ConfidentialVmEncryptedWithCustomerKey
        # and a different attach flow; out of scope for this action.
        raise AzureCMKSkip("Confidential VM — not handled by this action")
    os_disk_id, data_disk_ids = client.vm_disk_ids(vm)

    vault = client.get_vault(vault_id)
    client.validate_vault_for_des(vault, location)
    vault_props = vault.get("properties") or {}
    vault_uri = vault_props.get("vaultUri")
    if not vault_uri:
        raise AzureCMKError(f"Key Vault {vault_id} has no vaultUri")

    tags = {
        "cloudbolt_server": getattr(server, "global_id", "") or "",
        "cloudbolt_job": str(getattr(job, "id", "")),
        "managed_by": "CloudBolt",
    }

    # --- 1. per-VM key (idempotent: reuse if present) ----------------------------
    key_name = sanitize_name(f"{vm_name}{cfg['key_suffix']}", allowed=r"[^0-9A-Za-z-]", max_len=127)
    key = client.get_key(vault_uri, key_name)
    if key:
        # Reuse keeps re-runs idempotent, so the expiry is whatever the existing
        # key already carries — report it rather than minting a new version.
        existing_exp = (key.get("attributes") or {}).get("exp")
        when = time.strftime("%Y-%m-%d", time.gmtime(existing_exp)) if existing_exp else "no expiry set"
        set_progress(f"Azure CMK: {vm_name}: key '{key_name}' already exists; reusing (expires {when})")
    else:
        set_progress(f"Azure CMK: {vm_name}: creating key '{key_name}' "
                     f"({cfg['key_type']} {cfg['key_size']}, expires in {cfg['key_expiration_days']} days)")
        key = client.create_key(vault_uri, key_name, cfg["key_type"], cfg["key_size"], tags,
                                expiration_days=cfg["key_expiration_days"])
    key_url = ((key or {}).get("key") or {}).get("kid")
    if not key_url:
        raise AzureCMKError(f"Key Vault did not return a key id for '{key_name}'")

    # --- 2. per-VM DES (PUT is idempotent) -------------------------------------
    des_name = sanitize_name(f"{vm_name}{cfg['des_suffix']}", allowed=r"[^0-9A-Za-z_-]", max_len=80)
    set_progress(f"Azure CMK: {vm_name}: creating Disk Encryption Set '{des_name}'")
    uami_id = cfg["user_assigned_identity_id"]
    des = client.create_des(
        subscription_id, resource_group, des_name, location, vault_id, key_url,
        cfg["encryption_type"], cfg["auto_key_rotation"], tags,
        user_assigned_identity_id=uami_id or None,
    )
    des_id = des["id"]

    # --- 3. grant the DES identity access to the key ---------------------------
    if uami_id:
        # Customer-managed, pre-authorised identity shared by all DESes: it must
        # already hold get/wrapKey/unwrapKey on the vault, so there is no per-DES
        # grant to make (and nothing for the teardown to revoke).
        des_principal_id = user_assigned_principal_id(des, uami_id) or uami_id
        grant_ref = GRANT_REF_UAMI_PREFIX + uami_id
        set_progress(f"Azure CMK: {vm_name}: DES uses user-assigned identity "
                     f"'{uami_id.rsplit('/', 1)[-1]}'; no per-DES grant needed")
    else:
        des_principal_id = des["identity"]["principalId"]
        set_progress(f"Azure CMK: {vm_name}: granting DES identity access to the vault")
        grant_ref = client.grant_des_key_access(vault, des_principal_id)

    # Persist identifiers now, before touching disks, so a later failure can
    # still be torn down (Post-Delete reads these).
    server.set_value_for_custom_field(CF_DES_ID, des_id)
    server.set_value_for_custom_field(CF_DES_PRINCIPAL_ID, des_principal_id)
    server.set_value_for_custom_field(CF_KEY_ID, key_url)
    server.set_value_for_custom_field(CF_APPLIED_VAULT_ID, vault_id)
    server.set_value_for_custom_field(CF_GRANT_REF, grant_ref)

    # --- 4. re-encrypt disks (needs a deallocated VM) ----------------------------
    pending = []
    for disk_id in [os_disk_id] + data_disk_ids:
        current_des, _ = client.disk_current_des(client.get_disk(disk_id))
        if current_des.lower() != des_id.lower():
            pending.append(disk_id)

    eah_wanted = cfg["encryption_at_host"]
    eah_already = bool(((vm.get("properties") or {}).get("securityProfile") or {}).get("encryptionAtHost"))
    need_eah = eah_wanted and not eah_already

    encrypted = []
    warning = None
    if pending or need_eah:
        was_running = client.vm_power_state(vm) in ("running", "starting")
        set_progress(f"Azure CMK: {vm_name}: deallocating VM to change disk encryption")
        client.deallocate_vm(subscription_id, resource_group, vm_name)
        primary_error = None
        try:
            for disk_id in pending:
                disk_name = disk_id.rsplit("/", 1)[-1]
                set_progress(f"Azure CMK: {vm_name}: encrypting disk '{disk_name}' with '{des_name}'")
                client.set_disk_encryption(disk_id, des_id, cfg["encryption_type"])
                encrypted.append(disk_name)
            if need_eah:
                try:
                    set_progress(f"Azure CMK: {vm_name}: enabling encryption at host")
                    client.set_encryption_at_host(subscription_id, resource_group, vm_name, True)
                    eah_already = True
                except AzureCMKError as exc:
                    # Disks are already CMK-encrypted; don't leave the VM down for this.
                    warning = (f"{vm_name}: encryption at host NOT enabled: {exc} — register "
                               "Microsoft.Compute/EncryptionAtHost on the subscription and use a supported VM size")
                    logger.warning(warning)
        except Exception as exc:  # noqa: BLE001 — always try to bring the VM back first
            primary_error = exc
        finally:
            if was_running:
                set_progress(f"Azure CMK: {vm_name}: starting VM")
                try:
                    client.start_vm(subscription_id, resource_group, vm_name)
                except AzureCMKError as exc:
                    if primary_error is None:
                        raise
                    logger.error("Azure CMK: %s: VM restart also failed after an error: %s", vm_name, exc)
        if primary_error is not None:
            if encrypted:
                # Partial progress is still recorded for traceability before failing.
                server.set_value_for_custom_field(CF_ENCRYPTED_DISKS, ",".join(encrypted))
            raise primary_error

    all_disks = [d.rsplit("/", 1)[-1] for d in [os_disk_id] + data_disk_ids]
    server.set_value_for_custom_field(CF_ENCRYPTED_DISKS, ",".join(all_disks))
    server.set_value_for_custom_field(CF_EAH_APPLIED, bool(eah_already))

    message = "%s: key '%s', DES '%s', disks %s%s" % (
        vm_name, key_name, des_name,
        ", ".join(encrypted) if encrypted else "already encrypted",
        ", encryption at host on" if eah_already else "",
    )
    logger.info("Azure CMK applied [%s] des=%s key=%s grant=%s job=%s", vm_name, des_id, key_url, grant_ref, job.id)
    return message, warning
