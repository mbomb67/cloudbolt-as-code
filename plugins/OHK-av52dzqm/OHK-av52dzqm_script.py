"""
CloudBolt discovery plugin for existing Azure subscriptions.

Walks every AzureARMHandler in the system; for each handler's service
principal, queries the subscriptions visible to it and emits a discovery
dict per subscription. CloudBolt's discovery framework deduplicates by
RESOURCE_IDENTIFIER, so subscriptions already represented in CloudBolt
(e.g., from the build plugin) are not duplicated -- the framework merges
on azure_subscription_id.

Per-RH errors are logged and skipped: one bad SP must not poison the
entire sweep. This matches the pattern in azure/key_vault/discover_key_vault.py
and aws/aws_account/discover_aws_accounts.py.

Notable v1 limitations (documented in DEPLOYMENT_GUIDE.md):

- azure_subscription_source_rh_id is intentionally NOT set on discovered
  subscriptions -- the cross-tenant build plugin stores this for stuck-alias
  cleanup, but discovery cannot recover it from a single GET /subscriptions
  call. Stuck-alias cleanup on discovered cross-tenant subscriptions
  requires manual intervention via Azure CLI.
- When the same subscription is visible to multiple Resource Handlers
  (shared-platform subscription invited as guest to several SPs), the first
  RH iterated wins -- AzureARMHandler.objects.all() is ordered by pk.
  Operators can adjust azure_subscription_rh_id manually post-discovery.
"""

import requests

from common.methods import set_progress
from resourcehandlers.azure_arm.models import AzureARMHandler
from utilities.logger import ThreadLogger

from shared_modules.azure_subscription_helpers import (
    AZURE_MGMT_ENDPOINT,
    azure_api_call,
    get_azure_token,
)

logger = ThreadLogger(__name__)

RESOURCE_IDENTIFIER = "azure_subscription_id"

SUBSCRIPTIONS_API_VERSION = "2022-12-01"


def discover_resources(**kwargs):
    discovered = []

    rhs = AzureARMHandler.objects.all()
    set_progress(
        f"Azure subscription discovery: scanning {rhs.count()} resource handler(s)"
    )

    seen_subscription_ids = set()

    for rh in rhs:
        try:
            tenant_id = getattr(rh, "azure_tenant_id", None) or getattr(
                rh, "tenant_id", None
            )
            if not tenant_id:
                logger.warning(
                    f"Resource handler {rh.name} (id={rh.id}) has no tenant_id; "
                    f"skipping"
                )
                continue

            set_progress(
                f"Listing subscriptions visible to {rh.name} (tenant {tenant_id})"
            )

            token = get_azure_token(tenant_id, rh.client_id, rh.secret)

            url = f"{AZURE_MGMT_ENDPOINT}/subscriptions"
            params = {"api-version": SUBSCRIPTIONS_API_VERSION}
            page = 0

            while url:
                page += 1
                response = azure_api_call("GET", url, token, params=params)
                if response.get("error") == "NotFound":
                    break

                for sub in response.get("value", []) or []:
                    subscription_id = sub.get("subscriptionId")
                    if not subscription_id:
                        continue

                    if subscription_id in seen_subscription_ids:
                        # Same subscription visible to multiple RHs; first wins.
                        logger.info(
                            f"Subscription {subscription_id} already emitted from "
                            f"a previous handler; skipping duplicate from {rh.name}"
                        )
                        continue
                    seen_subscription_ids.add(subscription_id)

                    display_name = sub.get("displayName") or subscription_id
                    discovered.append(
                        {
                            "name": display_name,
                            # RESOURCE_IDENTIFIER -- bare GUID, NOT the full
                            # /subscriptions/{guid} path. Must match what
                            # build_azure_subscription.py stores for the
                            # discovery framework's dedup to work.
                            "azure_subscription_id": subscription_id,
                            "azure_subscription_name": display_name,
                            # tenantId from the subscription itself, NOT the
                            # RH's tenant -- they differ for guest-invited
                            # subscriptions.
                            "azure_subscription_tenant_id": sub.get(
                                "tenantId", tenant_id
                            ),
                            "azure_subscription_state": sub.get("state", "Unknown"),
                            "azure_subscription_rh_id": str(rh.id),
                        }
                    )

                # Azure paginates via nextLink in the body, not a header.
                next_link = response.get("nextLink")
                if next_link:
                    url = next_link
                    # nextLink already carries api-version in the query string
                    params = None
                else:
                    url = None

            set_progress(
                f"Handler {rh.name}: emitted {len(discovered)} subscription(s) so far"
            )

        except requests.exceptions.HTTPError as exc:
            logger.warning(
                f"HTTP error discovering subscriptions for handler {rh.name} "
                f"(id={rh.id}): {exc.response.status_code} {exc.response.text}"
            )
            set_progress(
                f"Handler {rh.name}: HTTP {exc.response.status_code} -- skipping"
            )
            continue
        except Exception as exc:
            logger.exception(
                f"Unexpected error discovering subscriptions for handler "
                f"{rh.name} (id={rh.id})"
            )
            set_progress(f"Handler {rh.name}: {exc} -- skipping")
            continue

    set_progress(
        f"Azure subscription discovery complete: {len(discovered)} unique subscription(s)"
    )
    return discovered
