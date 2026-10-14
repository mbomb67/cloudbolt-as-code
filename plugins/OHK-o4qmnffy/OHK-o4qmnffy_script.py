"""
Open a ServiceNow Request (REQ) with one Requested Item (RITM) when an order is
submitted for approval.

Bound to "Order Submitted for Approval": the order is PENDING and ServiceNow
becomes the approval system of record. A few seconds after the request is
created the action reads the decision once, so a request ServiceNow approves
outright proceeds immediately; anything still pending is picked up by the
ServiceNow Request Approval Sync recurring job. Orders CloudBolt approves
before this hook (auto-approval, super admins) are handled by the sibling
plugin "ServiceNow Request: Open on Approval".

Catalog path: when the generic catalog item sys_id is configured the item is
ordered through the Service Catalog API so its flow runs, including its
approvals. Table path: without one, sc_request and sc_req_item rows are
inserted through the Table API; when approvers are pinned, an approval record
is created on the RITM for each of them. Setup: docs/servicenow-request-setup.md.

The logic lives in the servicenow_requests shared module; this plugin only
reads its pinned inputs. Each action has its own plugin because CloudBolt's
importer keys pinned defaults by hook input and cannot refresh one plugin
bound to two actions.

Inputs (pinned on the orchestration action):
  servicenow_connection_name       ConnectionInfo or ITSM integration name (default "ServiceNow").
  servicenow_catalog_item_sys_id   sys_id of the generic catalog item; empty = Table API path.
  servicenow_requested_for_miss    "integration_user" (default) or "fail" when the
                                   order owner has no ServiceNow user.
  servicenow_table_path_approvers  Table path only: comma-separated user names or
                                   emails, or "manager", asked to approve the RITM.
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
)


def run(order=None, job=None, logger=logger, **kwargs):
    if order is None:
        return "SUCCESS", "No order in context; nothing to do.", ""
    if order.status != "PENDING":
        return "SUCCESS", f"Order {order.id} is {order.status}; nothing to do at submit.", ""
    try:
        with snr.ServiceNowRequestClient(CONNECTION_NAME) as snow:
            return snr.run_on_submit(snow, order, SETTINGS, job)
    except snr.ServiceNowError as err:
        return snr.handle_open_failure(order, err, False, SETTINGS, job)
