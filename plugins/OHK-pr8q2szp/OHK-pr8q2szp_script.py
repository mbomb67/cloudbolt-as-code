"""
CloudBolt Day-2 resource action: grant a named user the "CloudBolt Restricted
Contributor" role on an Azure subscription.

Restricted Contributor = built-in Contributor's actions (`["*"]`) MINUS:

- Privilege-escalation guards (NON-NEGOTIABLE):
  Microsoft.Authorization/roleDefinitions/{write,delete},
  Microsoft.Authorization/roleAssignments/{write,delete}.
  Without these, the grantee can rewrite the role definition or self-grant
  Owner via roleAssignments -- a one-API-call privilege escalation.

- Bulk-destruction guard (default-include):
  Microsoft.Resources/subscriptions/resourceGroups/delete,
  Microsoft.Resources/deployments/delete.

- Public-exposure guards: Public IP primitives, NAT/Firewall/Bastion,
  Front Door, CDN, AppGw/LB write, public DNS zones, storage public-access
  mutations (partial -- Apply-Policy backstops).

The role definition is created idempotently at subscription scope on first
call (GET by deterministic UUIDv5 -> 404 -> PUT). Subsequent grants reuse
the existing role. Two concurrent grants converge on the same GUID, so the
race is benign.

The action does NOT auto-apply the public-exposure policy initiative; that
is a separate Day-2 action (apply_public_exposure_policy.py). The first
Grant on a subscription surfaces a recommendation in its progress output to
run Apply-Policy.

Single Action Input: user_email (required, no default -- this grants
access to someone else, not the requester).

Returns the standard CloudBolt ("SUCCESS"|"FAILURE"|"WARNING", message, "")
3-tuple.
"""

import json
import uuid

import requests

from common.methods import set_progress
from resourcehandlers.azure_arm.models import AzureARMHandler
from utilities.logger import ThreadLogger

from shared_modules.azure_subscription_helpers import (
    AZURE_MGMT_ENDPOINT,
    azure_api_call,
    check_subscription_permissions,
    format_azure_http_error,
    get_azure_token,
    get_user_object_id_by_email,
    safe_message,
)

logger = ThreadLogger(__name__)

ROLE_DEFINITION_NAME = "CloudBolt Restricted Contributor"
ROLE_DEFINITION_DESCRIPTION = (
    "Contributor minus operations that expose resources publicly and minus "
    "RBAC self-modification. Subnet exposure and storage publicNetworkAccess "
    "are NOT fully blocked by this role alone -- pair with the CloudBolt "
    "public-exposure policy initiative (apply_public_exposure_policy action) "
    "for full coverage."
)

# Effective Contributor minus public-exposure ops + privilege-escalation
# guards. The exact strings track the Azure RBAC permissions reference --
# review when Azure adds new resource providers with public-exposure surfaces.
RESTRICTED_CONTRIBUTOR_NOT_ACTIONS = [
    # Privilege-escalation guards (NON-NEGOTIABLE -- mirrors built-in Contributor)
    "Microsoft.Authorization/roleDefinitions/write",
    "Microsoft.Authorization/roleDefinitions/delete",
    "Microsoft.Authorization/roleAssignments/write",
    "Microsoft.Authorization/roleAssignments/delete",
    # Bulk-destruction guard (default-include; remove only with a Risk-table entry)
    "Microsoft.Resources/subscriptions/resourceGroups/delete",
    "Microsoft.Resources/deployments/delete",
    # Public IP primitives
    "Microsoft.Network/publicIPAddresses/*",
    "Microsoft.Network/publicIPPrefixes/*",
    # Public-egress / public-ingress network resources
    "Microsoft.Network/natGateways/*",
    "Microsoft.Network/azureFirewalls/*",
    "Microsoft.Network/bastionHosts/*",
    # Public-facing edge (note: write over-blocks internal LB/AppGw -- Risk R-1b)
    "Microsoft.Network/frontDoors/*",
    "Microsoft.Cdn/*",
    "Microsoft.Network/applicationGateways/write",
    "Microsoft.Network/loadBalancers/write",
    # Public DNS
    "Microsoft.Network/dnsZones/*",
]

ROLE_DEFINITION_API_VERSION = "2022-04-01"
ROLE_ASSIGNMENT_API_VERSION = "2022-04-01"

REQUIRED_PERMISSIONS = [
    "Microsoft.Authorization/roleDefinitions/write",
    "Microsoft.Authorization/roleAssignments/write",
]


def _restricted_contributor_role_guid(subscription_id):
    """
    Deterministic role-definition GUID per subscription so concurrent Grant
    calls converge on the same target and Revoke can resolve the role
    without a name-based GET (which is ambiguous if duplicate-named roles
    exist).
    """
    return str(
        uuid.uuid5(
            uuid.NAMESPACE_OID,
            f"CloudBoltRestrictedContributor:{subscription_id}",
        )
    )


def _build_role_definition_payload(subscription_id):
    return {
        "properties": {
            "roleName": ROLE_DEFINITION_NAME,
            "description": ROLE_DEFINITION_DESCRIPTION,
            "type": "CustomRole",
            "permissions": [
                {
                    "actions": ["*"],
                    "notActions": list(RESTRICTED_CONTRIBUTOR_NOT_ACTIONS),
                    "dataActions": [],
                    "notDataActions": [],
                }
            ],
            # Restrict to exactly this subscription so out-of-band assignments
            # at sub-scopes are rejected by Azure on assignment-create.
            "assignableScopes": [f"/subscriptions/{subscription_id}"],
        }
    }


def run(job, resource, **kwargs):
    set_progress("Starting Grant Restricted Contributor Access...")

    # ----- Inputs and resource metadata --------------------------------------
    user_email = "{{ user_email }}".strip()
    if not user_email or user_email == "user_email":
        return "FAILURE", "user_email is required.", ""

    subscription_id = resource.get_value_for_custom_field("azure_subscription_id")
    tenant_id = resource.get_value_for_custom_field("azure_subscription_tenant_id")
    rh_id = resource.get_value_for_custom_field("azure_subscription_rh_id")

    if not subscription_id or not tenant_id or not rh_id:
        return (
            "FAILURE",
            "Subscription metadata missing on this resource "
            "(azure_subscription_id / azure_subscription_tenant_id / "
            "azure_subscription_rh_id). Discovery or build must populate "
            "these before granting access.",
            "",
        )

    try:
        rh = AzureARMHandler.objects.get(id=rh_id)
    except AzureARMHandler.DoesNotExist:
        return (
            "FAILURE",
            f"Azure resource handler id={rh_id} not found; cannot authenticate.",
            "",
        )

    set_progress(
        f"Granting Restricted Contributor on subscription {subscription_id} "
        f"in tenant {tenant_id} via handler {rh.name}"
    )

    # ----- Auth and pre-flight permission probe ------------------------------
    try:
        token = get_azure_token(tenant_id, rh.client_id, rh.secret)
    except Exception as exc:
        return "FAILURE", f"Azure token acquisition failed: {safe_message(str(exc))}", ""

    set_progress("Pre-flight: probing SP permissions at subscription scope...")
    missing, probe_error = check_subscription_permissions(
        subscription_id, REQUIRED_PERMISSIONS, token
    )
    if probe_error:
        return (
            "FAILURE",
            f"Could not verify SP permissions on this subscription. {probe_error}",
            "",
        )
    if missing:
        return (
            "FAILURE",
            (
                f"Service principal on handler '{rh.name}' lacks required "
                f"permissions on subscription {subscription_id}. Missing: "
                f"{', '.join(missing)}. Grant the SP 'User Access Administrator' "
                f"or 'Owner' at subscription scope and retry."
            ),
            "",
        )

    # ----- Resolve user via Microsoft Graph ----------------------------------
    set_progress(f"Looking up Azure AD user for {user_email}...")
    user_object_id, lookup_error = get_user_object_id_by_email(
        user_email, tenant_id, rh.client_id, rh.secret
    )
    if lookup_error:
        return "FAILURE", lookup_error, ""

    set_progress(f"Resolved {user_email} -> object id {user_object_id}")

    # ----- Idempotently ensure the custom role definition --------------------
    role_guid = _restricted_contributor_role_guid(subscription_id)
    role_definition_url = (
        f"{AZURE_MGMT_ENDPOINT}/subscriptions/{subscription_id}"
        f"/providers/Microsoft.Authorization/roleDefinitions/{role_guid}"
    )
    role_definition_id = (
        f"/subscriptions/{subscription_id}/providers/"
        f"Microsoft.Authorization/roleDefinitions/{role_guid}"
    )
    api_params = {"api-version": ROLE_DEFINITION_API_VERSION}

    role_existed = True
    try:
        get_response = azure_api_call(
            "GET", role_definition_url, token, params=api_params
        )
    except requests.exceptions.HTTPError as exc:
        return (
            "FAILURE",
            (
                f"Failed to query Restricted Contributor role definition: "
                f"{format_azure_http_error(exc)}"
            ),
            "",
        )

    if get_response.get("error") == "NotFound":
        role_existed = False
        set_progress(
            "Restricted Contributor role does not exist on this subscription; "
            "creating it now..."
        )
        try:
            azure_api_call(
                "PUT",
                role_definition_url,
                token,
                data=_build_role_definition_payload(subscription_id),
                params=api_params,
            )
            set_progress("✓ Restricted Contributor role definition created")
        except requests.exceptions.HTTPError as exc:
            # 409 here is harmless: a concurrent Grant created the same
            # deterministic GUID first. Treat as already-exists.
            if exc.response.status_code == 409:
                logger.info(
                    "Concurrent Grant created the role definition; continuing"
                )
                role_existed = True
            else:
                return (
                    "FAILURE",
                    (
                        f"Failed to create Restricted Contributor role "
                        f"definition: {format_azure_http_error(exc)}"
                    ),
                    "",
                )
    else:
        set_progress("Reusing existing Restricted Contributor role definition")

    # ----- Assign the role to the user ---------------------------------------
    assignment_guid = str(uuid.uuid4())
    assignment_url = (
        f"{AZURE_MGMT_ENDPOINT}/subscriptions/{subscription_id}"
        f"/providers/Microsoft.Authorization/roleAssignments/{assignment_guid}"
    )
    assignment_params = {"api-version": ROLE_ASSIGNMENT_API_VERSION}
    assignment_payload = {
        "properties": {
            "roleDefinitionId": role_definition_id,
            "principalId": user_object_id,
            "principalType": "User",
        }
    }

    set_progress(f"Assigning Restricted Contributor to {user_email}...")
    try:
        azure_api_call(
            "PUT",
            assignment_url,
            token,
            data=assignment_payload,
            params=assignment_params,
        )
    except requests.exceptions.HTTPError as exc:
        if exc.response.status_code == 409:
            # Azure returns 409 when the principal already has this role on
            # this scope. Treat as success.
            msg = (
                f"User {user_email} already has Restricted Contributor on "
                f"subscription {subscription_id}."
            )
            logger.info(msg)
            set_progress(f"✓ {msg}")
            return "SUCCESS", msg, ""
        return (
            "FAILURE",
            (
                f"Failed to assign Restricted Contributor to {user_email}: "
                f"{format_azure_http_error(exc)}"
            ),
            "",
        )

    success_lines = [
        f"Granted Restricted Contributor on subscription {subscription_id} to "
        f"{user_email} (object id {user_object_id}).",
    ]

    if not role_existed:
        success_lines.append("")
        success_lines.append(
            "RECOMMENDED: Run the 'Apply Public-Exposure Policy' action on this "
            "subscription. Restricted Contributor blocks the most common public-"
            "exposure operations via notActions, but cannot cleanly express:"
        )
        success_lines.append(
            "  - subnet exposure (the same write permission covers legitimate "
            "edits and exposure changes)"
        )
        success_lines.append(
            "  - storage account publicNetworkAccess / allowBlobPublicAccess "
            "property mutations"
        )
        success_lines.append(
            "Apply-Policy installs an Azure Policy initiative that closes these "
            "gaps with publicness-aware deny rules."
        )

    final_message = "\n".join(success_lines)
    logger.info(f"Grant succeeded: {json.dumps({'sub': subscription_id, 'user': user_email})}")
    return "SUCCESS", final_message, ""
