"""
azure_disk_encryption — shared helpers for per-VM customer-managed-key (CMK)
encryption of Azure managed disks from CloudBolt orchestration actions.

One Key Vault key + one Disk Encryption Set (DES) per VM, applied to the VM's
OS and data disks (server-side encryption with CMK), optional encryption at
host, and the matching teardown. Used by:

  - plugins/OHK-vklpnqhq  (Post-Provision: create key + DES, encrypt disks)
  - plugins/OHK-2vpg4pff  (Post-Delete: delete DES, revoke grant, delete key)

Why raw REST (requests) and not the Azure SDKs: the CloudBolt appliance venv
ships azure-mgmt-compute/network/storage/resource, but azure-keyvault-keys,
azure-mgmt-keyvault and azure-mgmt-authorization are not confirmed present.
Every call here is anchored to Microsoft's current REST reference (URL cited
at each call site) per docs/agents/external-apis.md, and authenticates with
the Resource Handler's own service principal (client_id / secret /
azure_tenant_id) — the same pattern as the azure_pricing shared module.

Azure facts this module relies on (all from the cited docs):
  - A DES cannot be attached to disks at VM-create time by CloudBolt's Azure
    handler (TechnologyWrapper.create_node has no DES parameter), so CMK is
    applied post-create: disks "must not be attached to a running VM" to be
    re-encrypted, hence deallocate -> PATCH disks -> start.
  - The DES key URL must be the *versioned* kid "regardless of
    rotationToLatestKeyVersionEnabled".
  - Key Vault must have soft delete AND purge protection enabled ("mandatory
    when using a Key Vault for encrypting managed disks"), and must be in the
    same region as the DES (a different subscription is allowed).
  - The DES system-assigned identity must be granted key get/wrapKey/unwrapKey:
    an access policy on policy-mode vaults, or the "Key Vault Crypto Service
    Encryption User" role (e147488a-f6f5-4113-8e2d-b22465e65bf6) on RBAC vaults.
  - Encryption at host needs the Microsoft.Compute/EncryptionAtHost feature
    registered on the subscription and a deallocated VM.
"""
import re
import time
import uuid

import requests

from infrastructure.models import CustomField
from utilities.logger import ThreadLogger

logger = ThreadLogger(__name__)

REQUEST_TIMEOUT = 60

# --- API versions (pinned to the docs consulted on 2026-09-15) ----------------
DISK_API = "2024-03-02"       # Microsoft.Compute disks + diskEncryptionSets
VM_API = "2024-07-01"         # Microsoft.Compute virtualMachines
KV_MGMT_API = "2024-11-01"    # Microsoft.KeyVault vaults (ARM control plane)
KV_DATA_API = "7.4"           # Key Vault keys data plane
AUTHZ_API = "2022-04-01"      # Microsoft.Authorization roleAssignments

# --- Endpoints per Azure cloud, keyed by AzureARMHandler.cloud_environment -----
_CLOUDS = {
    "PUBLIC": {
        "login": "https://login.microsoftonline.com",
        "arm": "https://management.azure.com",
        "vault": "https://vault.azure.net",
    },
    "US_GOV": {
        "login": "https://login.microsoftonline.us",
        "arm": "https://management.usgovcloudapi.net",
        "vault": "https://vault.usgovcloudapi.net",
    },
    "CHINA": {
        "login": "https://login.chinacloudapi.cn",
        "arm": "https://management.chinacloudapi.cn",
        "vault": "https://vault.azure.cn",
    },
    "GERMAN": {
        "login": "https://login.microsoftonline.de",
        "arm": "https://management.microsoftazure.de",
        "vault": "https://vault.microsoftazure.de",
    },
}

# Built-in role: Key Vault Crypto Service Encryption User (keys/read, wrap, unwrap).
# https://learn.microsoft.com/en-us/azure/role-based-access-control/built-in-roles/security#key-vault-crypto-service-encryption-user
ROLE_KV_CRYPTO_SERVICE_ENCRYPTION_USER = "e147488a-f6f5-4113-8e2d-b22465e65bf6"

# Access-policy key permissions for the DES identity (CLI doc: "--key-permissions wrapkey unwrapkey get").
DES_KEY_PERMISSIONS = ["get", "wrapKey", "unwrapKey"]

ENCRYPTION_TYPES = ("EncryptionAtRestWithCustomerKey", "EncryptionAtRestWithPlatformAndCustomerKeys")
KEY_TYPES = ("RSA", "RSA-HSM")
KEY_SIZES = (2048, 3072, 4096)

# Every per-VM key is created with an expiry (KeyAttributes.exp). Policy here is
# "strictly less than 90 days", so the accepted range is 1..89 and the shipped
# default is the longest life that still satisfies it.
KEY_EXPIRATION_DAYS_MAX = 90    # exclusive upper bound
KEY_EXPIRATION_DAYS_DEFAULT = 89

# Key Vault rotation policy. Azure allows exactly one Rotate action, triggered
# either a fixed time after a version is created or a fixed time before it
# expires; "Disabled" means this action writes no policy at all.
# Docs: https://learn.microsoft.com/en-us/azure/key-vault/keys/how-to-configure-key-rotation#key-rotation-policy
ROTATION_MODE_DISABLED = "Disabled"
ROTATION_MODE_BEFORE_EXPIRY = "TimeBeforeExpiry"
ROTATION_MODE_AFTER_CREATE = "TimeAfterCreate"
ROTATION_MODES = (ROTATION_MODE_DISABLED, ROTATION_MODE_BEFORE_EXPIRY, ROTATION_MODE_AFTER_CREATE)

# Azure minimums, both quoted from the docs:
#   "Rotation time: key rotation interval. The minimum value is seven days from
#    creation and seven days from expiration time."
#   expiryTime "should be at least 28 days".
ROTATION_MIN_DAYS = 7
ROTATION_MIN_EXPIRY_DAYS = 28
ROTATION_LEAD_DAYS_DEFAULT = 7

# How long to keep retrying steps that depend on Entra/RBAC propagation.
GRANT_PROPAGATION_TIMEOUT_SECS = 600
GRANT_PROPAGATION_POLL_SECS = 20
KEY_RECOVER_TIMEOUT_SECS = 300
KEY_RECOVER_POLL_SECS = 10
DES_INUSE_RETRY_TIMEOUT_SECS = 300
DES_INUSE_POLL_SECS = 15
LRO_TIMEOUT_SECS = 1800

# --- Custom fields ---------------------------------------------------------------
# Input (also honoured as a per-server override so an Environment/Group parameter
# can point different servers at different vaults).
CF_KEY_VAULT_ID = "azure_cmk_key_vault_id"
# Traceability written on the Server by the build plugin; the teardown plugin
# treats these as the sole authority for what it may delete.
CF_DES_ID = "azure_cmk_des_id"
CF_DES_PRINCIPAL_ID = "azure_cmk_des_principal_id"
CF_KEY_ID = "azure_cmk_key_id"
CF_APPLIED_VAULT_ID = "azure_cmk_applied_key_vault_id"
CF_ENCRYPTED_DISKS = "azure_cmk_encrypted_disks"
CF_GRANT_REF = "azure_cmk_grant_ref"
CF_EAH_APPLIED = "azure_cmk_encryption_at_host_applied"

# grant_ref marker for DESes that use a shared user-assigned identity (nothing to grant/revoke per DES).
GRANT_REF_UAMI_PREFIX = "userAssignedIdentity:"

_CF_DEFS = [
    (CF_KEY_VAULT_ID, "Azure CMK Key Vault (Resource ID)", "STR",
     "ARM resource ID of the Key Vault that holds per-VM disk-encryption keys. "
     "This parameter is what switches per-VM CMK encryption on: set it on an "
     "Environment, Group, blueprint or server to encrypt servers there, and leave "
     "it unset everywhere else. Servers with no value are skipped."),
    (CF_DES_ID, "Azure CMK Disk Encryption Set (Resource ID)", "STR",
     "Per-VM Disk Encryption Set created by CloudBolt for this server."),
    (CF_DES_PRINCIPAL_ID, "Azure CMK DES Identity (Principal ID)", "STR",
     "Object ID of the DES system-assigned managed identity."),
    (CF_KEY_ID, "Azure CMK Key (Versioned Key URL)", "STR",
     "Key Vault key backing this server's Disk Encryption Set."),
    (CF_APPLIED_VAULT_ID, "Azure CMK Key Vault Applied (Resource ID)", "STR",
     "Key Vault the per-VM key was created in."),
    (CF_ENCRYPTED_DISKS, "Azure CMK Encrypted Disks", "TXT",
     "Comma-separated managed-disk names re-encrypted with the per-VM DES."),
    (CF_GRANT_REF, "Azure CMK Key Access Grant", "STR",
     "Role-assignment resource ID (RBAC vaults), accessPolicy:<objectId> (policy vaults), or "
     "userAssignedIdentity:<resourceId> when the DES uses a pre-authorised user-assigned identity."),
    (CF_EAH_APPLIED, "Azure CMK Encryption At Host Applied", "BOOL",
     "True when encryption at host was enabled on the VM by CloudBolt."),
]


def ensure_custom_fields():
    """Idempotently create every custom field this feature reads or writes."""
    for name, label, cf_type, description in _CF_DEFS:
        CustomField.objects.get_or_create(
            name=name,
            defaults=dict(label=label, description=description, type=cf_type, show_on_servers=True),
        )


class AzureCMKError(Exception):
    """A hard failure for one server (surfaced as FAILURE by the caller)."""


class AzureCMKKeyDeleted(AzureCMKError):
    """
    The key name is held by a soft-deleted key, so create-key returns 409 and the
    name cannot be reused until the key is recovered or purged. DES vaults must
    have purge protection on, which blocks purging for the whole retention
    window, so recovery is the only way forward.
    """


class AzureCMKSkip(Exception):
    """A benign reason to skip one server (surfaced as a skip, not a failure)."""


# =============================================================================
# Small helpers
# =============================================================================
def parse_resource_id(resource_id):
    """
    Split an ARM resource ID into its parts. Index layout per
    docs/agents/common-patterns.md (Parsing Azure Resource IDs):
    /subscriptions/{sub}/resourceGroups/{rg}/providers/{ns}/{type}/{name}
    """
    parts = (resource_id or "").strip("/").split("/")
    if len(parts) < 8 or parts[0].lower() != "subscriptions" or parts[2].lower() != "resourcegroups":
        raise AzureCMKError(f"'{resource_id}' is not a full ARM resource ID")
    return {
        "subscription": parts[1],
        "resource_group": parts[3],
        "namespace": parts[5],
        "type": parts[6],
        "name": parts[7],
    }


def validate_user_assigned_identity_id(uami_id):
    """A user-assigned managed identity ARM ID: .../providers/Microsoft.ManagedIdentity/userAssignedIdentities/{name}."""
    parts = parse_resource_id(uami_id)
    if parts["namespace"].lower() != "microsoft.managedidentity" or parts["type"].lower() != "userassignedidentities":
        raise AzureCMKError(f"'{uami_id}' is not a Microsoft.ManagedIdentity/userAssignedIdentities resource ID")
    return uami_id


def user_assigned_principal_id(des, uami_id):
    """principalId of `uami_id` from a DES body's identity.userAssignedIdentities (case-insensitive key match)."""
    identities = (des.get("identity") or {}).get("userAssignedIdentities") or {}
    for rid, info in identities.items():
        if rid.lower() == uami_id.lower():
            return (info or {}).get("principalId") or ""
    return ""


def sanitize_name(base, allowed=r"[^0-9A-Za-z-]", max_len=80):
    """Replace characters Azure rejects with '-' and trim (DES: a-z A-Z 0-9 _ -; key: 0-9 a-z A-Z -)."""
    cleaned = re.sub(allowed, "-", base or "").strip("-")
    return cleaned[:max_len].rstrip("-") or "cloudbolt"


def key_expiry_epoch(expiration_days, now=None):
    """
    Convert a lifetime in days into the Unix-epoch seconds Key Vault wants in
    KeyAttributes.exp ("Expiry date in UTC", integer unixtime).
    Docs: https://learn.microsoft.com/en-us/rest/api/keyvault/keys/create-key/create-key#keyattributes

    Rejects anything outside 1..KEY_EXPIRATION_DAYS_MAX-1 so a misconfigured
    input can never mint a key that outlives the policy window.
    """
    try:
        days = int(expiration_days)
    except (TypeError, ValueError):
        raise AzureCMKError(
            f"key expiration days must be a whole number of days (got '{expiration_days}')"
        )
    if days < 1 or days >= KEY_EXPIRATION_DAYS_MAX:
        raise AzureCMKError(
            f"key expiration days must be between 1 and {KEY_EXPIRATION_DAYS_MAX - 1} "
            f"(policy: less than {KEY_EXPIRATION_DAYS_MAX} days); got {days}"
        )
    return int((now if now is not None else time.time()) + days * 86400)


def key_is_usable(key, min_remaining_days=0, now=None):
    """
    Return (usable, reason) for a KeyBundle. A key version can only wrap/unwrap
    while it is enabled and unexpired, and Azure shuts a VM down when the key
    behind its disks is disabled or expired, so anything that fails here must be
    replaced with a fresh version rather than handed to a DES.

    A version with no expiry at all counts as unusable: this action's whole
    premise is that keys expire inside the policy window, and keys created
    before that rule (or recovered from an older build) carry none.
    """
    attrs = (key or {}).get("attributes") or {}
    if attrs.get("enabled") is False:
        return False, "disabled"
    exp = attrs.get("exp")
    if not exp:
        return False, "no expiry set"
    remaining = (int(exp) - (now if now is not None else time.time())) / 86400.0
    if remaining <= min_remaining_days:
        return False, ("already expired" if remaining <= 0
                       else f"expires in {remaining:.1f} days, inside the rotation window")
    return True, f"expires in {int(remaining)} days"


def iso8601_days(days):
    """ISO 8601 duration for a whole number of days, e.g. 82 -> "P82D"."""
    return f"P{int(days)}D"


def plan_rotation_policy(mode, lead_days, expiration_days):
    """
    Turn (mode, lead_days, expiration_days) into the Update Key Rotation Policy
    body, or None when rotation is disabled. Both triggers describe the same
    schedule -- rotate `lead_days` before the version expires -- so the policy
    always follows the key's expiry rather than drifting from it.

    Azure constraints enforced here rather than letting Key Vault 400:
      - policy expiryTime must be at least ROTATION_MIN_EXPIRY_DAYS;
      - the rotate trigger must be at least ROTATION_MIN_DAYS from both
        creation and expiry, so lead_days and (expiration_days - lead_days)
        both have to clear that floor.
    Docs: https://learn.microsoft.com/en-us/azure/key-vault/keys/how-to-configure-key-rotation#key-rotation-policy
    """
    if mode == ROTATION_MODE_DISABLED:
        return None
    if mode not in ROTATION_MODES:
        raise AzureCMKError(
            f"unsupported key rotation policy '{mode}' (use one of {', '.join(ROTATION_MODES)})"
        )
    try:
        lead = int(lead_days)
    except (TypeError, ValueError):
        raise AzureCMKError(f"key rotation lead days must be a whole number (got '{lead_days}')")
    expiry = int(expiration_days)

    if expiry < ROTATION_MIN_EXPIRY_DAYS:
        raise AzureCMKError(
            f"a rotation policy needs a key expiry of at least {ROTATION_MIN_EXPIRY_DAYS} days "
            f"(Azure minimum for the policy expiryTime); got {expiry}. Raise the expiration days "
            f"or set the rotation policy to {ROTATION_MODE_DISABLED}."
        )
    if lead < ROTATION_MIN_DAYS or (expiry - lead) < ROTATION_MIN_DAYS:
        raise AzureCMKError(
            f"key rotation lead days must be between {ROTATION_MIN_DAYS} and "
            f"{expiry - ROTATION_MIN_DAYS} for a {expiry}-day key (Azure requires the rotate "
            f"trigger to be at least {ROTATION_MIN_DAYS} days from both creation and expiry); got {lead}"
        )

    if mode == ROTATION_MODE_BEFORE_EXPIRY:
        trigger = {"timeBeforeExpiry": iso8601_days(lead)}
    else:
        trigger = {"timeAfterCreate": iso8601_days(expiry - lead)}
    return {
        "lifetimeActions": [{"trigger": trigger, "action": {"type": "Rotate"}}],
        "attributes": {"expiryTime": iso8601_days(expiry)},
    }


def key_url_without_version(key_url):
    """https://v.vault.azure.net/keys/name/version -> https://v.vault.azure.net/keys/name"""
    parts = (key_url or "").rstrip("/").split("/")
    if len(parts) >= 6 and parts[-3] == "keys":
        return "/".join(parts[:-1])
    return key_url


def _never_sent(exc, _depth=12):
    """
    True when a transport failure happened before anything was written to the
    socket (DNS failure, TCP connect refused/unreachable, connect timeout), so
    the call provably never reached Azure and is safe to repeat even when the
    operation is not idempotent. Walks the cause chain because requests wraps
    urllib3, which wraps the OSError.
    """
    if isinstance(exc, requests.exceptions.ConnectTimeout):
        return True
    seen = exc
    while seen is not None and _depth > 0:
        if type(seen).__name__ in ("NewConnectionError", "NameResolutionError", "ConnectTimeoutError"):
            return True
        # requests raises ConnectionError(urllib3_error), so the cause is in args[0]
        # as well as in __cause__/__context__; follow whichever is populated.
        nxt = seen.__cause__ or seen.__context__
        if nxt is None:
            args = getattr(seen, "args", ())
            nxt = args[0] if args and isinstance(args[0], BaseException) else None
        seen = nxt
        _depth -= 1
    return False


def _err_text(resp):
    try:
        body = resp.json()
    except Exception:
        body = None
    if isinstance(body, dict):
        err = body.get("error") or body
        code = err.get("code", "")
        msg = err.get("message", "")
        return f"{resp.status_code} {code}: {msg}".strip()
    text = (resp.text or "").replace("\n", " ")
    return f"{resp.status_code}: {text[:500]}"


def _err_code(resp):
    try:
        err = (resp.json() or {}).get("error") or {}
        return str(err.get("code", ""))
    except Exception:
        return ""


def server_azure_coords(server):
    """
    Return (rh, subscription_id, resource_group, vm_name) for a CloudBolt Azure
    server. Confirmed against platform source: AzureARMServerInfo.resource_group
    holds the RG; the VM's Azure name equals server.hostname (azure_object calls
    get_vm_object(resource_group_name=self.resource_group, vm_name=self.server.hostname));
    the subscription id is the handler's inherited `serviceaccount`.
    """
    from resourcehandlers.azure_arm.models import AzureARMHandler

    rh = server.resource_handler.cast() if server.resource_handler else None
    if not isinstance(rh, AzureARMHandler):
        raise AzureCMKSkip("not an Azure VM (no AzureARMHandler)")
    info = getattr(server, "azurearmserverinfo", None)
    resource_group = getattr(info, "resource_group", None)
    if not resource_group or not server.hostname:
        raise AzureCMKError("could not resolve the Azure resource group / VM name for this server")
    return rh, getattr(rh, "serviceaccount", None), resource_group, server.hostname


# =============================================================================
# REST client bound to one Resource Handler's service principal
# =============================================================================
class AzureCMKClient:
    def __init__(self, rh):
        self.rh = rh
        cloud = getattr(rh, "cloud_environment", "PUBLIC") or "PUBLIC"
        self.endpoints = _CLOUDS.get(cloud, _CLOUDS["PUBLIC"])
        self.arm_base = self.endpoints["arm"]
        self.subscription_id = getattr(rh, "serviceaccount", None)
        self.tenant_id = getattr(rh, "azure_tenant_id", None)
        self._tokens = {}

    # --- auth ---------------------------------------------------------------
    def token(self, audience):
        """
        Client-credentials bearer token for `audience` (ARM base or Key Vault
        resource). Mirrors the repo's existing raw-REST Azure auth (v1 endpoint,
        `resource=`), see shared_modules/SHM-6gtujb8t. Falls back to the handler
        wrapper's SDK credential when the handler is not secret-based.
        OAuth doc: https://learn.microsoft.com/en-us/entra/identity-platform/v2-oauth2-client-creds-grant-flow
        """
        cached = self._tokens.get(audience)
        if cached and cached[1] - 60 > time.time():
            return cached[0]
        client_id = getattr(self.rh, "client_id", None)
        secret = getattr(self.rh, "secret", None)
        token, expires_in = None, 3000
        if client_id and secret and self.tenant_id:
            # ARM expects the trailing-slash resource; Key Vault expects the bare host.
            resource = audience if audience == self.endpoints["vault"] else audience.rstrip("/") + "/"
            data = {
                "grant_type": "client_credentials",
                "client_id": client_id,
                "client_secret": secret,
                "resource": resource,
            }
            login_url = f"{self.endpoints['login']}/{self.tenant_id}/oauth2/token"
            resp, delay = None, 5
            for attempt in range(3):
                try:
                    resp = requests.post(login_url, data=data, timeout=REQUEST_TIMEOUT)
                    break
                except requests.exceptions.RequestException as exc:
                    # Fetching a token has no side effects, so it is always safe to repeat.
                    if attempt == 2:
                        raise AzureCMKError(f"could not reach {login_url.split('/oauth2')[0]}: {exc}")
                    logger.warning("Azure token request failed (%s); retrying in %ss", exc, delay)
                    time.sleep(delay)
                    delay *= 2
            if resp.status_code != 200:
                raise AzureCMKError(f"token request for {audience} failed: {_err_text(resp)}")
            body = resp.json()
            token = body["access_token"]
            expires_in = int(body.get("expires_in", expires_in))
        else:
            # Handler configured without a client secret (e.g. managed identity):
            # use the SDK credential CloudBolt already built for this handler.
            try:
                cred = self.rh.get_api_wrapper().credentials
                access = cred.get_token(f"{audience.rstrip('/')}/.default")
                token, expires_in = access.token, max(60, int(access.expires_on - time.time()))
            except Exception as exc:  # noqa: BLE001
                raise AzureCMKError(f"could not obtain a token for {audience} from the handler credential: {exc}")
        self._tokens[audience] = (token, time.time() + expires_in)
        return token

    # --- generic request with retry on throttling / transient 5xx / network --
    def request(self, method, url, audience, body=None, params=None, ok=(200, 201, 202, 204),
                retries=4, idempotent=True):
        """
        `idempotent` says whether repeating this call is harmless. Every ARM call
        here is a GET or a PUT-by-name, so the default is True; Key Vault's
        create-key is a POST that mints a new version each time, so it passes
        False and is only repeated when the request provably never went out.
        """
        delay = 5
        resp = None
        for attempt in range(retries + 1):
            headers = {"Authorization": f"Bearer {self.token(audience)}", "Content-Type": "application/json"}
            try:
                resp = requests.request(method, url, headers=headers, json=body, params=params,
                                        timeout=REQUEST_TIMEOUT)
            except requests.exceptions.RequestException as exc:
                # A blip on the appliance's link to Azure (the socket never opened,
                # DNS failed, the connection dropped mid-flight) would otherwise fail
                # the whole server on one packet's worth of bad luck.
                if attempt < retries and (idempotent or _never_sent(exc)):
                    logger.warning("Azure %s %s: %s; retrying in %ss", method, url, exc, delay)
                    time.sleep(delay)
                    delay = min(delay * 2, 60)
                    continue
                raise AzureCMKError(
                    f"{method} {url.split('?')[0]} could not reach Azure after "
                    f"{attempt + 1} attempt(s): {exc}"
                )
            if resp.status_code in ok:
                return resp
            if resp.status_code in (429, 500, 502, 503, 504) and attempt < retries:
                wait = int(resp.headers.get("Retry-After") or delay)
                logger.warning("Azure %s %s -> %s; retrying in %ss", method, url, resp.status_code, wait)
                time.sleep(wait)
                delay = min(delay * 2, 60)
                continue
            return resp
        return resp

    def arm(self, method, path, api_version, body=None, ok=(200, 201, 202, 204), params=None):
        url = path if path.startswith("http") else f"{self.arm_base}{path}"
        params = dict(params or {})
        params["api-version"] = api_version
        return self.request(method, url, self.arm_base, body=body, params=params, ok=ok)

    def _json(self, resp):
        try:
            return resp.json() if resp.content else {}
        except Exception:
            return {}

    # --- long-running operations ------------------------------------------
    def wait_lro(self, resp, timeout=LRO_TIMEOUT_SECS):
        """
        Follow an ARM async operation to completion. 202/201 responses carry
        Azure-AsyncOperation (JSON with `status`) and/or Location (poll until
        not 202) headers per the ARM async conventions:
        https://learn.microsoft.com/en-us/azure/azure-resource-manager/management/async-operations
        Returns the final JSON body (may be empty).
        """
        if resp.status_code not in (201, 202):
            return self._json(resp)
        async_url = resp.headers.get("Azure-AsyncOperation")
        location = resp.headers.get("Location")
        deadline = time.time() + timeout
        while time.time() < deadline:
            wait = int(resp.headers.get("Retry-After") or 10)
            time.sleep(min(max(wait, 5), 60))
            if async_url:
                poll = self.request("GET", async_url, self.arm_base)
                if poll.status_code not in (200, 202):
                    raise AzureCMKError(f"async operation poll failed: {_err_text(poll)}")
                status = str(self._json(poll).get("status", "")).lower()
                if status == "succeeded":
                    if location:
                        final = self.request("GET", location, self.arm_base)
                        return self._json(final) if final.status_code == 200 else {}
                    return self._json(poll)
                if status in ("failed", "canceled", "cancelled"):
                    raise AzureCMKError(f"async operation {status}: {self._json(poll).get('error')}")
                resp = poll
                continue
            if location:
                poll = self.request("GET", location, self.arm_base)
                if poll.status_code == 202:
                    resp = poll
                    continue
                if poll.status_code in (200, 201, 204):
                    return self._json(poll)
                raise AzureCMKError(f"async operation failed: {_err_text(poll)}")
            return self._json(resp)  # no polling headers: treat as complete
        raise AzureCMKError("timed out waiting for an Azure operation to complete")

    def wait_provisioning(self, resource_id, api_version, timeout=LRO_TIMEOUT_SECS):
        """Poll a resource until properties.provisioningState is Succeeded (or fails)."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            resp = self.arm("GET", resource_id, api_version)
            if resp.status_code != 200:
                raise AzureCMKError(f"GET {resource_id} failed: {_err_text(resp)}")
            body = self._json(resp)
            state = str((body.get("properties") or {}).get("provisioningState", "")).lower()
            if state in ("", "succeeded"):
                return body
            if state in ("failed", "canceled"):
                raise AzureCMKError(f"{resource_id} provisioning {state}")
            time.sleep(10)
        raise AzureCMKError(f"timed out waiting for {resource_id} to finish provisioning")

    # =========================================================================
    # Key Vault — control plane
    # =========================================================================
    def get_vault(self, vault_id):
        """
        GET the vault resource. Fields used: properties.vaultUri,
        enableRbacAuthorization, enableSoftDelete, enablePurgeProtection, tenantId, location.
        Docs: https://learn.microsoft.com/en-us/rest/api/keyvault/keyvault/vaults/get
        """
        parts = parse_resource_id(vault_id)
        if parts["namespace"].lower() != "microsoft.keyvault" or parts["type"].lower() != "vaults":
            raise AzureCMKError(
                f"'{vault_id}' is not a Microsoft.KeyVault/vaults resource (Managed HSM is not supported by this action)"
            )
        resp = self.arm("GET", vault_id, KV_MGMT_API)
        if resp.status_code != 200:
            raise AzureCMKError(f"Key Vault lookup failed for {vault_id}: {_err_text(resp)}")
        return self._json(resp)

    def validate_vault_for_des(self, vault, des_location):
        """Enforce Azure's mandatory vault settings for DES use (see module docstring)."""
        props = vault.get("properties") or {}
        problems = []
        if not props.get("enableSoftDelete", True):
            problems.append("soft delete is disabled")
        if not props.get("enablePurgeProtection"):
            problems.append("purge protection is disabled")
        vault_loc = (vault.get("location") or "").replace(" ", "").lower()
        if vault_loc and des_location and vault_loc != des_location.replace(" ", "").lower():
            problems.append(f"vault region '{vault.get('location')}' differs from the VM region '{des_location}'")
        if problems:
            raise AzureCMKError(
                "Key Vault '%s' cannot back a Disk Encryption Set: %s. Azure requires soft delete + purge "
                "protection and the same region as the DES." % (vault.get("name"), "; ".join(problems))
            )

    # =========================================================================
    # Key Vault — data plane (keys)
    # =========================================================================
    def _vault_audience(self):
        return self.endpoints["vault"]

    def get_key(self, vault_uri, key_name):
        """
        GET a key's current version (None when absent). Requires keys/get.
        Docs: https://learn.microsoft.com/en-us/rest/api/keyvault/keys/get-key/get-key
        """
        url = f"{vault_uri.rstrip('/')}/keys/{key_name}"
        resp = self.request("GET", url, self._vault_audience(), params={"api-version": KV_DATA_API}, ok=(200, 404))
        if resp.status_code == 404:
            return None
        if resp.status_code != 200:
            raise AzureCMKError(f"Key Vault get-key '{key_name}' failed: {_err_text(resp)}")
        return self._json(resp)

    def create_key(self, vault_uri, key_name, key_type, key_size, tags=None,
                   expiration_days=KEY_EXPIRATION_DAYS_DEFAULT):
        """
        POST {vaultBaseUrl}/keys/{key-name}/create — RSA/RSA-HSM, wrap/unwrap ops.
        Requires keys/create. Returns the KeyBundle (key.kid is the versioned URL).
        `expiration_days` becomes KeyAttributes.exp (integer unixtime, UTC).
        Docs: https://learn.microsoft.com/en-us/rest/api/keyvault/keys/create-key/create-key
        """
        if key_type not in KEY_TYPES:
            raise AzureCMKError(f"unsupported key type '{key_type}' (use one of {', '.join(KEY_TYPES)})")
        if int(key_size) not in KEY_SIZES:
            raise AzureCMKError(f"unsupported RSA key size {key_size} (Azure Disk Storage supports 2048/3072/4096)")
        url = f"{vault_uri.rstrip('/')}/keys/{key_name}/create"
        body = {
            "kty": key_type,
            "key_size": int(key_size),
            "key_ops": ["wrapKey", "unwrapKey"],
            "attributes": {"enabled": True, "exp": key_expiry_epoch(expiration_days)},
            "tags": tags or {},
        }
        # idempotent=False: a repeated POST mints an extra key version.
        resp = self.request("POST", url, self._vault_audience(), body=body,
                            params={"api-version": KV_DATA_API}, ok=(200,), idempotent=False)
        if resp.status_code == 409:
            # "Key <name> is currently in a deleted but recoverable state, and its
            # name cannot be reused" -- ensure_key() handles this by recovering.
            raise AzureCMKKeyDeleted(f"key '{key_name}' is soft-deleted: {_err_text(resp)}")
        if resp.status_code != 200:
            raise AzureCMKError(f"Key Vault create-key '{key_name}' failed: {_err_text(resp)}")
        return self._json(resp)

    def get_deleted_key(self, vault_uri, key_name):
        """
        GET {vaultBaseUrl}/deletedkeys/{key-name} — the soft-deleted key, or None
        when the name is genuinely free. Requires keys/get.
        Docs: https://learn.microsoft.com/en-us/rest/api/keyvault/keys/get-deleted-key/get-deleted-key
        """
        url = f"{vault_uri.rstrip('/')}/deletedkeys/{key_name}"
        resp = self.request("GET", url, self._vault_audience(), params={"api-version": KV_DATA_API},
                            ok=(200, 404))
        if resp.status_code == 404:
            return None
        if resp.status_code != 200:
            raise AzureCMKError(f"Key Vault get-deleted-key '{key_name}' failed: {_err_text(resp)}")
        return self._json(resp)

    def recover_deleted_key(self, vault_uri, key_name, timeout=KEY_RECOVER_TIMEOUT_SECS):
        """
        POST {vaultBaseUrl}/deletedkeys/{key-name}/recover — "recovers the deleted key
        back to its latest version under /keys". Requires keys/recover (covered by the
        Key Vault Crypto Officer role's keys/* on RBAC vaults; access-policy vaults need
        the Recover key permission). Recovery is not instantaneous -- every Azure SDK
        models it as a long-running operation -- so poll GET /keys/{name} until the key
        is retrievable again and return that bundle.
        Docs: https://learn.microsoft.com/en-us/rest/api/keyvault/keys/recover-deleted-key/recover-deleted-key
        """
        url = f"{vault_uri.rstrip('/')}/deletedkeys/{key_name}/recover"
        resp = self.request("POST", url, self._vault_audience(), params={"api-version": KV_DATA_API},
                            ok=(200,))
        if resp.status_code != 200:
            raise AzureCMKError(f"Key Vault recover-key '{key_name}' failed: {_err_text(resp)}")
        deadline = time.time() + timeout
        while time.time() < deadline:
            key = self.get_key(vault_uri, key_name)
            if key:
                return key
            time.sleep(KEY_RECOVER_POLL_SECS)
        raise AzureCMKError(
            f"key '{key_name}' was recovered but did not become retrievable within {timeout}s")

    def ensure_key(self, vault_uri, key_name, key_type, key_size, tags=None,
                   expiration_days=KEY_EXPIRATION_DAYS_DEFAULT, min_remaining_days=0):
        """
        Return (KeyBundle, description) for a key that is safe to hand to a DES,
        whatever state the name is in:

          - live and usable                 -> reuse it as-is
          - live but disabled/expired/no exp -> add a fresh version under the same name
          - soft-deleted                     -> recover, then apply the same test
          - absent                           -> create it

        Rebuilding a VM with a hostname that was used before is the common path
        here: the teardown soft-deletes the key, so the name is taken but GET
        /keys returns 404 and a plain create-key would 409. Purge protection is
        mandatory on DES vaults, so the name cannot be freed -- recovery is the
        only route, and the caller re-applies the rotation policy afterwards
        either way (the policy lives on the key, not the version).
        """
        def _new_version(prefix, why):
            key = self.create_key(vault_uri, key_name, key_type, key_size, tags, expiration_days)
            return key, f"{prefix}new version ({why})"

        key = self.get_key(vault_uri, key_name)
        recovered = False
        if key is None and self.get_deleted_key(vault_uri, key_name) is not None:
            logger.info("Azure CMK: key '%s' is soft-deleted; recovering it", key_name)
            key = self.recover_deleted_key(vault_uri, key_name)
            recovered = True
        if key is None:
            try:
                return self.create_key(vault_uri, key_name, key_type, key_size, tags, expiration_days), "created"
            except AzureCMKKeyDeleted:
                # Deleted between our check and the create; recover and try once more.
                logger.warning("Azure CMK: key '%s' was deleted mid-flight; recovering it", key_name)
                key = self.recover_deleted_key(vault_uri, key_name)
                recovered = True

        prefix = "recovered, " if recovered else ""
        usable, why = key_is_usable(key, min_remaining_days)
        if usable:
            return key, f"{prefix}reused ({why})" if recovered else f"reused ({why})"
        return _new_version(prefix, why)

    def set_key_rotation_policy(self, vault_uri, key_name, policy):
        """
        PUT {vaultBaseUrl}/keys/{key-name}/rotationpolicy — schedules Key Vault to
        create new versions of the key on its own. The policy lives on the key
        (not on a version), so re-PUTting it is how a re-run brings an existing
        key back in line. Requires keys/update (role Key Vault Crypto Officer; on
        access-policy vaults, key permissions Rotate + Set/Get Rotation Policy).
        Docs: https://learn.microsoft.com/en-us/rest/api/keyvault/keys/update-key-rotation-policy/update-key-rotation-policy
        """
        url = f"{vault_uri.rstrip('/')}/keys/{key_name}/rotationpolicy"
        resp = self.request("PUT", url, self._vault_audience(), body=policy,
                            params={"api-version": KV_DATA_API}, ok=(200,))
        if resp.status_code != 200:
            raise AzureCMKError(f"Key Vault set rotation policy '{key_name}' failed: {_err_text(resp)}")
        return self._json(resp)

    def delete_key(self, vault_uri, key_name):
        """
        DELETE {vaultBaseUrl}/keys/{key-name} — soft-deletes the key (recoverable for
        the vault's retention window; purge is blocked by purge protection, which
        DES vaults must have). Requires keys/delete. Returns True if deleted, False if absent.
        Docs: https://learn.microsoft.com/en-us/rest/api/keyvault/keys/delete-key/delete-key
        """
        url = f"{vault_uri.rstrip('/')}/keys/{key_name}"
        resp = self.request("DELETE", url, self._vault_audience(), params={"api-version": KV_DATA_API}, ok=(200, 404))
        if resp.status_code == 404:
            return False
        if resp.status_code != 200:
            raise AzureCMKError(f"Key Vault delete-key '{key_name}' failed: {_err_text(resp)}")
        return True

    # =========================================================================
    # Disk Encryption Sets
    # =========================================================================
    def des_path(self, subscription_id, resource_group, name):
        return (f"/subscriptions/{subscription_id}/resourceGroups/{resource_group}"
                f"/providers/Microsoft.Compute/diskEncryptionSets/{name}")

    def get_des(self, des_id):
        """GET a DES; None when it does not exist. Docs: https://learn.microsoft.com/en-us/rest/api/compute/disk-encryption-sets/get"""
        resp = self.arm("GET", des_id, DISK_API, ok=(200, 404))
        if resp.status_code == 404:
            return None
        if resp.status_code != 200:
            raise AzureCMKError(f"GET DES {des_id} failed: {_err_text(resp)}")
        return self._json(resp)

    def create_des(self, subscription_id, resource_group, name, location, vault_id, key_url,
                   encryption_type, auto_rotate, tags=None, user_assigned_identity_id=None):
        """
        PUT .../diskEncryptionSets/{name} with the versioned key URL and
        (same-subscription only) sourceVault.id. Identity:
          - default: SystemAssigned (the caller then grants it key access per DES);
          - `user_assigned_identity_id`: UserAssigned, pointing at a pre-existing managed
            identity that must already hold get/wrapKey/unwrapKey on the vault (no per-DES
            grant). This is also the identity mode Azure uses for cross-tenant vaults.
        Docs: https://learn.microsoft.com/en-us/rest/api/compute/disk-encryption-sets/create-or-update
              "keyUrl: Fully versioned Key Url ... Version segment of the Url is required
               regardless of rotationToLatestKeyVersionEnabled value."
              "sourceVault ... cannot be used if the KeyVault subscription is not the
               same as the Disk Encryption Set subscription."
              identity.userAssignedIdentities keys are the identity ARM resource IDs.
        Returns the DES body after provisioning (identity.principalId populated for
        SystemAssigned; identity.userAssignedIdentities[id].principalId for UserAssigned).
        """
        if encryption_type not in ENCRYPTION_TYPES:
            raise AzureCMKError(f"unsupported encryptionType '{encryption_type}' (use one of {', '.join(ENCRYPTION_TYPES)})")
        active_key = {"keyUrl": key_url}
        if parse_resource_id(vault_id)["subscription"].lower() == subscription_id.lower():
            active_key["sourceVault"] = {"id": vault_id}
        if user_assigned_identity_id:
            validate_user_assigned_identity_id(user_assigned_identity_id)
            identity = {"type": "UserAssigned", "userAssignedIdentities": {user_assigned_identity_id: {}}}
        else:
            identity = {"type": "SystemAssigned"}
        body = {
            "location": location,
            "identity": identity,
            "properties": {
                "activeKey": active_key,
                "encryptionType": encryption_type,
                "rotationToLatestKeyVersionEnabled": bool(auto_rotate),
            },
            "tags": tags or {},
        }
        path = self.des_path(subscription_id, resource_group, name)
        resp = self.arm("PUT", path, DISK_API, body=body, ok=(200, 201, 202))
        if resp.status_code not in (200, 201, 202):
            raise AzureCMKError(f"create DES '{name}' failed: {_err_text(resp)}")
        self.wait_lro(resp)
        des = self.wait_provisioning(path, DISK_API)
        if user_assigned_identity_id:
            # Pre-existing identity: nothing to wait for or grant per DES.
            return des
        # The system-assigned identity can lag a little behind the resource; poll until principalId shows up.
        deadline = time.time() + 300
        while not ((des.get("identity") or {}).get("principalId")) and time.time() < deadline:
            time.sleep(10)
            des = self.get_des(path) or des
        if not (des.get("identity") or {}).get("principalId"):
            raise AzureCMKError(f"DES '{name}' was created but its managed identity never appeared")
        return des

    def delete_des(self, des_id):
        """
        DELETE a DES (200/202 deleted, 204 absent). Disk deletion can lag behind the
        VM delete, so an in-use conflict is retried for DES_INUSE_RETRY_TIMEOUT_SECS.
        Docs: https://learn.microsoft.com/en-us/rest/api/compute/disk-encryption-sets/delete
        Returns True if deleted, False if it did not exist.
        """
        deadline = time.time() + DES_INUSE_RETRY_TIMEOUT_SECS
        while True:
            resp = self.arm("DELETE", des_id, DISK_API, ok=(200, 202, 204))
            if resp.status_code == 204:
                return False
            if resp.status_code in (200, 202):
                self.wait_lro(resp)
                return True
            code = _err_code(resp)
            in_use = resp.status_code in (400, 409) and (
                "inuse" in code.lower() or "in use" in resp.text.lower() or "OperationNotAllowed" in code
            )
            if in_use and time.time() < deadline:
                logger.info("DES %s still referenced by disks; retrying delete in %ss", des_id, DES_INUSE_POLL_SECS)
                time.sleep(DES_INUSE_POLL_SECS)
                continue
            raise AzureCMKError(f"delete DES {des_id} failed: {_err_text(resp)}")

    # =========================================================================
    # Granting the DES identity access to the key
    # =========================================================================
    def grant_des_key_access(self, vault, des_principal_id):
        """
        Give the DES identity get/wrapKey/unwrapKey on the vault:
          - RBAC vaults (enableRbacAuthorization=true): role assignment of
            Key Vault Crypto Service Encryption User at vault scope.
          - Policy vaults: accessPolicies/add with keys get/wrapKey/unwrapKey.
        Returns an opaque grant reference the teardown uses to revoke.
        """
        vault_id = vault["id"]
        props = vault.get("properties") or {}
        if props.get("enableRbacAuthorization"):
            return self._assign_role(vault_id, des_principal_id)
        return self._add_access_policy(vault_id, props.get("tenantId") or self.tenant_id, des_principal_id)

    def _assign_role(self, scope, principal_id):
        """
        PUT {scope}/providers/Microsoft.Authorization/roleAssignments/{guid}.
        principalType=ServicePrincipal lets ARM accept a just-created managed
        identity without a directory lookup (avoids PrincipalNotFound during
        replication). The GUID is deterministic so re-runs are idempotent.
        Docs: https://learn.microsoft.com/en-us/rest/api/authorization/role-assignments/create
        Requires Microsoft.Authorization/roleAssignments/write on the vault
        (e.g. "Key Vault Data Access Administrator" or User Access Administrator).
        """
        sub = parse_resource_id(scope)["subscription"]
        role_def = f"/subscriptions/{sub}/providers/Microsoft.Authorization/roleDefinitions/{ROLE_KV_CRYPTO_SERVICE_ENCRYPTION_USER}"
        name = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{scope}|{principal_id}|{ROLE_KV_CRYPTO_SERVICE_ENCRYPTION_USER}"))
        path = f"{scope}/providers/Microsoft.Authorization/roleAssignments/{name}"
        body = {"properties": {"roleDefinitionId": role_def, "principalId": principal_id, "principalType": "ServicePrincipal"}}
        deadline = time.time() + GRANT_PROPAGATION_TIMEOUT_SECS
        while True:
            resp = self.arm("PUT", path, AUTHZ_API, body=body, ok=(200, 201, 409))
            if resp.status_code in (200, 201):
                return path
            if resp.status_code == 409 and "RoleAssignmentExists" in _err_code(resp):
                return path
            if "PrincipalNotFound" in _err_code(resp) and time.time() < deadline:
                time.sleep(GRANT_PROPAGATION_POLL_SECS)
                continue
            raise AzureCMKError(f"role assignment on {scope} failed: {_err_text(resp)}")

    def revoke_role(self, role_assignment_id):
        """DELETE a role assignment (200/204). Docs: https://learn.microsoft.com/en-us/rest/api/authorization/role-assignments/delete"""
        resp = self.arm("DELETE", role_assignment_id, AUTHZ_API, ok=(200, 204))
        if resp.status_code not in (200, 204):
            raise AzureCMKError(f"delete role assignment failed: {_err_text(resp)}")

    def _add_access_policy(self, vault_id, tenant_id, principal_id):
        """
        PUT .../vaults/{name}/accessPolicies/add
        Docs: https://learn.microsoft.com/en-us/rest/api/keyvault/keyvault/vaults/update-access-policy
        Requires Microsoft.KeyVault/vaults/accessPolicies/write.
        """
        body = {"properties": {"accessPolicies": [
            {"tenantId": tenant_id, "objectId": principal_id, "permissions": {"keys": DES_KEY_PERMISSIONS}}
        ]}}
        resp = self.arm("PUT", f"{vault_id}/accessPolicies/add", KV_MGMT_API, body=body, ok=(200, 201))
        if resp.status_code not in (200, 201):
            raise AzureCMKError(f"access-policy add on {vault_id} failed: {_err_text(resp)}")
        return f"accessPolicy:{principal_id}"

    def remove_access_policy(self, vault_id, tenant_id, principal_id):
        """PUT .../accessPolicies/remove for the DES identity (same doc as add)."""
        body = {"properties": {"accessPolicies": [
            {"tenantId": tenant_id, "objectId": principal_id, "permissions": {"keys": DES_KEY_PERMISSIONS}}
        ]}}
        resp = self.arm("PUT", f"{vault_id}/accessPolicies/remove", KV_MGMT_API, body=body, ok=(200, 201))
        if resp.status_code not in (200, 201):
            raise AzureCMKError(f"access-policy remove on {vault_id} failed: {_err_text(resp)}")

    def revoke_grant(self, grant_ref, vault_id, tenant_id):
        """Undo whatever grant_des_key_access recorded."""
        if not grant_ref:
            return "no grant recorded"
        if grant_ref.startswith(GRANT_REF_UAMI_PREFIX):
            # Shared user-assigned identity pre-authorised by the customer: never touch its access.
            return "shared user-assigned identity; nothing to revoke"
        if grant_ref.startswith("accessPolicy:"):
            self.remove_access_policy(vault_id, tenant_id, grant_ref.split(":", 1)[1])
            return "access policy removed"
        self.revoke_role(grant_ref)
        return "role assignment removed"

    # =========================================================================
    # Virtual machines and disks
    # =========================================================================
    def vm_path(self, subscription_id, resource_group, vm_name):
        return (f"/subscriptions/{subscription_id}/resourceGroups/{resource_group}"
                f"/providers/Microsoft.Compute/virtualMachines/{vm_name}")

    def get_vm(self, subscription_id, resource_group, vm_name, instance_view=False):
        """GET the VM model (optionally $expand=instanceView for power state). Docs: https://learn.microsoft.com/en-us/rest/api/compute/virtual-machines/get"""
        params = {"$expand": "instanceView"} if instance_view else None
        resp = self.arm("GET", self.vm_path(subscription_id, resource_group, vm_name), VM_API, params=params)
        if resp.status_code != 200:
            raise AzureCMKError(f"GET VM '{vm_name}' failed: {_err_text(resp)}")
        return self._json(resp)

    def vm_power_state(self, vm_with_instance_view):
        statuses = ((vm_with_instance_view.get("properties") or {}).get("instanceView") or {}).get("statuses") or []
        for status in statuses:
            code = str(status.get("code", ""))
            if code.startswith("PowerState/"):
                return code.split("/", 1)[1]
        return "unknown"

    def vm_disk_ids(self, vm):
        """(os_disk_id, [data_disk_ids]) from properties.storageProfile; raises for unmanaged OS disks."""
        storage = (vm.get("properties") or {}).get("storageProfile") or {}
        os_disk = storage.get("osDisk") or {}
        managed = os_disk.get("managedDisk") or {}
        if os_disk.get("diffDiskSettings"):
            raise AzureCMKError("the VM uses an ephemeral OS disk, which cannot be encrypted with a customer-managed key")
        if not managed.get("id"):
            raise AzureCMKError("the VM's OS disk is not a managed disk; customer-managed keys require managed disks")
        data_ids = [(d.get("managedDisk") or {}).get("id") for d in storage.get("dataDisks") or []]
        return managed["id"], [d for d in data_ids if d]

    def deallocate_vm(self, subscription_id, resource_group, vm_name):
        """POST .../deallocate then follow the LRO. Docs: https://learn.microsoft.com/en-us/rest/api/compute/virtual-machines/deallocate"""
        resp = self.arm("POST", f"{self.vm_path(subscription_id, resource_group, vm_name)}/deallocate", VM_API, ok=(200, 202))
        if resp.status_code not in (200, 202):
            raise AzureCMKError(f"deallocate VM '{vm_name}' failed: {_err_text(resp)}")
        self.wait_lro(resp)

    def start_vm(self, subscription_id, resource_group, vm_name):
        """POST .../start then follow the LRO. Docs: https://learn.microsoft.com/en-us/rest/api/compute/virtual-machines/start"""
        resp = self.arm("POST", f"{self.vm_path(subscription_id, resource_group, vm_name)}/start", VM_API, ok=(200, 202))
        if resp.status_code not in (200, 202):
            raise AzureCMKError(f"start VM '{vm_name}' failed: {_err_text(resp)}")
        self.wait_lro(resp)

    def set_encryption_at_host(self, subscription_id, resource_group, vm_name, enabled=True):
        """
        PATCH the VM with properties.securityProfile.encryptionAtHost. The VM must be
        deallocated and the subscription must have Microsoft.Compute/EncryptionAtHost registered.
        Docs: https://learn.microsoft.com/en-us/rest/api/compute/virtual-machines/update
              https://learn.microsoft.com/en-us/azure/virtual-machines/disks-enable-host-based-encryption-portal
        """
        body = {"properties": {"securityProfile": {"encryptionAtHost": bool(enabled)}}}
        resp = self.arm("PATCH", self.vm_path(subscription_id, resource_group, vm_name), VM_API, body=body, ok=(200, 201, 202))
        if resp.status_code not in (200, 201, 202):
            raise AzureCMKError(f"enable encryption at host on '{vm_name}' failed: {_err_text(resp)}")
        self.wait_lro(resp)

    def get_disk(self, disk_id):
        """GET a managed disk. Docs: https://learn.microsoft.com/en-us/rest/api/compute/disks/get"""
        resp = self.arm("GET", disk_id, DISK_API)
        if resp.status_code != 200:
            raise AzureCMKError(f"GET disk {disk_id} failed: {_err_text(resp)}")
        return self._json(resp)

    def disk_current_des(self, disk):
        enc = (disk.get("properties") or {}).get("encryption") or {}
        return (enc.get("diskEncryptionSetId") or ""), (enc.get("type") or "")

    def set_disk_encryption(self, disk_id, des_id, encryption_type):
        """
        PATCH .../disks/{name} with properties.encryption = {diskEncryptionSetId, type}.
        The disk must not be attached to a running VM. Key Vault authorization for the
        freshly-granted DES identity can lag, so Key-Vault access errors are retried
        for GRANT_PROPAGATION_TIMEOUT_SECS.
        Docs: https://learn.microsoft.com/en-us/rest/api/compute/disks/update
        """
        body = {"properties": {"encryption": {"diskEncryptionSetId": des_id, "type": encryption_type}}}
        deadline = time.time() + GRANT_PROPAGATION_TIMEOUT_SECS
        while True:
            resp = self.arm("PATCH", disk_id, DISK_API, body=body, ok=(200, 202))
            if resp.status_code in (200, 202):
                self.wait_lro(resp)
                self.wait_provisioning(disk_id, DISK_API)
                return
            text = resp.text or ""
            kv_lag = resp.status_code in (400, 403, 409) and (
                "KeyVault" in text or "Key Vault" in text or "Forbidden" in text or "does not have" in text
            )
            if kv_lag and time.time() < deadline:
                logger.info("disk %s: Key Vault access for the DES identity not yet effective; retrying", disk_id)
                time.sleep(GRANT_PROPAGATION_POLL_SECS)
                continue
            raise AzureCMKError(f"encrypt disk {disk_id.rsplit('/', 1)[-1]} failed: {_err_text(resp)}")
