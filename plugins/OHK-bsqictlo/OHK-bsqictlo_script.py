"""
Mirror ServiceNow approval decisions onto pending CloudBolt orders.

For every PENDING order that has a ServiceNow RITM (opened by the ServiceNow
Request: Open action) the job reads the approval decision and approves or
denies the CloudBolt order to match. Orders without a RITM are left alone, so
enabling this job does not touch orders that predate the integration. Orders
cancelled in CloudBolt in the last week get their still-open RITM closed as
incomplete.

Where the decision is read from follows ServiceNow's conventions. An approval
is a sysapproval_approver row on the RITM that an approver acts on; the task's
approval field is the roll-up. In auto mode:
  * catalog-path orders read the RITM's approval field, which the item's flow
    maintains;
  * Table API orders read the RITM's approval rows when the open action created
    any (a rejection denies, all approved approves), and otherwise the REQ's
    approval field, the only thing the out-of-the-box request workflow moves.

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
APPROVAL_TABLE = ("{{ servicenow_approval_table }}".strip() or "auto").lower()
APPROVAL_FIELD = "{{ servicenow_approval_field }}".strip() or "approval"
APPROVED_VALUES = "{{ servicenow_approved_values }}".strip() or snr.APPROVAL_APPROVED
DENIED_VALUES = "{{ servicenow_denied_values }}".strip() or f"{snr.APPROVAL_REJECTED},cancelled"
APPROVER_USERNAME = "{{ cloudbolt_approver_username }}".strip()

SOURCE_APPROVALS = "approvals"
CANCELLED_LOOKBACK_DAYS = 7


def _values(text):
    return {v.strip().lower() for v in text.split(",") if v.strip()}


def _approver():
    if not APPROVER_USERNAME:
        return None
    profile = UserProfile.objects.filter(user__username=APPROVER_USERNAME).first()
    if not profile:
        logger.warning(f"CloudBolt approver '{APPROVER_USERNAME}' not found; approving without one.")
    return profile


def _approval_rows_value(rows):
    """Collapse the RITM's approval rows into approved, rejected or requested."""
    states = {str(r.get("state", "")).lower() for r in rows}
    if snr.APPROVAL_REJECTED in states:
        return snr.APPROVAL_REJECTED
    pending = states - {snr.APPROVAL_APPROVED, "not_required", "not required", "cancelled", "duplicate"}
    if not pending and snr.APPROVAL_APPROVED in states:
        return snr.APPROVAL_APPROVED
    return snr.APPROVAL_REQUESTED


def _decision(snow, record):
    """Return (source, value) for the record the decision is read from."""
    source = APPROVAL_TABLE
    if source == "auto":
        if record["path"] == "catalog":
            source = snr.TABLE_RITM
        else:
            rows = snow.ritm_approvals(record["ritm_sys_id"])
            if rows:
                return SOURCE_APPROVALS, _approval_rows_value(rows)
            source = snr.TABLE_REQUEST
    if source == SOURCE_APPROVALS:
        return source, _approval_rows_value(snow.ritm_approvals(record["ritm_sys_id"]))
    if source == snr.TABLE_RITM and APPROVAL_FIELD == "approval":
        return source, record["approval"]
    sys_id = record["ritm_sys_id"] if source == snr.TABLE_RITM else record["request_sys_id"]
    if not sys_id:
        return source, ""
    row = snow.get(source, sys_id, fields=(APPROVAL_FIELD,))
    return source, str(row.get(APPROVAL_FIELD, "")).strip().lower()


def _sync_pending(snow, approver, job):
    approved = _values(APPROVED_VALUES)
    denied = _values(DENIED_VALUES)
    counts = {"approved": 0, "denied": 0, "pending": 0, "skipped": 0, "errors": 0}

    for order in Order.objects.filter(status="PENDING").order_by("id"):
        try:
            record = snr.load_request(snow, order)
            if not record:
                counts["skipped"] += 1
                continue
            source, value = _decision(snow, record)
        except snr.ServiceNowError as err:
            logger.warning(f"Order {order.id}: could not read its ServiceNow record: {err}")
            counts["errors"] += 1
            continue
        number = record["request_number"] if source == snr.TABLE_REQUEST else record["ritm_number"]
        what = "approval rows" if source == SOURCE_APPROVALS else APPROVAL_FIELD

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
