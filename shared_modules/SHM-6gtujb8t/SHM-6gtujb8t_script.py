"""
azure_pricing — shared pricing engine for the Azure Resource Manager rate hook.

Provides cost estimates for an Azure VM and its associated resources (compute,
OS disk, data disks, OS license, public IP) using a tiered source strategy:

  1. NEGOTIATED prices from the customer's Azure Price Sheet (EA / MCA), when a
     recent sheet has been downloaded by the companion recurring job and cached
     on disk. Negotiated unit prices are keyed by Azure meterId.
  2. PUBLIC RETAIL (list) prices from the unauthenticated Azure Retail Prices
     API (https://prices.azure.com) — the same data behind the Azure Pricing
     Calculator. Used for CSP/Partner subscriptions (which cannot read their
     own negotiated rate), and as the fallback for EA/MCA when no usable sheet
     exists (no billing-role grant, not yet downloaded, or a meter is missing).
  3. Nothing — caller falls back to CloudBolt's default_compute_rate.

This module makes raw REST calls with `requests` because the appliance venv has
no azure-mgmt-costmanagement / azure-mgmt-billing SDK. The handler's existing
service principal is reused for management.azure.com calls; for negotiated
pricing that SPN must additionally hold a billing-scope role (EA EnrollmentReader
/ MCA Billing Profile Reader).

External Azure APIs referenced (cite current docs at each call site):
  - Billing accounts list (agreementType):
    https://learn.microsoft.com/en-us/rest/api/billing/billing-accounts/list  (api 2024-04-01)
  - Price Sheet download by billing account (EA):
    https://learn.microsoft.com/en-us/azure/cost-management-billing/automate/migrate-ea-price-sheet-api  (api 2023-11-01)
  - Price Sheet download by billing profile (MCA/MPA):
    https://learn.microsoft.com/en-us/rest/api/cost-management/price-sheet/download-by-billing-profile  (api 2023-11-01)
  - Retail Prices API:
    https://learn.microsoft.com/en-us/rest/api/cost-management/retail-prices/azure-retail-prices  (api 2023-01-01-preview)

SHARED-MODULE NOTE: this code is cached in the running CloudBolt process. After a
repo sync that changes this file, restart CloudBolt for the change to take
effect. No secrets are stored in this repo; the handler SPN secret is read from
the handler at runtime.
"""
import csv
import gzip
import io
import json
import os
import time
import zipfile
from decimal import Decimal, InvalidOperation

import requests

from django.conf import settings

from utilities.logger import ThreadLogger

logger = ThreadLogger(__name__)

# --- Endpoints per Azure cloud -------------------------------------------------
# Sovereign clouds use different login/ARM endpoints. Keyed by the handler's
# `cloud_environment` field (CLOUD_CHOICES: PUBLIC/GERMAN/CHINA/US_GOV).
_CLOUD_ENDPOINTS = {
    "PUBLIC": ("https://login.microsoftonline.com", "https://management.azure.com"),
    "US_GOV": ("https://login.microsoftonline.us", "https://management.usgovcloudapi.net"),
    "CHINA": ("https://login.chinacloudapi.cn", "https://management.chinacloudapi.cn"),
    "GERMAN": ("https://login.microsoftonline.de", "https://management.microsoftazure.de"),
}

# Retail Prices API is public commercial cloud only; sovereign clouds have no
# equivalent unauthenticated endpoint, so retail fallback is disabled there.
RETAIL_PRICES_URL = "https://prices.azure.com/api/retail/prices"
RETAIL_API_VERSION = "2023-01-01-preview"

BILLING_ACCOUNTS_API = "2024-04-01"
PRICE_SHEET_API = "2023-11-01"

# Hours per global rate time unit. Aligned to standard calendar hours; the engine
# only appends the unit string for display, so consistency within this module is
# what matters. FLAT is treated as a monthly-equivalent single figure.
HOURS_PER_UNIT = {
    "HOUR": Decimal(1),
    "DAY": Decimal(24),
    "WEEK": Decimal(168),
    "MONTH": Decimal(730),
    "YEAR": Decimal(8760),
    "FLAT": Decimal(730),
}
HOURS_PER_MONTH = Decimal(730)

# --- Managed disk pricing ---------------------------------------------------
# Two distinct models, confirmed against the live Retail Prices API:
#
# 1. TIERED disks (Premium SSD, Standard SSD, Standard HDD): billed per DISK per
#    month by provisioned tier (P/E/S). The retail skuName carries the redundancy
#    suffix (e.g. "P10 LRS", NOT "P10") and the base meterName is "<sku> Disk"
#    (there is also a "<sku> Disk Mount" meter to avoid). Size selects the tier;
#    it is not a multiplier.
# 2. CAPACITY disks (Premium SSD v2): billed per PROVISIONED GiB per hour, with
#    separate provisioned IOPS / throughput meters that have large free baselines
#    (3000 IOPS / 125 MBps). We estimate the provisioned CAPACITY only (the
#    predictable, size-driven component); IOPS/throughput default to the free
#    baseline and have no order params here.
# https://azure.microsoft.com/en-us/pricing/details/managed-disks/

# GiB-capacity ladder per tier prefix -> tier code (smallest tier that fits).
_DISK_LADDER = {
    "P": [(4, "P1"), (8, "P2"), (16, "P3"), (32, "P4"), (64, "P6"), (128, "P10"),
          (256, "P15"), (512, "P20"), (1024, "P30"), (2048, "P40"), (4096, "P50"),
          (8192, "P60"), (16384, "P70"), (32767, "P80")],
    "E": [(4, "E1"), (8, "E2"), (16, "E3"), (32, "E4"), (64, "E6"), (128, "E10"),
          (256, "E15"), (512, "E20"), (1024, "E30"), (2048, "E40"), (4096, "E50"),
          (8192, "E60"), (16384, "E70"), (32767, "E80")],
    "S": [(32, "S4"), (64, "S6"), (128, "S10"), (256, "S15"), (512, "S20"),
          (1024, "S30"), (2048, "S40"), (4096, "S50"), (8192, "S60"),
          (16384, "S70"), (32767, "S80")],
}
# disk_type -> (productName, tier prefix, redundancy suffix)
_DISK_TIERED = {
    "Premium_LRS": ("Premium SSD Managed Disks", "P", "LRS"),
    "Premium_ZRS": ("Premium SSD Managed Disks", "P", "ZRS"),
    "StandardSSD_LRS": ("Standard SSD Managed Disks", "E", "LRS"),
    "StandardSSD_ZRS": ("Standard SSD Managed Disks", "E", "ZRS"),
    "Standard_LRS": ("Standard HDD Managed Disks", "S", "LRS"),
}
# disk_type -> (productName, skuName, provisioned-capacity meterName, note) for
# per-GiB disks. Capacity price uses unitOfMeasure "1 GiB/Hour". `note` is a label
# suffix flagging an incomplete estimate.
#   - Premium SSD v2: IOPS/throughput have a free baseline (3000 IOPS / 125 MBps),
#     so capacity is a fair whole estimate for typical disks -> no note.
#     - Ultra: IOPS and throughput are MANDATORY and have no free baseline, and we
#       have no order params for them, so capacity is only a floor -> "capacity only".
_DISK_CAPACITY = {
    "PremiumV2_LRS": ("Azure Premium SSD v2", "Premium LRS",
                      "Premium LRS Provisioned Capacity", ""),
    "UltraSSD_LRS": ("Ultra Disks", "Ultra LRS",
                     "Ultra LRS Provisioned Capacity", "capacity only"),
}

# Module-level memo of parsed price sheets, keyed by subscription id ->
# (file_mtime, {meter_id: (Decimal unitPrice, str unitOfMeasure)}). Persists for
# the life of the process (shared modules are process-cached), so a large sheet
# is parsed from disk at most once per change.
_PRICE_SHEET_MEMO = {}

REQUEST_TIMEOUT = 30

# Async Price Sheet generation is a slow bulk export, so we poll. Bound how long
# we wait on one handler so a stuck/doomed operation can't run the recurring job
# for many minutes. We honor the server's Retry-After but clamp it so completion
# (or failure) is detected promptly, and cap the total wait.
PRICE_SHEET_MAX_WAIT = 420          # max seconds to poll one handler before giving up
PRICE_SHEET_POLL_INTERVAL = 15      # default seconds between polls when no Retry-After
PRICE_SHEET_POLL_INTERVAL_MAX = 30  # clamp an over-long Retry-After down to this


# =============================================================================
# Token + endpoint helpers (reuse the handler service principal)
# =============================================================================
def cloud_endpoints(rh):
    """Return (login_base, arm_base) for the handler's Azure cloud."""
    cloud = getattr(rh, "cloud_environment", "PUBLIC") or "PUBLIC"
    return _CLOUD_ENDPOINTS.get(cloud, _CLOUD_ENDPOINTS["PUBLIC"])


def is_commercial_cloud(rh):
    cloud = getattr(rh, "cloud_environment", "PUBLIC") or "PUBLIC"
    return cloud == "PUBLIC"


def get_handler_token(rh):
    """
    Mint a management.azure.com bearer token from the handler's own service
    principal (client_id / secret / azure_tenant_id). Mirrors the repo's
    existing raw-REST Azure auth pattern (client-credentials grant). `rh.secret`
    is an EncryptedTextField and is already decrypted on read.
    """
    login_base, arm_base = cloud_endpoints(rh)
    token_url = f"{login_base}/{rh.azure_tenant_id}/oauth2/token"
    payload = {
        "grant_type": "client_credentials",
        "client_id": rh.client_id,
        "client_secret": rh.secret,
        "resource": f"{arm_base}/",
    }
    resp = requests.post(token_url, data=payload, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    return resp.json()["access_token"]


def _arm_get(arm_base, path, token, params=None):
    url = path if path.startswith("http") else f"{arm_base}{path}"
    headers = {"Authorization": f"Bearer {token}"}
    resp = requests.get(url, headers=headers, params=params, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    return resp.json() if resp.content else {}


def subscription_id_for(rh):
    """The Azure subscription id is the handler's inherited `serviceaccount`."""
    return getattr(rh, "serviceaccount", None)


def _snip(text, limit=500):
    """Trim a response body for logging."""
    if not text:
        return "<empty body>"
    text = str(text).replace("\n", " ")
    return text if len(text) <= limit else text[:limit] + "…(truncated)"


def _safe_json(resp):
    try:
        return resp.json()
    except Exception:
        return None


def _retry_after(resp, default):
    try:
        return max(1, int(resp.headers.get("Retry-After", default)))
    except (TypeError, ValueError):
        return default


# =============================================================================
# Billing scope detection (agreementType + account/profile)
# =============================================================================
def detect_billing_scope(rh, token=None):
    """
    Discover the billing account this handler's subscription belongs to and its
    agreement type, so the caller can pick the right Price Sheet endpoint.

    Returns a dict:
        {"agreement_type": "EnterpriseAgreement"|"MicrosoftCustomerAgreement"|
                           "MicrosoftPartnerAgreement"|"MicrosoftOnlineServicesProgram",
         "billing_account_name": str,
         "billing_profile_name": str | None}   # profile only for MCA/MPA
    or None if no billing account is readable (e.g. SPN lacks a billing role, or
    a CSP customer whose subscription sits under the partner's billing account).

    https://learn.microsoft.com/en-us/rest/api/billing/billing-accounts/list
    """
    _, arm_base = cloud_endpoints(rh)
    if token is None:
        token = get_handler_token(rh)
    try:
        data = _arm_get(
            arm_base,
            "/providers/Microsoft.Billing/billingAccounts",
            token,
            params={"api-version": BILLING_ACCOUNTS_API},
        )
    except requests.HTTPError as exc:
        status = getattr(exc.response, "status_code", "?")
        body = _snip(getattr(exc.response, "text", ""))
        logger.warning(f"[azure_pricing] billingAccounts list returned {status} "
                       f"for handler {rh.id} (sub {subscription_id_for(rh)}): "
                       f"{body}. The handler service principal likely lacks a "
                       f"billing-scope role (EA EnrollmentReader / MCA Billing "
                       f"Profile Reader); will use retail.")
        return None
    except Exception as exc:
        logger.warning(f"[azure_pricing] Billing-account lookup failed for "
                       f"handler {rh.id}: {exc!r}")
        return None

    all_accounts = data.get("value", [])
    detail = ", ".join(
        f"{a.get('name')}[{a.get('properties', {}).get('agreementType')},"
        f"read={a.get('properties', {}).get('hasReadAccess')}]"
        for a in all_accounts
    ) or "none"
    accounts = [a for a in all_accounts
                if a.get("properties", {}).get("hasReadAccess")]
    logger.info(f"[azure_pricing] handler {rh.id} (sub {subscription_id_for(rh)}): "
                f"{len(all_accounts)} billing account(s) visible [{detail}]; "
                f"{len(accounts)} with read access.")
    if not accounts:
        logger.info(f"[azure_pricing] handler {rh.id}: no readable billing "
                    f"account (SPN needs a billing-scope role granted); "
                    f"negotiated pricing unavailable, will use retail.")
        return None

    sub_id = subscription_id_for(rh)
    # Prefer the account that actually owns this subscription; fall back to the
    # sole readable account if the mapping can't be resolved.
    chosen = None
    if len(accounts) == 1:
        chosen = accounts[0]
    else:
        for acct in accounts:
            if _account_contains_subscription(arm_base, acct["name"], sub_id, token):
                chosen = acct
                break
        if chosen is None:
            logger.info("[azure_pricing] Multiple billing accounts readable but "
                        "none resolved to this subscription; skipping negotiated "
                        "pricing.")
            return None

    props = chosen.get("properties", {})
    agreement = props.get("agreementType")
    scope = {
        "agreement_type": agreement,
        "billing_account_name": chosen["name"],
        "billing_profile_name": None,
    }
    if agreement in ("MicrosoftCustomerAgreement", "MicrosoftPartnerAgreement"):
        scope["billing_profile_name"] = _billing_profile_for(
            arm_base, chosen["name"], sub_id, token
        )
        if not scope["billing_profile_name"]:
            logger.warning(f"[azure_pricing] handler {rh.id}: {agreement} billing "
                           f"account {chosen['name']} but could not resolve a "
                           f"billing profile for sub {sub_id} (and not exactly one "
                           f"profile); cannot build an MCA price-sheet URI.")
    logger.info(f"[azure_pricing] handler {rh.id}: selected billing account "
                f"{chosen['name']} (agreement={agreement}, "
                f"profile={scope['billing_profile_name']}).")
    return scope


def _account_contains_subscription(arm_base, account_name, sub_id, token):
    """Best-effort: does this billing account list our subscription?"""
    if not sub_id:
        return False
    try:
        data = _arm_get(
            arm_base,
            f"/providers/Microsoft.Billing/billingAccounts/{account_name}/billingSubscriptions",
            token,
            params={"api-version": BILLING_ACCOUNTS_API},
        )
    except Exception:
        return False
    for item in data.get("value", []):
        props = item.get("properties", {})
        if (props.get("subscriptionId") == sub_id
                or item.get("name") == sub_id):
            return True
    return False


def _billing_profile_for(arm_base, account_name, sub_id, token):
    """Resolve the MCA/MPA billing profile name for this subscription, or the
    sole profile if the subscription can't be matched."""
    try:
        data = _arm_get(
            arm_base,
            f"/providers/Microsoft.Billing/billingAccounts/{account_name}/billingSubscriptions",
            token,
            params={"api-version": BILLING_ACCOUNTS_API},
        )
        for item in data.get("value", []):
            props = item.get("properties", {})
            if props.get("subscriptionId") == sub_id or item.get("name") == sub_id:
                name = props.get("billingProfileName") or props.get("billingProfileId")
                if name:
                    return name.split("/")[-1]
    except Exception as exc:
        logger.debug(f"[azure_pricing] billingSubscriptions lookup failed: {exc}")
    # Fall back to the only billing profile if there is exactly one.
    try:
        data = _arm_get(
            arm_base,
            f"/providers/Microsoft.Billing/billingAccounts/{account_name}/billingProfiles",
            token,
            params={"api-version": BILLING_ACCOUNTS_API},
        )
        profiles = data.get("value", [])
        if len(profiles) == 1:
            return profiles[0]["name"]
    except Exception as exc:
        logger.debug(f"[azure_pricing] billingProfiles lookup failed: {exc}")
    return None


def current_ea_billing_period():
    """EA Price Sheet URIs take a billing-period name. The Web-Direct
    billingPeriods API does not apply to EA, so derive the current period as
    YYYYMM (the EA enrollment convention). Verify against the tenant if a sheet
    download 404s — some enrollments use a different period label."""
    return time.strftime("%Y%m")


# =============================================================================
# Price Sheet download + parse (negotiated)
# =============================================================================
def _price_sheet_download_uri(scope):
    """Build the right async Price Sheet download POST path for the scope."""
    agreement = scope.get("agreement_type")
    account = scope.get("billing_account_name")
    if agreement == "EnterpriseAgreement":
        period = current_ea_billing_period()
        return (f"/providers/Microsoft.Billing/billingAccounts/{account}"
                f"/billingPeriods/{period}/providers/Microsoft.CostManagement"
                f"/pricesheets/default/download")
    if agreement in ("MicrosoftCustomerAgreement", "MicrosoftPartnerAgreement"):
        profile = scope.get("billing_profile_name")
        if not profile:
            return None
        return (f"/providers/Microsoft.Billing/billingAccounts/{account}"
                f"/billingProfiles/{profile}/providers/Microsoft.CostManagement"
                f"/pricesheets/default/download")
    return None


def download_price_sheet(rh, scope, token=None):
    """
    Run the async Price Sheet download (POST -> 202 + Location poll -> 200 with a
    SAS downloadUrl), fetch the file, and parse it into {meter_id: (Decimal
    unitPrice, str unitOfMeasure)}. Returns the parsed map, or None on failure.

    Negotiated unit price is the `unitPrice` column (inclusive of negotiated
    discounts); `marketPrice` is list. We keep only Compute/Storage/Networking
    rows to bound the cache size.
    """
    _, arm_base = cloud_endpoints(rh)
    path = _price_sheet_download_uri(scope)
    if not path:
        logger.warning(f"[azure_pricing] handler {rh.id}: cannot build a Price "
                       f"Sheet URI for scope {scope} (agreement unsupported or "
                       f"missing billing profile).")
        return None
    if token is None:
        token = get_handler_token(rh)
    headers = {"Authorization": f"Bearer {token}"}
    url = f"{arm_base}{path}"
    logger.info(f"[azure_pricing] handler {rh.id}: requesting "
                f"{scope.get('agreement_type')} price sheet — POST {url} "
                f"(api-version={PRICE_SHEET_API}).")
    try:
        resp = requests.post(url, headers=headers,
                             params={"api-version": PRICE_SHEET_API},
                             timeout=REQUEST_TIMEOUT)
    except Exception as exc:
        logger.warning(f"[azure_pricing] handler {rh.id}: Price Sheet POST "
                       f"raised: {exc!r}")
        return None

    logger.info(f"[azure_pricing] handler {rh.id}: Price Sheet POST returned "
                f"{resp.status_code}.")
    download_url = _poll_for_download_url(resp, headers, rh_id=rh.id)
    if not download_url:
        logger.warning(f"[azure_pricing] handler {rh.id}: Price Sheet did not "
                       f"yield a download URL (see status/body logged above).")
        return None
    return _fetch_and_parse_sheet(download_url, rh_id=rh.id)


def _find_download_url(obj):
    """Recursively find the first non-empty `downloadUrl` string anywhere in a
    nested dict/list. Cost Management's async result wraps the entity in a
    `publishedEntity.properties.downloadUrl` envelope, and the exact nesting has
    varied across api-versions, so we search rather than assume a fixed path."""
    if isinstance(obj, dict):
        val = obj.get("downloadUrl")
        if isinstance(val, str) and val:
            return val
        for v in obj.values():
            found = _find_download_url(v)
            if found:
                return found
    elif isinstance(obj, list):
        for item in obj:
            found = _find_download_url(item)
            if found:
                return found
    return None


def _next_poll_url(resp, body):
    """Where to poll next: response headers first, then the Cost Management
    operation-results envelope fields in the body (`asyncHeaderUri` /
    `locationHeaderUri`)."""
    url = resp.headers.get("Location") or resp.headers.get("Azure-AsyncOperation")
    if url:
        return url
    if isinstance(body, dict):
        return body.get("asyncHeaderUri") or body.get("locationHeaderUri")
    return None


def _poll_for_download_url(resp, headers, rh_id="?", max_wait=PRICE_SHEET_MAX_WAIT):
    """Resolve the async Price Sheet operation to a SAS download URL. Robust to
    the response shape variants: the URL may be top-level, under `properties`, or
    nested in a `publishedEntity.properties` envelope; the poll continuation may
    come via the Location/Azure-AsyncOperation headers OR the body's
    asyncHeaderUri/locationHeaderUri. Logs status/body at each step and is bounded
    by max_wait."""
    def outcome(response):
        """Return ('url', <url>) | ('error', <msg>) | ('pending', None) for a
        response, based on its parsed body."""
        body = _safe_json(response)
        url = _find_download_url(body)
        if url:
            return "url", url
        err = body.get("errorMessage") if isinstance(body, dict) else None
        if err:
            return "error", err
        return "pending", None

    # The initial POST response may already carry the result envelope.
    if resp.status_code not in (200, 202):
        logger.warning(f"[azure_pricing] handler {rh_id}: Price Sheet POST "
                       f"{resp.status_code}: {_snip(resp.text)}")
        return None
    state, value = outcome(resp)
    if state == "url":
        return value
    if state == "error":
        logger.warning(f"[azure_pricing] handler {rh_id}: Price Sheet operation "
                       f"error: {value}")
        return None

    poll_url = _next_poll_url(resp, _safe_json(resp))
    if not poll_url:
        logger.warning(f"[azure_pricing] handler {rh_id}: Price Sheet accepted "
                       f"({resp.status_code}) but no downloadUrl and no poll URL in "
                       f"headers/body: {_snip(resp.text)}")
        return None

    waited = 0
    interval = min(_retry_after(resp, PRICE_SHEET_POLL_INTERVAL),
                   PRICE_SHEET_POLL_INTERVAL_MAX)
    while waited < max_wait:
        time.sleep(interval)
        waited += interval
        try:
            poll = requests.get(poll_url, headers=headers, timeout=REQUEST_TIMEOUT)
        except Exception as exc:
            logger.warning(f"[azure_pricing] handler {rh_id}: Price Sheet poll "
                           f"raised: {exc!r}")
            return None
        if poll.status_code not in (200, 202):
            logger.warning(f"[azure_pricing] handler {rh_id}: Price Sheet poll "
                           f"{poll.status_code}: {_snip(poll.text)}")
            return None
        state, value = outcome(poll)
        if state == "url":
            logger.info(f"[azure_pricing] handler {rh_id}: price sheet ready after "
                        f"{waited}s.")
            return value
        if state == "error":
            logger.warning(f"[azure_pricing] handler {rh_id}: Price Sheet operation "
                           f"error: {value}")
            return None
        poll_url = _next_poll_url(poll, _safe_json(poll)) or poll_url
        interval = min(_retry_after(poll, interval), PRICE_SHEET_POLL_INTERVAL_MAX)
        logger.info(f"[azure_pricing] handler {rh_id}: price sheet still generating "
                    f"({poll.status_code}, waited {waited}s of {max_wait}s).")
    logger.warning(f"[azure_pricing] handler {rh_id}: Price Sheet generation timed "
                   f"out after {max_wait}s.")
    return None


def _fetch_and_parse_sheet(download_url, rh_id="?"):
    try:
        resp = requests.get(download_url, timeout=120)
        resp.raise_for_status()
    except Exception as exc:
        logger.warning(f"[azure_pricing] handler {rh_id}: Price Sheet file "
                       f"download failed: {exc!r}")
        return None
    content = resp.content
    is_zip = content[:2] == b"PK"
    logger.info(f"[azure_pricing] handler {rh_id}: downloaded price sheet "
                f"({len(content)} bytes, {'zip' if is_zip else 'csv/raw'}, "
                f"content-type={resp.headers.get('Content-Type', '?')}).")
    meters = {}
    try:
        if is_zip:
            with zipfile.ZipFile(io.BytesIO(content)) as zf:
                infos = zf.infolist()
                # Log EVERY member + size so the real format is visible (members
                # may be .csv, .csv.gz, or .json — not just .csv).
                logger.info(f"[azure_pricing] handler {rh_id}: zip members="
                            f"{[(i.filename, i.file_size) for i in infos]}")
                for info in infos:
                    _parse_zip_member(zf, info.filename, meters, rh_id)
        else:
            _parse_sheet_csv(
                io.StringIO(content.decode("utf-8-sig", errors="replace")), meters,
                rh_id=rh_id)
    except Exception as exc:
        logger.warning(f"[azure_pricing] handler {rh_id}: Price Sheet parse "
                       f"failed: {exc!r}")
        _persist_raw_sheet(content, rh_id)
        return None
    logger.info(f"[azure_pricing] handler {rh_id}: parsed {len(meters)} negotiated "
                f"meters from the sheet.")
    if not meters:
        # Nothing extracted though a sheet downloaded — save the raw file so its
        # real structure can be inspected (or handed off) rather than lost.
        _persist_raw_sheet(content, rh_id)
    return meters or None


def _parse_zip_member(zf, name, meters, rh_id):
    """Parse one zip member into `meters`, handling .csv, .csv.gz, and .json."""
    lower = name.lower()
    try:
        with zf.open(name) as fh:
            if lower.endswith(".csv"):
                _parse_sheet_csv(io.TextIOWrapper(fh, encoding="utf-8-sig"),
                                 meters, rh_id=rh_id)
            elif lower.endswith(".csv.gz") or lower.endswith(".gz"):
                with gzip.open(fh, mode="rt", encoding="utf-8-sig") as gz:
                    _parse_sheet_csv(gz, meters, rh_id=rh_id)
            elif lower.endswith(".json"):
                _parse_sheet_json(fh.read(), meters, rh_id=rh_id)
            else:
                logger.info(f"[azure_pricing] handler {rh_id}: skipping zip member "
                            f"of unknown type: {name!r}")
    except Exception as exc:
        logger.warning(f"[azure_pricing] handler {rh_id}: failed parsing member "
                       f"{name!r}: {exc!r}")


def _parse_sheet_json(raw_bytes, meters, rh_id="?"):
    """Best-effort parse of a JSON price-sheet member into `meters`. Handles a
    top-level array, common wrappers ({pricesheets|Items|value|items: [...]}), or
    JSON Lines. Field names are matched case-insensitively."""
    text = raw_bytes.decode("utf-8-sig", errors="replace") if isinstance(raw_bytes, bytes) else raw_bytes
    rows = None
    try:
        data = json.loads(text)
        if isinstance(data, list):
            rows = data
        elif isinstance(data, dict):
            for key in ("pricesheets", "Items", "items", "value"):
                if isinstance(data.get(key), list):
                    rows = data[key]
                    break
    except Exception:
        rows = []
        for line in text.splitlines():
            line = line.strip()
            if line:
                try:
                    rows.append(json.loads(line))
                except Exception:
                    continue
    if not rows:
        logger.warning(f"[azure_pricing] handler {rh_id}: JSON member had no "
                       f"recognizable rows (structure not [list] / wrapped / JSONL).")
        return
    kept = 0
    for row in rows:
        if not isinstance(row, dict):
            continue
        low = {k.lower().replace(" ", "").replace("_", ""): v for k, v in row.items()}
        if not _keep_row(low.get("pricetype"), low.get("tierminimumunits")):
            continue
        mid = low.get("meterid")
        if not mid:
            continue
        mid = str(mid).strip()
        if mid in meters:
            continue  # keep first base-consumption row per meter
        price = _to_decimal(low.get("unitprice"))
        if price is None:
            continue
        meters[mid] = (price, str(low.get("unitofmeasure") or ""))
        kept += 1
    logger.info(f"[azure_pricing] handler {rh_id}: JSON member -> kept {kept} "
                f"base-Consumption meter(s) of {len(rows)} row(s).")


def _persist_raw_sheet(content, rh_id):
    """Save the raw downloaded sheet to disk so its structure can be inspected
    when parsing yields nothing. Overwrites per handler."""
    try:
        path = os.path.join(_cache_dir(), f"rawsheet_handler_{rh_id}.zip")
        with open(path, "wb") as fh:
            fh.write(content)
        logger.warning(f"[azure_pricing] handler {rh_id}: saved raw downloaded sheet "
                       f"to {path} ({len(content)} bytes) for inspection.")
    except Exception as exc:
        logger.warning(f"[azure_pricing] handler {rh_id}: could not save raw sheet: "
                       f"{exc!r}")


# Service families worth caching for VM cost previews.
_RELEVANT_FAMILIES = {"compute", "storage", "networking"}


def _keep_row(price_type, tier_min):
    """Keep only on-demand Consumption rows at the base tier (tierMinimumUnits 0).

    The MCA/EA price sheet lists each meterId MANY times — Consumption,
    ReservedInstance, Savings Plan, and multiple pricing tiers. Without this
    filter, a last-write-wins dict keeps an arbitrary row (often a reservation
    total or a high tier), producing wildly wrong per-VM costs."""
    pt = str(price_type or "").strip().lower()
    if pt and pt != "consumption":
        return False
    if tier_min not in (None, ""):
        try:
            if float(tier_min) > 0:
                return False
        except (TypeError, ValueError):
            pass
    return True


def _parse_sheet_csv(text_stream, meters, rh_id="?"):
    """Parse a price-sheet CSV (header names vary by schema version), keeping the
    base on-demand price per meterId as {meter_id: (Decimal unitPrice, uom)}."""
    reader = csv.DictReader(text_stream)
    if not reader.fieldnames:
        logger.warning(f"[azure_pricing] handler {rh_id}: price-sheet CSV had no "
                       f"header row.")
        return
    cols = {name.lower().replace(" ", "").replace("_", ""): name
            for name in reader.fieldnames}
    c_meter = cols.get("meterid")
    c_price = cols.get("unitprice")
    c_uom = cols.get("unitofmeasure")
    c_pt = cols.get("pricetype")
    c_tier = cols.get("tierminimumunits")
    if not (c_meter and c_price):
        logger.warning(f"[azure_pricing] handler {rh_id}: price-sheet CSV missing "
                       f"meterId/unitPrice columns. Header was: "
                       f"{reader.fieldnames}.")
        return
    rows = kept = 0
    for row in reader:
        rows += 1
        if not _keep_row(row.get(c_pt) if c_pt else None,
                         row.get(c_tier) if c_tier else None):
            continue
        meter_id = (row.get(c_meter) or "").strip()
        if not meter_id or meter_id in meters:
            continue  # keep first base-consumption row per meter
        price = _to_decimal(row.get(c_price))
        if price is None:
            continue
        uom = (row.get(c_uom) or "").strip() if c_uom else ""
        meters[meter_id] = (price, uom)
        kept += 1
    logger.info(f"[azure_pricing] handler {rh_id}: CSV had {rows} rows; kept "
                f"{kept} base-Consumption meter(s).")


# =============================================================================
# On-disk cache (durable across restarts/evictions)
# =============================================================================
def _cache_dir():
    proserv = getattr(settings, "PROSERV_DIR", "/var/opt/cloudbolt/proserv/")
    path = os.path.join(proserv, "azure_pricing_cache")
    os.makedirs(path, exist_ok=True)
    return path


def cache_file_for(rh):
    return os.path.join(_cache_dir(), f"pricesheet_{subscription_id_for(rh)}.json")


def write_price_cache(rh, scope, meters, currency=None):
    """Persist the parsed negotiated sheet for this subscription."""
    payload = {
        "subscription_id": subscription_id_for(rh),
        "agreement_type": scope.get("agreement_type"),
        "billing_account_name": scope.get("billing_account_name"),
        "billing_profile_name": scope.get("billing_profile_name"),
        "currency": currency,
        "fetched_at": time.time(),
        # JSON can't key by tuple; store meter_id -> [price_str, uom].
        "meters": {mid: [str(p), uom] for mid, (p, uom) in meters.items()},
    }
    path = cache_file_for(rh)
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(payload, fh)
    os.replace(tmp, path)
    logger.info(f"[azure_pricing] Wrote negotiated cache: {path} "
                f"({len(meters)} meters).")


def load_negotiated_meters(rh, max_age_days=35):
    """Return (meters_map, meta) for this subscription from the on-disk cache,
    or (None, None) if absent/stale. meters_map: {meter_id: (Decimal, uom)}.
    Uses a process-level memo keyed by file mtime to avoid re-parsing a large
    file on every cost preview. Default staleness window spans one billing
    period plus slack."""
    path = cache_file_for(rh)
    if not os.path.exists(path):
        return None, None
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return None, None
    sub_id = subscription_id_for(rh)
    memo = _PRICE_SHEET_MEMO.get(sub_id)
    if memo and memo[0] == mtime:
        meters, meta = memo[1], memo[2]
    else:
        try:
            with open(path) as fh:
                payload = json.load(fh)
        except Exception as exc:
            logger.warning(f"[azure_pricing] Failed to read cache {path}: {exc}")
            return None, None
        meters = {}
        for mid, val in payload.get("meters", {}).items():
            price = _to_decimal(val[0])
            if price is not None:
                meters[mid] = (price, val[1] if len(val) > 1 else "")
        meta = {k: payload.get(k) for k in
                ("agreement_type", "billing_account_name",
                 "billing_profile_name", "currency", "fetched_at")}
        _PRICE_SHEET_MEMO[sub_id] = (mtime, meters, meta)
    if max_age_days and meta.get("fetched_at"):
        if (time.time() - meta["fetched_at"]) > max_age_days * 86400:
            logger.info(f"[azure_pricing] Negotiated cache for {sub_id} is stale.")
            return None, None
    return meters, meta


# =============================================================================
# Retail Prices API (public list prices)
# =============================================================================
def retail_query(filter_str, currency="USD"):
    """Query the Azure Retail Prices API. Returns a list of price items (first
    page only — sufficient for our narrow filters). Public commercial cloud
    only."""
    params = {"api-version": RETAIL_API_VERSION, "$filter": filter_str}
    if currency and currency != "USD":
        params["currencyCode"] = currency
    try:
        resp = requests.get(RETAIL_PRICES_URL, params=params, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        return resp.json().get("Items", [])
    except Exception as exc:
        logger.warning(f"[azure_pricing] Retail query failed ({filter_str}): {exc}")
        return []


# =============================================================================
# Unit normalization
# =============================================================================
def _hourly_from_uom(unit_price, unit_of_measure):
    """Convert a unit price to a per-hour figure based on its unitOfMeasure.
    Compute/IP/license meters are '1 Hour'; managed disks are '1/Month'."""
    uom = (unit_of_measure or "").lower()
    if "month" in uom:
        return unit_price / HOURS_PER_MONTH
    # '1 hour', 'hour', or unknown -> treat as hourly (the dominant VM case).
    return unit_price


def normalize_to_time_unit(unit_price, unit_of_measure, rate_time_unit):
    """Convert an Azure unit price (with its native unitOfMeasure) into the
    CloudBolt global rate time unit."""
    hourly = _hourly_from_uom(unit_price, unit_of_measure)
    return hourly * HOURS_PER_UNIT.get(rate_time_unit, HOURS_PER_MONTH)


def _to_decimal(value):
    try:
        if value is None or value == "":
            return None
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None


# =============================================================================
# Disk tier mapping
# =============================================================================
def _tier_for_size(prefix, size_gb):
    """Return the smallest tier code (e.g. 'P10') whose capacity >= size_gb."""
    ladder = _DISK_LADDER.get(prefix)
    if not ladder or not size_gb:
        return None
    for capacity, code in ladder:
        if size_gb <= capacity:
            return code
    return ladder[-1][1]  # larger than the top tier -> use the top tier


# =============================================================================
# vCPU count (for RHEL/SLES per-vCPU license band)
# =============================================================================
def get_vcpu_count(rh, region, arm_sku):
    """Return the vCPU count for an Azure VM size, via the Compute Resource SKUs
    API (reusing the handler wrapper's compute client). Cached in django cache.
    https://learn.microsoft.com/en-us/rest/api/compute/resource-skus/list"""
    from django.core.cache import cache
    key = f"azure_vcpu_{region}_{arm_sku}".replace(" ", "_")
    cached = cache.get(key)
    if cached is not None:
        return cached or None
    try:
        wrapper = rh.get_api_wrapper()
        skus = wrapper.compute_client.resource_skus.list(
            filter=f"location eq '{region}'"
        )
        for sku in skus:
            if sku.name == arm_sku and sku.resource_type == "virtualMachines":
                for cap in (sku.capabilities or []):
                    if cap.name == "vCPUs":
                        count = int(cap.value)
                        cache.set(key, count, 86400)
                        return count
    except Exception as exc:
        logger.warning(f"[azure_pricing] vCPU lookup failed for {arm_sku}: {exc}")
    cache.set(key, 0, 3600)
    return None


def _rhel_band_for_vcpus(vcpus):
    """Map a vCPU count to the RHEL/SLES license sku band string used by the
    Retail/Price Sheet license meters (e.g. '1-4 vCPU VM')."""
    if not vcpus or vcpus <= 4:
        return "1-4 vCPU VM"
    if vcpus <= 8:
        return "5 or more vCPU VM"  # verify exact band labels against live API
    return "5 or more vCPU VM"


# =============================================================================
# Per-line price resolution: retail first to get (price, meterId, uom), then
# override with the negotiated unitPrice when that meterId is in the sheet.
# =============================================================================
def _pick_negotiated(meter_id, negotiated):
    if negotiated and meter_id and meter_id in negotiated:
        price, uom = negotiated[meter_id]
        return price, uom
    return None


def resolve_compute(rh, region, arm_sku, is_windows, ahb, currency, negotiated):
    """Return (price, uom, basis, label) for the VM compute meter, or None.
    Windows license is bundled in the Windows meter unless AHB is in effect."""
    want_windows = is_windows and not ahb
    items = retail_query(
        f"serviceName eq 'Virtual Machines' and armRegionName eq '{region}' "
        f"and armSkuName eq '{arm_sku}' and priceType eq 'Consumption'",
        currency,
    )
    best = None
    for it in items:
        name = (it.get("meterName") or "") + (it.get("skuName") or "")
        if "Spot" in name or "Low Priority" in name:
            continue
        has_win = "Windows" in (it.get("productName") or "")
        if want_windows and not has_win:
            continue
        if not want_windows and has_win:
            continue
        best = it
        break
    if not best:
        return None
    meter_id = best.get("meterId")
    neg = _pick_negotiated(meter_id, negotiated)
    if neg:
        logger.info(f"[azure_pricing] compute {arm_sku}: NEGOTIATED meter {meter_id} "
                    f"= {neg[0]} per {neg[1]!r}.")
        return neg[0], neg[1], "negotiated", "Node Cost"
    logger.info(f"[azure_pricing] compute {arm_sku}: RETAIL {best.get('retailPrice')} "
                f"per {best.get('unitOfMeasure')!r} (meter {meter_id}).")
    return (_to_decimal(best.get("retailPrice")),
            best.get("unitOfMeasure", "1 Hour"), "list", "Node Cost")


def resolve_disk(rh, region, disk_type, size_gb, currency, negotiated):
    """Return (price, uom, basis, note) for a managed disk, or None. Dispatches by
    pricing model: tiered per-disk (P/E/S) vs per-GiB provisioned capacity (v2 /
    Ultra). `note` is a label suffix ('' or 'capacity only')."""
    if disk_type in _DISK_CAPACITY:
        return _resolve_capacity_disk(rh, region, disk_type, size_gb, currency, negotiated)
    if disk_type in _DISK_TIERED:
        return _resolve_tiered_disk(rh, region, disk_type, size_gb, currency, negotiated)
    logger.warning(f"[azure_pricing] disk: unmapped storage type {disk_type!r} "
                   f"(size={size_gb}). Known: {sorted(list(_DISK_TIERED) + list(_DISK_CAPACITY))}. "
                   f"Skipping this disk.")
    return None


def _resolve_tiered_disk(rh, region, disk_type, size_gb, currency, negotiated):
    """Per-disk, per-month tiered pricing (Premium/Standard SSD, Standard HDD).
    skuName carries the redundancy suffix ('P10 LRS') and the base meter is
    '<sku> Disk' (not '<sku> Disk Mount')."""
    product, prefix, redundancy = _DISK_TIERED[disk_type]
    tier = _tier_for_size(prefix, size_gb)
    if not tier:
        logger.warning(f"[azure_pricing] disk: no tier for {disk_type} size="
                       f"{size_gb}; skipping.")
        return None
    sku = f"{tier} {redundancy}"          # e.g. "P10 LRS"
    meter = f"{sku} Disk"                  # e.g. "P10 LRS Disk"
    items = retail_query(
        f"serviceName eq 'Storage' and armRegionName eq '{region}' "
        f"and productName eq '{product}' and skuName eq '{sku}' "
        f"and meterName eq '{meter}' and priceType eq 'Consumption'",
        currency,
    )
    logger.info(f"[azure_pricing] disk: {disk_type} {size_gb}GB -> {product} / "
                f"{sku} / meter {meter!r} in {region}: {len(items)} row(s).")
    best = items[0] if items else None
    if not best:
        _probe_disk_meters(region, product, sku, currency)
        return None
    meter_id = best.get("meterId")
    neg = _pick_negotiated(meter_id, negotiated)
    if neg:
        logger.info(f"[azure_pricing] disk {sku}: NEGOTIATED meter {meter_id} = "
                    f"{neg[0]} per {neg[1]!r}.")
        return neg[0], neg[1], "negotiated", ""
    logger.info(f"[azure_pricing] disk {sku}: RETAIL {best.get('retailPrice')} per "
                f"{best.get('unitOfMeasure')!r} (meter {meter_id}).")
    return _to_decimal(best.get("retailPrice")), best.get("unitOfMeasure", "1/Month"), "list", ""


def _resolve_capacity_disk(rh, region, disk_type, size_gb, currency, negotiated):
    """Per-provisioned-GiB capacity pricing (Premium SSD v2). Only the capacity
    component is estimated; provisioned IOPS/throughput default to the free
    baseline and have no order params. The per-GiB rate is multiplied by size and
    returned as a plain hourly price for the whole disk."""
    product, sku, meter, note = _DISK_CAPACITY[disk_type]
    if not size_gb:
        return None
    items = retail_query(
        f"serviceName eq 'Storage' and armRegionName eq '{region}' "
        f"and productName eq '{product}' and skuName eq '{sku}' "
        f"and meterName eq '{meter}' and priceType eq 'Consumption'",
        currency,
    )
    logger.info(f"[azure_pricing] disk: {disk_type} {size_gb}GB -> {product} "
                f"capacity meter {meter!r} in {region}: {len(items)} row(s).")
    best = items[0] if items else None
    if not best:
        _probe_disk_meters(region, product, None, currency)
        return None
    meter_id = best.get("meterId")
    neg = _pick_negotiated(meter_id, negotiated)
    basis = "negotiated" if neg else "list"
    per_gib = neg[0] if neg else _to_decimal(best.get("retailPrice"))
    if per_gib is None:
        return None
    total = per_gib * Decimal(size_gb)  # per-GiB/hour -> whole-disk/hour
    excluded = ("IOPS/throughput MANDATORY for Ultra and NOT estimated — this is a "
                "capacity-only floor" if note
                else "IOPS/throughput not estimated — free baseline assumed")
    logger.info(f"[azure_pricing] disk {product} ({basis}): {size_gb} GiB x "
                f"{per_gib}/GiB/hr = {total}/hr (meter {meter_id}; {excluded}).")
    return total, "1 Hour", basis, note


def _probe_disk_meters(region, product, sku, currency):
    """Diagnostic: when a disk meter isn't found, log the skus/meters Azure
    actually exposes for that product so the mismatch is visible."""
    probe = retail_query(
        f"serviceName eq 'Storage' and armRegionName eq '{region}' "
        f"and productName eq '{product}' and priceType eq 'Consumption'",
        currency,
    )
    sample = sorted({
        f"skuName={i.get('skuName')!r}/meterName={i.get('meterName')!r}"
        for i in probe
    })[:25]
    logger.warning(f"[azure_pricing] disk: no meter matched for {product!r} "
                   f"(sku {sku!r}) in {region}. Broad product query returned "
                   f"{len(probe)} row(s); available: {sample}.")


def resolve_public_ip(rh, region, currency, negotiated):
    """Return (price, uom, basis) for a Standard static public IPv4, or None."""
    items = retail_query(
        f"serviceName eq 'Virtual Network' and armRegionName eq '{region}' "
        f"and productName eq 'IP Addresses' and skuName eq 'Standard' "
        f"and priceType eq 'Consumption'",
        currency,
    )
    best = None
    for it in items:
        mn = it.get("meterName") or ""
        if "Static" in mn and "IPv4" in mn:
            best = it
            break
    if not best:
        return None
    meter_id = best.get("meterId")
    neg = _pick_negotiated(meter_id, negotiated)
    if neg:
        return neg[0], neg[1], "negotiated"
    return _to_decimal(best.get("retailPrice")), best.get("unitOfMeasure", "1 Hour"), "list"


def resolve_os_license(rh, region, product_name, vcpus, currency, negotiated):
    """Return (price, uom, basis) for a separate OS license meter (RHEL/SLES),
    selecting the per-vCPU band, or None. product_name e.g. 'Red Hat Enterprise
    Linux'."""
    band = _rhel_band_for_vcpus(vcpus)
    items = retail_query(
        f"serviceName eq 'Virtual Machines Licenses' "
        f"and productName eq '{product_name}' and priceType eq 'Consumption'",
        currency,
    )
    best = None
    for it in items:
        if it.get("skuName") == band:
            best = it
            break
    best = best or (items[0] if items else None)
    if not best:
        return None
    meter_id = best.get("meterId")
    neg = _pick_negotiated(meter_id, negotiated)
    if neg:
        return neg[0], neg[1], "negotiated"
    return _to_decimal(best.get("retailPrice")), best.get("unitOfMeasure", "1 Hour"), "list"


def _os_license_product(os_build):
    """Detect a separately-metered OS license from the OS build, or None.
    Generic Linux (Ubuntu/Debian/CentOS/etc.) has no separate Azure license."""
    try:
        name = (getattr(os_build, "name", "") or "").lower()
    except Exception:
        return None
    if "red hat" in name or "rhel" in name:
        return "Red Hat Enterprise Linux"
    if "suse" in name or "sles" in name:
        return "SUSE Linux Enterprise Server"
    return None


# =============================================================================
# High-level: assemble cost lines for a VM, normalized to the global time unit
# =============================================================================
def get_vm_cost_components(rh, region, arm_sku, is_windows, os_build, disks,
                           has_public_ip, ahb, quantity, rate_time_unit,
                           currency="USD"):
    """
    Return a dict:
        {"basis": "negotiated"|"list"|"none",
         "hardware": {label: Decimal, ...},   # node, disks, public IP
         "software": {label: Decimal, ...}}   # RHEL/SLES license
    All values are already scaled to (rate_time_unit * quantity). Disks is a
    list of (disk_type, size_gb, label) tuples (e.g. "OS Disk", "Data Disk 1"),
    each priced by its provisioned tier for the given disk_type.

    Negotiated prices are used per-meter when available; any line that falls
    back to list price is labelled '… (list price)' so the basis is visible.
    """
    negotiated, meta = load_negotiated_meters(rh)
    agreement_type = (meta or {}).get("agreement_type")
    # Retail is only available on commercial cloud; on sovereign clouds we can
    # only use negotiated data.
    retail_ok = is_commercial_cloud(rh)
    if negotiated is None and not retail_ok:
        return {"basis": "none", "agreement_type": agreement_type,
                "hardware": {}, "software": {}}

    qty = Decimal(quantity or 1)
    hardware, software = {}, {}
    used_negotiated = False
    used_list = False

    def scale(price, uom):
        return normalize_to_time_unit(price, uom, rate_time_unit) * qty

    def tag(label, basis):
        nonlocal used_negotiated, used_list
        if basis == "negotiated":
            used_negotiated = True
            return label
        used_list = True
        # Only mark as list price when negotiated data was expected (EA/MCA).
        return f"{label} (list price)" if negotiated is not None else label

    # --- Compute (Node) ---
    comp = resolve_compute(rh, region, arm_sku, is_windows, ahb, currency, negotiated)
    if comp:
        price, uom, basis, label = comp
        if price is not None:
            hardware[tag(label, basis)] = scale(price, uom)

    # --- Disks (OS + data), each labelled by the caller ---
    for disk in (disks or []):
        disk_type, size_gb, disk_label = disk
        d = resolve_disk(rh, region, disk_type, size_gb, currency, negotiated)
        if not d or d[0] is None:
            continue
        price, uom, basis, note = d
        label = f"{disk_label} ({note})" if note else disk_label
        hardware[tag(label, basis)] = scale(price, uom)

    # --- Public IP ---
    if has_public_ip:
        ip = resolve_public_ip(rh, region, currency, negotiated)
        if ip and ip[0] is not None:
            price, uom, basis = ip
            hardware[tag("Public IP", basis)] = scale(price, uom)

    # --- OS license (RHEL/SLES) under Software, unless AHB/BYOS ---
    if not ahb:
        product = _os_license_product(os_build)
        if product:
            vcpus = get_vcpu_count(rh, region, arm_sku)
            lic = resolve_os_license(rh, region, product, vcpus, currency, negotiated)
            if lic and lic[0] is not None:
                price, uom, basis = lic
                label = product.split(" Linux")[0] + " License" \
                    if "Red Hat" in product else "SLES License"
                software[tag(label, basis)] = scale(price, uom)

    if not hardware and not software:
        basis = "none"
    elif used_negotiated and not used_list:
        basis = "negotiated"
    elif used_negotiated and used_list:
        basis = "mixed"
    else:
        basis = "list"
    logger.info(
        f"[azure_pricing] components for {arm_sku} in {region}: basis={basis}, "
        f"{len(disks or [])} disk(s) requested; hardware lines="
        f"{ {k: str(v) for k, v in hardware.items()} }; software lines="
        f"{ {k: str(v) for k, v in software.items()} }.")
    return {"basis": basis, "agreement_type": agreement_type,
            "hardware": hardware, "software": software}
