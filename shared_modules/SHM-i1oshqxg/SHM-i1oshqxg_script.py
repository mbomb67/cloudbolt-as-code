"""
Shared module: azure_management_locks

REST helpers for Azure Resource Manager management locks, authenticated as a
CloudBolt Azure resource handler's service principal.

CloudBolt appliances ship the azure-mgmt-resource SDK but not its separate
management-locks client (azure.mgmt.resource.locks), so the Azure Resource
Group blueprint talks to the locks API directly over HTTPS through this module.

REST reference (api-version 2016-09-01, cited per call below):
  https://learn.microsoft.com/en-us/rest/api/resources/management-locks

Usage:
    from shared_modules.azure_management_locks import (
        AzureLockError, ManagementLocks, lock_level, strongest_level,
    )
    locks = ManagementLocks(rh)  # rh is an AzureARMHandler
    locks.set_resource_group_lock("my-rg", "cloudbolt-lock", "CanNotDelete")
    [lock_level(lock) for lock in locks.list_resource_group_locks("my-rg")]
    locks.delete_resource_group_lock("my-rg", "cloudbolt-lock")  # False if absent
"""

import time

import requests

from utilities.logger import ThreadLogger

logger = ThreadLogger(__name__)

LOCKS_API_VERSION = "2016-09-01"
REQUEST_TIMEOUT = 60

# Known lock levels, most restrictive first. ReadOnly blocks changes and
# deletion; CanNotDelete blocks deletion only. Every level here blocks deletion.
# NotSpecified (the third LockLevel value) restricts nothing and is not listed.
# https://learn.microsoft.com/en-us/rest/api/resources/management-locks/list-at-resource-group-level#locklevel
LOCK_LEVELS = ("ReadOnly", "CanNotDelete")

# Entra ID login and ARM endpoints per Azure cloud, keyed by the handler's
# cloud_environment value. Mirrors shared_modules/azure_pricing.
_CLOUD_ENDPOINTS = {
    "PUBLIC": ("https://login.microsoftonline.com", "https://management.azure.com"),
    "US_GOV": ("https://login.microsoftonline.us", "https://management.usgovcloudapi.net"),
    "CHINA": ("https://login.chinacloudapi.cn", "https://management.chinacloudapi.cn"),
    "GERMAN": ("https://login.microsoftonline.de", "https://management.microsoftazure.de"),
}


class AzureLockError(Exception):
    """An ARM locks call failed.

    status_code is the HTTP status of the failing response, or None when no
    response was received (network error, token acquisition failure).
    """

    def __init__(self, message, status_code=None):
        super().__init__(message)
        self.status_code = status_code


def cloud_endpoints(rh):
    """Return (login_base, arm_base) for the handler's Azure cloud."""
    cloud = getattr(rh, "cloud_environment", "PUBLIC") or "PUBLIC"
    return _CLOUD_ENDPOINTS.get(cloud, _CLOUD_ENDPOINTS["PUBLIC"])


def lock_level(lock):
    """Return a lock's level as one of LOCK_LEVELS, or None.

    `lock` is a ManagementLockObject dict as returned by the API; the level
    lives at properties.level. NotSpecified and unknown values return None.
    """
    text = str(((lock or {}).get("properties") or {}).get("level") or "")
    for known in LOCK_LEVELS:
        if text.lower() == known.lower():
            return known
    return None


def strongest_level(levels):
    """Return the most restrictive level in `levels`, or "None" if nothing restricts."""
    present = {level for level in levels if level}
    for level in LOCK_LEVELS:
        if level in present:
            return level
    return "None"


def resource_group_of(resource_id):
    """Extract the resource group from an Azure resource ID, or None if it has none.

    IDs look like /subscriptions/{sub}/resourceGroups/{rg}/providers/...; the
    group is segment 4. See docs/agents/common-patterns.md (Parsing Azure Resource IDs).
    """
    parts = (resource_id or "").split("/")
    if len(parts) > 4 and parts[3].lower() == "resourcegroups":
        return parts[4]
    return None


def _error_text(resp):
    """Return 'code: message' from an error body, else the status and a body snippet.

    Handles both ARM errors (error is an object with code/message) and Entra
    token errors (error is a string plus error_description). Never includes
    request headers, so the text is safe to surface to users.
    """
    try:
        body = resp.json() or {}
    except ValueError:
        body = {}
    err = body.get("error") if isinstance(body, dict) else None
    if isinstance(err, dict):
        code, message = err.get("code"), err.get("message")
        if code or message:
            return f"{code or 'Error'}: {message or ''}".strip()
    elif err:
        return f"{err}: {body.get('error_description', '')}".strip()
    return f"HTTP {resp.status_code}: {(resp.text or '')[:300]}"


class ManagementLocks:
    """Management-lock operations for one Azure resource handler's subscription."""

    def __init__(self, rh):
        self.rh = rh
        self.login_base, self.arm_base = cloud_endpoints(rh)
        # The Azure subscription id is the handler's inherited `serviceaccount`.
        self.subscription_id = getattr(rh, "serviceaccount", None)
        if not self.subscription_id:
            raise AzureLockError(f"Azure handler '{rh}' has no subscription id (serviceaccount).")
        self._token = None
        self._token_expiry = 0.0

    # ---- auth --------------------------------------------------------------
    def _get_token(self):
        """Return a bearer token for ARM, cached until shortly before it expires.

        Uses the handler's own service principal via the OAuth2 client-credentials
        grant. When the handler has no client secret (certificate or managed
        identity), falls back to the SDK credential CloudBolt already built for it.
        https://learn.microsoft.com/en-us/entra/identity-platform/v2-oauth2-client-creds-grant-flow
        """
        if self._token and time.time() < self._token_expiry - 60:
            return self._token

        client_id = getattr(self.rh, "client_id", None)
        secret = getattr(self.rh, "secret", None)
        tenant_id = getattr(self.rh, "azure_tenant_id", None)
        if client_id and secret and tenant_id:
            try:
                resp = requests.post(
                    f"{self.login_base}/{tenant_id}/oauth2/v2.0/token",
                    data={
                        "grant_type": "client_credentials",
                        "client_id": client_id,
                        "client_secret": secret,
                        "scope": f"{self.arm_base}/.default",
                    },
                    timeout=REQUEST_TIMEOUT,
                )
            except requests.exceptions.RequestException as exc:
                raise AzureLockError(f"Could not reach {self.login_base}: {exc}")
            if resp.status_code != 200:
                raise AzureLockError(f"Token request failed: {_error_text(resp)}", resp.status_code)
            body = resp.json()
            self._token = body["access_token"]
            self._token_expiry = time.time() + int(body.get("expires_in", 3000))
            return self._token

        try:
            access = self.rh.get_api_wrapper().credentials.get_token(f"{self.arm_base}/.default")
        except Exception as exc:  # noqa: BLE001 - any credential failure is a token failure
            raise AzureLockError(f"Could not obtain an ARM token from the handler credential: {exc}")
        self._token = access.token
        self._token_expiry = float(getattr(access, "expires_on", time.time() + 3000))
        return self._token

    # ---- transport ---------------------------------------------------------
    def _request(self, method, path, body=None, params=None):
        """Make one ARM call and return the response.

        `path` is relative to the ARM base, or an absolute nextLink URL (which
        already carries its own api-version). Honors a single 429 Retry-After;
        every call in this module is a GET or a PUT/DELETE by name, so repeating
        it is safe.
        """
        absolute = path.startswith("http")
        url = path if absolute else f"{self.arm_base}{path}"
        query = dict(params or {})
        if not absolute:
            query.setdefault("api-version", LOCKS_API_VERSION)
        headers = {
            "Authorization": f"Bearer {self._get_token()}",
            "Content-Type": "application/json",
        }
        try:
            resp = requests.request(
                method, url, headers=headers, json=body, params=query, timeout=REQUEST_TIMEOUT
            )
            if resp.status_code == 429:
                try:
                    delay = min(int(resp.headers.get("Retry-After", "10")), 60)
                except ValueError:
                    delay = 10
                logger.warning("ARM throttled %s %s; retrying in %ss", method, url, delay)
                time.sleep(delay)
                resp = requests.request(
                    method, url, headers=headers, json=body, params=query, timeout=REQUEST_TIMEOUT
                )
        except requests.exceptions.RequestException as exc:
            raise AzureLockError(f"Could not reach {self.arm_base}: {exc}")
        return resp

    def _list(self, path):
        """GET a ManagementLockListResult, following nextLink until exhausted."""
        items, next_path = [], path
        while next_path:
            resp = self._request("GET", next_path)
            if resp.status_code != 200:
                raise AzureLockError(f"Listing locks failed: {_error_text(resp)}", resp.status_code)
            data = resp.json() if resp.content else {}
            items.extend(data.get("value") or [])
            next_path = data.get("nextLink")
        return items

    def _lock_path(self, rg_name, lock_name):
        return (
            f"/subscriptions/{self.subscription_id}/resourceGroups/{rg_name}"
            f"/providers/Microsoft.Authorization/locks/{lock_name}"
        )

    # ---- operations --------------------------------------------------------
    def list_resource_group_locks(self, rg_name):
        """Return every lock scoped at or under the resource group, as API dicts.

        GET /subscriptions/{sub}/resourceGroups/{rg}/providers/Microsoft.Authorization/locks -> 200
        https://learn.microsoft.com/en-us/rest/api/resources/management-locks/list-at-resource-group-level
        """
        return self._list(
            f"/subscriptions/{self.subscription_id}/resourceGroups/{rg_name}"
            "/providers/Microsoft.Authorization/locks"
        )

    def list_subscription_locks(self):
        """Return every lock in the subscription, as API dicts.

        GET /subscriptions/{sub}/providers/Microsoft.Authorization/locks -> 200
        https://learn.microsoft.com/en-us/rest/api/resources/management-locks/list-at-subscription-level
        """
        return self._list(
            f"/subscriptions/{self.subscription_id}/providers/Microsoft.Authorization/locks"
        )

    def set_resource_group_lock(self, rg_name, lock_name, level, notes=None):
        """Create or update a lock on the resource group and return the lock dict.

        PUT /subscriptions/{sub}/resourceGroups/{rg}/providers/Microsoft.Authorization/locks/{lockName}
        Body: properties.level (required LockLevel), properties.notes (optional, max 512 chars)
        Responses: 200 (updated) or 201 (created), both returning the ManagementLockObject.
        https://learn.microsoft.com/en-us/rest/api/resources/management-locks/create-or-update-at-resource-group-level
        """
        properties = {"level": level}
        if notes:
            properties["notes"] = notes[:512]
        resp = self._request("PUT", self._lock_path(rg_name, lock_name), body={"properties": properties})
        if resp.status_code not in (200, 201):
            raise AzureLockError(
                f"Applying lock '{lock_name}' failed: {_error_text(resp)}", resp.status_code
            )
        return resp.json() if resp.content else {}

    def delete_resource_group_lock(self, rg_name, lock_name):
        """Delete a lock from the resource group.

        Returns True when the lock was removed and False when there was no such
        lock (404). Anything else raises AzureLockError.
        DELETE /subscriptions/{sub}/resourceGroups/{rg}/providers/Microsoft.Authorization/locks/{lockName} -> 200 or 204
        https://learn.microsoft.com/en-us/rest/api/resources/management-locks/delete-at-resource-group-level
        """
        resp = self._request("DELETE", self._lock_path(rg_name, lock_name))
        if resp.status_code in (200, 204):
            return True
        if resp.status_code == 404:
            return False
        raise AzureLockError(
            f"Removing lock '{lock_name}' failed: {_error_text(resp)}", resp.status_code
        )
