"""
CloudBolt build plugin for creating Azure subscriptions with cross-tenant billing.

This plugin:
- Creates a subscription in a destination tenant (e.g., dev)
- Bills the subscription to a source tenant (e.g., production)
- Auto-accepts subscription ownership by using destination service principal's Object ID
- Optionally adds additional users as Owners via Azure RBAC role assignment
- Places subscription in specified management group
- Uses cost center tags via invoice section for AP routing

Requirements:
- Separate Azure service principals configured in both source and destination tenants
- Microsoft Customer Agreement (MCA) billing account
- Management group pre-created in destination tenant (optional)
- Destination SP needs Microsoft Graph API permissions to:
  - Read its own service principal Object ID (ServicePrincipalEndpoint.ReadWrite.All or Directory.Read.All)
  - (Optional) Look up users for additional owner assignment (User.Read.All)
- Destination SP needs Owner or User Access Administrator role for RBAC assignments
"""

import json
import time
import requests
from typing import Dict, List, Tuple, Optional

from accounts.models import Group
from common.methods import set_progress
from infrastructure.models import Environment
from c2_wrapper import create_custom_field
from utilities.logger import ThreadLogger

logger = ThreadLogger(__name__)

# Azure Management API endpoints
AZURE_MGMT_ENDPOINT = "https://management.azure.com"
AZURE_LOGIN_ENDPOINT = "https://login.microsoftonline.com"


def create_custom_fields():
    """
    Pre-create custom fields for subscription metadata.
    """
    create_custom_field("azure_subscription_id", "Azure Subscription ID", "STR")
    create_custom_field("azure_subscription_name", "Subscription Name", "STR")
    create_custom_field("azure_subscription_tenant_id", "Subscription Tenant ID", "STR")
    create_custom_field("azure_subscription_state", "Subscription State", "STR")
    create_custom_field("azure_subscription_billing_scope", "Billing Scope", "STR")
    create_custom_field("azure_subscription_management_group", "Management Group", "STR")
    create_custom_field("azure_subscription_rh_id", "Resource Handler ID", "STR")
    create_custom_field("azure_subscription_source_rh_id", "Source RH ID (for alias cleanup)", "STR")


def _get_azure_token(tenant_id: str, client_id: str, client_secret: str, resource: str = "https://management.azure.com/") -> str:
    """
    Get Azure AD access token for API calls.

    Args:
        tenant_id: Azure AD tenant ID
        client_id: Service principal client ID
        client_secret: Service principal client secret
        resource: Resource to get token for (default: Azure Management API)

    Returns:
        Access token string
    """
    token_url = f"{AZURE_LOGIN_ENDPOINT}/{tenant_id}/oauth2/token"

    payload = {
        "grant_type": "client_credentials",
        "client_id": client_id,
        "client_secret": client_secret,
        "resource": resource,
    }

    response = requests.post(token_url, data=payload)
    response.raise_for_status()

    token_data = response.json()
    return token_data["access_token"]


def _get_service_principal_object_id(app_id: str, tenant_id: str, client_id: str, client_secret: str) -> Optional[str]:
    """
    Get the Object ID of a service principal from its Application ID.

    Args:
        app_id: The Application (Client) ID of the service principal
        tenant_id: Azure AD tenant ID
        client_id: Service principal client ID (for authentication)
        client_secret: Service principal client secret (for authentication)

    Returns:
        Service Principal's Object ID (GUID) or None if not found
    """
    try:
        # Get token for Microsoft Graph API
        graph_token = _get_azure_token(tenant_id, client_id, client_secret, resource="https://graph.microsoft.com/")

        # Query service principals by appId
        graph_url = "https://graph.microsoft.com/v1.0/servicePrincipals"
        params = {
            "$filter": f"appId eq '{app_id}'",
            "$select": "id,appId,displayName"
        }

        headers = {
            "Authorization": f"Bearer {graph_token}",
            "Content-Type": "application/json"
        }

        response = requests.get(graph_url, headers=headers, params=params)
        response.raise_for_status()

        sps = response.json().get("value", [])

        if not sps:
            logger.warning(f"No service principal found with app ID: {app_id}")
            return None

        sp_object_id = sps[0].get("id")
        logger.info(f"Found service principal {app_id} with Object ID: {sp_object_id}")
        return sp_object_id

    except requests.exceptions.HTTPError as e:
        logger.error(f"Graph API error looking up service principal: {e.response.status_code} - {e.response.text}")
        return None
    except Exception as e:
        logger.error(f"Error looking up service principal by app ID: {str(e)}")
        return None


def _get_user_object_id_by_email(email: str, tenant_id: str, client_id: str, client_secret: str) -> Optional[str]:
    """
    Look up a user's Object ID from their email address using Microsoft Graph API.

    Args:
        email: User's email address or UPN
        tenant_id: Azure AD tenant ID
        client_id: Service principal client ID
        client_secret: Service principal client secret

    Returns:
        User's Object ID (GUID) or None if not found
    """
    try:
        # Get token for Microsoft Graph API
        graph_token = _get_azure_token(tenant_id, client_id, client_secret, resource="https://graph.microsoft.com/")

        # Query users by email or UPN
        graph_url = "https://graph.microsoft.com/v1.0/users"
        params = {
            "$filter": f"userPrincipalName eq '{email}' or mail eq '{email}'",
            "$select": "id,userPrincipalName,mail"
        }

        headers = {
            "Authorization": f"Bearer {graph_token}",
            "Content-Type": "application/json"
        }

        response = requests.get(graph_url, headers=headers, params=params)
        response.raise_for_status()

        users = response.json().get("value", [])

        if not users:
            logger.warning(f"No user found with email: {email}")
            return None

        if len(users) > 1:
            logger.warning(f"Multiple users found for email {email}, using first match")

        user_object_id = users[0].get("id")
        logger.info(f"Found user {email} with Object ID: {user_object_id}")
        return user_object_id

    except requests.exceptions.HTTPError as e:
        logger.error(f"Graph API error looking up user: {e.response.status_code} - {e.response.text}")
        return None
    except Exception as e:
        logger.error(f"Error looking up user by email: {str(e)}")
        return None


def _add_subscription_owner(
    subscription_id: str,
    user_object_id: str,
    tenant_id: str,
    client_id: str,
    client_secret: str
) -> bool:
    """
    Add a user as Owner of an Azure subscription using RBAC.

    Args:
        subscription_id: Azure subscription ID
        user_object_id: User's Object ID (GUID) from Azure AD
        tenant_id: Azure AD tenant ID
        client_id: Service principal client ID
        client_secret: Service principal client secret

    Returns:
        True if successful, False otherwise
    """
    try:
        # Get Management API token
        token = _get_azure_token(tenant_id, client_id, client_secret)

        # Owner role definition ID (built-in Azure role)
        owner_role_id = "8e3af657-a8ff-443c-a75c-2fe8c4bcb635"

        # Generate unique GUID for role assignment
        import uuid
        role_assignment_id = str(uuid.uuid4())

        # Create role assignment
        role_assignment_url = (
            f"{AZURE_MGMT_ENDPOINT}/subscriptions/{subscription_id}/providers/"
            f"Microsoft.Authorization/roleAssignments/{role_assignment_id}"
        )

        params = {"api-version": "2022-04-01"}

        role_assignment_payload = {
            "properties": {
                "roleDefinitionId": f"/subscriptions/{subscription_id}/providers/Microsoft.Authorization/roleDefinitions/{owner_role_id}",
                "principalId": user_object_id,
                "principalType": "User"
            }
        }

        response = _azure_api_call("PUT", role_assignment_url, token, data=role_assignment_payload, params=params)

        logger.info(f"Successfully added user {user_object_id} as Owner of subscription {subscription_id}")
        return True

    except requests.exceptions.HTTPError as e:
        # Check if role assignment already exists (409 Conflict)
        if e.response.status_code == 409:
            logger.info(f"User {user_object_id} is already an Owner of subscription {subscription_id}")
            return True
        logger.error(f"Error adding subscription owner: {e.response.status_code} - {e.response.text}")
        return False
    except Exception as e:
        logger.error(f"Unexpected error adding subscription owner: {str(e)}")
        return False


def _azure_api_call(
    method: str,
    url: str,
    token: str,
    data: Optional[Dict] = None,
    params: Optional[Dict] = None,
) -> Dict:
    """
    Make authenticated API call to Azure Management API.

    Args:
        method: HTTP method (GET, POST, PUT, DELETE)
        url: Full API URL
        token: Azure AD access token
        data: Request body (for POST/PUT)
        params: Query parameters

    Returns:
        Response JSON as dictionary
    """
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

    response = requests.request(
        method=method,
        url=url,
        headers=headers,
        json=data,
        params=params,
    )

    # Log response for debugging
    logger.debug(f"Azure API call: {method} {url}")
    logger.debug(f"Status: {response.status_code}")

    if response.status_code == 202:
        # Accepted - async operation
        return {"status": "Accepted", "location": response.headers.get("Location")}

    response.raise_for_status()

    if response.content:
        return response.json()
    return {}


def _get_rh_by_id(rh_id: str, group_name: str):
    """
    Find Azure Resource Handler by ID from group's available environments.
    Validates that the group has access to an environment using this RH.

    Args:
        rh_id: Resource Handler ID
        group_name: Group name for RBAC filtering

    Returns:
        AzureARMHandler instance or None
    """
    from resourcehandlers.azure_arm.models import AzureARMHandler

    try:
        group = Group.objects.get(name=group_name)
    except Group.DoesNotExist:
        return None

    # Get all environments the group has access to
    envs = group.get_available_environments()

    # Check if any of the group's environments use this RH
    for env in envs:
        if str(env.resource_handler.id) == str(rh_id):
            # Group has access to an environment using this RH
            return AzureARMHandler.objects.get(id=rh_id)

    return None


def generate_options_for_source_tenant(**kwargs):
    """
    List available source tenants (for billing).
    Returns unique Azure Resource Handlers.

    Each RH may have multiple environments, but we only show unique RHs.
    Returns: List of (rh_id, "rh_name (tenant_id)") tuples sorted by RH name.
    """
    group_name = kwargs.get("group")
    if not group_name:
        return []

    try:
        group = Group.objects.get(name=group_name)
    except Group.DoesNotExist:
        return []

    # Get all Azure environments the group has access to
    envs = group.get_available_environments()
    azure_envs = [env for env in envs if env.resource_handler.resource_technology.name == "Azure"]

    # Build unique list of (rh_id, display_name) from resource handlers
    seen_rh_ids = set()
    options = []

    for env in azure_envs:
        rh = env.resource_handler.cast()
        rh_id = str(rh.id)

        if rh_id not in seen_rh_ids:
            seen_rh_ids.add(rh_id)
            tenant_id = getattr(rh, "azure_tenant_id", None) or getattr(rh, "tenant_id", None)
            display_name = f"{rh.name} ({tenant_id})" if tenant_id else rh.name
            options.append((rh_id, display_name))

    # Sort alphabetically by display name
    options.sort(key=lambda x: x[1])

    # Add default "Please Select" option at the beginning
    if options:
        options.insert(0, ("", "------ Please Select a Source Tenant ------"))
    else:
        return [("", "--- No Azure Resource Handlers available ---")]

    return options


def generate_options_for_destination_tenant(**kwargs):
    """
    List available destination tenants (where subscription will be created).
    Returns unique Azure Resource Handlers with appropriate default option.
    """
    group_name = kwargs.get("group")
    if not group_name:
        return []

    try:
        group = Group.objects.get(name=group_name)
    except Group.DoesNotExist:
        return []

    # Get all Azure environments the group has access to
    envs = group.get_available_environments()
    azure_envs = [env for env in envs if env.resource_handler.resource_technology.name == "Azure"]

    # Build unique list of (rh_id, display_name) from resource handlers
    seen_rh_ids = set()
    options = []

    for env in azure_envs:
        rh = env.resource_handler.cast()
        rh_id = str(rh.id)

        if rh_id not in seen_rh_ids:
            seen_rh_ids.add(rh_id)
            tenant_id = getattr(rh, "azure_tenant_id", None) or getattr(rh, "tenant_id", None)
            display_name = f"{rh.name} ({tenant_id})" if tenant_id else rh.name
            options.append((rh_id, display_name))

    # Sort alphabetically by display name
    options.sort(key=lambda x: x[1])

    # Add default "Please Select" option at the beginning
    if options:
        options.insert(0, ("", "------ Please Select a Destination Tenant ------"))
    else:
        return [("", "--- No Azure Resource Handlers available ---")]

    return options


def generate_options_for_billing_account(field, control_value=None, control_value_dict=None, **kwargs):
    """
    List billing accounts in the source tenant.
    Depends on: source_tenant (RH ID)
    """
    if not control_value_dict:
        return [("", "------ Select source tenant first ------")]

    source_rh_id = control_value_dict.get("source_tenant")
    group_name = kwargs.get("group")

    if not source_rh_id:
        return [("", "------ Select source tenant first ------")]

    if not group_name:
        return [("", "------ Error: No group specified ------")]

    # Find RH by ID
    rh = _get_rh_by_id(source_rh_id, group_name)
    if not rh:
        return [("", "------ Error: Resource handler not found ------")]

    # Get service principal credentials from handler
    client_id = rh.client_id
    client_secret = rh.secret
    source_tenant = getattr(rh, "azure_tenant_id", None) or getattr(rh, "tenant_id", None)

    try:
        # Get token for source tenant
        token = _get_azure_token(source_tenant, client_id, client_secret)

        # List billing accounts
        url = f"{AZURE_MGMT_ENDPOINT}/providers/Microsoft.Billing/billingAccounts"
        params = {"api-version": "2024-04-01"}

        response = _azure_api_call("GET", url, token, params=params)

        accounts = response.get("value", [])
        options = []

        for account in accounts:
            account_id = account.get("id")
            account_name = account.get("properties", {}).get("displayName", account.get("name"))
            agreement_type = account.get("properties", {}).get("agreementType")

            # Only show MCA billing accounts
            if agreement_type == "MicrosoftCustomerAgreement":
                options.append((account_id, f"{account_name} (MCA)"))

        options.sort(key=lambda x: x[1])

        if not options:
            return [("", "------ No MCA billing accounts found ------")]

        return options
    except Exception as e:
        logger.exception(f"Failed to list billing accounts: {e}")
        return [("", f"------ Error: {str(e)} ------")]


def generate_options_for_invoice_section(field, control_value=None, control_value_dict=None, **kwargs):
    """
    List invoice sections in the selected billing account.
    Invoice section name should match cost center for AP routing.
    Depends on: source_tenant (RH ID), billing_account
    """
    if not control_value_dict:
        return [("", "------ Select billing account first ------")]

    source_rh_id = control_value_dict.get("source_tenant")
    billing_account = control_value_dict.get("billing_account")
    group_name = kwargs.get("group")

    if not source_rh_id or not billing_account:
        return [("", "------ Select source tenant and billing account first ------")]

    if not group_name:
        return [("", "------ Error: No group specified ------")]

    # Find RH by ID
    rh = _get_rh_by_id(source_rh_id, group_name)
    if not rh:
        return [("", "------ Error: Resource handler not found ------")]

    # Get service principal credentials from handler
    client_id = rh.client_id
    client_secret = rh.secret
    source_tenant = getattr(rh, "azure_tenant_id", None) or getattr(rh, "tenant_id", None)

    try:
        # Get token for source tenant
        token = _get_azure_token(source_tenant, client_id, client_secret)

        # Extract billing account name from ID
        billing_account_name = billing_account.split("/")[-1]

        # List billing profiles
        url = f"{AZURE_MGMT_ENDPOINT}{billing_account}/billingProfiles"
        params = {"api-version": "2024-04-01"}

        response = _azure_api_call("GET", url, token, params=params)

        profiles = response.get("value", [])
        invoice_sections = []

        # For each billing profile, get invoice sections
        for profile in profiles:
            profile_id = profile.get("id")

            sections_url = f"{AZURE_MGMT_ENDPOINT}{profile_id}/invoiceSections"
            sections_response = _azure_api_call("GET", sections_url, token, params=params)

            sections = sections_response.get("value", [])
            for section in sections:
                section_id = section.get("id")
                section_name = section.get("properties", {}).get("displayName", section.get("name"))
                profile_name = profile.get("properties", {}).get("displayName")

                # Billing scope format for subscription creation
                billing_scope = section_id

                invoice_sections.append((
                    billing_scope,
                    f"{section_name} (Profile: {profile_name})"
                ))

        invoice_sections.sort(key=lambda x: x[1])

        if not invoice_sections:
            return [("", "------ No invoice sections found ------")]

        return invoice_sections
    except Exception as e:
        logger.exception(f"Failed to list invoice sections: {e}")
        return [("", f"------ Error: {str(e)} ------")]


def generate_options_for_management_group(field, control_value=None, control_value_dict=None, **kwargs):
    """
    List management groups in the destination tenant.
    Depends on: destination_tenant (RH ID)
    """
    if not control_value_dict:
        return [("", "------ Select destination tenant first ------")]

    destination_rh_id = control_value_dict.get("destination_tenant")
    group_name = kwargs.get("group")

    if not destination_rh_id:
        return [("", "------ Select destination tenant first ------")]

    if not group_name:
        return [("", "------ Error: No group specified ------")]

    # Find RH by ID
    rh = _get_rh_by_id(destination_rh_id, group_name)
    if not rh:
        return [("", "------ Error: Resource handler not found ------")]

    # Get service principal credentials from handler
    client_id = rh.client_id
    client_secret = rh.secret
    destination_tenant = getattr(rh, "azure_tenant_id", None) or getattr(rh, "tenant_id", None)

    try:
        # Get token for destination tenant
        token = _get_azure_token(destination_tenant, client_id, client_secret)

        # List management groups
        url = f"{AZURE_MGMT_ENDPOINT}/providers/Microsoft.Management/managementGroups"
        params = {"api-version": "2020-05-01"}

        response = _azure_api_call("GET", url, token, params=params)

        groups = response.get("value", [])
        options = []

        for group in groups:
            group_id = group.get("name")  # This is the management group ID
            group_name = group.get("properties", {}).get("displayName", group_id)

            options.append((group_id, group_name))

        options.sort(key=lambda x: x[1])

        if not options:
            return [("", "------ No management groups found ------")]

        return options
    except Exception as e:
        logger.exception(f"Failed to list management groups: {e}")
        return [("", f"------ Error: {str(e)} ------")]


def run(job, **kwargs):
    """
    Build entry point for creating Azure subscription with cross-tenant billing.

    Expected Action Inputs:
    - subscription_name: Name for the new subscription
    - source_tenant: Resource Handler ID for source tenant (where billing account lives)
    - destination_tenant: Resource Handler ID for destination tenant (where subscription will be created)
    - billing_account: Billing account ID (in source tenant)
    - invoice_section: Invoice section ID (for cost center/AP routing)
    - management_group: (Optional) Management group ID (in destination tenant)
    - owner_email: (Optional) Email address of user to add as additional Owner via RBAC

    Subscription Creation Flow:
    1. Looks up destination service principal's Object ID via Microsoft Graph API
    2. Creates subscription with subscriptionOwnerId set to destination SP's Object ID (enables auto-accept)
    3. Accepts ownership in destination tenant using the SP (no email confirmation required)
    4. If owner_email provided, adds that user as Owner via Azure RBAC role assignment

    Requires destination service principal to have:
    - Microsoft Graph API permissions to read service principals and users (Directory.Read.All or equivalent)
    - Owner or User Access Administrator role for RBAC assignments
    """
    set_progress("Starting Azure cross-tenant subscription creation...")
    logger.info("Starting Azure subscription build for job %s", job.id)

    # Pre-create custom fields
    create_custom_fields()

    # Get template parameters
    subscription_name = "{{ subscription_name }}".strip()
    source_rh_id = "{{ source_tenant }}".strip()  # This is actually RH ID now
    destination_rh_id = "{{ destination_tenant }}".strip()  # This is actually RH ID now
    billing_account = "{{ billing_account }}".strip()
    invoice_section = "{{ invoice_section }}".strip()  # This is the billing scope
    management_group = "{{ management_group }}".strip()
    owner_email = "{{ owner_email }}".strip()

    # Helper function to check if parameter is valid (not empty, not a template literal)
    def is_valid_param(value):
        """Check if parameter is valid (not empty and not an unrendered template)."""
        if not value:
            return False
        if value in ["management_group", "owner_email", "subscription_name", "source_tenant", "destination_tenant", "billing_account", "invoice_section"]:
            return False
        return True

    # Validate required parameters
    if not subscription_name or not is_valid_param(subscription_name):
        return "FAILURE", "Subscription name is required", ""

    if not source_rh_id or not is_valid_param(source_rh_id):
        return "FAILURE", "Source tenant is required", ""

    if not destination_rh_id or not is_valid_param(destination_rh_id):
        return "FAILURE", "Destination tenant is required", ""

    if not invoice_section or not is_valid_param(invoice_section):
        return "FAILURE", "Invoice section (billing scope) is required", ""

    # Sanitize optional parameters (set to None if invalid)
    if not is_valid_param(management_group):
        logger.info(f"Management group not provided or invalid (value: '{management_group}'), skipping")
        management_group = None
    else:
        logger.info(f"Management group provided: {management_group}")

    if not is_valid_param(owner_email):
        logger.info(f"Owner email not provided or invalid (value: '{owner_email}'), skipping owner assignment")
        owner_email = None
    else:
        logger.info(f"Owner email provided: {owner_email}")

    # Get the group from the job/order
    resource = kwargs.get("resource")
    if not resource or not resource.group:
        return "FAILURE", "Unable to determine group from job", ""

    group_name = resource.group.name

    # Get Azure handlers from RH IDs
    source_rh = _get_rh_by_id(source_rh_id, group_name)
    if not source_rh:
        return "FAILURE", f"Unable to find source Azure Resource Handler (ID: {source_rh_id})", ""

    destination_rh = _get_rh_by_id(destination_rh_id, group_name)
    if not destination_rh:
        return "FAILURE", f"Unable to find destination Azure Resource Handler (ID: {destination_rh_id})", ""

    # Get service principal credentials from BOTH RHs (separate SPs per tenant)
    source_client_id = source_rh.client_id
    source_client_secret = source_rh.secret
    dest_client_id = destination_rh.client_id
    dest_client_secret = destination_rh.secret

    # Get tenant IDs from RHs
    source_tenant = getattr(source_rh, "azure_tenant_id", None) or getattr(source_rh, "tenant_id", None)
    destination_tenant = getattr(destination_rh, "azure_tenant_id", None) or getattr(destination_rh, "tenant_id", None)

    # Log parameters for debugging
    logger.info(f"Subscription parameters - Name: {subscription_name}")
    logger.info(f"  Source RH: {source_rh.name} (Tenant: {source_tenant})")
    logger.info(f"  Destination RH: {destination_rh.name} (Tenant: {destination_tenant})")
    logger.info(f"  Invoice Section: {invoice_section}")
    logger.info(f"  Management Group: {management_group or 'None'}")
    logger.info(f"  Owner Email: {owner_email or 'None'}")

    set_progress(f"Creating subscription '{subscription_name}' in tenant {destination_tenant}...")
    set_progress(f"Billing will be charged to source tenant {source_tenant}")

    try:
        # Step 1: Get token for SOURCE tenant (where billing account is)
        set_progress("Authenticating to source tenant for billing...")
        source_token = _get_azure_token(source_tenant, source_client_id, source_client_secret)

        # Step 1.5: Determine subscriptionOwnerId
        # Always use the destination SP's Object ID so ownership is auto-accepted
        # without requiring email confirmation. If owner_email is provided, that
        # user will be added as an additional Owner via RBAC after acceptance.
        set_progress("Looking up destination service principal for subscription ownership...")
        subscription_owner_id = _get_service_principal_object_id(
            dest_client_id,
            destination_tenant,
            dest_client_id,
            dest_client_secret
        )

        if not subscription_owner_id:
            logger.warning("Could not find destination service principal Object ID - subscription creation may fail")
            logger.warning("Ensure the service principal exists in the destination tenant")

        # Step 2: Create subscription alias with cross-tenant parameters
        set_progress("Creating subscription via Alias API...")

        # Generate a unique alias name (GUID format recommended)
        import uuid
        alias_name = str(uuid.uuid4())

        alias_url = f"{AZURE_MGMT_ENDPOINT}/providers/Microsoft.Subscription/aliases/{alias_name}"
        params = {"api-version": "2021-10-01"}

        subscription_payload = {
            "properties": {
                "displayName": subscription_name,
                "billingScope": invoice_section,
                "workload": "Production",
                "additionalProperties": {
                    "subscriptionTenantId": destination_tenant,
                }
            }
        }

        # Add subscription owner
        if subscription_owner_id:
            subscription_payload["properties"]["additionalProperties"]["subscriptionOwnerId"] = subscription_owner_id
            logger.info(f"Using subscriptionOwnerId: {subscription_owner_id} (destination SP)")
        else:
            logger.warning("No subscriptionOwnerId set - Azure may reject cross-tenant subscription creation")

        # Add management group if specified
        if management_group:
            subscription_payload["properties"]["additionalProperties"]["managementGroupId"] = f"/providers/Microsoft.Management/managementGroups/{management_group}"

        set_progress(f"Subscription payload: {json.dumps(subscription_payload, indent=2)}")

        response = _azure_api_call("PUT", alias_url, source_token, data=subscription_payload, params=params)

        # Handle async operation
        if response.get("status") == "Accepted":
            location_url = response.get("location")
            set_progress("Subscription creation accepted, polling for completion...")

            # Poll for completion
            max_attempts = 30
            poll_interval = 10

            for attempt in range(max_attempts):
                time.sleep(poll_interval)

                poll_response = _azure_api_call("GET", location_url, source_token)

                provisioning_state = poll_response.get("properties", {}).get("provisioningState")

                if provisioning_state == "Succeeded":
                    response = poll_response
                    break
                elif provisioning_state == "Failed":
                    error = poll_response.get("properties", {}).get("failureReason", "Unknown error")
                    return "FAILURE", f"Subscription creation failed: {error}", ""

                set_progress(f"Polling subscription creation... (attempt {attempt + 1}/{max_attempts})")
            else:
                return "FAILURE", "Subscription creation timed out", ""

        # Extract subscription details
        subscription_id = response.get("properties", {}).get("subscriptionId")

        if not subscription_id:
            logger.error(f"No subscription ID in response: {response}")
            return "FAILURE", "Subscription created but ID not returned", ""

        set_progress(f"Subscription created successfully: {subscription_id}")

        # CRITICAL: Immediately save subscription ID and basic metadata to resource
        # This allows cleanup via teardown even if subsequent steps fail
        resource = job.resource_set.first()
        if resource:
            resource.set_value_for_custom_field("azure_subscription_id", subscription_id)
            resource.set_value_for_custom_field("azure_subscription_name", subscription_name)
            resource.set_value_for_custom_field("azure_subscription_tenant_id", destination_tenant)
            resource.set_value_for_custom_field("azure_subscription_rh_id", str(destination_rh.id))
            resource.set_value_for_custom_field("azure_subscription_source_rh_id", str(source_rh.id))
            resource.save()
            logger.info(f"Saved subscription ID {subscription_id} and source RH {source_rh.id} to resource for cleanup capability")
            set_progress("✓ Subscription metadata saved (enables automatic cleanup if needed)")
        else:
            logger.warning("No resource found to save subscription ID - cleanup may not be possible if errors occur")

        # Step 3: Get token for DESTINATION tenant
        set_progress("Authenticating to destination tenant...")
        dest_token = _get_azure_token(destination_tenant, dest_client_id, dest_client_secret)

        # Step 4: Accept ownership in destination tenant (SP is always the owner)
        # Wait briefly for Azure to propagate the alias before accepting
        set_progress("Waiting for subscription alias to propagate before accepting ownership...")
        time.sleep(15)

        set_progress("Accepting subscription ownership in destination tenant...")

        accept_url = f"{AZURE_MGMT_ENDPOINT}/providers/Microsoft.Subscription/subscriptions/{subscription_id}/acceptOwnership"
        accept_params = {"api-version": "2021-10-01"}

        accept_payload = {
            "properties": {
                "displayName": subscription_name,
            }
        }

        if management_group:
            accept_payload["properties"]["managementGroupId"] = f"/providers/Microsoft.Management/managementGroups/{management_group}"
            logger.info(f"Including management group in Accept Ownership: {management_group}")

        ownership_accepted = False
        max_accept_retries = 3

        for accept_attempt in range(max_accept_retries):
            try:
                logger.info(f"Calling Accept Ownership (attempt {accept_attempt + 1}/{max_accept_retries}): POST {accept_url}")
                logger.info(f"Accept payload: {json.dumps(accept_payload, indent=2)}")
                logger.info(f"Using destination tenant token for SP: {dest_client_id}")

                accept_response = _azure_api_call("POST", accept_url, dest_token, data=accept_payload, params=accept_params)

                logger.info(f"Accept ownership response: {json.dumps(accept_response, indent=2)}")

                if accept_response.get("status") == "Accepted":
                    location_url = accept_response.get("location")
                    set_progress("Accept ownership request accepted (async), polling for completion...")

                    if location_url:
                        max_poll = 30
                        poll_interval = 10
                        for poll_attempt in range(max_poll):
                            time.sleep(poll_interval)
                            try:
                                poll_response = _azure_api_call("GET", location_url, dest_token)

                                # Log full body on first poll so we have a record of the
                                # actual response shape returned by subscriptionOperations.
                                if poll_attempt == 0:
                                    logger.info(
                                        f"Accept ownership poll body (first): "
                                        f"{json.dumps(poll_response, indent=2)}"
                                    )

                                # The Location URL for Microsoft.Subscription/acceptOwnership
                                # points at /subscriptionOperations/{id}, which returns a
                                # long-running-operation document with `status` at the top
                                # level (Succeeded/Failed/InProgress). Resource-style bodies
                                # with nested properties.* are handled defensively.
                                op_status = (poll_response.get("status") or "").strip()
                                nested = poll_response.get("properties") or {}
                                prov_state = nested.get("provisioningState", "") or ""
                                accept_state = nested.get("acceptOwnershipState", "") or ""

                                logger.info(
                                    f"Accept ownership poll {poll_attempt + 1}: "
                                    f"status={op_status}, provisioningState={prov_state}, "
                                    f"acceptOwnershipState={accept_state}"
                                )

                                if (
                                    op_status in ("Succeeded", "Completed")
                                    or prov_state == "Succeeded"
                                    or accept_state == "Completed"
                                ):
                                    set_progress("✓ Ownership accepted in destination tenant")
                                    ownership_accepted = True
                                    break

                                if op_status == "Failed" or prov_state == "Failed":
                                    logger.error(
                                        f"Accept ownership failed: {json.dumps(poll_response, indent=2)}"
                                    )
                                    set_progress("⚠ Accept ownership failed during async processing")
                                    break

                                set_progress(f"Waiting for ownership acceptance... (poll {poll_attempt + 1}/{max_poll})")
                            except Exception as poll_err:
                                logger.warning(f"Error polling accept ownership: {poll_err}")
                        else:
                            logger.warning("Accept ownership polling timed out")
                    else:
                        logger.info("No Location header in 202 response, checking alias state directly")
                        time.sleep(15)
                        try:
                            alias_check_url = f"{AZURE_MGMT_ENDPOINT}/providers/Microsoft.Subscription/aliases/{alias_name}"
                            alias_check = _azure_api_call("GET", alias_check_url, source_token, params={"api-version": "2021-10-01"})
                            accept_state = alias_check.get("properties", {}).get("acceptOwnershipState", "Unknown")
                            logger.info(f"Alias accept state after 202: {accept_state}")
                            if accept_state == "Completed":
                                ownership_accepted = True
                                set_progress("✓ Ownership accepted in destination tenant")
                        except Exception as check_err:
                            logger.warning(f"Could not check alias state: {check_err}")
                else:
                    set_progress("✓ Ownership accepted in destination tenant")
                    ownership_accepted = True

                if ownership_accepted:
                    break

            except requests.exceptions.HTTPError as e:
                error_details = e.response.text if hasattr(e.response, 'text') else str(e)
                logger.error(f"Accept ownership HTTP error: {e.response.status_code} - {error_details}")

                if e.response.status_code == 409:
                    logger.info("Ownership already accepted (409 Conflict)")
                    set_progress("Ownership already accepted")
                    ownership_accepted = True
                    break
                elif (
                    e.response.status_code == 400
                    and (
                        "SubscriptionCountReachedLimit" in error_details
                        or "reached its subscription limit" in error_details
                    )
                ):
                    # This is a deterministic platform limit, not a transient propagation issue.
                    # Retrying acceptOwnership will not succeed until the quota condition is resolved.
                    quota_msg = (
                        "Accept ownership failed: destination tenant/subscription quota reached "
                        "(SubscriptionCountReachedLimit). Azure reported the subscription is pending "
                        "ownership acceptance and cannot be activated until capacity is available. "
                        "Please open capacity with Azure support or free existing subscriptions, "
                        "then retry ownership acceptance."
                    )
                    logger.error(quota_msg)
                    return "FAILURE", quota_msg, ""
                elif e.response.status_code == 404 and accept_attempt < max_accept_retries - 1:
                    logger.info(f"Subscription not found for accept ownership (404), retrying in 15s...")
                    set_progress(f"Subscription not ready for ownership acceptance, retrying...")
                    time.sleep(15)
                    continue
                else:
                    logger.error(f"Accept ownership failed with status {e.response.status_code}: {error_details}")
                    set_progress(f"⚠ Accept ownership call failed: {e.response.status_code}")
                    if accept_attempt < max_accept_retries - 1:
                        time.sleep(10)
                        continue
                    break

            except Exception as e:
                logger.error(f"Unexpected error accepting ownership: {type(e).__name__}: {str(e)}")
                set_progress(f"⚠ Accept ownership call failed: {str(e)}")
                if accept_attempt < max_accept_retries - 1:
                    time.sleep(10)
                    continue
                break

        if not ownership_accepted:
            set_progress("Verifying subscription ownership state via alias...")
            try:
                aliases_url = f"{AZURE_MGMT_ENDPOINT}/providers/Microsoft.Subscription/aliases/{alias_name}"
                alias_check = _azure_api_call("GET", aliases_url, source_token, params={"api-version": "2021-10-01"})

                acceptance_state = alias_check.get("properties", {}).get("acceptOwnershipState", "Unknown")
                logger.info(f"Final alias acceptance state: {acceptance_state}")
                logger.info(f"Full alias response: {json.dumps(alias_check, indent=2)}")

                if acceptance_state == "Completed":
                    set_progress("✓ Ownership verified as accepted (alias)")
                    ownership_accepted = True
                elif acceptance_state == "Pending":
                    logger.warning(f"Subscription {subscription_id} is in Pending state - SP auto-accept did not complete")
                    set_progress("⚠ Ownership still pending on alias - checking subscription directly...")
            except Exception as e:
                logger.warning(f"Could not verify alias state: {e}")

            # Independent signal: if the destination SP can read the subscription and it's
            # in an active state, ownership has effectively been accepted in the destination
            # tenant regardless of what the alias/operation endpoints report.
            if not ownership_accepted:
                try:
                    sub_check_url = f"{AZURE_MGMT_ENDPOINT}/subscriptions/{subscription_id}"
                    sub_check = _azure_api_call(
                        "GET", sub_check_url, dest_token, params={"api-version": "2022-12-01"}
                    )
                    direct_state = sub_check.get("state")
                    logger.info(f"Direct subscription GET returned state: {direct_state}")

                    if direct_state in ("Enabled", "Warned", "PastDue"):
                        set_progress(f"✓ Ownership verified as accepted (subscription state: {direct_state})")
                        ownership_accepted = True
                except Exception as e:
                    logger.warning(f"Could not verify subscription state directly: {e}")

        # Step 5: Add user as subscription Owner via RBAC (if requested and ownership accepted)
        if owner_email and ownership_accepted:
            set_progress(f"Adding {owner_email} as subscription Owner via RBAC...")

            user_object_id = _get_user_object_id_by_email(
                owner_email,
                destination_tenant,
                dest_client_id,
                dest_client_secret
            )

            if user_object_id:
                success = _add_subscription_owner(
                    subscription_id,
                    user_object_id,
                    destination_tenant,
                    dest_client_id,
                    dest_client_secret
                )

                if success:
                    set_progress(f"Successfully added {owner_email} as subscription Owner")
                else:
                    logger.warning(f"Failed to add {owner_email} as Owner, but subscription was created successfully")
                    set_progress(f"Warning: Could not add {owner_email} as Owner - you may need to add them manually")
            else:
                logger.warning(f"Could not find user {owner_email} in Azure AD tenant {destination_tenant}")
                set_progress(f"Warning: User {owner_email} not found in Azure AD - you may need to add them manually as Owner")

        # Step 6: Validate subscription is actually active before declaring success
        set_progress("Validating subscription is active...")

        sub_state = None
        sub_check_url = f"{AZURE_MGMT_ENDPOINT}/subscriptions/{subscription_id}"
        sub_check_params = {"api-version": "2022-12-01"}

        max_validation_attempts = 24
        validation_interval = 10

        for val_attempt in range(max_validation_attempts):
            try:
                sub_check = _azure_api_call("GET", sub_check_url, dest_token, params=sub_check_params)
                sub_state = sub_check.get("state", None)

                if sub_state == "Enabled":
                    set_progress("✓ Subscription verified as active (Enabled)")
                    break
                elif sub_state in ("Warned", "PastDue"):
                    logger.warning(f"Subscription active but in state: {sub_state}")
                    set_progress(f"✓ Subscription is accessible (state: {sub_state})")
                    break

                logger.info(f"Subscription state: {sub_state} (attempt {val_attempt + 1}/{max_validation_attempts})")
                set_progress(f"Subscription not yet active (state: {sub_state}), waiting...")

            except requests.exceptions.HTTPError as e:
                if e.response.status_code == 404:
                    logger.info(f"Subscription not visible yet (404), attempt {val_attempt + 1}/{max_validation_attempts}")
                    set_progress(f"Subscription not visible yet, waiting... (attempt {val_attempt + 1}/{max_validation_attempts})")
                else:
                    logger.warning(f"Error checking subscription state: {e.response.status_code}")
            except Exception as e:
                logger.warning(f"Error checking subscription state: {e}")

            if val_attempt < max_validation_attempts - 1:
                time.sleep(validation_interval)

        # Step 7: Update metadata on CloudBolt resource with actual state
        resource = job.resource_set.first()
        actual_state = sub_state or "Unknown"

        if resource:
            resource.set_value_for_custom_field("azure_subscription_state", actual_state)
            resource.set_value_for_custom_field("azure_subscription_billing_scope", invoice_section)
            resource.set_value_for_custom_field("azure_subscription_management_group", management_group or "")
            resource.name = subscription_name
            resource.save()
            set_progress("✓ Final subscription metadata saved to CloudBolt resource")

        success_msg = f"Azure subscription '{subscription_name}' created.\n"
        success_msg += f"Subscription ID: {subscription_id}\n"
        success_msg += f"Destination Tenant: {destination_tenant}\n"
        success_msg += f"Management Group: {management_group or 'None'}\n"
        success_msg += f"Billing: {invoice_section}\n"
        success_msg += f"State: {actual_state}\n"

        if owner_email:
            success_msg += f"Owner: {owner_email} added via RBAC role assignment\n"

        if not ownership_accepted:
            success_msg += "\n⚠ Ownership auto-accept did not complete - subscription may require manual acceptance."
            logger.warning(success_msg)
            return "WARNING", success_msg, ""

        if actual_state not in ("Enabled", "Warned", "PastDue"):
            success_msg += (
                f"\n⚠ Subscription did not reach 'Enabled' state within "
                f"{max_validation_attempts * validation_interval}s (current: {actual_state})."
            )
            logger.warning(success_msg)
            return "WARNING", success_msg, ""

        logger.info(success_msg)
        return "SUCCESS", success_msg, ""

    except requests.exceptions.HTTPError as e:
        error_msg = f"Azure API error: {e.response.status_code} - {e.response.text}"
        logger.exception(error_msg)
        return "FAILURE", error_msg, ""
    except Exception as e:
        error_msg = f"Unexpected error creating subscription: {str(e)}"
        logger.exception(error_msg)
        return "FAILURE", error_msg, ""