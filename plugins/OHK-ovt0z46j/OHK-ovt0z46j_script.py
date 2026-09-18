"""
CloudBolt Day-2 resource action: list role assignments at subscription scope
on an Azure subscription, with principal type and resolved display names.

Output is a preformatted table in the action message. Inherited assignments
from management groups or tenant root are intentionally excluded ($filter=
atScope() constrains to direct subscription-scope assignments) -- the
output footer documents this so an operator does not mistake the list for
a complete access audit.

The action does NOT have any inputs. It runs against the resource it is
launched from.

Returns the standard CloudBolt 3-tuple. SUCCESS even when some Graph
lookups fail with 403 (insufficient Group.Read.All / Application.Read.All)
or 404 (deleted/external principals): partial output is more useful than
no output. The footer notes which principals could not be resolved.

Output sensitivity: this enumerates every identity assigned at subscription
scope, which is reconnaissance-grade information. The action inherits
CloudBolt's standard job-output visibility model (resource owner + group
admins see job history). DEPLOYMENT_GUIDE.md notes the sensitivity.
"""

import requests

from common.methods import set_progress
from resourcehandlers.azure_arm.models import AzureARMHandler
from utilities.logger import ThreadLogger

from shared_modules.azure_subscription_helpers import (
    AZURE_GRAPH_ENDPOINT,
    AZURE_MGMT_ENDPOINT,
    azure_api_call,
    check_subscription_permissions,
    format_azure_http_error,
    get_azure_token,
    safe_message,
)

logger = ThreadLogger(__name__)

ROLE_ASSIGNMENT_API_VERSION = "2022-04-01"
ROLE_DEFINITION_API_VERSION = "2022-04-01"

REQUIRED_PERMISSIONS = ["Microsoft.Authorization/roleAssignments/read"]


def run(job, resource, **kwargs):
    set_progress("Starting List Subscription Access...")

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

    set_progress(f"Listing direct subscription-scope assignments for {subscription_id}")

    try:
        mgmt_token = get_azure_token(tenant_id, rh.client_id, rh.secret)
    except Exception as exc:
        return "FAILURE", f"Azure token acquisition failed: {safe_message(str(exc))}", ""

    set_progress("Pre-flight: probing SP read permissions on role assignments...")
    missing, probe_error = check_subscription_permissions(
        subscription_id, REQUIRED_PERMISSIONS, mgmt_token
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

    # ----- List assignments at subscription scope only ----------------------
    list_url = (
        f"{AZURE_MGMT_ENDPOINT}/subscriptions/{subscription_id}"
        f"/providers/Microsoft.Authorization/roleAssignments"
    )
    list_params = {
        "api-version": ROLE_ASSIGNMENT_API_VERSION,
        "$filter": "atScope()",
    }

    try:
        list_response = azure_api_call("GET", list_url, mgmt_token, params=list_params)
    except requests.exceptions.HTTPError as exc:
        return (
            "FAILURE",
            f"Failed to list role assignments: {format_azure_http_error(exc)}",
            "",
        )

    assignments = list_response.get("value", []) or []

    if not assignments:
        return (
            "SUCCESS",
            f"No role assignments at subscription scope on {subscription_id}.",
            "",
        )

    # ----- Acquire a Graph token for principal display-name resolution ------
    graph_token = None
    graph_token_error = None
    try:
        graph_token = get_azure_token(
            tenant_id,
            rh.client_id,
            rh.secret,
            resource="https://graph.microsoft.com/",
        )
    except Exception as exc:
        graph_token_error = str(exc)
        logger.warning(f"Graph token acquisition failed: {exc}")

    # ----- Resolve role definitions (cache by id) ---------------------------
    role_def_cache = {}

    def _resolve_role_name(role_definition_id):
        if not role_definition_id:
            return "<unknown role>"
        if role_definition_id in role_def_cache:
            return role_def_cache[role_definition_id]
        url = f"{AZURE_MGMT_ENDPOINT}{role_definition_id}"
        try:
            response = azure_api_call(
                "GET", url, mgmt_token, params={"api-version": ROLE_DEFINITION_API_VERSION}
            )
            name = response.get("properties", {}).get("roleName", "<unknown role>")
        except Exception as exc:
            logger.warning(f"Could not resolve role definition {role_definition_id}: {exc}")
            name = role_definition_id.rsplit("/", 1)[-1]
        role_def_cache[role_definition_id] = name
        return name

    # ----- Resolve principal display name + canonical type ------------------
    rows = []
    unresolved_count = 0
    for assignment in assignments:
        props = assignment.get("properties", {}) or {}
        principal_id = props.get("principalId")
        principal_type = props.get("principalType", "Unknown")
        role_definition_id = props.get("roleDefinitionId")
        role_name = _resolve_role_name(role_definition_id)

        display, type_label, resolved = _resolve_principal(
            principal_id, principal_type, graph_token, graph_token_error
        )
        if not resolved:
            unresolved_count += 1

        rows.append((display, type_label, role_name))

    # ----- Format the output table ------------------------------------------
    col1 = max(len("Principal"), max((len(r[0]) for r in rows), default=0))
    col2 = max(len("Type"), max((len(r[1]) for r in rows), default=0))
    col3 = max(len("Role"), max((len(r[2]) for r in rows), default=0))

    header = f"{'Principal':<{col1}}  {'Type':<{col2}}  {'Role':<{col3}}"
    separator = "-" * (col1 + col2 + col3 + 4)
    lines = [header, separator]
    for display, type_label, role_name in rows:
        lines.append(f"{display:<{col1}}  {type_label:<{col2}}  {role_name:<{col3}}")

    lines.append("")
    lines.append(
        f"Showing {len(rows)} direct assignment(s) at subscription scope "
        f"({subscription_id})."
    )
    lines.append(
        "Note: This list shows only direct assignments at subscription scope. "
        "Role assignments inherited from management groups or the tenant root "
        "are not shown and may grant additional access."
    )
    if unresolved_count:
        lines.append(
            f"{unresolved_count} principal(s) could not be resolved via Microsoft "
            f"Graph (deleted, external, or insufficient SP Graph permissions: "
            f"Group.Read.All / Application.Read.All / User.Read.All)."
        )

    return "SUCCESS", "\n".join(lines), ""


def _resolve_principal(principal_id, principal_type, graph_token, graph_token_error):
    """
    Look up a principal's display name via Microsoft Graph based on its type.
    Returns (display_string, type_label, resolved_bool). Falls back to a raw-
    GUID rendering on any failure rather than aborting the action.
    """
    if not principal_id:
        return ("<missing id>", principal_type or "Unknown", False)

    if not graph_token:
        return (
            f"<{graph_token_error or 'no graph token'}> {principal_id}",
            principal_type or "Unknown",
            False,
        )

    endpoint_by_type = {
        "User": ("/users", "userPrincipalName"),
        "Group": ("/groups", "displayName"),
        "ServicePrincipal": ("/servicePrincipals", "displayName"),
    }

    path, label_field = endpoint_by_type.get(
        principal_type, ("/directoryObjects", "displayName")
    )
    url = f"{AZURE_GRAPH_ENDPOINT}{path}/{principal_id}"
    headers = {"Authorization": f"Bearer {graph_token}"}

    try:
        response = requests.get(url, headers=headers)
    except Exception as exc:
        logger.warning(f"Graph lookup network error for {principal_id}: {exc}")
        return (f"<network error> {principal_id}", principal_type or "Unknown", False)

    if response.status_code == 404:
        return (f"<deleted or external> {principal_id}", principal_type or "Unknown", False)
    if response.status_code == 403:
        return (
            f"<insufficient Graph permissions> {principal_id}",
            principal_type or "Unknown",
            False,
        )
    if response.status_code >= 400:
        return (
            f"<graph {response.status_code}> {principal_id}",
            principal_type or "Unknown",
            False,
        )

    body = response.json() if response.content else {}
    label = body.get(label_field) or body.get("displayName") or principal_id
    return (safe_message(str(label)), principal_type or "Unknown", True)
