"""
CloudBolt teardown plugin for canceling Azure subscriptions.

This plugin:
- Cancels the subscription in the destination tenant
- Handles already-canceled subscriptions gracefully (idempotent)
- Detects and deletes stuck subscription aliases (Pending ownership state)
- Returns WARNING if subscription metadata is missing

Note: Subscription cancellation is a soft delete. The subscription
moves to "Cancelled" state and can be reactivated within 90 days.
After 90 days, it's permanently deleted.

If a subscription is not found but a matching alias exists (e.g., stuck
in "Pending" ownership state), this plugin will automatically delete the
alias to clean up the stuck creation request.
"""

import requests
from typing import Dict, Optional

from common.methods import set_progress
from utilities.logger import ThreadLogger

logger = ThreadLogger(__name__)

# Azure Management API endpoints
AZURE_MGMT_ENDPOINT = "https://management.azure.com"
AZURE_LOGIN_ENDPOINT = "https://login.microsoftonline.com"


def _get_azure_token(tenant_id: str, client_id: str, client_secret: str) -> str:
    """Get Azure AD access token."""
    token_url = f"{AZURE_LOGIN_ENDPOINT}/{tenant_id}/oauth2/token"

    payload = {
        "grant_type": "client_credentials",
        "client_id": client_id,
        "client_secret": client_secret,
        "resource": "https://management.azure.com/",
    }

    response = requests.post(token_url, data=payload)
    response.raise_for_status()

    return response.json()["access_token"]


def _azure_api_call(method: str, url: str, token: str, params: Optional[Dict] = None) -> Dict:
    """Make authenticated API call to Azure."""
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

    response = requests.request(method=method, url=url, headers=headers, params=params)

    logger.debug(f"Azure API: {method} {url} - Status: {response.status_code}")

    if response.status_code == 404:
        return {"error": "NotFound", "status_code": 404}

    response.raise_for_status()

    if response.content:
        return response.json()
    return {}


def _check_and_delete_stuck_alias(subscription_id: str, subscription_name: str, token: str) -> Optional[str]:
    """
    Check if a stuck subscription alias exists and delete it if found.

    Returns a message string if an alias was found and deleted, None otherwise.
    """
    set_progress("Checking for stuck subscription aliases...")

    # List all aliases
    aliases_url = f"{AZURE_MGMT_ENDPOINT}/providers/Microsoft.Subscription/aliases"
    params = {"api-version": "2021-10-01"}

    try:
        logger.info(f"Calling aliases API: GET {aliases_url}")
        logger.info(f"Looking for subscription ID: {subscription_id}")
        logger.info(f"Looking for subscription name: {subscription_name}")

        response = _azure_api_call("GET", aliases_url, token, params=params)

        # Log the full response for debugging
        import json
        logger.info(f"Aliases API response: {json.dumps(response, indent=2)}")

        if response.get("error") == "NotFound":
            logger.warning("Aliases API returned NotFound error")
            return None

        aliases = response.get("value", [])
        logger.info(f"Found {len(aliases)} total aliases")

        # Search for matching alias by subscription ID or display name
        matching_alias = None
        for i, alias in enumerate(aliases):
            props = alias.get("properties", {})
            alias_sub_id = props.get("subscriptionId", "")
            alias_display_name = props.get("displayName", "")
            acceptance_state = props.get("acceptOwnershipState", "")

            logger.info(f"Alias {i+1}: ID={alias_sub_id}, Name={alias_display_name}, State={acceptance_state}")
            logger.info(f"  Comparing: '{alias_sub_id}' == '{subscription_id}' ? {alias_sub_id == subscription_id}")
            logger.info(f"  Comparing: '{alias_display_name}' == '{subscription_name}' ? {alias_display_name == subscription_name}")

            # Match by subscription ID or display name
            if alias_sub_id == subscription_id or alias_display_name == subscription_name:
                matching_alias = alias
                logger.info(
                    f"✓ MATCH FOUND! Alias: {alias.get('name')} "
                    f"(state: {acceptance_state}, displayName: {alias_display_name})"
                )
                break

        if not matching_alias:
            logger.warning(f"❌ No stuck alias found for subscription {subscription_id}")
            logger.warning(f"   Searched {len(aliases)} aliases but found no match")
            return None

        # Delete the stuck alias
        alias_name = matching_alias.get("name")
        acceptance_state = matching_alias.get("properties", {}).get("acceptOwnershipState", "Unknown")

        set_progress(f"Deleting stuck alias '{alias_name}' (state: {acceptance_state})...")

        delete_url = f"{AZURE_MGMT_ENDPOINT}/providers/Microsoft.Subscription/aliases/{alias_name}"
        delete_params = {"api-version": "2021-10-01"}

        _azure_api_call("DELETE", delete_url, token, params=delete_params)

        msg = (
            f"Stuck subscription alias '{alias_name}' deleted successfully. "
            f"Subscription '{subscription_name}' was in '{acceptance_state}' state and has been cleaned up."
        )
        logger.info(msg)
        return msg

    except requests.exceptions.HTTPError as e:
        logger.warning(f"Error checking aliases: {e.response.status_code} - {e.response.text}")
        return None
    except Exception as e:
        logger.warning(f"Unexpected error checking aliases: {str(e)}")
        return None


def run(job, **kwargs):
    """
    Teardown entry point - cancels Azure subscription.

    This is idempotent - returns WARNING if subscription is already canceled or metadata is missing.
    """
    set_progress("Starting Azure subscription teardown...")
    logger.info("Starting Azure subscription teardown for job %s", job.id)

    # Get the resource
    resource = job.resource_set.first()
    if not resource:
        msg = "No resource associated with this job"
        logger.warning(msg)
        return "WARNING", msg, ""

    # Get subscription metadata
    subscription_id = resource.get_value_for_custom_field("azure_subscription_id")
    subscription_name = resource.get_value_for_custom_field("azure_subscription_name")
    tenant_id = resource.get_value_for_custom_field("azure_subscription_tenant_id")
    rh_id = resource.get_value_for_custom_field("azure_subscription_rh_id")
    source_rh_id = resource.get_value_for_custom_field("azure_subscription_source_rh_id")

    # Check for missing metadata
    if not subscription_id:
        msg = "Subscription ID missing from resource metadata; assuming already deleted"
        logger.warning(msg)
        return "WARNING", msg, ""

    if not tenant_id:
        msg = "Tenant ID missing from resource metadata; assuming already deleted"
        logger.warning(msg)
        return "WARNING", msg, ""

    if not rh_id:
        msg = "Resource handler ID missing; cannot authenticate to Azure"
        logger.warning(msg)
        return "WARNING", msg, ""

    set_progress(f"Canceling subscription: {subscription_name} ({subscription_id})")

    # Initialize tokens to None for exception handler access
    token = None
    alias_token = None

    try:
        # Get resource handler for credentials
        from resourcehandlers.azure_arm.models import AzureARMHandler

        rh = AzureARMHandler.objects.get(id=rh_id)

        # Get service principal credentials from handler
        client_id = rh.client_id
        client_secret = rh.secret

        # Get token for destination tenant (where subscription lives)
        set_progress("Authenticating to destination tenant...")
        token = _get_azure_token(tenant_id, client_id, client_secret)

        # Get token for source tenant (for alias operations)
        # Aliases are created in the source/billing tenant context
        alias_token = token  # Default to destination token for backwards compatibility
        if source_rh_id:
            try:
                source_rh = AzureARMHandler.objects.get(id=source_rh_id)
                source_tenant = getattr(source_rh, "azure_tenant_id", None) or getattr(source_rh, "tenant_id", None)
                source_client_id = source_rh.client_id
                source_client_secret = source_rh.secret

                set_progress("Authenticating to source tenant for alias operations...")
                alias_token = _get_azure_token(source_tenant, source_client_id, source_client_secret)
                logger.info(f"✓ Using SOURCE tenant {source_tenant} for alias operations")
            except AzureARMHandler.DoesNotExist:
                logger.warning(f"⚠ Source RH {source_rh_id} not found, using DESTINATION token for alias operations (may not find aliases!)")
            except Exception as e:
                logger.warning(f"⚠ Could not get source token: {e}, using DESTINATION token for alias operations (may not find aliases!)")
        else:
            logger.warning(
                f"⚠ No source RH ID found on resource (was created before source_rh_id field was added). "
                f"Using DESTINATION tenant token for alias operations - this will likely fail to find aliases. "
                f"To clean up stuck aliases for old resources, use Azure CLI with source tenant credentials."
            )

        # Cancel the subscription
        cancel_url = f"{AZURE_MGMT_ENDPOINT}/subscriptions/{subscription_id}/providers/Microsoft.Subscription/cancel"
        params = {"api-version": "2021-10-01"}

        set_progress(f"Canceling subscription {subscription_id}...")

        response = _azure_api_call("POST", cancel_url, token, params=params)

        # Check if already canceled
        if response.get("error") == "NotFound":
            # Check for stuck aliases before returning (use source tenant token)
            alias_msg = _check_and_delete_stuck_alias(subscription_id, subscription_name, alias_token)
            if alias_msg:
                return "SUCCESS", alias_msg, ""

            msg = f"Subscription '{subscription_name}' not found; assuming already deleted"
            logger.warning(msg)
            return "WARNING", msg, ""

        # Check subscription state
        sub_url = f"{AZURE_MGMT_ENDPOINT}/subscriptions/{subscription_id}"
        sub_params = {"api-version": "2022-12-01"}

        sub_response = _azure_api_call("GET", sub_url, token, params=sub_params)

        if sub_response.get("error") == "NotFound":
            # Check for stuck aliases before returning (use source tenant token)
            alias_msg = _check_and_delete_stuck_alias(subscription_id, subscription_name, alias_token)
            if alias_msg:
                return "SUCCESS", alias_msg, ""

            msg = f"Subscription '{subscription_name}' not found; assuming already deleted"
            logger.warning(msg)
            return "WARNING", msg, ""

        sub_state = sub_response.get("properties", {}).get("state")

        if sub_state == "Cancelled":
            msg = f"Subscription '{subscription_name}' successfully canceled"
            logger.info(msg)
            return "SUCCESS", msg, ""
        elif sub_state in ["Deleted", "Expired"]:
            msg = f"Subscription '{subscription_name}' already in state: {sub_state}"
            logger.warning(msg)
            return "WARNING", msg, ""
        else:
            # Cancellation initiated but not yet reflected
            msg = f"Subscription '{subscription_name}' cancellation initiated (current state: {sub_state})"
            logger.info(msg)
            return "SUCCESS", msg, ""

    except AzureARMHandler.DoesNotExist:
        msg = f"Azure resource handler {rh_id} not found; cannot authenticate"
        logger.warning(msg)
        return "WARNING", msg, ""
    except requests.exceptions.HTTPError as e:
        if e.response.status_code == 404:
            # Check for stuck aliases before returning (use source tenant token if available)
            if alias_token:
                alias_msg = _check_and_delete_stuck_alias(subscription_id, subscription_name, alias_token)
                if alias_msg:
                    return "SUCCESS", alias_msg, ""

            msg = f"Subscription '{subscription_name}' not found; assuming already deleted"
            logger.warning(msg)
            return "WARNING", msg, ""
        elif e.response.status_code == 403:
            msg = f"Insufficient permissions to cancel subscription '{subscription_name}'"
            logger.error(msg)
            return "FAILURE", msg, ""
        else:
            msg = f"Azure API error: {e.response.status_code} - {e.response.text}"
            logger.exception(msg)
            return "FAILURE", msg, ""
    except Exception as e:
        msg = f"Unexpected error canceling subscription: {str(e)}"
        logger.exception(msg)
        return "FAILURE", msg, ""
