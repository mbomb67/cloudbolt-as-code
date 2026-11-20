"""Issue, fetch and delete TLS certificates in Azure Key Vault with an Azure resource handler's identity.

    from shared_modules import azure_key_vault_certificates as kv

    vaults = kv.list_vaults(handler, location="eastus")          # [{"name", "location", "uri", "rbac"}]
    cert = kv.issue_certificate(handler, vault_uri, "web-001", ["web-001.example.com"])
    cert["certificate_pem"], cert["private_key_pem"], cert["id"], cert["thumbprint"]
    kv.delete_certificate(handler, vault_uri, "web-001")

The handler's service principal needs, on the vault, Key Vault Certificates Officer (create and
delete certificates) and Key Vault Secrets User (read the certificate with its private key):
https://learn.microsoft.com/en-us/azure/key-vault/general/rbac-guide
A vault that uses access policies instead needs certificate Get/Create/Delete and secret Get.

Calls raise KeyVaultError with Azure's own message.
"""

import re
import time

import requests

from utilities.helpers import get_ssl_verification

# Key Vault data plane and Microsoft.KeyVault control plane, as documented on the pages cited below.
DATA_API = "2025-07-01"
ARM_API = "2024-11-01"
REQUEST_TIMEOUT = 60
ISSUE_TIMEOUT_SECONDS = 300

# Endpoints per Azure cloud, keyed by AzureARMHandler.cloud_environment.
CLOUDS = {
    "PUBLIC": ("https://login.microsoftonline.com", "https://management.azure.com", "https://vault.azure.net"),
    "US_GOV": ("https://login.microsoftonline.us", "https://management.usgovcloudapi.net", "https://vault.usgovcloudapi.net"),
    "CHINA": ("https://login.chinacloudapi.cn", "https://management.chinacloudapi.cn", "https://vault.azure.cn"),
}

PEM_BLOCK = re.compile(r"-----BEGIN ([A-Z ]+)-----.*?-----END \1-----", re.DOTALL)


class KeyVaultError(Exception):
    """Azure refused a call or could not be reached."""


def _endpoints(handler):
    return CLOUDS.get(getattr(handler, "cloud_environment", None) or "PUBLIC", CLOUDS["PUBLIC"])


def _token(handler, audience):
    """A bearer token for the handler's service principal.

    Client-credentials grant; the scope is the audience plus /.default.
    https://learn.microsoft.com/en-us/entra/identity-platform/v2-oauth2-client-creds-grant-flow
    """
    login = _endpoints(handler)[0]
    client_id = getattr(handler, "client_id", None)
    secret = getattr(handler, "secret", None)
    tenant = getattr(handler, "azure_tenant_id", None)
    scope = f"{audience}/.default"
    if not (client_id and secret and tenant):
        # A handler without a client secret: use the credential CloudBolt already built for it.
        try:
            return handler.get_api_wrapper().credentials.get_token(scope).token
        except Exception as error:
            raise KeyVaultError(f"Could not get a token for {audience} from resource handler '{handler.name}': {error}")
    try:
        response = requests.post(
            f"{login}/{tenant}/oauth2/v2.0/token",
            data={"grant_type": "client_credentials", "client_id": client_id, "client_secret": secret, "scope": scope},
            timeout=REQUEST_TIMEOUT,
            verify=get_ssl_verification(),
        )
    except requests.exceptions.RequestException as error:
        raise KeyVaultError(f"Could not reach {login}: {error}")
    if response.status_code != 200:
        raise KeyVaultError(f"Azure refused a token for {audience}: {_error_text(response)}")
    return response.json()["access_token"]


def _error_text(response):
    """Azure's error message: both planes answer an error object with a code and a message."""
    try:
        error = response.json().get("error") or {}
        if isinstance(error, dict):
            return f"{response.status_code} {error.get('code', '')}: {error.get('message', '')}".strip()
        return f"{response.status_code} {error}: {response.json().get('error_description', '')}"
    except ValueError:
        return f"{response.status_code} {response.text[:300]}"


def _call(handler, method, url, audience, api_version, body=None, ok=(200,), not_found_ok=False):
    try:
        response = requests.request(
            method,
            url,
            params={"api-version": api_version} if "api-version=" not in url else None,
            json=body,
            headers={"Authorization": f"Bearer {_token(handler, audience)}"},
            timeout=REQUEST_TIMEOUT,
            verify=get_ssl_verification(),
        )
    except requests.exceptions.RequestException as error:
        raise KeyVaultError(f"Could not reach {url.split('?')[0]}: {error}")
    if response.status_code == 404 and not_found_ok:
        return None
    if response.status_code not in ok:
        hint = ""
        if response.status_code == 403:
            hint = (
                " The resource handler's service principal needs the Key Vault Certificates Officer "
                "and Key Vault Secrets User roles on the vault."
            )
        raise KeyVaultError(f"{method} {url.split('?')[0]} failed: {_error_text(response)}{hint}")
    return response.json() if response.content else {}


def list_vaults(handler, location=None):
    """The subscription's key vaults, optionally only those in one Azure location.

    https://learn.microsoft.com/en-us/rest/api/keyvault/keyvault/vaults/list-by-subscription
    """
    arm = _endpoints(handler)[1]
    url = f"{arm}/subscriptions/{handler.serviceaccount}/providers/Microsoft.KeyVault/vaults"
    vaults = []
    while url:
        page = _call(handler, "GET", url, arm, ARM_API)
        for item in page.get("value", []):
            if location and (item.get("location") or "").lower() != location.lower():
                continue
            properties = item.get("properties") or {}
            vaults.append({
                "name": item["name"],
                "location": item.get("location"),
                "uri": (properties.get("vaultUri") or "").rstrip("/"),
                "rbac": bool(properties.get("enableRbacAuthorization")),
            })
        url = page.get("nextLink")
    return sorted(vaults, key=lambda vault: vault["name"])


def certificate_name(text):
    """A valid Key Vault object name from any text: letters, digits and hyphens, at most 127."""
    return re.sub(r"[^0-9A-Za-z-]", "-", text)[:127].strip("-")


def issue_certificate(handler, vault_uri, name, dns_names, validity_months=12, key_size=2048):
    """Create a certificate with an exportable key and return it with its private key, in PEM.

    The certificate comes from the vault's built-in Self issuer, so it is self-signed; its subject
    is the first DNS name and all of them are subject alternative names. Calling this again for the
    same name issues a new version.
    https://learn.microsoft.com/en-us/rest/api/keyvault/certificates/create-certificate/create-certificate
    """
    vault_uri = vault_uri.rstrip("/")
    audience = _endpoints(handler)[2]
    _call(handler, "POST", f"{vault_uri}/certificates/{name}/create", audience, DATA_API, ok=(202,), body={
        "policy": {
            "key_props": {"exportable": True, "kty": "RSA", "key_size": key_size, "reuse_key": False},
            # PEM, so the secret holds the key and the certificate as text and nothing has to
            # convert a PKCS#12 bundle.
            "secret_props": {"contentType": "application/x-pem-file"},
            "x509_props": {
                "subject": f"CN={dns_names[0]}",
                "sans": {"dns_names": list(dns_names)},
                "validity_months": validity_months,
                "key_usage": ["digitalSignature", "keyEncipherment"],
                "ekus": ["1.3.6.1.5.5.7.3.1"],  # TLS server authentication
            },
            "issuer": {"name": "Self"},
        },
        "tags": {"created-by": "cloudbolt"},
    })

    # https://learn.microsoft.com/en-us/rest/api/keyvault/certificates/get-certificate-operation/get-certificate-operation
    deadline = time.time() + ISSUE_TIMEOUT_SECONDS
    while True:
        operation = _call(handler, "GET", f"{vault_uri}/certificates/{name}/pending", audience, DATA_API)
        status = (operation.get("status") or "").lower()
        if status != "inprogress":
            break
        if time.time() > deadline:
            raise KeyVaultError(f"Key Vault was still issuing certificate {name} after {ISSUE_TIMEOUT_SECONDS} seconds.")
        time.sleep(5)
    if status != "completed":
        detail = operation.get("status_details") or (operation.get("error") or {}).get("message") or ""
        raise KeyVaultError(f"Key Vault did not issue certificate {name} (status {status}). {detail}")

    # https://learn.microsoft.com/en-us/rest/api/keyvault/certificates/get-certificate/get-certificate
    certificate = _call(handler, "GET", f"{vault_uri}/certificates/{name}", audience, DATA_API)
    # The private key is only returned through the certificate's secret.
    # https://learn.microsoft.com/en-us/rest/api/keyvault/secrets/get-secret/get-secret
    # https://learn.microsoft.com/en-us/azure/key-vault/certificates/about-certificates
    secret = _call(handler, "GET", f"{vault_uri}/secrets/{name}", audience, DATA_API)
    blocks = [(match.group(1), match.group(0)) for match in PEM_BLOCK.finditer(secret.get("value") or "")]
    keys = [text for kind, text in blocks if kind.endswith("PRIVATE KEY")]
    certificates = [text for kind, text in blocks if kind == "CERTIFICATE"]
    if not keys or not certificates:
        raise KeyVaultError(
            f"The secret for certificate {name} holds no PEM private key and certificate "
            f"(content type {secret.get('contentType')}); its policy must have an exportable key."
        )
    return {
        "id": certificate.get("id"),
        "thumbprint": certificate.get("x5t"),
        "expires": (certificate.get("attributes") or {}).get("exp"),
        "private_key_pem": keys[0] + "\n",
        "certificate_pem": "\n".join(certificates) + "\n",
    }


def delete_certificate(handler, vault_uri, name):
    """Delete every version of a certificate; return False when it was already gone.

    On a vault with soft delete the name stays reserved until the certificate is purged.
    https://learn.microsoft.com/en-us/rest/api/keyvault/certificates/delete-certificate/delete-certificate
    """
    audience = _endpoints(handler)[2]
    deleted = _call(
        handler, "DELETE", f"{vault_uri.rstrip('/')}/certificates/{name}", audience, DATA_API, not_found_ok=True
    )
    return deleted is not None
