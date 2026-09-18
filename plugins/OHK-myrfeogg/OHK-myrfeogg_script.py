"""
CloudBolt Day-2 resource action: revoke the "CloudBolt Restricted Contributor"
role from a named user on an Azure subscription.

Scope: this action ONLY removes role assignments whose roleDefinitionId
matches the Restricted Contributor role created by Grant. Other roles the
user holds at subscription scope (Owner, Reader, separately-granted custom
roles, ...) are NOT touched. The output message lists those untouched
assignments so the operator can see what wasn't removed.

The action label and progress messages must read "Revoke Restricted
Contributor Access" (not "Revoke Subscription Access") so the operator's
mental model matches the scope -- the action does less than its bare
filename suggests, and the safer default is to surface that explicitly.

The role definition is resolved by the same deterministic UUIDv5 used by
Grant. This makes Revoke unambiguous even if duplicate-named role
definitions exist on the subscription due to historical drift.

This action looks at assignments at AND below subscription scope (not just
atScope()), so any out-of-band Restricted Contributor assignment created
at a resource-group or resource scope is also cleaned up. The role's
assignableScopes is constrained to /subscriptions/{sub} exactly, but
nothing outside this plugin enforces that constraint -- a portal admin
could in principle create an assignment at a sub-scope, and Revoke should
catch it.

Single Action Input: user_email.

Returns the standard CloudBolt 3-tuple. WARNING for idempotent no-op
(nothing to revoke / role definition does not exist); FAILURE for real
errors (auth, permissions, user not found).
"""

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

ROLE_ASSIGNMENT_API_VERSION = "2022-04-01"
ROLE_DEFINITION_API_VERSION = "2022-04-01"

REQUIRED_PERMISSIONS = [
    "Microsoft.Authorization/roleAssignments/read",
    "Microsoft.Authorization/roleAssignments/delete",
]


def _restricted_contributor_role_guid(subscription_id):
    """Same deterministic GUID Grant uses (kept in sync intentionally)."""
    return str(
        uuid.uuid5(
            uuid.NAMESPACE_OID,
            f"CloudBoltRestrictedContributor:{subscription_id}",
        )
    )


def _restricted_contributor_role_definition_id(subscription_id):
    return (
        f"/subscriptions/{subscription_id}/providers/"
        f"Microsoft.Authorization/roleDefinitions/"
        f"{_restricted_contributor_role_guid(subscription_id)}"
    )


def run(job, resource, **kwargs):
    set_progress("Starting Revoke Restricted Contributor Access...")

    user_email = "{{ user_email }}".strip()
    if not user_email or user_email == "user_email":
        return "FAILURE", "user_email is required.", ""

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

    set_progress(
        f"Revoke Restricted Contributor on subscription {subscription_id} for {user_email}"
    )

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
                f"{', '.join(missing)}."
            ),
            "",
        )

    # ----- Resolve the user --------------------------------------------------
    user_object_id, lookup_error = get_user_object_id_by_email(
        user_email, tenant_id, rh.client_id, rh.secret
    )
    if lookup_error:
        return "FAILURE", lookup_error, ""

    # ----- Resolve the Restricted Contributor role definition ---------------
    rc_role_definition_id = _restricted_contributor_role_definition_id(subscription_id)
    rc_role_url = f"{AZURE_MGMT_ENDPOINT}{rc_role_definition_id}"
    try:
        role_check = azure_api_call(
            "GET",
            rc_role_url,
            token,
            params={"api-version": ROLE_DEFINITION_API_VERSION},
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

    if role_check.get("error") == "NotFound":
        msg = (
            f"No Restricted Contributor role definition exists on subscription "
            f"{subscription_id}. Nothing to revoke. (No prior Grant has run "
            f"on this subscription.)"
        )
        logger.warning(msg)
        return "WARNING", msg, ""

    # Match against the resource-style id Azure returns (case-insensitive,
    # since Azure occasionally normalizes case in IDs).
    target_role_definition_id_norm = rc_role_definition_id.lower()

    # ----- List role assignments for this principal -------------------------
    # Intentionally NOT using $filter=atScope() so we also catch out-of-band
    # assignments at resource-group / resource scope under this subscription.
    list_url = (
        f"{AZURE_MGMT_ENDPOINT}/subscriptions/{subscription_id}"
        f"/providers/Microsoft.Authorization/roleAssignments"
    )
    list_params = {
        "api-version": ROLE_ASSIGNMENT_API_VERSION,
        "$filter": f"principalId eq '{user_object_id}'",
    }

    set_progress("Listing role assignments for this principal at subscription scope...")
    try:
        list_response = azure_api_call("GET", list_url, token, params=list_params)
    except requests.exceptions.HTTPError as exc:
        return (
            "FAILURE",
            f"Failed to list role assignments: {format_azure_http_error(exc)}",
            "",
        )

    all_assignments = list_response.get("value", []) or []

    matching = []
    other = []
    for assignment in all_assignments:
        props = assignment.get("properties", {}) or {}
        rd_id = (props.get("roleDefinitionId") or "").lower()
        if rd_id == target_role_definition_id_norm:
            matching.append(assignment)
        else:
            other.append(assignment)

    if not matching:
        msg_lines = [
            f"User {user_email} has no Restricted Contributor role assignment "
            f"on subscription {subscription_id}; nothing to revoke."
        ]
        if other:
            msg_lines.append("")
            msg_lines.append("Other role assignments on this subscription for this user (NOT touched):")
            for assignment in other:
                msg_lines.append(
                    f"  - {_describe_assignment(assignment)}"
                )
        msg_lines.append("")
        msg_lines.append(
            "NOTE: Inherited assignments from management groups or tenant root "
            "are not visible to this action and may grant additional access."
        )
        return "WARNING", "\n".join(msg_lines), ""

    # ----- Delete each matching assignment ----------------------------------
    deleted = []
    failures = []
    for assignment in matching:
        assignment_id_path = assignment.get("id")
        if not assignment_id_path:
            failures.append(("<unknown>", "assignment had no id"))
            continue
        delete_url = f"{AZURE_MGMT_ENDPOINT}{assignment_id_path}"
        scope_label = (assignment.get("properties", {}) or {}).get("scope", "<unknown scope>")
        try:
            azure_api_call(
                "DELETE",
                delete_url,
                token,
                params={"api-version": ROLE_ASSIGNMENT_API_VERSION},
            )
            deleted.append((assignment.get("name"), scope_label))
            set_progress(f"✓ Removed assignment {assignment.get('name')} at {scope_label}")
        except requests.exceptions.HTTPError as exc:
            failures.append((assignment.get("name"), format_azure_http_error(exc)))

    msg_lines = [
        f"Revoke Restricted Contributor for {user_email} on subscription "
        f"{subscription_id}:",
        f"  Removed {len(deleted)} assignment(s):",
    ]
    for name, scope_label in deleted:
        msg_lines.append(f"    - {name} (scope: {scope_label})")

    if failures:
        msg_lines.append("")
        msg_lines.append(f"  Failed to remove {len(failures)} assignment(s):")
        for name, err in failures:
            msg_lines.append(f"    - {name}: {err}")

    if other:
        msg_lines.append("")
        msg_lines.append(
            "Other role assignments still held by this user at subscription scope "
            "(NOT touched -- use the Azure portal to remove these):"
        )
        for assignment in other:
            msg_lines.append(f"  - {_describe_assignment(assignment)}")

    msg_lines.append("")
    msg_lines.append(
        "NOTE: Inherited assignments from management groups or tenant root are "
        "not visible to this action and may grant additional access."
    )

    final_message = "\n".join(msg_lines)
    if failures:
        return "FAILURE", final_message, ""
    return "SUCCESS", final_message, ""


def _describe_assignment(assignment):
    props = assignment.get("properties", {}) or {}
    rd_id = props.get("roleDefinitionId") or "<unknown role def>"
    role_name_hint = rd_id.rsplit("/", 1)[-1] if "/" in rd_id else rd_id
    scope = props.get("scope") or "<unknown scope>"
    return f"role={role_name_hint} scope={scope}"
