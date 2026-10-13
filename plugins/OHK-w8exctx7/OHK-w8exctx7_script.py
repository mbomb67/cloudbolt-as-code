"""
Close the ServiceNow RITM as incomplete when an order is denied inside CloudBolt.

Bound to "Post-Order Denied". Denials that originated in ServiceNow (the RITM
is already rejected or closed, and the approval sync job mirrored it) are left
to ServiceNow's own flow.

Inputs (pinned on the orchestration action):
  servicenow_connection_name  ConnectionInfo or ITSM integration name (default "ServiceNow").
"""
from utilities.logger import ThreadLogger

from shared_modules import servicenow_requests as snr

logger = ThreadLogger(__name__)

CONNECTION_NAME = "{{ servicenow_connection_name }}".strip() or snr.DEFAULT_CONNECTION_NAME


def run(order=None, job=None, logger=logger, **kwargs):
    if order is None:
        return "SUCCESS", "No order in context; nothing to do.", ""

    approver = order.approved_by.username if order.approved_by else "a CloudBolt approver"
    reason = order.denied_reason or "no reason given"
    try:
        with snr.ServiceNowRequestClient(CONNECTION_NAME) as snow:
            record = snr.load_request(snow, order)
            if not record:
                return "SUCCESS", f"Order {order.id} has no ServiceNow RITM; nothing to close.", ""
            if record["closed"]:
                return "SUCCESS", f"ServiceNow {record['ritm_number']} was already closed.", ""
            if record["approval"] == snr.APPROVAL_REJECTED:
                return (
                    "SUCCESS",
                    f"ServiceNow {record['ritm_number']} is rejected in ServiceNow; left to its flow.",
                    "",
                )
            snow.close_ritm(
                record["ritm_sys_id"],
                snr.RITM_STATE_CLOSED_INCOMPLETE,
                f"Denied in CloudBolt by {approver}: {reason}",
                work_note=f"CloudBolt order {order.id} was denied by {approver}. Reason: {reason}",
            )
    except snr.ServiceNowError as err:
        return "FAILURE", "", f"Could not close the order's ServiceNow RITM: {err}"

    msg = f"ServiceNow {record['ritm_number']} closed incomplete after CloudBolt denial."
    snr.record_event(order, msg)
    snr.progress(msg, job)
    return "SUCCESS", msg, ""
