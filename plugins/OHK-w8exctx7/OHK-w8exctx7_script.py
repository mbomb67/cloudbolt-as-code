"""
Close the ServiceNow RITM as incomplete when an order is denied inside CloudBolt.

Bound to "Post-Order Denied". Denials that originated in ServiceNow (mirrored
by the approval sync job) are skipped, as are RITMs ServiceNow already closed.

Inputs (pinned on the orchestration action):
  servicenow_connection_name  ConnectionInfo name (default "ServiceNow").
"""
from utilities.logger import ThreadLogger

import servicenow_requests as snr

logger = ThreadLogger(__name__)

CONNECTION_NAME = "{{ servicenow_connection_name }}".strip() or snr.DEFAULT_CONNECTION_NAME


def run(order=None, job=None, logger=logger, **kwargs):
    if order is None:
        return "SUCCESS", "No order in context; nothing to do.", ""

    ids = snr.load_request_ids(order)
    if not ids or not ids.get("ritm_sys_id"):
        return "SUCCESS", f"Order {order.id} has no ServiceNow RITM; nothing to close.", ""
    if ids.get("closed"):
        return "SUCCESS", f"ServiceNow {ids.get('ritm_number')} is already closed.", ""
    if ids.get("denied_by") == "servicenow":
        return "SUCCESS", f"Denial of order {order.id} came from ServiceNow; RITM left to its flow.", ""

    approver = order.approved_by.username if order.approved_by else "a CloudBolt approver"
    reason = order.denied_reason or "no reason given"
    try:
        with snr.ServiceNowRequestClient(CONNECTION_NAME) as snow:
            ritm = snow.get_ritm(ids["ritm_sys_id"])
            if str(ritm.get("state", "")) in snr.RITM_CLOSED_STATES:
                return "SUCCESS", f"ServiceNow {ids.get('ritm_number')} was already closed.", ""
            snow.close_ritm(
                ids["ritm_sys_id"],
                snr.RITM_STATE_CLOSED_INCOMPLETE,
                f"Denied in CloudBolt by {approver}: {reason}",
                work_note=f"CloudBolt order {order.id} was denied by {approver}. Reason: {reason}",
            )
    except snr.ServiceNowError as err:
        return "FAILURE", "", f"Could not close ServiceNow {ids.get('ritm_number')}: {err}"

    ids["closed"] = True
    ids["denied_by"] = "cloudbolt"
    snr.store_request_ids(order, ids)
    msg = f"ServiceNow {ids.get('ritm_number')} closed incomplete after CloudBolt denial."
    snr.record_event(order, msg)
    snr.progress(msg, job)
    return "SUCCESS", msg, ""
