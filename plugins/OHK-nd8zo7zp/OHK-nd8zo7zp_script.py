"""
Give ServiceNow the last word on an order CloudBolt has just approved.

Bound to "Post-Order Approval", the last hook before an approved order's jobs
are created and the only one auto-approved orders reach: super admins,
approvers ordering for themselves, and auto-approving groups never pass
through "Order Submitted for Approval". What happens depends on the wait pin:

  * wait = true (default): ServiceNow is the authority. If the order is
    subject to ServiceNow (its RITM exists, or the Open on Submit action is
    enabled and in scope for its group and blueprint) a missing RITM is opened
    as pending and the decision is read. Approved: the jobs start. Rejected:
    the order is denied. Anything else: the order is set back to Pending and
    waits for the ServiceNow Request Approval Sync job.
  * wait = false: a missing RITM is opened flagged approved so ServiceNow
    records what is being built, and the jobs start.

Orders outside Open on Submit's scope are ignored entirely.

The logic lives in the servicenow_requests shared module; this plugin only
reads its pinned inputs. It is a separate plugin from "ServiceNow Request:
Open on Submit" because CloudBolt's importer keys pinned defaults by hook input and
cannot refresh one plugin bound to two actions.

Inputs (pinned on the orchestration action):
  servicenow_connection_name       ConnectionInfo or ITSM integration name (default "ServiceNow").
  servicenow_catalog_item_sys_id   sys_id of the generic catalog item; empty = Table API path.
  servicenow_requested_for_miss    "integration_user" (default) or "fail" when the
                                   order owner has no ServiceNow user.
  servicenow_table_path_approvers  Table path only: comma-separated user names or
                                   emails, or "manager", asked to approve the RITM.
  servicenow_wait_for_approval     "true" (default) holds every in-scope order until
                                   ServiceNow approves it; "false" lets CloudBolt-approved
                                   orders proceed with a record-only RITM.
  servicenow_unavailable_policy    With wait: "hold" (default) sets the order back to
                                   Pending when ServiceNow cannot be reached, so approving
                                   it again retries; "proceed" lets it run.
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
    if order.status != "ACTIVE":
        return "SUCCESS", f"Order {order.id} is {order.status}; nothing to do after approval.", ""
    try:
        with snr.ServiceNowRequestClient(CONNECTION_NAME) as snow:
            return snr.run_on_approval(snow, order, SETTINGS, job)
    except snr.ServiceNowError as err:
        return snr.handle_open_failure(order, err, True, SETTINGS, job)
