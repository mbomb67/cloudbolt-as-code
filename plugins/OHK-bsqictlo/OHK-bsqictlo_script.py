"""
Mirror ServiceNow approval decisions onto pending CloudBolt orders.

For every PENDING order that has a ServiceNow request (opened by the
ServiceNow Request: Open action) the job reads one field on the RITM (or the
REQ) and approves or denies the CloudBolt order to match. Orders without a
ServiceNow request are left alone, so enabling this job does not touch orders
that predate the integration. Orders cancelled in CloudBolt in the last week
get their still-open RITM closed as incomplete.

Inputs (pinned on the recurring job):
  servicenow_connection_name  ConnectionInfo name (default "ServiceNow").
  servicenow_approval_table   "sc_req_item" (default) or "sc_request".
  servicenow_approval_field   Field read for the decision (default "approval";
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
APPROVAL_TABLE = "{{ servicenow_approval_table }}".strip() or snr.TABLE_RITM
APPROVAL_FIELD = "{{ servicenow_approval_field }}".strip() or "approval"
APPROVED_VALUES = "{{ servicenow_approved_values }}".strip() or snr.APPROVAL_APPROVED
DENIED_VALUES = "{{ servicenow_denied_values }}".strip() or f"{snr.APPROVAL_REJECTED},cancelled"
APPROVER_USERNAME = "{{ cloudbolt_approver_username }}".strip()

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


def _record_sys_id(ids):
    if APPROVAL_TABLE == snr.TABLE_REQUEST:
        return ids.get("request_sys_id")
    return ids.get("ritm_sys_id")


def _sync_pending(snow, approver, job):
    approved = _values(APPROVED_VALUES)
    denied = _values(DENIED_VALUES)
    counts = {"approved": 0, "denied": 0, "pending": 0, "skipped": 0, "errors": 0}

    for order in Order.objects.filter(status="PENDING").order_by("id"):
        ids = snr.load_request_ids(order)
        sys_id = _record_sys_id(ids) if ids else None
        if not sys_id:
            counts["skipped"] += 1
            continue
        try:
            record = snow.get(APPROVAL_TABLE, sys_id, fields=(APPROVAL_FIELD, "state", "number"))
        except snr.ServiceNowError as err:
            logger.warning(f"Order {order.id}: could not read {APPROVAL_TABLE} {sys_id}: {err}")
            counts["errors"] += 1
            continue
        value = str(record.get(APPROVAL_FIELD, "")).strip().lower()
        number = record.get("number") or ids.get("ritm_number")
        ritm_closed = (
            APPROVAL_TABLE == snr.TABLE_RITM
            and str(record.get("state", "")) in snr.RITM_CLOSED_STATES
        )

        if value in approved:
            try:
                order.approve(approver)
            except CloudBoltException as err:
                logger.warning(f"Order {order.id} approved in ServiceNow but CloudBolt refused: {err}")
                counts["errors"] += 1
                continue
            msg = f"Order {order.id} approved: ServiceNow {number} {APPROVAL_FIELD}={value}."
            snr.record_event(order, msg)
            snr.progress(msg, job)
            counts["approved"] += 1
        elif value in denied or ritm_closed:
            reason = f"Rejected in ServiceNow ({number}: {APPROVAL_FIELD}={value or record.get('state')})"
            # Tell the Post-Order Denied action the decision came from ServiceNow
            # so it does not overwrite the RITM with a CloudBolt denial.
            ids["denied_by"] = "servicenow"
            snr.store_request_ids(order, ids)
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
        ids = snr.load_request_ids(order)
        if not ids or ids.get("closed") or not ids.get("ritm_sys_id"):
            continue
        try:
            ritm = snow.get_ritm(ids["ritm_sys_id"])
            if str(ritm.get("state", "")) not in snr.RITM_CLOSED_STATES:
                snow.close_ritm(
                    ids["ritm_sys_id"],
                    snr.RITM_STATE_CLOSED_INCOMPLETE,
                    f"Cancelled in CloudBolt (order {order.id}).",
                    work_note=f"CloudBolt order {order.id} was cancelled by its owner before fulfillment.",
                )
        except snr.ServiceNowError as err:
            logger.warning(f"Order {order.id}: could not close RITM {ids.get('ritm_number')}: {err}")
            continue
        ids["closed"] = True
        snr.store_request_ids(order, ids)
        snr.record_event(order, f"ServiceNow {ids.get('ritm_number')} closed after cancellation.")
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
