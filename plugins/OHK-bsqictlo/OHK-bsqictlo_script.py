"""
Mirror ServiceNow approval decisions onto pending CloudBolt orders.

For every PENDING order that has a ServiceNow RITM (opened by the ServiceNow
Request: Open action) the job reads the approval decision and approves or
denies the CloudBolt order to match. Orders without a RITM are left alone, so
enabling this job does not touch orders that predate the integration. Orders
cancelled in CloudBolt in the last week get their still-open RITM closed as
incomplete.

Where the decision is read from follows ServiceNow's conventions (see
read_decision in the servicenow_requests shared module). An approval is a
sysapproval_approver row on the RITM that an approver acts on; the task's
approval field is the roll-up. In auto mode catalog-path orders read the
RITM's approval field, Table API orders read the RITM's approval rows when the
open action created any and otherwise the REQ's approval field.

Inputs (pinned on the recurring job):
  servicenow_connection_name  ConnectionInfo or ITSM integration name (default "ServiceNow").
  servicenow_approval_table   "auto" (default), "sc_req_item", "sc_request" or
                              "approvals" (the RITM's approval rows).
  servicenow_approval_field   Field read on sc_req_item or sc_request (default "approval";
                              "stage" or "state" also work with matching values).
  servicenow_approved_values  Comma-separated values meaning approved (default "approved").
  servicenow_denied_values    Comma-separated values meaning denied (default "rejected,cancelled").
  cloudbolt_approver_username CloudBolt user recorded as the approver; empty records none.
"""
from datetime import datetime, timedelta

from accounts.models import UserProfile
from history.models import OrderHistory
from orders.models import Order
from utilities.exceptions import CloudBoltException
from utilities.logger import ThreadLogger

from shared_modules import servicenow_requests as snr

logger = ThreadLogger(__name__)

CONNECTION_NAME = "{{ servicenow_connection_name }}".strip() or snr.DEFAULT_CONNECTION_NAME
APPROVAL_TABLE = ("{{ servicenow_approval_table }}".strip() or snr.DECISION_AUTO).lower()
APPROVAL_FIELD = "{{ servicenow_approval_field }}".strip() or "approval"
APPROVED_VALUES = "{{ servicenow_approved_values }}".strip()
DENIED_VALUES = "{{ servicenow_denied_values }}".strip()
APPROVER_USERNAME = "{{ cloudbolt_approver_username }}".strip()

CANCELLED_LOOKBACK_DAYS = 7


def _values(text, default):
    return {v.strip().lower() for v in text.split(",") if v.strip()} or set(default)


def _approver():
    if not APPROVER_USERNAME:
        return None
    profile = UserProfile.objects.filter(user__username=APPROVER_USERNAME).first()
    if not profile:
        logger.warning(f"CloudBolt approver '{APPROVER_USERNAME}' not found; approving without one.")
    return profile


def _sync_pending(snow, approver, job):
    approved = _values(APPROVED_VALUES, snr.APPROVED_VALUES)
    denied = _values(DENIED_VALUES, snr.DENIED_VALUES)
    counts = {"approved": 0, "denied": 0, "pending": 0, "skipped": 0, "errors": 0}

    for order in Order.objects.filter(status="PENDING").order_by("id"):
        try:
            record = snr.load_request(snow, order)
            if not record:
                counts["skipped"] += 1
                continue
            source, value = snr.read_decision(snow, record, APPROVAL_TABLE, APPROVAL_FIELD)
        except snr.ServiceNowError as err:
            logger.warning(f"Order {order.id}: could not read its ServiceNow record: {err}")
            counts["errors"] += 1
            continue
        number, what = snr.decision_label(record, source, APPROVAL_FIELD)

        if value in approved:
            try:
                order.approve(approver)
            except CloudBoltException as err:
                logger.warning(f"Order {order.id} approved in ServiceNow but CloudBolt refused: {err}")
                counts["errors"] += 1
                continue
            msg = f"Order {order.id} approved: ServiceNow {number} {what}={value}."
            snr.record_event(order, msg)
            snr.progress(msg, job)
            counts["approved"] += 1
        elif value in denied or record["closed"]:
            reason = (
                f"Rejected in ServiceNow ({number}: {what}={value})"
                if value in denied
                else f"Closed in ServiceNow ({record['ritm_number']}: state={record['state']})"
            )
            # The Post-Order Denied action sees the rejected or closed RITM and
            # leaves it to ServiceNow instead of writing a CloudBolt denial on it.
            try:
                order.deny(approver, reason=reason)
            except Exception as err:
                logger.warning(f"Order {order.id}: deny failed: {err}")
                counts["errors"] += 1
                continue
            msg = f"Order {order.id} denied: {reason}."
            snr.record_event(order, msg)
            snr.progress(msg, job)
            counts["denied"] += 1
        else:
            counts["pending"] += 1
    return counts


def _close_cancelled(snow, job):
    """Close the RITM of orders cancelled in CloudBolt recently; ServiceNow has no other signal."""
    cutoff = datetime.now() - timedelta(days=CANCELLED_LOOKBACK_DAYS)
    order_ids = (
        OrderHistory.objects.filter(event_type="CANCELED", action_time__gte=cutoff)
        .values_list("order_id", flat=True)
        .distinct()
    )
    closed = 0
    for order in Order.objects.filter(id__in=list(order_ids), status="CANCELED"):
        try:
            record = snr.load_request(snow, order)
            if not record or record["closed"]:
                continue
            snow.close_ritm(
                record["ritm_sys_id"],
                snr.RITM_STATE_CLOSED_INCOMPLETE,
                f"Cancelled in CloudBolt (order {order.id}).",
                work_note=f"CloudBolt order {order.id} was cancelled by its owner before fulfillment.",
            )
        except snr.ServiceNowError as err:
            logger.warning(f"Order {order.id}: could not close its RITM: {err}")
            continue
        snr.record_event(order, f"ServiceNow {record['ritm_number']} closed after cancellation.")
        closed += 1
    return closed


def run(job=None, logger=logger, **kwargs):
    approver = _approver()
    try:
        with snr.ServiceNowRequestClient(CONNECTION_NAME) as snow:
            counts = _sync_pending(snow, approver, job)
            counts["cancelled_closed"] = _close_cancelled(snow, job)
    except snr.ServiceNowError as err:
        return "FAILURE", "", f"ServiceNow approval sync failed: {err}"

    msg = (
        f"Approved {counts['approved']}, denied {counts['denied']}, still pending "
        f"{counts['pending']}, not tracked in ServiceNow {counts['skipped']}, errors "
        f"{counts['errors']}, cancelled orders closed {counts['cancelled_closed']}."
    )
    snr.progress(msg, job)
    status = "WARNING" if counts["errors"] else "SUCCESS"
    return status, msg, ""
