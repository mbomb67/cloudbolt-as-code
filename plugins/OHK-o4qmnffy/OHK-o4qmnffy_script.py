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
ordered through the Service Catalog API so its flow runs. Table path: without
one, sc_request and sc_req_item rows are inserted through the Table API.
Setup for both: docs/servicenow-request-setup.md.

Inputs (pinned on the orchestration action):
  servicenow_connection_name     ConnectionInfo name (default "ServiceNow").
  servicenow_catalog_item_sys_id sys_id of the generic catalog item; empty = Table API path.
  servicenow_requested_for_miss  "integration_user" (default) or "fail" when the
                                 order owner has no ServiceNow user.
  cloudbolt_base_url             Base URL for links in the RITM; empty = default portal.
"""
from utilities.logger import ThreadLogger

import servicenow_requests as snr

logger = ThreadLogger(__name__)

CONNECTION_NAME = "{{ servicenow_connection_name }}".strip() or snr.DEFAULT_CONNECTION_NAME
CATALOG_ITEM_SYS_ID = "{{ servicenow_catalog_item_sys_id }}".strip()
REQUESTED_FOR_MISS = ("{{ servicenow_requested_for_miss }}".strip() or "integration_user").lower()
BASE_URL = "{{ cloudbolt_base_url }}".strip()


def run(order=None, job=None, logger=logger, **kwargs):
    if order is None:
        return "SUCCESS", "No order in context; nothing to do.", ""

    existing = snr.load_request_ids(order)
    if existing:
        msg = (
            f"Order {order.id} already has ServiceNow request "
            f"{existing.get('request_number')} / {existing.get('ritm_number')}; nothing to do."
        )
        snr.progress(msg, job)
        return "SUCCESS", msg, ""

    # ACTIVE means CloudBolt already approved the order (auto-approval or a
    # CloudBolt approver); PENDING means ServiceNow decides.
    approval = "approved" if order.status == "ACTIVE" else "pending"

    try:
        with snr.ServiceNowRequestClient(CONNECTION_NAME) as snow:
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
                ids["path"] = "catalog"
                # The catalog engine names the records; decorate them so approvers
                # see the blueprint and target instead of the generic item name,
                # and so requested_for sticks on instances that override it with
                # the integration user.
                snow.update(snr.TABLE_REQUEST, ids["request_sys_id"], fields)
                ritms = snow.ritms_for_request(ids["request_sys_id"])
                for ritm in ritms:
                    snow.update(snr.TABLE_RITM, ritm["sys_id"], fields)
                if ritms:
                    ids["ritm_sys_id"] = ritms[0]["sys_id"]
                    ids["ritm_number"] = ritms[0].get("number")
                    ids["ritm_sys_ids"] = [r["sys_id"] for r in ritms]
            else:
                snr.progress(
                    f"Inserting sc_request and sc_req_item for order {order.id} (Table API path)",
                    job,
                )
                approval_value = (
                    snr.APPROVAL_APPROVED if approval == "approved" else snr.APPROVAL_REQUESTED
                )
                request_fields = dict(fields, approval=approval_value)
                ritm_fields = dict(fields, approval=approval_value, quantity="1")
                ids = snow.create_request_via_table(request_fields, ritm_fields)
                ids["path"] = "table"
    except snr.ServiceNowError as err:
        return "FAILURE", "", f"ServiceNow request not created for order {order.id}: {err}"

    ids["approval"] = approval
    snr.store_request_ids(order, ids)

    msg = (
        f"ServiceNow request {ids.get('request_number')} / RITM {ids.get('ritm_number')} "
        f"opened for order {order.id} via the {ids['path']} path (approval {approval})."
    )
    snr.record_event(order, msg)
    snr.progress(msg, job)
    return "SUCCESS", msg, ""
