"""
Open a ServiceNow Request (REQ) with one Requested Item (RITM) for a CloudBolt order.

Bound twice:
  * "Order Submitted for Approval": the order is PENDING and ServiceNow becomes
    the approval system of record. A few seconds after the request is created
    the action reads the decision once, so a request ServiceNow approves
    outright proceeds immediately; anything still pending is picked up by the
    ServiceNow Request Approval Sync recurring job.
  * "Post-Order Approval": CloudBolt has just approved the order, by a group or
    environment auto-approval, by the owner being an approver (super admins
    always are), by a CloudBolt approver, or by the sync job mirroring
    ServiceNow. Auto-approval skips the submit hook, so the order may have no
    RITM yet. What happens next depends on the wait pin:
      - wait = true: ServiceNow is the authority. If the order is subject to
        ServiceNow (its RITM exists, or this action is enabled at "Order
        Submitted for Approval" and in scope for the order's group and
        blueprint) a missing RITM is opened as pending and the decision is
        read. Approved: the jobs start. Rejected: the order is denied.
        Anything else: the order is set back to Pending for the sync job.
      - wait = false: a missing RITM is opened flagged approved so ServiceNow
        records what is being built, and the jobs start.
    Orders outside the submit action's scope are ignored entirely.

Catalog path: when the generic catalog item sys_id is configured the item is
ordered through the Service Catalog API so its flow runs, including its
approvals. Table path: without one, sc_request and sc_req_item rows are
inserted through the Table API; when approvers are pinned, an approval record
is created on the RITM for each of them. Setup: docs/servicenow-request-setup.md.
The logic lives in the servicenow_requests shared module.

Inputs (pinned on each orchestration action):
  servicenow_connection_name       ConnectionInfo or ITSM integration name (default "ServiceNow").
  servicenow_catalog_item_sys_id   sys_id of the generic catalog item; empty = Table API path.
  servicenow_requested_for_miss    "integration_user" (default) or "fail" when the
                                   order owner has no ServiceNow user.
  servicenow_table_path_approvers  Table path only: comma-separated user names or
                                   emails, or "manager", asked to approve the RITM.
  servicenow_wait_for_approval     Post-Order Approval only: "true" (default) holds
                                   every in-scope order until ServiceNow approves it;
                                   "false" lets CloudBolt-approved orders proceed.
  servicenow_unavailable_policy    Post-Order Approval with wait: "hold" (default)
                                   sets the order back to Pending when ServiceNow
                                   cannot be reached; "proceed" lets it run.
  cloudbolt_base_url               Base URL for links in the RITM; empty = default portal.
"""
from utilities.logger import ThreadLogger

from shared_modules import servicenow_requests as snr

logger = ThreadLogger(__name__)

CONNECTION_NAME = "{{ servicenow_connection_name }}".strip() or snr.DEFAULT_CONNECTION_NAME
SETTINGS = snr.OpenSettings(
    catalog_item_sys_id="{{ servicenow_catalog_item_sys_id }}",
    requested_for_miss="{{ servicenow_requested_for_miss }}",
    table_path_approvers="{{ servicenow_table_path_approvers }}",
    base_url="{{ cloudbolt_base_url }}",
    wait_for_approval=("{{ servicenow_wait_for_approval }}".strip() or "true").lower() != "false",
    unavailable_policy="{{ servicenow_unavailable_policy }}",
)


def run(order=None, job=None, logger=logger, **kwargs):
    if order is None:
        return "SUCCESS", "No order in context; nothing to do.", ""

    # PENDING here means the submit hook; ACTIVE means CloudBolt already approved.
    on_approval = order.status == "ACTIVE"
    if order.status not in ("PENDING", "ACTIVE"):
        return "SUCCESS", f"Order {order.id} is {order.status}; nothing to do.", ""
    try:
        with snr.ServiceNowRequestClient(CONNECTION_NAME) as snow:
            if on_approval:
                return snr.run_on_approval(snow, order, SETTINGS, job)
            return snr.run_on_submit(snow, order, SETTINGS, job)
    except snr.ServiceNowError as err:
        return snr.handle_open_failure(order, err, on_approval, SETTINGS, job)
