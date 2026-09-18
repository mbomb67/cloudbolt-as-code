"""
CloudBolt Day-2 resource action: install the CloudBolt public-exposure
Azure Policy initiative on a subscription.

This action is opt-in and one-shot per subscription. It backstops the
gaps the Restricted Contributor role's notActions cannot cleanly close:

  - subnet exposure (NSG attachment / public NAT/IGW),
  - storage publicNetworkAccess / allowBlobPublicAccess mutations.

The initiative is a custom Azure policySetDefinition that aggregates
built-in Azure policy definitions by reference and pins their `effect`
parameter (Deny where supported). The full definition is inlined below
in INITIATIVE_NAME and INITIATIVE_PAYLOAD so the plugin is a single
self-contained file -- CloudBolt's plugin runtime does not set __file__,
so loading from a sibling JSON path is not viable.

Built-in policy GUIDs are pinned to the values in INITIATIVE_PAYLOAD.
Microsoft occasionally retires or replaces built-in policies, which would
cause the PUT to fail with a missing-definition error. Verify against the
Azure built-in policies reference before each major deployment:

  https://learn.microsoft.com/en-us/azure/governance/policy/samples/built-in-policies

Idempotency:

  - Custom set definition: GET by name; PUT if missing.
  - Assignment: GET by name; if missing, PUT.
  - If the assignment exists and points at our set definition by ID,
    return WARNING ("already applied") -- explicitly NOT FAILURE.
  - If the assignment exists but points at a DIFFERENT
    policyDefinitionId / policySetDefinitionId, return FAILURE with
    "conflicting assignment exists" rather than silently no-opping.
    A silent no-op against the wrong assignment leaves the public-exposure
    gap open while reporting success.

No inputs. Returns the standard CloudBolt 3-tuple.
"""

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
    safe_message,
)

logger = ThreadLogger(__name__)

POLICY_API_VERSION = "2023-04-01"

ASSIGNMENT_NAME = "cloudbolt-public-exposure"
ASSIGNMENT_DISPLAY_NAME = "CloudBolt Public-Exposure Restrictions"
ASSIGNMENT_DESCRIPTION = (
    "Installed by the CloudBolt apply_public_exposure_policy action. "
    "Denies storage public network access mutations. Audits subnets "
    "without an NSG (Azure's built-in for that check is audit-only)."
)

REQUIRED_PERMISSIONS = [
    "Microsoft.Authorization/policySetDefinitions/write",
    "Microsoft.Authorization/policyAssignments/write",
]

# ============================================================================
# Inline initiative definition.
#
# v1 scope: storage public-access (Deny) + subnet NSG (AuditIfNotExists).
# SQL/Cosmos/KeyVault/AppService public-access controls are deferred per the plan.
#
# Each entry references a tenant-scope built-in policy by its GUID and pins
# its `effect` parameter explicitly. The effect value MUST be one Azure
# advertises as allowed for that specific built-in -- otherwise PUT fails
# with PolicyParameterValueNotAllowed naming the missing/disallowed value.
#
# Note on the subnet-NSG policy: built-in e71308d3-... is structurally an
# AuditIfNotExists policy and does NOT support Deny -- Azure only audits the
# NSG-association relationship after the fact. The Restricted Contributor
# role still over-blocks subnet writes via notActions on
# Microsoft.Network/applicationGateways/write etc., so a grantee cannot
# easily attach public NAT/IGW; the policy here surfaces non-compliant
# subnets to auditors as a defense-in-depth complement, not a hard gate.
# ============================================================================

INITIATIVE_NAME = "cloudbolt-public-exposure-initiative"

INITIATIVE_PAYLOAD = {
    "properties": {
        "displayName": "CloudBolt Public-Exposure Restrictions",
        "description": (
            "Denies operations that expose subscription resources publicly "
            "(storage public network access). Audits subnets without an NSG "
            "(no Deny mode available for that built-in). Pairs with the "
            "CloudBolt Restricted Contributor role to close gaps that "
            "role-level notActions cannot express."
        ),
        "policyType": "Custom",
        "metadata": {"category": "CloudBolt", "version": "1.0.1"},
        "parameters": {},
        "policyDefinitions": [
            {
                # Storage account public access should be disallowed (built-in).
                # Supports Deny.
                "policyDefinitionReferenceId": "storage-disable-public-network-access",
                "policyDefinitionId": (
                    "/providers/Microsoft.Authorization/policyDefinitions/"
                    "4fa4b6c0-31ca-4c0d-b10d-24b96f62a751"
                ),
                "parameters": {"effect": {"value": "Deny"}},
            },
            {
                # Storage accounts should restrict network access (built-in).
                # Supports Deny.
                "policyDefinitionReferenceId": "storage-restrict-network-access",
                "policyDefinitionId": (
                    "/providers/Microsoft.Authorization/policyDefinitions/"
                    "34c877ad-507e-4c82-993e-3452a6e0ad3c"
                ),
                "parameters": {"effect": {"value": "Deny"}},
            },
            {
                # Subnets should be associated with a Network Security Group
                # (built-in). Audit-only -- this built-in does NOT expose Deny;
                # allowed values are AuditIfNotExists or Disabled (per Azure's
                # PolicyParameterValueNotAllowed error). Surfaces non-compliant
                # subnets to auditors but does not block creation.
                "policyDefinitionReferenceId": "subnets-require-nsg",
                "policyDefinitionId": (
                    "/providers/Microsoft.Authorization/policyDefinitions/"
                    "e71308d3-144b-4262-b144-efdc3cc90517"
                ),
                "parameters": {"effect": {"value": "AuditIfNotExists"}},
            },
        ],
    }
}


def run(job, resource, **kwargs):
    set_progress("Starting Apply Public-Exposure Policy...")

    subscription_id = resource.get_value_for_custom_field("azure_subscription_id")
    tenant_id = resource.get_value_for_custom_field("azure_subscription_tenant_id")
    rh_id = resource.get_value_for_custom_field("azure_subscription_rh_id")

    if not subscription_id or not tenant_id or not rh_id:
        return (
            "FAILURE",
            "Subscription metadata missing on this resource (azure_subscription_id "
            "/ azure_subscription_tenant_id / azure_subscription_rh_id).",
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

    set_progress(f"Applying public-exposure policy to subscription {subscription_id}")

    set_name = INITIATIVE_NAME
    set_payload = INITIATIVE_PAYLOAD

    try:
        token = get_azure_token(tenant_id, rh.client_id, rh.secret)
    except Exception as exc:
        return "FAILURE", f"Azure token acquisition failed: {safe_message(str(exc))}", ""

    set_progress("Pre-flight: probing SP permissions for policy installation...")
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
                f"{', '.join(missing)}. Add 'Resource Policy Contributor' (or "
                f"'Owner') at subscription scope and retry."
            ),
            "",
        )

    set_definition_path = (
        f"/subscriptions/{subscription_id}/providers/"
        f"Microsoft.Authorization/policySetDefinitions/{set_name}"
    )
    set_definition_url = f"{AZURE_MGMT_ENDPOINT}{set_definition_path}"
    api_params = {"api-version": POLICY_API_VERSION}

    # ----- Ensure custom policySetDefinition exists -------------------------
    try:
        existing_set = azure_api_call("GET", set_definition_url, token, params=api_params)
    except requests.exceptions.HTTPError as exc:
        return (
            "FAILURE",
            f"Failed to query policy set definition: {format_azure_http_error(exc)}",
            "",
        )

    if existing_set.get("error") == "NotFound":
        set_progress(f"Creating custom policy set definition '{set_name}'...")
        try:
            azure_api_call(
                "PUT",
                set_definition_url,
                token,
                data=set_payload,
                params=api_params,
            )
        except requests.exceptions.HTTPError as exc:
            return (
                "FAILURE",
                (
                    f"Failed to create policy set definition: "
                    f"{format_azure_http_error(exc)}. "
                    f"This often means a referenced built-in policyDefinitionId "
                    f"in INITIATIVE_PAYLOAD (apply_public_exposure_policy.py) "
                    f"is stale -- verify the GUIDs against the Azure built-in "
                    f"policies reference."
                ),
                "",
            )
    else:
        set_progress(f"Reusing existing policy set definition '{set_name}'")

    # ----- Ensure assignment exists, and check it points at OUR set ---------
    assignment_path = (
        f"/subscriptions/{subscription_id}/providers/"
        f"Microsoft.Authorization/policyAssignments/{ASSIGNMENT_NAME}"
    )
    assignment_url = f"{AZURE_MGMT_ENDPOINT}{assignment_path}"

    try:
        existing_assignment = azure_api_call("GET", assignment_url, token, params=api_params)
    except requests.exceptions.HTTPError as exc:
        return (
            "FAILURE",
            f"Failed to query policy assignment: {format_azure_http_error(exc)}",
            "",
        )

    if existing_assignment.get("error") != "NotFound":
        # Assignment exists -- verify it points at OUR set definition.
        existing_target = (
            existing_assignment.get("properties", {}) or {}
        ).get("policyDefinitionId", "")

        # set_definition_path uses subscription-scoped path; Azure may return
        # the same value verbatim or with case-normalization. Compare loosely.
        if existing_target.lower() == set_definition_path.lower():
            msg = (
                f"Public-exposure policy assignment is already applied on "
                f"subscription {subscription_id} and points at the expected "
                f"set definition. No-op."
            )
            return "WARNING", msg, ""

        return (
            "FAILURE",
            (
                f"A policy assignment named '{ASSIGNMENT_NAME}' already exists on "
                f"subscription {subscription_id}, but it targets a different "
                f"policyDefinitionId ({safe_message(existing_target)!r}) than the "
                f"CloudBolt public-exposure initiative ({set_definition_path!r}). "
                f"Refusing to overwrite -- manual review required. Delete the "
                f"existing assignment via Azure portal or CLI if you want this "
                f"action to replace it."
            ),
            "",
        )

    # ----- Create the assignment --------------------------------------------
    set_progress(f"Creating policy assignment '{ASSIGNMENT_NAME}'...")
    assignment_payload = {
        "properties": {
            "displayName": ASSIGNMENT_DISPLAY_NAME,
            "description": ASSIGNMENT_DESCRIPTION,
            "policyDefinitionId": set_definition_path,
            "enforcementMode": "Default",
        }
    }

    try:
        azure_api_call(
            "PUT",
            assignment_url,
            token,
            data=assignment_payload,
            params=api_params,
        )
    except requests.exceptions.HTTPError as exc:
        return (
            "FAILURE",
            f"Failed to create policy assignment: {format_azure_http_error(exc)}",
            "",
        )

    msg = (
        f"CloudBolt public-exposure policy installed on subscription "
        f"{subscription_id}. Assignment name: {ASSIGNMENT_NAME}.\n\n"
        f"Effects:\n"
        f"  - Storage public network access: DENY (blocks creation/update).\n"
        f"  - Storage network access restriction: DENY (blocks public default).\n"
        f"  - Subnets without an NSG: AUDIT (built-in does not expose Deny; "
        f"non-compliant subnets are surfaced for review but creation is not blocked).\n\n"
        f"Verify in the Azure portal under Policy → Assignments."
    )
    logger.info(msg)
    return "SUCCESS", msg, ""
