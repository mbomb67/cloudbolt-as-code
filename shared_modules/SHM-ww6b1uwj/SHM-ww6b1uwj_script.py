"""
ServiceNow Request (REQ / RITM) client and order-summary helpers for CloudBolt.

CloudBolt stays the service catalog. For every submitted order this module
opens one ServiceNow Request (sc_request, "REQ") holding one Requested Item
(sc_req_item, "RITM") so ServiceNow carries the approval and the audit trail
while CloudBolt performs the fulfillment. Two creation paths exist:

  * Catalog path (preferred): order ONE generic ServiceNow catalog item through
    the Service Catalog API (POST /api/sn_sc/servicecatalog/items/<sys_id>/order_now)
    so the item's flow runs (approvals, SLAs, notifications). The same item is
    used for every CloudBolt blueprint and action; what differs is the variable
    values. Variable names are documented in docs/servicenow-request-setup.md.
  * Table path (fallback): insert sc_request then sc_req_item through the Table
    API when no catalog item sys_id is configured. No catalog flow runs; the
    customer's own business rules decide what happens next.

After creation the RITM is decorated through the Table API (short_description,
description, correlation_id = CloudBolt order global id, requested_for), and
later closed by the Post-Order Execution hook with the created CIs attached
through the task_ci ("Affected CIs") table. On the table path the open action
can also ask named users (or the requester's manager) to approve the RITM by
inserting sysapproval_approver rows, which is how ServiceNow itself represents
an approval; the sync job then reads those rows.

The order's anchor objects (blueprint order item, or the server / resource a
day-2 or decommission order targets) carry two URL custom fields that link to
the REQ and the RITM by number. Nothing else is stored in CloudBolt: the RITM
is looked up by number and verified through correlation_id (the order's global
id), and its cat_item, state and approval say which path created it and where
it stands.

ServiceNow references used at the call sites:
  * Table API (GET/POST/PATCH /api/now/table/<table>[/<sys_id>]):
    REST API Explorer > Now Table API; docs: servicenow.com/docs "Table API".
  * Service Catalog API (POST /api/sn_sc/servicecatalog/items/<sys_id>/order_now,
    POST /api/sn_sc/servicecatalog/cart/submit_order):
    REST API Explorer > sn_sc > Service Catalog API; docs: "Service Catalog API".
  * Requested-for on behalf of another user: system properties
    glide.sc.req_for.roles / glide.sc.req_for.roles.default.
  * RITM state and approval choice values vary per instance; verify in sys_choice
    (table sc_req_item, elements state and approval). Defaults below match the
    out-of-the-box values.

TLS verification follows CloudBolt's platform setting (Admin > SSL Certificates)
via get_ssl_verification(); never hard-code verify=True/False.
"""
import json
import re
import time
from decimal import Decimal

import requests
from django.utils.html import strip_tags

from common.methods import get_proxies, set_progress
from common.mixins import HasCustomFieldValuesMixin
from infrastructure.models import CustomField
from utilities.helpers import get_ssl_verification
from utilities.logger import ThreadLogger
from utilities.models import ConnectionInfo

logger = ThreadLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Bound every HTTP call so a hung ServiceNow request cannot pin a web or
# jobengine thread (the order-submit hook runs inside the web request).
HTTP_TIMEOUT_S = 60

# ConnectionInfo (or Admin > ITSM integration) that holds the ServiceNow instance
# and the integration user; see ServiceNowRequestClient._resolve_connection.
DEFAULT_CONNECTION_NAME = "ServiceNow"

TABLE_API = "/api/now/table"
CATALOG_API = "/api/sn_sc/servicecatalog"

TABLE_REQUEST = "sc_request"
TABLE_RITM = "sc_req_item"
TABLE_TASK_CI = "task_ci"
TABLE_USER = "sys_user"
# Approval records. A ServiceNow approval is a row here that points at the
# task (sysapproval); approvers act on it from My Approvals, and the task's
# approval field is only a roll-up of these rows.
TABLE_APPROVAL = "sysapproval_approver"
APPROVER_MANAGER = "manager"
DEFAULT_CI_TABLE = "cmdb_ci_server"

# sc_req_item.state choice values (out-of-the-box). Verify in sys_choice.
RITM_STATE_PENDING = "-5"
RITM_STATE_OPEN = "1"
RITM_STATE_WORK_IN_PROGRESS = "2"
RITM_STATE_CLOSED_COMPLETE = "3"
RITM_STATE_CLOSED_INCOMPLETE = "4"
RITM_STATE_CLOSED_SKIPPED = "7"
RITM_CLOSED_STATES = (
    RITM_STATE_CLOSED_COMPLETE,
    RITM_STATE_CLOSED_INCOMPLETE,
    RITM_STATE_CLOSED_SKIPPED,
)

# task.approval choice values (out-of-the-box).
APPROVAL_NOT_REQUESTED = "not requested"
APPROVAL_REQUESTED = "requested"
APPROVAL_APPROVED = "approved"
APPROVAL_REJECTED = "rejected"
# Values of the decision field that approve or deny a CloudBolt order.
APPROVED_VALUES = {APPROVAL_APPROVED}
DENIED_VALUES = {APPROVAL_REJECTED, "cancelled"}
# Where the decision is read from (see read_decision).
DECISION_AUTO = "auto"
DECISION_APPROVALS = "approvals"
# sysapproval_approver states that do not block an approval.
APPROVAL_ROW_SETTLED = {APPROVAL_APPROVED, "not_required", "not required", "cancelled", "duplicate"}

# Shown in ServiceNow as the external system that owns the record.
CORRELATION_DISPLAY = "CloudBolt"

# URL custom fields that link CloudBolt objects to their ServiceNow records.
# The link opens the record's form by number through the table's own form URL
# (a form URL accepts sysparm_query and shows the first matching record):
#   https://<instance>/sc_req_item.do?sysparm_query=number=RITM0010465
# The number stays visible at the end of the URL, which is also the link text
# CloudBolt shows. Everything else about the request (sys_ids, path, state)
# is read back from the RITM, which carries the order's global id in
# correlation_id.
CF_REQUEST_LINK = "servicenow_request_link"
CF_RITM_LINK = "servicenow_ritm_link"
# Optional: other content may set this on a Resource so the close hook can
# attach the resource's CI without a CMDB lookup.
CF_RESOURCE_CI_SYS_ID = "servicenow_ci_sys_id"

# Catalog item variable names. The generic ServiceNow catalog item must define
# variables with exactly these names (docs/servicenow-request-setup.md).
VAR_ORDER_ID = "cloudbolt_order_id"
VAR_ORDER_URL = "cloudbolt_order_url"
VAR_ACTION = "cloudbolt_action"
VAR_BLUEPRINT = "cloudbolt_blueprint"
VAR_RESOURCE_TYPE = "cloudbolt_resource_type"
VAR_GROUP = "cloudbolt_group"
VAR_ENVIRONMENT = "cloudbolt_environment"
VAR_CLOUD = "cloudbolt_cloud"
VAR_COST = "cloudbolt_estimated_cost"
VAR_REQUESTED_FOR = "cloudbolt_requested_for"
VAR_JUSTIFICATION = "cloudbolt_justification"
VAR_APPROVAL = "cloudbolt_approval"
VAR_ITEMS = "cloudbolt_items"  # multi-row variable set, JSON string
VAR_PAYLOAD = "cloudbolt_payload"  # multi-line text, JSON string

# Multi-row variable set column names (one row per requested item).
MRVS_COLUMNS = ("item_type", "name", "environment", "size", "quantity", "details")

# Parameters never copied into the RITM description or payload.
SECRET_HINTS = ("password", "secret", "token", "key", "credential", "passphrase")
MAX_PAYLOAD_CHARS = 30000
MAX_DETAILS_CHARS = 400

# How the CloudBolt order maps onto the ServiceNow "cloudbolt_action" variable.
ACTION_BUILD = "build"
ACTION_DAY2 = "day2"
ACTION_MODIFY = "modify"
ACTION_TEARDOWN = "teardown"
ACTION_OTHER = "other"


class ServiceNowError(Exception):
    """Raised when ServiceNow rejects a request or returns an unusable body."""


class _Endpoint:
    """ConnectionInfo-shaped view of an ITSM integration's host and credentials."""

    def __init__(self, name, ip, port, protocol, username, password):
        self.name = name
        self.ip = ip
        self.port = port
        self.protocol = protocol
        self.username = username
        self.password = password


# ---------------------------------------------------------------------------
# REST client
# ---------------------------------------------------------------------------


class ServiceNowRequestClient:
    """
    Thin requests-based client bound to one ServiceNow ConnectionInfo.

    Use as a context manager so the underlying session is closed:

        with ServiceNowRequestClient("ServiceNow") as snow:
            snow.query("sc_req_item", "number=RITM0010001")
    """

    def __init__(self, connection_name=DEFAULT_CONNECTION_NAME):
        self.connection_name = (connection_name or "").strip()
        self.conn = self._resolve_connection(self.connection_name)
        self.base_url = self._base_url(self.conn)
        self.session = requests.Session()
        self.session.auth = (self.conn.username, self.conn.password)
        self.session.headers.update(
            {"Content-Type": "application/json", "Accept": "application/json"}
        )
        self.session.verify = get_ssl_verification()
        try:
            self.session.proxies = get_proxies(self.base_url)
        except Exception:  # proxies are optional
            pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.session.close()
        return False

    @staticmethod
    def _resolve_connection(name):
        """
        Find the ServiceNow endpoint and credentials, in this order:
          1. a ConnectionInfo named `name`;
          2. an Admin > ITSM integration named `name`;
          3. the ServiceNow ITSM integration (any name) when there is one;
          4. the ConnectionInfo of the legacy Admin > ServiceNow object.
        Returns an object with ip, port, protocol, username, password and name.
        """
        if name:
            conn = ConnectionInfo.objects.filter(name=name).first()
            if conn:
                return conn
        try:
            from django.db.models import Q

            from itsm.models import ITSM

            candidates = ITSM.objects.all()
            itsm = candidates.filter(name=name).first() if name else None
            if not itsm:
                itsm = candidates.filter(
                    Q(itsm_technology__name__icontains="servicenow")
                    | Q(itsm_technology__modulename__icontains="servicenow")
                ).first()
            if itsm:
                logger.info(f"Using ITSM integration '{itsm.name or itsm.global_id}' for ServiceNow.")
                return _Endpoint(
                    name=itsm.name or itsm.global_id,
                    ip=itsm.ip,
                    port=itsm.port,
                    protocol=itsm.protocol,
                    username=itsm.service_account,
                    password=itsm.password,
                )
        except Exception as err:
            logger.debug(f"ITSM lookup skipped: {err}")
        try:
            from utilities.models import ServiceNow

            native = ServiceNow.objects.first()
            if native and native.connection_info:
                logger.info(
                    f"Using the legacy ServiceNow object's connection '{native.connection_info.name}'."
                )
                return native.connection_info
        except Exception:
            pass
        raise ServiceNowError(
            f"No ServiceNow connection found: no ConnectionInfo or ITSM integration named "
            f"'{name or DEFAULT_CONNECTION_NAME}' and no ServiceNow ITSM integration. Create one "
            f"under Admin > ITSM or Admin > Connection Info."
        )

    @staticmethod
    def _base_url(conn):
        protocol = (conn.protocol or "https").lower()
        host = conn.ip.strip().rstrip("/")
        if host.startswith("http://") or host.startswith("https://"):
            return host
        if conn.port and int(conn.port) not in (80, 443):
            return f"{protocol}://{host}:{conn.port}"
        return f"{protocol}://{host}"

    # -- low level --------------------------------------------------------

    def _request(self, method, path, params=None, body=None, ok=(200, 201, 204)):
        url = f"{self.base_url}{path}"
        try:
            response = self.session.request(
                method,
                url,
                params=params,
                data=json.dumps(body) if body is not None else None,
                timeout=HTTP_TIMEOUT_S,
            )
        except requests.RequestException as err:
            raise ServiceNowError(f"{method} {path} failed: {err}") from err
        if response.status_code not in ok:
            raise ServiceNowError(
                f"{method} {path} returned HTTP {response.status_code}: "
                f"{response.text[:1000]}"
            )
        if response.status_code == 204 or not response.content:
            return {}
        try:
            return response.json().get("result", {})
        except ValueError as err:
            raise ServiceNowError(f"{method} {path} returned non-JSON body") from err

    # -- Table API ----------------------------------------------------------
    # GET  /api/now/table/<table>?sysparm_query=...&sysparm_fields=...
    # GET  /api/now/table/<table>/<sys_id>
    # POST /api/now/table/<table>            -> 201, result = new record
    # PATCH /api/now/table/<table>/<sys_id>  -> 200, result = updated record

    def query(self, table, encoded_query, fields=None, limit=10):
        params = {
            "sysparm_query": encoded_query,
            "sysparm_limit": limit,
            "sysparm_display_value": "false",
            "sysparm_exclude_reference_link": "true",
        }
        if fields:
            params["sysparm_fields"] = ",".join(fields)
        result = self._request("GET", f"{TABLE_API}/{table}", params=params)
        return result if isinstance(result, list) else []

    def get(self, table, sys_id, fields=None):
        params = {
            "sysparm_display_value": "false",
            "sysparm_exclude_reference_link": "true",
        }
        if fields:
            params["sysparm_fields"] = ",".join(fields)
        return self._request("GET", f"{TABLE_API}/{table}/{sys_id}", params=params)

    def insert(self, table, data):
        return self._request("POST", f"{TABLE_API}/{table}", body=data, ok=(200, 201))

    def update(self, table, sys_id, data):
        return self._request(
            "PATCH", f"{TABLE_API}/{table}/{sys_id}", body=data, ok=(200,)
        )

    # -- users --------------------------------------------------------------

    def find_user(self, email=None, username=None):
        """
        Return the sys_user sys_id matching the email (preferred) or user_name.
        Returns None when nobody matches.
        """
        clauses = []
        if email:
            clauses.append(f"email={email}")
        if username:
            clauses.append(f"user_name={username}")
        if not clauses:
            return None
        rows = self.query(
            TABLE_USER,
            "^OR".join(clauses) + "^active=true",
            fields=("sys_id", "email", "user_name"),
            limit=5,
        )
        if not rows:
            return None
        if email:
            for row in rows:
                if (row.get("email") or "").lower() == email.lower():
                    return row["sys_id"]
        if username:
            for row in rows:
                if (row.get("user_name") or "").lower() == username.lower():
                    return row["sys_id"]
        return rows[0]["sys_id"]

    def find_users(self, spec, requested_for_sys_id=None):
        """
        Resolve a comma-separated list of user_names or emails to sys_ids.
        The word "manager" resolves to the requested-for user's manager.
        Unknown entries are logged and skipped.
        """
        sys_ids = []
        for item in [x.strip() for x in (spec or "").split(",") if x.strip()]:
            if item.lower() == APPROVER_MANAGER:
                if not requested_for_sys_id:
                    logger.warning("Approver 'manager' skipped: the requester has no ServiceNow user.")
                    continue
                user = self.get(TABLE_USER, requested_for_sys_id, fields=("manager",))
                manager = user.get("manager") or ""
                if manager:
                    sys_ids.append(manager)
                else:
                    logger.warning("Approver 'manager' skipped: the requester has no manager in ServiceNow.")
                continue
            sys_id = self.find_user(email=item if "@" in item else None, username=item)
            if sys_id:
                sys_ids.append(sys_id)
            else:
                logger.warning(f"Approver '{item}' not found in ServiceNow; skipped.")
        return list(dict.fromkeys(sys_ids))

    # -- approvals ------------------------------------------------------------

    def request_ritm_approval(self, ritm_sys_id, approver_sys_ids):
        """
        Ask the given users to approve the RITM: one sysapproval_approver row
        per approver (state requested) and the RITM's approval set to
        requested. The rows appear under My Approvals for each approver.
        """
        for approver in approver_sys_ids:
            self.insert(
                TABLE_APPROVAL,
                {
                    "sysapproval": ritm_sys_id,
                    "document_id": ritm_sys_id,
                    "source_table": TABLE_RITM,
                    "approver": approver,
                    "state": APPROVAL_REQUESTED,
                },
            )
        if approver_sys_ids:
            self.update(TABLE_RITM, ritm_sys_id, {"approval": APPROVAL_REQUESTED})

    def ritm_approvals(self, ritm_sys_id):
        """Approval rows for the RITM: list of {sys_id, approver, state}."""
        return self.query(
            TABLE_APPROVAL,
            f"sysapproval={ritm_sys_id}",
            fields=("sys_id", "approver", "state"),
            limit=50,
        )

    # -- Service Catalog API ------------------------------------------------

    def order_catalog_item(self, cat_item_sys_id, variables, requested_for=None, quantity=1):
        """
        Order one catalog item and return {"request_sys_id", "request_number"}.

        POST /api/sn_sc/servicecatalog/items/<sys_id>/order_now with body
        sysparm_quantity, variables and (optional) sysparm_requested_for.
        Normal instances answer with result.sys_id / result.number (the REQ).
        Instances with two-step checkout enabled (glide.sc.checkout.twostep)
        answer with result.cart_id instead; the cart is then submitted with
        POST /api/sn_sc/servicecatalog/cart/submit_order, which returns
        result.request_id / result.request_number.
        """
        body = {"sysparm_quantity": str(quantity), "variables": variables}
        if requested_for:
            body["sysparm_requested_for"] = requested_for
        result = self._request(
            "POST",
            f"{CATALOG_API}/items/{cat_item_sys_id}/order_now",
            body=body,
            ok=(200, 201),
        )
        request_sys_id = result.get("request_id") or result.get("sys_id")
        request_number = result.get("request_number") or result.get("number")
        if not request_sys_id and result.get("cart_id"):
            submitted = self._request(
                "POST", f"{CATALOG_API}/cart/submit_order", body={}, ok=(200, 201)
            )
            request_sys_id = submitted.get("request_id") or submitted.get("sys_id")
            request_number = submitted.get("request_number") or submitted.get("number")
        if not request_sys_id:
            raise ServiceNowError(
                f"order_now did not return a request sys_id: {json.dumps(result)[:500]}"
            )
        return {"request_sys_id": request_sys_id, "request_number": request_number}

    def ritms_for_request(self, request_sys_id):
        return self.query(
            TABLE_RITM,
            f"request={request_sys_id}^ORDERBYnumber",
            fields=("sys_id", "number", "state", "approval", "stage", "cat_item"),
            limit=50,
        )

    # -- Table API fallback -------------------------------------------------

    def create_request_via_table(self, request_fields, ritm_fields):
        """
        Insert sc_request then sc_req_item. Returns the same dict shape as the
        catalog path plus the RITM identifiers.
        """
        req = self.insert(TABLE_REQUEST, request_fields)
        ritm_fields = dict(ritm_fields, request=req["sys_id"])
        ritm = self.insert(TABLE_RITM, ritm_fields)
        return {
            "request_sys_id": req["sys_id"],
            "request_number": req.get("number"),
            "ritm_sys_id": ritm["sys_id"],
            "ritm_number": ritm.get("number"),
        }

    # -- lifecycle helpers --------------------------------------------------

    RITM_FIELDS = (
        "sys_id", "number", "state", "approval", "stage", "cmdb_ci", "request",
        "cat_item", "correlation_id",
    )

    def get_ritm(self, ritm_sys_id):
        return self.get(TABLE_RITM, ritm_sys_id, fields=self.RITM_FIELDS)

    def find_ritm_by_number(self, number):
        rows = self.query(TABLE_RITM, f"number={number}", fields=self.RITM_FIELDS, limit=1)
        return rows[0] if rows else None

    def record_link(self, number):
        """URL that opens the REQ or RITM form for a record number."""
        table = TABLE_RITM if number.upper().startswith("RITM") else TABLE_REQUEST
        return f"{self.base_url}/{table}.do?sysparm_query=number={number}"

    def add_work_note(self, ritm_sys_id, note):
        return self.update(TABLE_RITM, ritm_sys_id, {"work_notes": note})

    def close_ritm(self, ritm_sys_id, state, close_notes, work_note=None, extra=None):
        data = {"state": state, "close_notes": close_notes}
        if work_note:
            data["work_notes"] = work_note
        if extra:
            data.update(extra)
        return self.update(TABLE_RITM, ritm_sys_id, data)

    def link_ci(self, ritm_sys_id, ci_sys_id):
        """
        Add the CI to the RITM's Affected CIs list (task_ci: task, ci_item).
        Idempotent: an existing row is left alone.
        """
        existing = self.query(
            TABLE_TASK_CI,
            f"task={ritm_sys_id}^ci_item={ci_sys_id}",
            fields=("sys_id",),
            limit=1,
        )
        if existing:
            return existing[0]
        return self.insert(TABLE_TASK_CI, {"task": ritm_sys_id, "ci_item": ci_sys_id})

    def find_server_ci(self, server, ci_table=DEFAULT_CI_TABLE):
        """
        Locate the CI CloudBolt's CMDB integration created for a server. That
        integration writes the resource handler's VM id to serial_number and
        asset_tag, so those are tried first, then the hostname.
        """
        clauses = []
        rh_id = getattr(server, "resource_handler_svr_id", None)
        if rh_id:
            clauses.append(f"serial_number={rh_id}")
            clauses.append(f"asset_tag={rh_id}")
        if server.hostname:
            clauses.append(f"name={server.hostname}")
        if not clauses:
            return None
        rows = self.query(
            ci_table,
            "^OR".join(clauses),
            fields=("sys_id", "name", "serial_number", "asset_tag"),
            limit=5,
        )
        if not rows:
            return None
        if rh_id:
            for row in rows:
                if rh_id in (row.get("serial_number"), row.get("asset_tag")):
                    return row["sys_id"]
        return rows[0]["sys_id"]


# ---------------------------------------------------------------------------
# CloudBolt order summary
# ---------------------------------------------------------------------------


def _is_secret(name):
    lowered = (name or "").lower()
    return any(hint in lowered for hint in SECRET_HINTS)


def _clean_details(mapping, limit=MAX_DETAILS_CHARS):
    parts = []
    for key in sorted(mapping):
        if _is_secret(key):
            continue
        value = mapping[key]
        if value in (None, "", [], {}):
            continue
        parts.append(f"{key}={value}")
    text = "; ".join(parts)
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _cfv_dict(obj):
    try:
        return obj.get_cf_values_as_dict(mask_secrets=True) or {}
    except Exception:
        return {}


def _env_name(env):
    return env.name if env else ""


def _env_cloud(env):
    try:
        return env.resource_handler.cast().resource_technology.name
    except Exception:
        return ""


def _action_label(oi):
    action = getattr(oi, "action_type", None)
    for attr in ("label", "name"):
        value = getattr(action, attr, None)
        if value:
            return str(value)
    return str(oi)


def order_action(order):
    """Classify the order for the cloudbolt_action variable."""
    kinds = set()
    for oi in order.top_level_items.all():
        oi = oi.cast()
        name = oi.__class__.__name__
        if name in ("BlueprintOrderItem", "ProvisionServerOrderItem", "ProvisionNetworkOrderItem"):
            kinds.add(ACTION_BUILD)
        elif name == "DecomServerOrderItem":
            kinds.add(ACTION_TEARDOWN)
        elif name == "ServerModOrderItem":
            kinds.add(ACTION_MODIFY)
        elif name == "ActionJobOrderItem":
            label = _action_label(oi).lower()
            kinds.add(ACTION_TEARDOWN if "delete" in label or "teardown" in label else ACTION_DAY2)
        else:
            kinds.add(ACTION_OTHER)
    if len(kinds) == 1:
        return kinds.pop()
    return ACTION_OTHER if not kinds else ",".join(sorted(kinds))


def order_rows(order):
    """
    One row per requested thing, normalized across servers, resources and
    actions so the RITM's multi-row variable set reads the same for every
    blueprint. Keys match MRVS_COLUMNS.
    """
    rows = []
    for oi in order.top_level_items.all():
        oi = oi.cast()
        name = oi.__class__.__name__
        if name == "BlueprintOrderItem":
            for bia in oi.blueprintitemarguments_set.all():
                si = bia.service_item.cast()
                si_type = si.__class__.__name__.replace("ServiceItem", "")
                item_type = "Server" if si_type == "ProvisionServer" else si_type
                details = _cfv_dict(bia)
                size = ", ".join(
                    f"{k}={details.pop(k)}" for k in ("cpu_cnt", "mem_size", "disk_size") if k in details
                )
                if bia.os_build:
                    size = f"{bia.os_build.name}; {size}" if size else bia.os_build.name
                rows.append(
                    {
                        "item_type": item_type,
                        "name": bia.hostname or si.name,
                        "environment": _env_name(bia.environment),
                        "size": size,
                        "quantity": str(bia.quantity or 1),
                        "details": _clean_details(details),
                    }
                )
            if not rows:
                rows.append(
                    {
                        "item_type": "Blueprint",
                        "name": oi.blueprint.name if oi.blueprint else str(oi),
                        "environment": ", ".join(e.name for e in oi.get_environments()),
                        "size": "",
                        "quantity": "1",
                        "details": _clean_details(_cfv_dict(oi)),
                    }
                )
        elif name == "ProvisionServerOrderItem":
            details = _cfv_dict(oi)
            size = ", ".join(
                f"{k}={details.pop(k)}" for k in ("cpu_cnt", "mem_size", "disk_size") if k in details
            )
            os_build = getattr(oi, "os_build", None)
            if os_build:
                size = f"{os_build.name}; {size}" if size else os_build.name
            rows.append(
                {
                    "item_type": "Server",
                    "name": getattr(oi, "hostname", "") or str(oi),
                    "environment": _env_name(oi.environment),
                    "size": size,
                    "quantity": str(getattr(oi, "quantity", 1) or 1),
                    "details": _clean_details(details),
                }
            )
        elif name == "DecomServerOrderItem":
            for server in oi.servers.all():
                rows.append(
                    {
                        "item_type": "Server (decommission)",
                        "name": server.hostname,
                        "environment": _env_name(server.environment),
                        "size": "",
                        "quantity": "1",
                        "details": f"ip={server.ip or ''}; id={server.global_id}",
                    }
                )
        elif name == "ServerModOrderItem":
            server = oi.server
            rows.append(
                {
                    "item_type": "Server (modify)",
                    "name": server.hostname,
                    "environment": _env_name(server.environment),
                    "size": "",
                    "quantity": "1",
                    "details": str(oi),
                }
            )
        elif name == "ActionJobOrderItem":
            target = oi.resource or oi.server
            target_name = getattr(target, "name", None) or getattr(target, "hostname", "") or ""
            env = getattr(target, "environment", None)
            try:
                arguments = oi.arguments or {}
            except Exception:
                arguments = {}
            rows.append(
                {
                    "item_type": f"Action: {_action_label(oi)}",
                    "name": target_name,
                    "environment": _env_name(env),
                    "size": "",
                    "quantity": "1",
                    "details": _clean_details(arguments),
                }
            )
        else:
            rows.append(
                {
                    "item_type": name.replace("OrderItem", ""),
                    "name": str(oi),
                    "environment": "",
                    "size": "",
                    "quantity": "1",
                    "details": "",
                }
            )
    return rows


def portal_base_url(explicit=""):
    """CloudBolt URL used for links in the RITM; the default portal domain unless pinned."""
    if explicit:
        return explicit.rstrip("/")
    try:
        from portals.models import PortalConfig

        portal = PortalConfig.get_current_portal()
        if portal and portal.site_url:
            return portal.site_url.rstrip("/")
    except Exception:
        pass
    return ""


def order_url(order, base_url=""):
    base = portal_base_url(base_url)
    path = order.get_absolute_url()
    return f"{base}{path}" if base else path


# CloudBolt rate time unit (GlobalPreferences.rate_time_unit) to the ServiceNow
# recurring_frequency choice and the multiplier that turns the rate into that
# frequency. ServiceNow has no hourly frequency, so hourly rates are passed as
# a monthly amount (730 hours).
RATE_UNIT_TO_FREQUENCY = {
    "HOUR": ("monthly", Decimal("730")),
    "DAY": ("daily", Decimal("1")),
    "WEEK": ("weekly", Decimal("1")),
    "MONTH": ("monthly", Decimal("1")),
    "YEAR": ("yearly", Decimal("1")),
    "FLAT": ("", Decimal("1")),  # one-time cost: price only, no recurring price
}


def _rate_numbers(order):
    """Return (recurring amount as a 2-dp string, ServiceNow frequency) or ("", "")."""
    try:
        rate = order.get_rate()
    except Exception:
        return "", ""
    if rate in (None, "") or rate == Decimal("Infinity"):
        return "", ""
    unit = "MONTH"
    try:
        from utilities.models import GlobalPreferences

        unit = (GlobalPreferences.get().rate_time_unit or "MONTH").upper()
    except Exception:
        pass
    frequency, factor = RATE_UNIT_TO_FREQUENCY.get(unit, ("monthly", Decimal("1")))
    amount = (Decimal(rate) * factor).quantize(Decimal("1.00"))
    return str(amount), frequency


def _rate_text(order):
    try:
        rate = order.get_rate()
    except Exception:
        return ""
    if rate in (None, ""):
        return ""
    try:
        from costs.models import render_rate

        return strip_tags(str(render_rate(rate))).strip()
    except Exception:
        return str(Decimal(rate))


def _requested_for_profile(order):
    return order.recipient or order.owner


def _profile_identity(profile):
    if not profile:
        return {"email": "", "username": "", "display": ""}
    user = getattr(profile, "user", None)
    email = getattr(user, "email", "") or ""
    username = getattr(user, "username", "") or getattr(profile, "username", "") or ""
    display = getattr(profile, "full_name", "") or username
    return {"email": email, "username": username, "display": f"{display} <{email}>" if email else display}


def _justification(order):
    for oi in order.top_level_items.all():
        values = _cfv_dict(oi.cast())
        for key in ("business_justification", "justification", "reason", "order_justification"):
            if values.get(key):
                return str(values[key])
    return order.comment or ""


def _payload(order, summary):
    payload = {}
    try:
        payload = order.order_dict() or {}
    except Exception as err:
        logger.debug(f"order_dict unavailable for order {order.id}: {err}")
    if not payload:
        payload = {
            "order_id": order.id,
            "global_id": order.global_id,
            "items": summary["rows"],
        }
    text = json.dumps(payload, default=str)
    if len(text) > MAX_PAYLOAD_CHARS:
        text = json.dumps(
            {"truncated": True, "order_id": order.id, "items": summary["rows"]}, default=str
        )
    return text


def describe_order(order, base_url="", approval="pending"):
    """
    Build the normalized summary every ServiceNow record is derived from.
    Returns a dict with plain-string values plus "rows" (list of dicts),
    "text" (human-readable description) and "requested_for" identity.
    """
    rows = order_rows(order)
    environments = sorted({r["environment"] for r in rows if r["environment"]})
    clouds = set()
    for oi in order.top_level_items.all():
        oi = oi.cast()
        envs = []
        if hasattr(oi, "get_environments"):
            try:
                envs = list(oi.get_environments())
            except Exception:
                envs = []
        elif getattr(oi, "environment", None):
            envs = [oi.environment]
        for env in envs:
            cloud = _env_cloud(env)
            if cloud:
                clouds.add(cloud)
    if order.blueprint:
        blueprint = order.blueprint.name
        resource_type = ""
        try:
            resource_type = order.blueprint.resource_type.label or order.blueprint.resource_type.name
        except Exception:
            pass
    else:
        first = order.top_level_items.first()
        blueprint = _action_label(first.cast()) if first else order.name
        resource_type = ""
        if first:
            target = getattr(first.cast(), "resource", None)
            try:
                resource_type = target.resource_type.label if target else ""
            except Exception:
                resource_type = ""
    action = order_action(order)
    requested_for = _profile_identity(_requested_for_profile(order))
    rate_amount, rate_frequency = _rate_numbers(order)
    summary = {
        "rate_amount": rate_amount,
        "rate_frequency": rate_frequency,
        "order_id": str(order.id),
        "order_global_id": order.global_id,
        "order_url": order_url(order, base_url),
        "action": action,
        "blueprint": blueprint,
        "resource_type": resource_type,
        "group": order.group.name if order.group else "",
        "environment": ", ".join(environments),
        "cloud": ", ".join(sorted(clouds)),
        "cost": _rate_text(order),
        "requested_for": requested_for,
        "justification": _justification(order),
        "approval": approval,
        "rows": rows,
    }
    summary["short_description"] = short_description(summary)
    summary["text"] = description_text(summary)
    summary["payload"] = _payload(order, summary)
    return summary


def short_description(summary):
    names = [r["name"] for r in summary["rows"] if r["name"]]
    target = names[0] if len(names) == 1 else (f"{len(names)} items" if names else "")
    base = f"{summary['blueprint']}: {target}" if target else summary["blueprint"]
    return f"CloudBolt {summary['action']} - {base}"[:160]


def description_text(summary):
    lines = [
        f"CloudBolt order #{summary['order_id']} ({summary['order_global_id']})",
        f"Link: {summary['order_url']}",
        f"Action: {summary['action']}",
        f"Blueprint / action: {summary['blueprint']}",
    ]
    if summary["resource_type"]:
        lines.append(f"Resource type: {summary['resource_type']}")
    lines.append(f"Group: {summary['group']}")
    if summary["environment"]:
        lines.append(f"Environment: {summary['environment']}")
    if summary["cloud"]:
        lines.append(f"Cloud: {summary['cloud']}")
    if summary["cost"]:
        lines.append(f"Estimated cost: {summary['cost']}")
    lines.append(f"Requested for: {summary['requested_for']['display']}")
    if summary["justification"]:
        lines.append(f"Justification: {summary['justification']}")
    lines.append(f"Approval: {summary['approval']}")
    lines.append("")
    lines.append("Requested items:")
    for row in summary["rows"]:
        line = f"- [{row['item_type']}] {row['name']}"
        if row["environment"]:
            line += f" in {row['environment']}"
        if row["quantity"] not in ("", "1"):
            line += f" x{row['quantity']}"
        if row["size"]:
            line += f" ({row['size']})"
        if row["details"]:
            line += f" :: {row['details']}"
        lines.append(line)
    return "\n".join(lines)


def catalog_variables(summary):
    """Map the summary onto the generic catalog item's variable names."""
    return {
        VAR_ORDER_ID: summary["order_id"],
        VAR_ORDER_URL: summary["order_url"],
        VAR_ACTION: summary["action"],
        VAR_BLUEPRINT: summary["blueprint"],
        VAR_RESOURCE_TYPE: summary["resource_type"],
        VAR_GROUP: summary["group"],
        VAR_ENVIRONMENT: summary["environment"],
        VAR_CLOUD: summary["cloud"],
        VAR_COST: summary["cost"],
        VAR_REQUESTED_FOR: summary["requested_for"]["display"],
        VAR_JUSTIFICATION: summary["justification"],
        VAR_APPROVAL: summary["approval"],
        # A multi-row variable set is passed as a JSON string of row objects
        # whose keys are the set's column names.
        VAR_ITEMS: json.dumps(
            [{col: row.get(col, "") for col in MRVS_COLUMNS} for row in summary["rows"]]
        ),
        VAR_PAYLOAD: summary["payload"],
    }


def task_fields(summary, requested_for_sys_id=None):
    """Table-API columns shared by sc_request and sc_req_item."""
    fields = {
        "short_description": summary["short_description"],
        "description": summary["text"],
        "correlation_id": summary["order_global_id"],
        "correlation_display": CORRELATION_DISPLAY,
    }
    if requested_for_sys_id:
        fields["requested_for"] = requested_for_sys_id
    return fields


def request_price_fields(summary):
    """
    sc_request price column. The REQ's price is normally rolled up from its
    items; it is written directly as well so the request shows the estimate
    even on instances where the roll-up only runs for catalog orders.
    """
    if not summary.get("rate_amount"):
        return {}
    return {"price": summary["rate_amount"]}


def ritm_price_fields(summary):
    """
    sc_req_item price columns: the CloudBolt net rate as the recurring price
    with its frequency, and as the price so request-level roll-ups and list
    views that only show Price carry the same estimate.
    """
    if not summary.get("rate_amount"):
        return {}
    fields = {"price": summary["rate_amount"]}
    if summary.get("rate_frequency"):
        fields["recurring_price"] = summary["rate_amount"]
        fields["recurring_frequency"] = summary["rate_frequency"]
    return fields


# ---------------------------------------------------------------------------
# Links between CloudBolt objects and ServiceNow records
# ---------------------------------------------------------------------------


def ensure_custom_fields():
    CustomField.objects.get_or_create(
        name=CF_REQUEST_LINK,
        defaults={
            "label": "ServiceNow Request",
            "type": "URL",
            "description": "Link to the ServiceNow Request (REQ) opened for this order.",
            "show_on_servers": True,
            "show_as_attribute": True,
        },
    )
    CustomField.objects.get_or_create(
        name=CF_RITM_LINK,
        defaults={
            "label": "ServiceNow RITM",
            "type": "URL",
            "description": "Link to the ServiceNow Requested Item (RITM) opened for this order.",
            "show_on_servers": True,
            "show_as_attribute": True,
        },
    )


def anchor_objects(order):
    """
    Objects that can hold custom field values for this order: the blueprint
    or server order item for builds, and the targeted server(s) or resource
    for day-2, modify and decommission orders.
    """
    anchors = []
    for oi in order.top_level_items.all():
        oi = oi.cast()
        if isinstance(oi, HasCustomFieldValuesMixin):
            anchors.append(oi)
            continue
        for attr in ("resource", "server"):
            target = getattr(oi, attr, None)
            if target is not None and isinstance(target, HasCustomFieldValuesMixin):
                anchors.append(target)
        servers = getattr(oi, "servers", None)
        if servers is not None:
            try:
                anchors.extend(list(servers.all()))
            except Exception:
                pass
    return anchors


def store_request_links(snow, order, request_number, ritm_number, targets=None):
    """Write the REQ and RITM links onto the order's anchors (or explicit targets)."""
    ensure_custom_fields()
    for obj in targets if targets is not None else anchor_objects(order):
        try:
            if request_number:
                obj.set_value_for_custom_field(CF_REQUEST_LINK, snow.record_link(request_number))
            if ritm_number:
                obj.set_value_for_custom_field(CF_RITM_LINK, snow.record_link(ritm_number))
        except Exception as err:
            logger.warning(f"Could not store ServiceNow links on {obj}: {err}")


_NUMBER_RE = re.compile(r"(RITM\d+|REQ\d+)")


def _number_from_link(value):
    match = _NUMBER_RE.search(value or "")
    return match.group(1) if match else None


def load_request(snow, order):
    """
    Return the live ServiceNow record for this order, or None when the order
    has no RITM link or the RITM does not belong to it (correlation_id must
    equal the order's global id). Keys: ritm_sys_id, ritm_number,
    request_sys_id, request_number, path ("catalog" or "table"), state,
    approval, closed.
    """
    tried = set()
    for obj in anchor_objects(order):
        try:
            ritm_link = obj.get_value_for_custom_field(CF_RITM_LINK)
            request_link = obj.get_value_for_custom_field(CF_REQUEST_LINK)
        except Exception:
            continue
        number = _number_from_link(ritm_link)
        if not number or number in tried:
            continue
        tried.add(number)
        ritm = snow.find_ritm_by_number(number)
        if not ritm or ritm.get("correlation_id") != order.global_id:
            continue
        return {
            "ritm_sys_id": ritm["sys_id"],
            "ritm_number": ritm.get("number") or number,
            "request_sys_id": ritm.get("request") or "",
            "request_number": _number_from_link(request_link) or "",
            "path": "catalog" if ritm.get("cat_item") else "table",
            "state": str(ritm.get("state", "")),
            "approval": str(ritm.get("approval", "")).lower(),
            "closed": str(ritm.get("state", "")) in RITM_CLOSED_STATES,
        }
    return None


# ---------------------------------------------------------------------------
# Approval decision
# ---------------------------------------------------------------------------


def approval_rows_value(rows):
    """Collapse a RITM's sysapproval_approver rows into approved, rejected or requested."""
    states = {str(r.get("state", "")).lower() for r in rows}
    if APPROVAL_REJECTED in states:
        return APPROVAL_REJECTED
    if states and not (states - APPROVAL_ROW_SETTLED) and APPROVAL_APPROVED in states:
        return APPROVAL_APPROVED
    return APPROVAL_REQUESTED


def read_decision(snow, record, source=DECISION_AUTO, field="approval"):
    """
    Return (source, value) for the ServiceNow decision on an order's record
    (as returned by load_request). Sources follow ServiceNow's conventions:
      * "auto": catalog-path orders read the RITM's approval field, which the
        item's flow maintains; Table API orders read the RITM's approval rows
        when the open action created any, else the REQ's approval field, the
        only thing the out-of-the-box request workflow moves;
      * "approvals": the RITM's approval rows;
      * "sc_req_item" / "sc_request": that table's `field`.
    Values are lower-cased strings; compare with APPROVED_VALUES / DENIED_VALUES.
    """
    source = (source or DECISION_AUTO).lower()
    if source == DECISION_AUTO:
        if record["path"] == "catalog":
            source = TABLE_RITM
        else:
            rows = snow.ritm_approvals(record["ritm_sys_id"])
            if rows:
                return DECISION_APPROVALS, approval_rows_value(rows)
            source = TABLE_REQUEST
    if source == DECISION_APPROVALS:
        return source, approval_rows_value(snow.ritm_approvals(record["ritm_sys_id"]))
    if source == TABLE_RITM and field == "approval":
        return source, record["approval"]
    sys_id = record["ritm_sys_id"] if source == TABLE_RITM else record["request_sys_id"]
    if not sys_id:
        return source, ""
    row = snow.get(source, sys_id, fields=(field,))
    return source, str(row.get(field, "")).strip().lower()


def decision_label(record, source, field="approval"):
    """Human text naming the record and field a decision came from."""
    number = record["request_number"] if source == TABLE_REQUEST else record["ritm_number"]
    what = "approval rows" if source == DECISION_APPROVALS else field
    return number, what


def record_event(order, message):
    """Add a line to the order's history so users can see the ServiceNow numbers."""
    try:
        order.add_event("MODIFICATION", message)
    except Exception as err:
        logger.debug(f"Could not add order history event: {err}")


# ---------------------------------------------------------------------------
# Opening requests and gating orders (shared by the two Open plugins)
# ---------------------------------------------------------------------------

# Seconds to wait before the one-time decision check, so ServiceNow's own
# workflow or flow has run on the new request. The sync job covers the rest.
IMMEDIATE_CHECK_DELAY_S = 5

# Name of the Open on Submit plugin; used to ask CloudBolt whether that action
# is enabled and in scope for an order.
OPEN_ON_SUBMIT_HOOK_NAME = "ServiceNow Request: Open"


class OpenSettings:
    """Pinned inputs of the Open plugins, passed to the functions below."""

    def __init__(
        self,
        catalog_item_sys_id="",
        requested_for_miss="integration_user",
        table_path_approvers="",
        base_url="",
        wait_for_approval=True,
        unavailable_policy="hold",
    ):
        self.catalog_item_sys_id = (catalog_item_sys_id or "").strip()
        self.requested_for_miss = (requested_for_miss or "integration_user").strip().lower()
        self.table_path_approvers = (table_path_approvers or "").strip()
        self.base_url = (base_url or "").strip()
        self.wait_for_approval = wait_for_approval
        self.unavailable_policy = (unavailable_policy or "hold").strip().lower()


def submit_action_in_scope(order):
    """
    True when Open on Submit is enabled and CloudBolt's own group and blueprint
    constraints would run it for this order. Auto-approved orders skip that hook
    point, so this is how Post-Order Approval tells "ServiceNow applies here"
    from "ServiceNow was switched off for this blueprint or group".
    """
    from cbhooks import _get_orchestration_actions_to_run

    for action in _get_orchestration_actions_to_run("order_approval", order=order):
        hook = getattr(action, "hook", None)
        if hook is not None and (hook.name or "") == OPEN_ON_SUBMIT_HOOK_NAME:
            return True
    return False


def open_request(snow, order, approval, settings, job=None):
    """Create the REQ and RITM for the order; returns (ids, path, message)."""
    note = ""
    summary = describe_order(order, settings.base_url, approval)
    identity = summary["requested_for"]
    requested_for = snow.find_user(identity["email"], identity["username"])
    if not requested_for:
        who = identity["display"] or "the order owner"
        if settings.requested_for_miss == "fail":
            raise ServiceNowError(f"No active ServiceNow user matches {who}.")
        progress(
            f"No ServiceNow user matches {who}; the request will be opened for the "
            f"integration user and name the requester in its description.",
            job,
        )

    fields = task_fields(summary, requested_for)

    if settings.catalog_item_sys_id:
        progress(
            f"Ordering ServiceNow catalog item {settings.catalog_item_sys_id} for order {order.id}", job
        )
        ids = snow.order_catalog_item(
            settings.catalog_item_sys_id, catalog_variables(summary), requested_for
        )
        path = "catalog"
        # The catalog engine names the records; decorate them so approvers see
        # the blueprint and target, and so requested_for sticks on instances
        # that override it with the integration user.
        snow.update(TABLE_REQUEST, ids["request_sys_id"], dict(fields, **request_price_fields(summary)))
        ritms = snow.ritms_for_request(ids["request_sys_id"])
        for ritm in ritms:
            snow.update(TABLE_RITM, ritm["sys_id"], dict(fields, **ritm_price_fields(summary)))
        ids["ritm_number"] = ritms[0].get("number") if ritms else ""
    else:
        progress(f"Inserting sc_request and sc_req_item for order {order.id} (Table API path)", job)
        approval_value = APPROVAL_APPROVED if approval == "approved" else APPROVAL_REQUESTED
        request_fields = dict(fields, approval=approval_value, **request_price_fields(summary))
        ritm_fields = dict(fields, approval=approval_value, quantity="1", **ritm_price_fields(summary))
        ids = snow.create_request_via_table(request_fields, ritm_fields)
        path = "table"
        if approval == "pending" and settings.table_path_approvers:
            approvers = snow.find_users(settings.table_path_approvers, requested_for)
            if approvers:
                snow.request_ritm_approval(ids["ritm_sys_id"], approvers)
                note = f" Approval requested from {len(approvers)} approver(s) on the RITM."
            else:
                note = " No pinned approver resolved to a ServiceNow user; the REQ workflow decides."

    store_request_links(snow, order, ids.get("request_number"), ids.get("ritm_number"))
    msg = (
        f"ServiceNow request {ids.get('request_number')} / RITM {ids.get('ritm_number')} "
        f"opened for order {order.id} via the {path} path (approval {approval}).{note}"
    )
    record_event(order, msg)
    progress(msg, job)
    return ids, path, msg


def decide(snow, order, delay=0):
    """
    Read ServiceNow's decision once, after an optional delay.
    Returns ("approved" | "denied" | "pending", note).
    """
    if delay:
        time.sleep(delay)
    record = load_request(snow, order)
    if not record:
        return "pending", " Decision not checked: the request could not be read back."
    source, value = read_decision(snow, record)
    number, what = decision_label(record, source)
    if value in APPROVED_VALUES:
        return "approved", f" ServiceNow {number} {what}={value}."
    if value in DENIED_VALUES or record["closed"]:
        return "denied", f" ServiceNow {number} {what}={value or record['state']}."
    return "pending", f" Awaiting ServiceNow approval ({number} {what}={value or 'none'})."


def hold_order(order, why, job=None):
    """Set an approved order back to Pending so no jobs start until ServiceNow approves."""
    order.status = "PENDING"
    order.save()
    msg = f"Order {order.id} held for ServiceNow approval.{why}"
    record_event(order, msg)
    progress(msg, job)
    return msg


def run_on_submit(snow, order, settings, job=None):
    """Order Submitted for Approval: the order is PENDING and ServiceNow decides."""
    if load_request(snow, order):
        return "SUCCESS", f"Order {order.id} already has a ServiceNow request; nothing to do.", ""
    _, _, msg = open_request(snow, order, "pending", settings, job)
    try:
        outcome, note = decide(snow, order, IMMEDIATE_CHECK_DELAY_S)
        if outcome == "approved":
            order.approve()
            note = f" Order approved immediately:{note}"
        elif outcome == "denied":
            order.deny(reason=f"Rejected in ServiceNow:{note}")
            note = f" Order denied immediately:{note}"
        if outcome != "pending":
            record_event(order, note.strip())
        msg += note
    except Exception as err:  # the sync job retries; never fail the submit here
        logger.warning(f"Order {order.id}: immediate decision check failed: {err}")
        msg += " Immediate decision check failed; the sync job will retry."
    return "SUCCESS", msg, ""


def run_on_approval(snow, order, settings, job=None):
    """Post-Order Approval: CloudBolt approved; ServiceNow may still have the last word."""
    record = load_request(snow, order)
    if not record and not submit_action_in_scope(order):
        return "SUCCESS", f"Order {order.id} is not subject to ServiceNow approval; nothing to do.", ""

    if not settings.wait_for_approval:
        if record:
            return "SUCCESS", f"Order {order.id} already has ServiceNow {record['ritm_number']}.", ""
        _, _, msg = open_request(snow, order, "approved", settings, job)
        return "SUCCESS", msg, ""

    msg = ""
    delay = 0
    if not record:
        _, _, msg = open_request(snow, order, "pending", settings, job)
        delay = IMMEDIATE_CHECK_DELAY_S
    outcome, note = decide(snow, order, delay)
    if outcome == "approved":
        return "SUCCESS", f"{msg} ServiceNow approved; proceeding.{note}".strip(), ""
    if outcome == "denied":
        order.status = "PENDING"
        order.save()
        order.deny(reason=f"Rejected in ServiceNow:{note}")
        denied = f"Order {order.id} denied:{note}"
        record_event(order, denied)
        return "SUCCESS", f"{msg} {denied}".strip(), ""
    return "SUCCESS", f"{msg} {hold_order(order, note, job)}".strip(), ""


def handle_open_failure(order, err, on_approval, settings, job=None):
    """Turn a ServiceNowError from the Open plugins into the action's return tuple."""
    if on_approval and settings.wait_for_approval and settings.unavailable_policy != "proceed":
        held = hold_order(
            order,
            f" ServiceNow could not be reached or refused the request ({err}). "
            f"Approve the order again to retry, or set servicenow_unavailable_policy to proceed.",
            job,
        )
        return "SUCCESS", held, ""
    return "FAILURE", "", f"ServiceNow request not created for order {order.id}: {err}"


# ---------------------------------------------------------------------------
# Objects produced by a completed order
# ---------------------------------------------------------------------------


def order_servers(order):
    seen = {}
    for oi in order.orderitem_set.all():
        oi = oi.cast()
        for job in oi.job_set.all():
            for server in job.server_set.all():
                seen[server.id] = server
        for attr in ("server",):
            server = getattr(oi, attr, None)
            if server is not None:
                seen[server.id] = server
        servers = getattr(oi, "servers", None)
        if servers is not None:
            try:
                for server in servers.all():
                    seen[server.id] = server
            except Exception:
                pass
    return list(seen.values())


def order_resources(order):
    seen = {}
    for oi in order.orderitem_set.all():
        oi = oi.cast()
        resource = getattr(oi, "resource", None)
        if resource is not None:
            seen[resource.id] = resource
        for job in oi.job_set.all():
            try:
                for resource in job.resource_set.all():
                    seen[resource.id] = resource
            except Exception:
                pass
    return list(seen.values())


def object_url(obj, base_url=""):
    base = portal_base_url(base_url)
    try:
        path = obj.get_absolute_url()
    except Exception:
        return ""
    return f"{base}{path}" if base else path


def progress(message, job=None):
    """set_progress that also works outside a job (order hooks run in the web request)."""
    try:
        set_progress(message, job)
    except Exception:
        logger.info(message)
