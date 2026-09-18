"""
CloudBolt orchestration plugin (Post-Delete): remove the per-VM customer-
managed-key resources that the Post-Provision action (plugins/OHK-vklpnqhq)
created for each decommissioned Azure server.

Hook point: "Post-Delete" (internal post_decom — the server lifecycle, NOT the
blueprint-Resource "Post-Delete Resource"). It runs after CloudBolt's Azure
handler has destroyed the VM and its disks, which is the earliest moment the
Disk Encryption Set can be deleted: Azure refuses to delete a DES while any
disk still references it. Servers come from job.server_set.all() (this hook
passes no `server` kwarg; the Server row survives as HISTORICAL).

Per server, using ONLY the identifiers recorded on the Server at build time
(azure_cmk_des_id, azure_cmk_key_id, azure_cmk_applied_key_vault_id,
azure_cmk_grant_ref) — there is no speculative re-derive-and-delete:

  1. Delete the DES (retrying while Azure still reports it in use by disks
     whose deletion is propagating). Ownership check: the DES's active key
     must be the key CloudBolt recorded, so a repointed DES is never removed.
  2. Revoke the key-access grant the build gave the DES identity (role
     assignment or access policy). Best effort — a stale identity is harmless.
  3. Soft-delete the per-VM key. Purge is deliberately not attempted: DES
     vaults must have purge protection, so Azure purges it after the vault's
     retention window (7-90 days).

Idempotent and PROVFAILED-tolerant: nothing recorded, already gone, or an
ownership mismatch all report as skips (WARNING at most), not FAILURE.

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
    CF_GRANT_REF,
    CF_KEY_ID,
    key_url_without_version,
    parse_resource_id,
)

logger = ThreadLogger(__name__)


def run(job, *args, **kwargs):
    servers = list(job.server_set.all())
    if not servers:
        return "SUCCESS", "No servers on this job; no disk-encryption resources to remove.", ""

    removed, skipped, warnings, failures = [], [], [], []
    for server in servers:
        host = getattr(server, "hostname", "?")
        try:
            message, notes = _process_server(server)
            removed.append(message)
            warnings.extend(notes)
            set_progress(f"Azure CMK teardown: {message}")
        except AzureCMKSkip as exc:
            skipped.append(f"{host}: skipped ({exc})")
            set_progress(f"Azure CMK teardown: {host}: skipped ({exc})")
        except AzureCMKError as exc:
            failures.append(f"{host}: {exc}")
            logger.error("Azure CMK teardown failed for %s: %s", host, exc)
        except Exception as exc:  # noqa: BLE001 — surface, don't abort other servers
            failures.append(f"{host}: unexpected error: {exc}")
            logger.exception("Azure CMK teardown error for %s", host)

    summary = "; ".join(removed + skipped) or "no disk-encryption resources recorded"
    if failures:
        return "FAILURE", summary, " | ".join(failures)
    if warnings:
        return "WARNING", summary, " | ".join(warnings)
    return "SUCCESS", summary, ""


def _handler_for(server, des_id):
    """The server's Azure handler; if the FK is gone, match the DES subscription."""
    from resourcehandlers.azure_arm.models import AzureARMHandler

    rh = server.resource_handler.cast() if server.resource_handler else None
    if isinstance(rh, AzureARMHandler):
        return rh
    subscription = parse_resource_id(des_id)["subscription"]
    rh = AzureARMHandler.objects.filter(serviceaccount__iexact=subscription).first()
    if not rh:
        raise AzureCMKError(f"no Azure resource handler found for subscription {subscription}")
    return rh


def _process_server(server):
    host = getattr(server, "hostname", "?")
    des_id = (server.get_value_for_custom_field(CF_DES_ID) or "").strip()
    key_url = (server.get_value_for_custom_field(CF_KEY_ID) or "").strip()
    vault_id = (server.get_value_for_custom_field(CF_APPLIED_VAULT_ID) or "").strip()
    grant_ref = (server.get_value_for_custom_field(CF_GRANT_REF) or "").strip()
    principal_id = (server.get_value_for_custom_field(CF_DES_PRINCIPAL_ID) or "").strip()

    if not des_id and not key_url:
        raise AzureCMKSkip("no CloudBolt-managed disk-encryption resources recorded")

    client = AzureCMKClient(_handler_for(server, des_id or vault_id))
    notes = []
    parts = []

    # --- 1. Disk Encryption Set --------------------------------------------------
    if des_id:
        des = client.get_des(des_id)
        if des is None:
            parts.append("DES already gone")
        else:
            active = (((des.get("properties") or {}).get("activeKey") or {}).get("keyUrl") or "")
            same_key = key_url_without_version(active).lower() == key_url_without_version(key_url).lower()
            if key_url and active and not same_key:
                notes.append(f"{host}: DES {des_id.rsplit('/', 1)[-1]} now uses a different key; left in place")
                parts.append("DES left in place (key mismatch)")
            else:
                set_progress(f"Azure CMK teardown: {host}: deleting DES {des_id.rsplit('/', 1)[-1]}")
                client.delete_des(des_id)
                parts.append("DES deleted")

    # --- 2. Revoke the key-access grant (best effort; skipped only when the DES
    #        was deliberately left in place because it now uses another key) -----
    des_gone = not des_id or (parts and parts[-1] in ("DES deleted", "DES already gone"))
    if grant_ref and vault_id and des_gone:
        try:
            tenant_id = client.tenant_id
            if grant_ref.startswith("accessPolicy:"):
                tenant_id = ((client.get_vault(vault_id).get("properties") or {}).get("tenantId")) or tenant_id
            parts.append(client.revoke_grant(grant_ref, vault_id, tenant_id))
        except AzureCMKError as exc:
            notes.append(f"{host}: could not revoke key-access grant: {exc}")

    # --- 3. Soft-delete the per-VM key ----------------------------------------------
    if key_url and vault_id:
        key_name = key_url_without_version(key_url).rstrip("/").rsplit("/", 1)[-1]
        try:
            vault_uri = (client.get_vault(vault_id).get("properties") or {}).get("vaultUri")
            if not vault_uri:
                raise AzureCMKError("vault has no vaultUri")
            set_progress(f"Azure CMK teardown: {host}: deleting key '{key_name}'")
            if client.delete_key(vault_uri, key_name):
                parts.append(f"key '{key_name}' soft-deleted")
            else:
                parts.append(f"key '{key_name}' already gone")
        except AzureCMKError as exc:
            notes.append(f"{host}: key '{key_name}' not deleted: {exc}")

    logger.info("Azure CMK teardown [%s] des=%s key=%s principal=%s result=%s", host, des_id, key_url, principal_id, parts)
    return "%s: %s" % (host, ", ".join(parts) if parts else "nothing to remove"), notes
