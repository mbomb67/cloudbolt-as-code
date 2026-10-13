"""
Open a ServiceNow Request (REQ) with one Requested Item (RITM) for a CloudBolt order.

Bound twice:
  * "Order Submitted for Approval": the order is PENDING. ServiceNow becomes the
    approval system of record; the ServiceNow Request Approval Sync recurring
    job mirrors the decision back onto the order.
  * "Post-Order Approval": the order was auto-approved (group or environment
    setting, owner is an approver) or approved inside CloudBolt before this
    content ran. A RITM is still opened, flagged approved, so ServiceNow holds
    the record of what is being built. If the order already has a RITM, the
    action exits without doing anything.

Catalog path: when the generic catalog item sys_id is configured the item is
ordered through the Service Catalog API so its flow runs, including its
approvals. Table path: without one, sc_request and sc_req_item rows are
inserted through the Table API; when approvers are pinned, an approval record
is created on the RITM for each of them, which is what ServiceNow approvers
act on from My Approvals. Setup for both: docs/servicenow-request-setup.md.

The order's item (or the targeted server / resource) receives two URL
parameters, ServiceNow Request and ServiceNow RITM, that open the records.

Inputs (pinned on the orchestration action):
  servicenow_connection_name      ConnectionInfo or ITSM integration name (default "ServiceNow").
  servicenow_catalog_item_sys_id  sys_id of the generic catalog item; empty = Table API path.
  servicenow_requested_for_miss   "integration_user" (default) or "fail" when the
                                  order owner has no ServiceNow user.
  servicenow_table_path_approvers Table path only: comma-separated user names or
                                  emails, or "manager" for the requester's manager,
                                  asked to approve the RITM. Empty leaves approval
                                  to the out-of-the-box request workflow on the REQ.
  cloudbolt_base_url              Base URL for links in the RITM; empty = default portal.
"""
from utilities.logger import ThreadLogger

from shared_modules import servicenow_requests as snr

logger = ThreadLogger(__name__)

CONNECTION_NAME = "{{ servicenow_connection_name }}".strip() or snr.DEFAULT_CONNECTION_NAME
CATALOG_ITEM_SYS_ID = "{{ servicenow_catalog_item_sys_id }}".strip()
REQUESTED_FOR_MISS = ("{{ servicenow_requested_for_miss }}".strip() or "integration_user").lower()
TABLE_PATH_APPROVERS = "{{ servicenow_table_path_approvers }}".strip()
BASE_URL = "{{ cloudbolt_base_url }}".strip()


def run(order=None, job=None, logger=logger, **kwargs):
    if order is None:
        return "SUCCESS", "No order in context; nothing to do.", ""

    # ACTIVE means CloudBolt already approved the order (auto-approval or a
    # CloudBolt approver); PENDING means ServiceNow decides.
    approval = "approved" if order.status == "ACTIVE" else "pending"
    approvers_note = ""

    try:
        with snr.ServiceNowRequestClient(CONNECTION_NAME) as snow:
            existing = snr.load_request(snow, order)
            if existing:
                msg = (
                    f"Order {order.id} already has ServiceNow request "
                    f"{existing['request_number']} / {existing['ritm_number']}; nothing to do."
                )
                snr.progress(msg, job)
                return "SUCCESS", msg, ""

            summary = snr.describe_order(order, BASE_URL, approval)
            identity = summary["requested_for"]
            requested_for = snow.find_user(identity["email"], identity["username"])
            if not requested_for:
                who = identity["display"] or "the order owner"
                if REQUESTED_FOR_MISS == "fail":
                    return "FAILURE", "", f"No active ServiceNow user matches {who}."
                snr.progress(
                    f"No ServiceNow user matches {who}; the request will be opened "
                    f"for the integration user and name the requester in its description.",
                    job,
                )

            fields = snr.task_fields(summary, requested_for)

            if CATALOG_ITEM_SYS_ID:
                snr.progress(
                    f"Ordering ServiceNow catalog item {CATALOG_ITEM_SYS_ID} for order {order.id}", job
                )
                ids = snow.order_catalog_item(
                    CATALOG_ITEM_SYS_ID, snr.catalog_variables(summary), requested_for
                )
                path = "catalog"
                # The catalog engine names the records; decorate them so approvers
                # see the blueprint and target instead of the generic item name,
                # and so requested_for sticks on instances that override it with
                # the integration user.
                snow.update(
                    snr.TABLE_REQUEST,
                    ids["request_sys_id"],
                    dict(fields, **snr.request_price_fields(summary)),
                )
                ritms = snow.ritms_for_request(ids["request_sys_id"])
                for ritm in ritms:
                    snow.update(
                        snr.TABLE_RITM, ritm["sys_id"], dict(fields, **snr.ritm_price_fields(summary))
                    )
                ids["ritm_number"] = ritms[0].get("number") if ritms else ""
            else:
                snr.progress(
                    f"Inserting sc_request and sc_req_item for order {order.id} (Table API path)",
                    job,
                )
                approval_value = (
                    snr.APPROVAL_APPROVED if approval == "approved" else snr.APPROVAL_REQUESTED
                )
                request_fields = dict(
                    fields, approval=approval_value, **snr.request_price_fields(summary)
                )
                ritm_fields = dict(
                    fields, approval=approval_value, quantity="1", **snr.ritm_price_fields(summary)
                )
                ids = snow.create_request_via_table(request_fields, ritm_fields)
                path = "table"
                if approval == "pending" and TABLE_PATH_APPROVERS:
                    approvers = snow.find_users(TABLE_PATH_APPROVERS, requested_for)
                    if approvers:
                        snow.request_ritm_approval(ids["ritm_sys_id"], approvers)
                        approvers_note = f" Approval requested from {len(approvers)} approver(s) on the RITM."
                    else:
                        approvers_note = (
                            " No pinned approver resolved to a ServiceNow user; the REQ workflow decides."
                        )

            snr.store_request_links(snow, order, ids.get("request_number"), ids.get("ritm_number"))
    except snr.ServiceNowError as err:
        return "FAILURE", "", f"ServiceNow request not created for order {order.id}: {err}"

    msg = (
        f"ServiceNow request {ids.get('request_number')} / RITM {ids.get('ritm_number')} "
        f"opened for order {order.id} via the {path} path (approval {approval}).{approvers_note}"
    )
    snr.record_event(order, msg)
    snr.progress(msg, job)
    return "SUCCESS", msg, ""
