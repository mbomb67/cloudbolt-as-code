"""
CloudBolt orchestration plugin (Post-Provision): give every newly built Azure
VM its own customer-managed disk-encryption key and Disk Encryption Set (DES),
and re-encrypt the VM's OS and data disks with it.

Per server on the job (Post-Provision passes NO `server` kwarg; iterate
job.server_set.all() like the OOTB hooks):

  1. Resolve the Azure coordinates (handler, subscription, resource group,
     VM name == hostname) and read the VM (region, security type, disks).
  2. Resolve the Key Vault from the server's azure_cmk_key_vault_id parameter.
     That parameter is the switch for this whole action: a server with no value
     for it is skipped (SUCCESS), so CMK is opt-in per Environment, Group,
     blueprint or server. Validate soft delete + purge protection (mandatory
     for DES) and same region.
  3. Resolve the per-VM key  <vm>-key  (RSA 2048/3072/4096 or RSA-HSM, wrap/unwrap)
     with an expiry azure_cmk_key_expiration_days from now — the policy ceiling
     is under 90 days, so the input is capped at 89. A live, usable key of that
     name is reused as-is; one that is soft-deleted (the usual case when a VM is
     rebuilt on an old hostname, since the teardown deletes it and purge
     protection stops the name being freed) is recovered first; one that is
     disabled, expired, or carries no expiry gets a fresh version.
     Then PUT a Key Vault rotation policy derived from that same expiry, so
     Key Vault mints a fresh version azure_cmk_key_rotation_lead_days before
     the current one expires. The policy is per-key, not per-version, so it is
     re-applied on the reuse path too.
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

The Key Vault is NOT an action input: it comes only from the
azure_cmk_key_vault_id parameter on the server, which is what makes the action
safe to enable globally.

Inputs (declared on this plug-in; defaults supplied by the orchestration
action HPA-w1dmx20b): azure_cmk_key_type,
azure_cmk_key_size, azure_cmk_key_expiration_days,
azure_cmk_key_rotation_policy, azure_cmk_key_rotation_lead_days,
azure_cmk_des_suffix, azure_cmk_key_suffix, azure_cmk_encryption_type,
azure_cmk_encryption_at_host, azure_cmk_auto_key_rotation,
azure_cmk_user_assigned_identity_id (optional).

Rotation is two halves that must both be on: Key Vault creates the new key
version (this rotation policy), and the DES follows it within about an hour
(azure_cmk_auto_key_rotation -> rotationToLatestKeyVersionEnabled). Without the
policy nothing ever mints a new version and the key simply expires, which
shuts the VM down; without the DES flag the new version is ignored.

Return contract: (status, output, error). Servers with no
azure_cmk_key_vault_id parameter, and non-Azure servers, are skipped
(SUCCESS). Any per-server hard failure -> FAILURE for the job with the other
servers still processed. Encryption-at-host problems are WARNING (the disks
are still CMK-encrypted and the VM is restarted).

Vendor APIs are wrapped in shared_modules/azure_disk_encryption, with the
Microsoft REST reference cited at each call site.
"""
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
    ROTATION_LEAD_DAYS_DEFAULT,
    ROTATION_MODE_BEFORE_EXPIRY,
    ROTATION_MODE_DISABLED,
    ensure_custom_fields,
    plan_rotation_policy,
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
        "key_type": _str("{{ azure_cmk_key_type }}", "RSA"),
        "key_size": key_size,
        "key_expiration_days": expiration_days,
        # Validated in plan_rotation_policy() against the Azure minimums, which
        # depend on the expiry above.
        "key_rotation_policy": _str("{{ azure_cmk_key_rotation_policy }}", ROTATION_MODE_BEFORE_EXPIRY),
        "key_rotation_lead_days": _int(
            "{{ azure_cmk_key_rotation_lead_days }}", ROTATION_LEAD_DAYS_DEFAULT,
            "azure_cmk_key_rotation_lead_days"),
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
    # Fail the whole job on a bad rotation config rather than once per server.
    cfg["rotation_policy_body"] = plan_rotation_policy(
        cfg["key_rotation_policy"], cfg["key_rotation_lead_days"], cfg["key_expiration_days"])

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
    # --- Key Vault: the azure_cmk_key_vault_id parameter is the on/off switch ---
    # No value resolved for this server (no parameter on its Environment, Group,
    # blueprint or the server itself) means CMK was not asked for here, so leave
    # the server alone. This is the normal case for most servers, not an error.
    vault_id = (server.get_value_for_custom_field(CF_KEY_VAULT_ID) or "").strip()
    if not vault_id:
        raise AzureCMKSkip(f"no {CF_KEY_VAULT_ID} parameter set for this server")

    rh, subscription_id, resource_group, vm_name = server_azure_coords(server)
    if not subscription_id:
        raise AzureCMKError("the Azure handler has no subscription id (serviceaccount)")
    client = AzureCMKClient(rh)

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
    # Create, reuse, or recover — rebuilding a VM on a previously used hostname
    # finds the old key soft-deleted by the teardown, and the name cannot be
    # reused until it is recovered. A version that is disabled, expired, or has
    # no expiry is replaced with a fresh one rather than handed to the DES.
    set_progress(f"Azure CMK: {vm_name}: resolving key '{key_name}' "
                 f"({cfg['key_type']} {cfg['key_size']}, {cfg['key_expiration_days']}-day expiry)")
    key, key_action = client.ensure_key(
        vault_uri, key_name, cfg["key_type"], cfg["key_size"], tags,
        expiration_days=cfg["key_expiration_days"],
        # A version must outlive the window in which rotation is meant to replace it.
        min_remaining_days=cfg["key_rotation_lead_days"] if cfg["rotation_policy_body"] else 0,
    )
    set_progress(f"Azure CMK: {vm_name}: key '{key_name}': {key_action}")
    key_url = ((key or {}).get("key") or {}).get("kid")
    if not key_url:
        raise AzureCMKError(f"Key Vault did not return a key id for '{key_name}'")

    # Record the key before anything else can fail. The DES and grant below can
    # leave the key behind, and Post-Delete only removes what is recorded here.
    server.set_value_for_custom_field(CF_KEY_ID, key_url)
    server.set_value_for_custom_field(CF_APPLIED_VAULT_ID, vault_id)

    # Rotation policy: attached to the key, so a re-run re-applies it even when
    # the key itself was reused. Paired with azure_cmk_auto_key_rotation on the
    # DES, this is what keeps the VM alive past the key's expiry.
    if cfg["rotation_policy_body"]:
        set_progress(f"Azure CMK: {vm_name}: setting rotation policy on '{key_name}' "
                     f"({cfg['key_rotation_policy']}, {cfg['key_rotation_lead_days']} days before expiry)")
        client.set_key_rotation_policy(vault_uri, key_name, cfg["rotation_policy_body"])
        if not cfg["auto_key_rotation"]:
            logger.warning(
                "Azure CMK: %s: Key Vault will rotate '%s' but the DES has auto key rotation off, "
                "so disks stay on the old version and will fail when it expires", vm_name, key_name)

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

    # Persist the rest before touching disks, so a later failure can still be
    # torn down (Post-Delete reads these).
    server.set_value_for_custom_field(CF_DES_ID, des_id)
    server.set_value_for_custom_field(CF_DES_PRINCIPAL_ID, des_principal_id)
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

    message = "%s: key '%s' %s (%s), DES '%s', disks %s%s" % (
        vm_name, key_name, key_action,
        "rotation off" if cfg["key_rotation_policy"] == ROTATION_MODE_DISABLED
        else f"rotates {cfg['key_rotation_lead_days']}d before expiry",
        des_name,
        ", ".join(encrypted) if encrypted else "already encrypted",
        ", encryption at host on" if eah_already else "",
    )
    logger.info("Azure CMK applied [%s] des=%s key=%s grant=%s job=%s", vm_name, des_id, key_url, grant_ref, job.id)
    return message, warning
