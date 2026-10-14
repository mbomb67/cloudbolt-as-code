"""
Open a ServiceNow Request (REQ) with one Requested Item (RITM) for a CloudBolt order.

Bound twice:
  * "Order Submitted for Approval": the order is PENDING. ServiceNow becomes the
    approval system of record. A few seconds after the request is created the
    action reads the decision once, so a request ServiceNow approves outright
    proceeds immediately; anything still pending is picked up by the
    ServiceNow Request Approval Sync recurring job.
  * "Post-Order Approval": CloudBolt has just approved the order, by a group or
    environment auto-approval, by the owner being an approver (super admins
    always are), by a CloudBolt approver, or by the sync job mirroring
    ServiceNow. Auto-approval skips the submit hook, so the order may have no
    RITM yet. What happens next depends on the wait pin:
      - wait = true: ServiceNow is the authority. If the order is subject to
        ServiceNow (its RITM exists, or Open on Submit is enabled and in scope
        for it) the RITM is opened as pending when missing and the decision is
        read. Approved: the jobs start. Rejected: the order is denied. Anything
        else: the order is set back to Pending and waits for the sync job.
      - wait = false: a missing RITM is opened flagged approved so ServiceNow
        records what is being built, and the jobs start.
    Orders outside Open on Submit's scope are ignored entirely.

Catalog path: when the generic catalog item sys_id is configured the item is
ordered through the Service Catalog API so its flow runs, including its
approvals. Table path: without one, sc_request and sc_req_item rows are
inserted through the Table API; when approvers are pinned, an approval record
is created on the RITM for each of them. Setup: docs/servicenow-request-setup.md.

Inputs (pinned on the orchestration action):
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
import time

from utilities.logger import ThreadLogger

from shared_modules import servicenow_requests as snr

logger = ThreadLogger(__name__)

CONNECTION_NAME = "{{ servicenow_connection_name }}".strip() or snr.DEFAULT_CONNECTION_NAME
CATALOG_ITEM_SYS_ID = "{{ servicenow_catalog_item_sys_id }}".strip()
REQUESTED_FOR_MISS = ("{{ servicenow_requested_for_miss }}".strip() or "integration_user").lower()
TABLE_PATH_APPROVERS = "{{ servicenow_table_path_approvers }}".strip()
WAIT_FOR_APPROVAL = ("{{ servicenow_wait_for_approval }}".strip() or "true").lower() != "false"
UNAVAILABLE_POLICY = ("{{ servicenow_unavailable_policy }}".strip() or "hold").lower()
BASE_URL = "{{ cloudbolt_base_url }}".strip()

# Seconds to wait before the one-time decision check, so ServiceNow's own
# workflow or flow has run on the new request. The sync job covers the rest.
IMMEDIATE_CHECK_DELAY_S = 5

# Name of this plugin's hook as it appears on the Open on Submit action; used
# to ask CloudBolt whether that action is enabled and in scope for an order.
OPEN_HOOK_NAME = "ServiceNow Request: Open"


def _submit_action_in_scope(order):
    """
    True when Open on Submit is enabled and CloudBolt's own group and blueprint
    constraints would run it for this order. Auto-approved orders skip that hook
    point, so this is how Post-Order Approval tells "ServiceNow applies here"
    from "ServiceNow was switched off for this blueprint or group".
    """
    from cbhooks import _get_orchestration_actions_to_run

    for action in _get_orchestration_actions_to_run("order_approval", order=order):
        hook = getattr(action, "hook", None)
        if hook is not None and OPEN_HOOK_NAME in (hook.name or ""):
            return True
    return False


def _open_request(snow, order, approval, job):
    """Create the REQ and RITM for the order; returns (ids, path, note)."""
    note = ""
    summary = snr.describe_order(order, BASE_URL, approval)
    identity = summary["requested_for"]
    requested_for = snow.find_user(identity["email"], identity["username"])
    if not requested_for:
        who = identity["display"] or "the order owner"
        if REQUESTED_FOR_MISS == "fail":
            raise snr.ServiceNowError(f"No active ServiceNow user matches {who}.")
        snr.progress(
            f"No ServiceNow user matches {who}; the request will be opened for the "
            f"integration user and name the requester in its description.",
            job,
        )

    fields = snr.task_fields(summary, requested_for)

    if CATALOG_ITEM_SYS_ID:
        snr.progress(f"Ordering ServiceNow catalog item {CATALOG_ITEM_SYS_ID} for order {order.id}", job)
        ids = snow.order_catalog_item(CATALOG_ITEM_SYS_ID, snr.catalog_variables(summary), requested_for)
        path = "catalog"
        # The catalog engine names the records; decorate them so approvers see
        # the blueprint and target, and so requested_for sticks on instances
        # that override it with the integration user.
        snow.update(
            snr.TABLE_REQUEST, ids["request_sys_id"], dict(fields, **snr.request_price_fields(summary))
        )
        ritms = snow.ritms_for_request(ids["request_sys_id"])
        for ritm in ritms:
            snow.update(snr.TABLE_RITM, ritm["sys_id"], dict(fields, **snr.ritm_price_fields(summary)))
        ids["ritm_number"] = ritms[0].get("number") if ritms else ""
    else:
        snr.progress(f"Inserting sc_request and sc_req_item for order {order.id} (Table API path)", job)
        approval_value = snr.APPROVAL_APPROVED if approval == "approved" else snr.APPROVAL_REQUESTED
        request_fields = dict(fields, approval=approval_value, **snr.request_price_fields(summary))
        ritm_fields = dict(
            fields, approval=approval_value, quantity="1", **snr.ritm_price_fields(summary)
        )
        ids = snow.create_request_via_table(request_fields, ritm_fields)
        path = "table"
        if approval == "pending" and TABLE_PATH_APPROVERS:
            approvers = snow.find_users(TABLE_PATH_APPROVERS, requested_for)
            if approvers:
                snow.request_ritm_approval(ids["ritm_sys_id"], approvers)
                note = f" Approval requested from {len(approvers)} approver(s) on the RITM."
            else:
                note = " No pinned approver resolved to a ServiceNow user; the REQ workflow decides."

    snr.store_request_links(snow, order, ids.get("request_number"), ids.get("ritm_number"))
    msg = (
        f"ServiceNow request {ids.get('request_number')} / RITM {ids.get('ritm_number')} "
        f"opened for order {order.id} via the {path} path (approval {approval}).{note}"
    )
    snr.record_event(order, msg)
    snr.progress(msg, job)
    return ids, path, msg


def _decide(snow, order, job, delay):
    """
    Read ServiceNow's decision once. Returns ("approved" | "denied" | "pending", note).
    Approves or denies the order only from the submit hook, where the order is
    still PENDING; at Post-Order Approval the caller acts on the result.
    """
    if delay:
        time.sleep(delay)
    record = snr.load_request(snow, order)
    if not record:
        return "pending", " Decision not checked: the request could not be read back."
    source, value = snr.read_decision(snow, record)
    number, what = snr.decision_label(record, source)
    if value in snr.APPROVED_VALUES:
        return "approved", f" ServiceNow {number} {what}={value}."
    if value in snr.DENIED_VALUES or record["closed"]:
        return "denied", f" ServiceNow {number} {what}={value or record['state']}."
    return "pending", f" Awaiting ServiceNow approval ({number} {what}={value or 'none'})."


def _hold(order, why, job):
    """Set an approved order back to Pending so no jobs start until ServiceNow approves."""
    order.status = "PENDING"
    order.save()
    msg = f"Order {order.id} held for ServiceNow approval.{why}"
    snr.record_event(order, msg)
    snr.progress(msg, job)
    return msg


def _run_on_submit(snow, order, job):
    """Order Submitted for Approval: the order is PENDING and ServiceNow decides."""
    if snr.load_request(snow, order):
        return "SUCCESS", f"Order {order.id} already has a ServiceNow request; nothing to do.", ""
    _, _, msg = _open_request(snow, order, "pending", job)
    try:
        outcome, note = _decide(snow, order, job, IMMEDIATE_CHECK_DELAY_S)
        if outcome == "approved":
            order.approve()
            note = f" Order approved immediately:{note}"
        elif outcome == "denied":
            order.deny(reason=f"Rejected in ServiceNow:{note}")
            note = f" Order denied immediately:{note}"
        if outcome != "pending":
            snr.record_event(order, note.strip())
        msg += note
    except Exception as err:  # the sync job retries; never fail the submit here
        logger.warning(f"Order {order.id}: immediate decision check failed: {err}")
        msg += " Immediate decision check failed; the sync job will retry."
    return "SUCCESS", msg, ""


def _run_on_approval(snow, order, job):
    """Post-Order Approval: CloudBolt approved; ServiceNow may still have the last word."""
    record = snr.load_request(snow, order)
    if not record and not _submit_action_in_scope(order):
        return "SUCCESS", f"Order {order.id} is not subject to ServiceNow approval; nothing to do.", ""

    if not WAIT_FOR_APPROVAL:
        if record:
            return "SUCCESS", f"Order {order.id} already has ServiceNow {record['ritm_number']}.", ""
        _, _, msg = _open_request(snow, order, "approved", job)
        return "SUCCESS", msg, ""

    msg = ""
    delay = 0
    if not record:
        _, _, msg = _open_request(snow, order, "pending", job)
        delay = IMMEDIATE_CHECK_DELAY_S
    outcome, note = _decide(snow, order, job, delay)
    if outcome == "approved":
        return "SUCCESS", f"{msg} ServiceNow approved; proceeding.{note}".strip(), ""
    if outcome == "denied":
        order.status = "PENDING"
        order.save()
        order.deny(reason=f"Rejected in ServiceNow:{note}")
        denied = f"Order {order.id} denied:{note}"
        snr.record_event(order, denied)
        return "SUCCESS", f"{msg} {denied}".strip(), ""
    return "SUCCESS", f"{msg} {_hold(order, note, job)}".strip(), ""


def run(order=None, job=None, logger=logger, **kwargs):
    if order is None:
        return "SUCCESS", "No order in context; nothing to do.", ""

    # PENDING here means the submit hook; ACTIVE means CloudBolt already approved.
    on_approval = order.status == "ACTIVE"
    try:
        with snr.ServiceNowRequestClient(CONNECTION_NAME) as snow:
            if on_approval:
                return _run_on_approval(snow, order, job)
            return _run_on_submit(snow, order, job)
    except snr.ServiceNowError as err:
        if on_approval and WAIT_FOR_APPROVAL and UNAVAILABLE_POLICY != "proceed":
            held = _hold(
                order,
                f" ServiceNow could not be reached or refused the request ({err}). "
                f"Approve the order again to retry, or set servicenow_unavailable_policy to proceed.",
                job,
            )
            return "SUCCESS", held, ""
        return "FAILURE", "", f"ServiceNow request not created for order {order.id}: {err}"
