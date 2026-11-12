"""
CloudBolt shared module: CloudBolt Engine budgets.

CloudBolt Engine is CloudBolt's FinOps service. This module gives a CMP Group a
budget in Engine that counts only the cost rows whose cloud tag
(cloudbolt_group by default) equals a per-group value, and keeps a snapshot of
that budget's performance and month-end trend next to the group so the CMP UI
does not call Engine on every page view.

Imported by the CloudBolt Engine Budgets UI extension and by the
"Refresh CloudBolt Engine Budgets" recurring job as:

    from shared_modules import cloudbolt_engine

Layers:

- EngineClient is a REST client over the Engine public API. It reads nothing
  from the CMP database: it takes a ConnectionInfo (API base URL, client ID,
  client secret) and nothing else. The client secret is sent only to the token
  endpoint and the access token only in the Authorization header; neither is
  logged, returned or put in an exception message.
- The registration store keeps one JSON document per Group in a namespaced
  Multi-line Text parameter (one CustomFieldValue row per group, updated in
  place under a row lock).
- The group operations (register_group, update_group_budget,
  remove_group_budget, ensure_budget, refresh_snapshot, refresh_all) combine
  the two. compute_trend is pure and has no I/O.

ConnectionInfo mapping: protocol + ip (+ port) is the API base URL, username
is the API client ID, password is the API client secret. Only connections that
carry the label in CONNECTION_INFO_LABEL are used, even when a reference is
passed in.

Engine API reference: the API reference page at /docs/api on the Engine app
host (OpenAPI), covering the token endpoint, /api/v1/budgets,
/api/v1/budgets/<id>/performance, /api/v1/cost-explorer and
/api/v1/cost-explorer/available-fields. Details this module relies on, each
checked against the Engine source:

- Token: form-urlencoded POST to <api base>/oauth/token with grant_type
  client_credentials, client_id, client_secret, audience (the API base with a
  trailing slash) and scope.
- Budget errors are {"error": "<code>", "detail": "<text>"}; cost-explorer
  errors are {"error": "<text>"} (validation adds "details"); token errors are
  the OAuth form {"error": ..., "error_description": ...}.
- Budget periods are breakpoints ({"month": "YYYY-MM", "amount": ...}); PATCH
  replaces the whole periods list and the whole filters list.
- Cost-explorer rows at day granularity carry the day under
  "charge_period_start.day" (an ISO timestamp, midnight) and the measure under
  its bare name ("billed_cost").
- Label values for cloud resources: lowercase letters, digits, underscores and
  dashes, at most 63 characters (Google Cloud's label requirements:
  https://cloud.google.com/resource-manager/docs/labels-overview#requirements).
"""

import calendar
import hashlib
import json
import re
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone as dt_timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from urllib.parse import urlsplit

import requests
from django.core.cache import cache
from django.db import transaction

from accounts.models import Group
from common.methods import get_proxies
from infrastructure.models import CustomField, Namespace
from orders.models import CustomFieldValue
from utilities.helpers import get_ssl_verification
from utilities.logger import ThreadLogger
from utilities.models import ConnectionInfo

logger = ThreadLogger(__name__)

CONNECTION_INFO_LABEL = "cloudbolt_engine"
DEFAULT_TAG_KEY = "cloudbolt_group"

STORE_NAMESPACE = "cloudbolt_engine"
STORE_FIELD = "cloudbolt_engine_group_budgets"
STORE_FIELD_LABEL = "CloudBolt Engine group budgets"
STORE_FIELD_DESCRIPTION = (
    "Managed by the CloudBolt Engine Budgets UI extension: one value per group. "
    "Do not delete; deleting it removes every group's budget registration on this appliance."
)

# Token request. The scopes cover reading and writing budgets and reading cost
# data (Engine quick start, /docs/api).
TOKEN_SCOPE = "reports:read reports:write"
TOKEN_PATH = "/oauth/token"
TOKEN_SAFETY_MARGIN_S = 60
TOKEN_DEFAULT_TTL_S = 300

CONNECT_TIMEOUT_S = 10
READ_TIMEOUT_S = 60

TAG_VALUE_MAX = 63
TREND_RATE_DAYS = 7
PERFORMANCE_MONTHS = 12
COST_QUERY_LIMIT = 10000

# Characters a tag key may not contain, and its length limit, mirror the Engine
# data layer's tag dimension builder: keys it rejects get no dimension.
TAG_KEY_MAX = 256


# =============================================================================
# Errors
# =============================================================================
class EngineError(Exception):
    """A failure talking to CloudBolt Engine or using its data. str() is a
    sentence an operator can act on. status is the HTTP status when there was
    one, code the Engine's machine-readable error code when it sent one."""

    def __init__(self, message, status=None, code=None):
        super().__init__(message)
        self.status = status
        self.code = code


class EngineConfigError(EngineError):
    """The connection is missing, unlabeled or incomplete."""


class EngineNotFound(EngineError):
    """Engine has no such budget (HTTP 404)."""


# =============================================================================
# Small helpers
# =============================================================================
CENT = Decimal("0.01")


def _dec(value):
    """Decimal from a string, number or None; anything unreadable is 0."""
    if value is None or isinstance(value, bool):
        return Decimal(0)
    try:
        result = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        return Decimal(0)
    return result if result.is_finite() else Decimal(0)


def _money(value):
    """A money amount as a decimal string with 2 places."""
    return str(_dec(value).quantize(CENT, rounding=ROUND_HALF_UP))


def _now_iso():
    # CMP's timezone.now() is naive appliance-local time; stamp UTC explicitly.
    return datetime.now(dt_timezone.utc).isoformat()


def _utc_today():
    return datetime.now(dt_timezone.utc).date()


def _month_key(day):
    return "{:04d}-{:02d}".format(day.year, day.month)


def _add_months(day, count):
    index = day.year * 12 + (day.month - 1) + count
    return date(index // 12, index % 12 + 1, 1)


def _gid(group):
    """The global_id of a Group object, or the string passed in."""
    value = getattr(group, "global_id", group)
    value = str(value or "").strip()
    if not value:
        raise EngineError("A group is required.")
    return value


# =============================================================================
# Tag dimension naming
# =============================================================================
def tag_dimension_name(tag_key):
    """
    The name Engine's data layer gives the per-key dimension for a cloud tag
    key: "tag_<sanitised key>_<4 hex of a hash>". A faithful port of
    buildTagDimensions in the Engine cube's lib/tag-dimensions.js (provider
    tags). Returns None for a key that function rejects (over 256 characters,
    control characters, backslash, backtick or double quote, or characters
    outside the Basic Multilingual Plane).

    The hash is a 32-bit signed integer wrap of h = h * 31 + code, over the
    key's characters. JavaScript iterates a string by code point and reads the
    first UTF-16 code unit of each one; for the BMP-only keys that survive the
    rejection above that equals one UTF-16 code unit per character, which is
    what ord() gives here.
    """
    tag_key = str(tag_key or "")
    if not tag_key or len(tag_key) > TAG_KEY_MAX:
        return None
    for char in tag_key:
        code = ord(char)
        if code < 0x20 or code == 0x7F or char in '\\`"' or code > 0xFFFF:
            return None
    sanitized = re.sub(r"[^a-z0-9_]", "_", tag_key.lower())
    sanitized = re.sub(r"_+", "_", sanitized)
    sanitized = re.sub(r"^_|_$", "", sanitized) or "unnamed"
    h = 0
    for char in tag_key:
        h = (h * 31 + ord(char)) & 0xFFFFFFFF
    # (h << 5) - h + c in JS is h * 31 + c; the unsigned view of the signed
    # 32-bit wrap is the same low 32 bits.
    return "tag_{}_{}".format(sanitized, format(h, "08x")[-4:])


# =============================================================================
# REST client
# =============================================================================
_CODE_RE = re.compile(r"^[a-z][a-z0-9_]*$")


def _error_from_response(response, action):
    """EngineError (or a subclass) for a non-success response. Reads the three
    error shapes the API uses and never includes credentials."""
    status = response.status_code
    code = None
    detail = None
    try:
        body = response.json()
    except ValueError:
        body = None
    if isinstance(body, dict):
        raw_error = body.get("error")
        if isinstance(raw_error, str) and _CODE_RE.match(raw_error):
            code = raw_error
        elif isinstance(raw_error, str):
            detail = raw_error
        for key in ("detail", "error_description", "message"):
            if isinstance(body.get(key), str) and body[key]:
                detail = body[key]
                break
        issues = body.get("details")
        if isinstance(issues, list) and issues:
            parts = []
            for issue in issues[:5]:
                if isinstance(issue, dict):
                    path = ".".join(str(p) for p in issue.get("path") or [])
                    text = str(issue.get("message") or "")
                    parts.append("{}: {}".format(path, text) if path else text)
            if parts:
                detail = "{} ({})".format(detail or "Invalid request", "; ".join(parts))
    if not detail:
        detail = "no detail returned"
    detail = " ".join(str(detail).split())[:300]
    label = "HTTP {}".format(status) if not code else "HTTP {} {}".format(status, code)
    message = "CloudBolt Engine could not {} ({}): {}".format(action, label, detail)
    if status in (401, 403):
        message += (
            ". Check the API client ID and secret on the connection and that the API key "
            "has the reports:read and reports:write scopes."
        )
    if status == 404:
        return EngineNotFound(message, status=status, code=code)
    return EngineError(message, status=status, code=code)


class EngineClient(object):
    """CloudBolt Engine public API client for one ConnectionInfo."""

    def __init__(self, connection):
        self.connection = connection
        self.base_url = _base_url(connection)
        self.client_id = (connection.username or "").strip()
        self._client_secret = (connection.password or "").strip()
        if not self.client_id or not self._client_secret:
            raise EngineConfigError(
                "Connection '{}' needs the Engine API client ID in its username and the "
                "client secret in its password. Repository syncs redact secrets, so "
                "re-enter the secret under Admin > Connection Info.".format(connection.name)
            )
        origin = urlsplit(self.base_url)
        self.audience = "{}://{}/".format(origin.scheme, origin.netloc)
        digest = hashlib.sha256(
            "{}|{}|{}".format(connection.id, self.base_url, self.client_id).encode("utf-8")
        ).hexdigest()
        self._cache_key = "cloudbolt_engine_token_{}".format(digest[:40])

    def __repr__(self):
        return "<EngineClient {}>".format(self.base_url)

    # -- HTTP ----------------------------------------------------------------
    def _send(self, method, url, action, **kwargs):
        try:
            proxies = get_proxies(url)
        except Exception as exc:
            raise EngineConfigError(
                "CloudBolt Engine could not {}: the appliance's proxy settings could not be "
                "read ({}). Check Admin > Global Preferences.".format(action, type(exc).__name__)
            )
        try:
            return requests.request(
                method,
                url,
                timeout=(CONNECT_TIMEOUT_S, READ_TIMEOUT_S),
                verify=get_ssl_verification(),
                proxies=proxies,
                **kwargs
            )
        except requests.exceptions.SSLError:
            raise EngineError(
                "CloudBolt Engine could not {}: the TLS certificate was not trusted. Upload "
                "the issuing CA under Admin > SSL Certificates.".format(action)
            )
        except requests.exceptions.RequestException as exc:
            raise EngineError(
                "CloudBolt Engine could not {}: the connection failed ({}). Check the connection's "
                "host and the appliance's network access.".format(action, type(exc).__name__)
            )

    def _fetch_token(self):
        url = self.base_url + TOKEN_PATH
        response = self._send(
            "POST",
            url,
            "get an access token",
            data={
                "grant_type": "client_credentials",
                "client_id": self.client_id,
                "client_secret": self._client_secret,
                "audience": self.audience,
                "scope": TOKEN_SCOPE,
            },
        )
        if response.status_code == 404:
            raise EngineConfigError(
                "The connection's IP/Hostname must be the CloudBolt Engine API host (the host "
                "that serves /oauth/token), not the Engine web app host.",
                status=404,
            )
        if response.status_code != 200:
            raise _error_from_response(response, "get an access token")
        try:
            body = response.json()
        except ValueError:
            body = None
        token = body.get("access_token") if isinstance(body, dict) else None
        if not token:
            raise EngineError(
                "CloudBolt Engine answered the token request without an access token.",
                status=response.status_code,
            )
        try:
            expires_in = int(body.get("expires_in"))
        except (TypeError, ValueError):
            expires_in = None
        if expires_in is None or expires_in <= 0:
            ttl = TOKEN_DEFAULT_TTL_S
        elif expires_in > 2 * TOKEN_SAFETY_MARGIN_S:
            ttl = expires_in - TOKEN_SAFETY_MARGIN_S
        else:
            ttl = max(expires_in // 2, 1)
        try:
            cache.set(self._cache_key, token, ttl)
        except Exception:
            logger.warning("Could not cache the CloudBolt Engine access token")
        return token

    def _token(self):
        try:
            token = cache.get(self._cache_key)
        except Exception:
            token = None
        return token or self._fetch_token()

    def _drop_token(self):
        try:
            cache.delete(self._cache_key)
        except Exception:
            pass

    def _call(self, method, path, action, params=None, body=None, expect=(200,)):
        url = self.base_url + path
        for attempt in (1, 2):
            headers = {"Authorization": "Bearer " + self._token(), "Accept": "application/json"}
            kwargs = {"headers": headers, "params": params}
            if body is not None:
                kwargs["json"] = body
            response = self._send(method, url, action, **kwargs)
            if response.status_code == 401 and attempt == 1:
                self._drop_token()
                continue
            break
        if response.status_code not in expect:
            raise _error_from_response(response, action)
        if response.status_code == 204 or not response.content:
            return None
        try:
            return response.json()
        except ValueError:
            raise EngineError(
                "CloudBolt Engine returned a response that is not JSON when asked to {}.".format(action),
                status=response.status_code,
            )

    # -- Budgets -------------------------------------------------------------
    def list_budgets(self):
        body = self._call("GET", "/api/v1/budgets", "list budgets")
        return list((body or {}).get("data") or [])

    def get_budget(self, budget_id):
        return self._call("GET", "/api/v1/budgets/{}".format(_path_id(budget_id)), "read the budget")

    def create_budget(self, payload):
        return self._call("POST", "/api/v1/budgets", "create the budget", body=payload, expect=(201,))

    def update_budget(self, budget_id, payload):
        return self._call(
            "PATCH", "/api/v1/budgets/{}".format(_path_id(budget_id)), "update the budget", body=payload
        )

    def delete_budget(self, budget_id):
        self._call(
            "DELETE", "/api/v1/budgets/{}".format(_path_id(budget_id)), "delete the budget", expect=(204,)
        )

    def get_performance(self, budget_id, start, end):
        """Monthly budgeted versus actual from month start to month end
        ("YYYY-MM", at most 24 months)."""
        return self._call(
            "GET",
            "/api/v1/budgets/{}/performance".format(_path_id(budget_id)),
            "read the budget performance",
            params={"start": start, "end": end},
        )

    # -- Cost data -----------------------------------------------------------
    def available_fields(self, data_source="FOCUS"):
        return self._call(
            "GET",
            "/api/v1/cost-explorer/available-fields",
            "list the cost fields",
            params={"dataSource": data_source},
        ) or {}

    def query_costs(self, body):
        return self._call("POST", "/api/v1/cost-explorer", "query cost data", body=body) or {}

    def find_tag_field(self, tag_key):
        """The dimension name Engine gives this tag key, or None while that
        dimension does not exist yet (it appears once the key shows up in
        billing data). The computed name is verified against the list Engine
        returns, never assumed."""
        name = tag_dimension_name(tag_key)
        if not name:
            return None
        group_by = self.available_fields().get("groupBy") or []
        return name if name in group_by else None


def _path_id(value):
    value = str(value or "").strip()
    if not value or not re.match(r"^[A-Za-z0-9_.-]+$", value):
        raise EngineError("'{}' is not a valid CloudBolt Engine budget ID.".format(value[:40]))
    return value


def _base_url(connection):
    """The API origin from the connection: https only (the client secret is
    sent to it), trailing slashes and any path dropped, a full URL in the host
    field tolerated."""
    host = (connection.ip or "").strip()
    if not host:
        raise EngineConfigError(
            "Connection '{}' has no host. Put the Engine API host in the IP/Hostname field.".format(
                connection.name
            )
        )
    protocol = (connection.protocol or "https").strip().lower() or "https"
    if "://" in host:
        parts = urlsplit(host)
        protocol = (parts.scheme or protocol).lower()
        host = parts.netloc
    else:
        host = host.split("/", 1)[0]
    host = host.strip().rstrip("/")
    if protocol != "https":
        raise EngineConfigError(
            "Connection '{}' uses {}. CloudBolt Engine connections must use https, because the "
            "client secret is sent to the host.".format(connection.name, protocol or "no protocol")
        )
    if not host:
        raise EngineConfigError(
            "Connection '{}' has no host. Put the Engine API host in the IP/Hostname field.".format(
                connection.name
            )
        )
    port = connection.port
    if port and ":" not in host and int(port) != 443:
        host = "{}:{}".format(host, int(port))
    return "https://{}".format(host)


# =============================================================================
# Connections
# =============================================================================
def list_connections():
    """Every ConnectionInfo carrying the CloudBolt Engine label, by name."""
    return list(
        ConnectionInfo.objects.filter(labels__name=CONNECTION_INFO_LABEL).distinct().order_by("name")
    )


def get_connection(ref):
    """The labeled ConnectionInfo with this global ID. The label is enforced
    even when a reference is supplied: a global ID in a form or a stored
    document is plain data, and the label is what an administrator uses to
    mark a connection as safe for this integration."""
    reference = str(ref or "").strip()
    connection = None
    if reference:
        connection = (
            ConnectionInfo.objects.filter(labels__name=CONNECTION_INFO_LABEL, global_id=reference)
            .distinct()
            .first()
        )
    if connection is None:
        raise EngineConfigError(
            "No connection labeled '{}' has the ID '{}'. Check it under Admin > Connection Info: "
            "protocol https, the Engine API host, the API client ID as username, the client "
            "secret as password, and the '{}' label.".format(
                CONNECTION_INFO_LABEL, reference or "(none)", CONNECTION_INFO_LABEL
            )
        )
    return connection


# =============================================================================
# Registration store: one CustomFieldValue (a JSON document) per group
# =============================================================================
def _existing_field():
    return CustomField.objects.filter(name=STORE_FIELD).first()


def _field():
    """The store field, created on first use in its own Namespace so it stays
    off the Parameters list and the blueprint parameter picker."""
    namespace, _ = Namespace.objects.get_or_create(name=STORE_NAMESPACE)
    field, _ = CustomField.objects.get_or_create(
        name=STORE_FIELD,
        defaults=dict(
            label=STORE_FIELD_LABEL,
            description=STORE_FIELD_DESCRIPTION,
            type="TXT",
            namespace=namespace,
        ),
    )
    if field.type not in ("TXT", "CODE"):
        raise EngineConfigError(
            "The {} parameter must be Multi-line Text or Code; it is {}.".format(STORE_FIELD, field.type)
        )
    if field.namespace_id != namespace.id:
        field.namespace = namespace
        field.save()
    return field


@contextmanager
def _locked():
    """A transaction holding a row lock on the store field, so concurrent
    writes run one at a time and see each other's results."""
    field = _field()
    with transaction.atomic():
        list(CustomField.objects.select_for_update().filter(pk=field.pk))
        yield field


def _serialize(document):
    """The one place a registration becomes stored text. _group_fragment
    depends on its exact form (the "group" key first, default separators), so
    it is only ever produced here."""
    return json.dumps(document, indent=2)


def _group_fragment(gid):
    """The text _serialize produces for the group key; lets a lookup narrow
    the rows in the database before parsing any JSON."""
    return '"group": {}'.format(json.dumps(gid))


def _rows(field, gid=None):
    """[(CustomFieldValue, document)] for the stored registrations, oldest
    first; with gid, only that group's (found by narrowing the query on its
    serialized key, then verifying after parsing). An unreadable value is
    logged and skipped; a second value for a group already seen is ignored so
    the oldest row stays the registration."""
    if field is None:
        return []
    queryset = CustomFieldValue.objects.filter(field=field)
    if gid is not None:
        queryset = queryset.filter(txt_value__contains=_group_fragment(gid))
    rows, seen = [], set()
    for cfv in queryset.order_by("id"):
        try:
            document = json.loads(cfv.txt_value or "")
        except ValueError:
            document = None
        if not isinstance(document, dict) or not document.get("group"):
            logger.warning("Skipping unreadable CloudBolt Engine registration value %s", cfv.id)
            continue
        if gid is not None and document["group"] != gid:
            continue
        if document["group"] in seen:
            logger.warning("Ignoring duplicate CloudBolt Engine registration for %s", document["group"])
            continue
        seen.add(document["group"])
        rows.append((cfv, document))
    return rows


def _write(cfv, document):
    # Set through the typed value property and save so an existing row is
    # updated in place rather than matched or re-created by value.
    cfv.value = _serialize(document)
    cfv.save()


def get_registration(group):
    """The registration document for this group (a Group or its global ID), or
    None. For a pending registration `snapshot` is None until the first
    refresh, then a snapshot built from the pending request (no Engine data)."""
    gid = _gid(group)
    rows = _rows(_existing_field(), gid)
    return rows[0][1] if rows else None


def list_registrations():
    """Every registration, ordered by group global ID."""
    return sorted((d for _, d in _rows(_existing_field())), key=lambda d: d["group"])


def _save_registration(gid, mutate):
    """Read the group's document under the store lock, apply mutate(document)
    (which edits it in place; returning False skips the write) and write it
    back to the same row. Returns the document as it stands. Raises
    EngineError if the group has no registration."""
    with _locked() as field:
        for cfv, document in _rows(field, gid):
            if mutate(document) is not False:
                document["updated_at"] = _now_iso()
                _write(cfv, document)
            return document
    raise EngineError("Group {} has no CloudBolt Engine budget.".format(gid))


def _require_registration(group):
    gid = _gid(group)
    document = get_registration(gid)
    if document is None:
        raise EngineError("Group {} has no CloudBolt Engine budget.".format(gid))
    return document


def _client_for(document):
    return EngineClient(get_connection(document.get("connection")))


GONE_MESSAGE = (
    "This budget no longer exists in CloudBolt Engine. A group manager should remove it here "
    "and set it again."
)


def _budget_call(function, *args):
    """Call a budget-specific client method; a 404 means the budget was
    deleted in Engine, which has its own explanation."""
    try:
        return function(*args)
    except EngineNotFound:
        raise EngineNotFound(GONE_MESSAGE, status=404, code="budget_not_found")


# =============================================================================
# Naming
# =============================================================================
def _slug(text):
    slug = re.sub(r"[^a-z0-9_-]+", "-", str(text or "").lower())
    slug = re.sub(r"-{2,}", "-", slug).strip("-_")
    return slug[:TAG_VALUE_MAX].rstrip("-_")


def default_tag_value(group):
    """The tag value for this group: its stored one when it is registered
    (never changes, even if the group is renamed), else a lowercase slug of
    its name using a-z 0-9 _ -, at most 63 characters, made unique across
    registrations by appending the group's global ID suffix. One scan of the
    store."""
    gid = _gid(group)
    registrations = list_registrations()
    for document in registrations:
        if document["group"] == gid:
            return document["tag_value"]
    base = _slug(getattr(group, "name", "")) or "group"
    taken = {d["tag_value"] for d in registrations}
    if base not in taken:
        return base
    suffix = re.sub(r"[^a-z0-9]", "", gid.split("-", 1)[-1].lower()) or "x"
    tail = "-" + suffix
    return base[: TAG_VALUE_MAX - len(tail)].rstrip("-_") + tail


def default_budget_name(group):
    """"<group name> [<group global_id>]": unique in the organization and
    traceable to the group. Truncated to Engine's 255 character limit."""
    gid = _gid(group)
    tail = " [{}]".format(gid)
    return (str(getattr(group, "name", "") or gid)[: 255 - len(tail)] + tail)


# =============================================================================
# Budget helpers
# =============================================================================
def _amount_for_month(periods, month):
    """The amount a breakpoint list gives a month: the last period starting at
    or before it, or None when none does."""
    amount = None
    for period in sorted(periods or [], key=lambda p: p.get("month") or ""):
        if (period.get("month") or "") <= month:
            amount = period.get("amount")
    return amount


def _with_amount(periods, month, amount):
    """The period list with this month's amount replaced (or added) and the
    other periods kept, sorted by month."""
    kept = [
        {"month": p["month"], "amount": str(p["amount"])}
        for p in periods or []
        if p.get("month") and p["month"] != month
    ]
    kept.append({"month": month, "amount": _money(amount)})
    return sorted(kept, key=lambda p: p["month"])


_AMOUNT_RE = re.compile(r"^[0-9]+(\.[0-9]+)?$")
AMOUNT_MAX = Decimal("1000000000000")


def _clean_amount(amount):
    """A budget amount as a Decimal with 2 places. Strict: a plain positive
    number with at most 2 decimals (a Decimal, string or integer); anything
    else is an error, never 0."""
    text = str(amount).strip() if amount is not None else ""
    problem = "The budget amount must be a number greater than 0 with at most 2 decimal places, such as 1500 or 1500.50."
    if not _AMOUNT_RE.match(text):
        raise EngineError(problem)
    try:
        value = Decimal(text)
    except (InvalidOperation, ValueError):
        raise EngineError(problem)
    if not value.is_finite() or value <= 0 or value >= AMOUNT_MAX:
        raise EngineError(problem)
    if value != value.quantize(CENT):
        raise EngineError(problem)
    return value.quantize(CENT)


def _filter_for(registration):
    return {
        "field": registration["tag_field"],
        "operator": "EQUALS",
        "value": registration["tag_value"],
    }


def _sync_budget(client, budget_id, current, name=None, description=None, amount=None):
    """PATCH only what differs from `current` (the budget as Engine returned
    it): None means leave alone; description "" clears it. The periods list is
    sent (whole, as Engine replaces it) only when `amount` differs from the
    current month's effective amount. Returns True when a PATCH was sent."""
    payload = {}
    if name is not None and name != (current.get("name") or ""):
        payload["name"] = name
    if description is not None and description != (current.get("description") or ""):
        payload["description"] = description or None
    if amount is not None:
        month = _month_key(_utc_today())
        effective = _amount_for_month(current.get("periods"), month)
        if effective is None or _dec(effective) != amount:
            payload["periods"] = _with_amount(current.get("periods"), month, amount)
    if not payload:
        return False
    _budget_call(client.update_budget, budget_id, payload)
    return True


def _create_remote(client, registration):
    """Create the Engine budget for a registration that has `pending` and a
    tag field. When Engine says the name is taken (budget_name_conflict), a
    budget of that name from an earlier attempt that stopped before saving is
    adopted if its filter matches, and its amount is corrected to the pending
    one."""
    pending = registration["pending"]
    wanted = _filter_for(registration)
    amount = _clean_amount(pending["amount"])
    payload = {
        "name": pending["name"],
        "currency": pending.get("currency") or "USD",
        "periods": [{"month": _month_key(_utc_today()), "amount": _money(amount)}],
        "filters": [wanted],
    }
    if pending.get("description"):
        payload["description"] = pending["description"]
    try:
        return client.create_budget(payload)
    except EngineError as exc:
        if exc.status != 422 or "budget_name_conflict" not in str(exc):
            raise
        for summary in client.list_budgets():
            if summary.get("name") != pending["name"]:
                continue
            budget = client.get_budget(summary["id"])
            if not any(
                f.get("field") == wanted["field"]
                and f.get("operator") == "EQUALS"
                and f.get("value") == wanted["value"]
                for f in budget.get("filters") or []
            ):
                raise EngineError(
                    "A budget named '{}' already exists in CloudBolt Engine and does not filter "
                    "on this group's tag. Rename or delete it there, then try again.".format(pending["name"])
                )
            _sync_budget(client, budget["id"], budget, amount=amount)
            return budget
        raise


# =============================================================================
# Group operations
# =============================================================================
def register_group(
    group, connection_ref, amount, currency="USD", name=None, description=None, tag_key=DEFAULT_TAG_KEY
):
    """
    Give a group an Engine budget of `amount` for the current month that
    counts only cost rows tagged tag_key = the group's tag value. The Engine
    budget is created now when the tag key already has a dimension in Engine's
    billing data; otherwise the request is stored as `pending` and
    ensure_budget creates it once the dimension appears. Returns the
    registration document.

    The registration is saved before anything is created in Engine. If
    creating the budget then fails, the registration stays (pending) so the
    scheduled refresh retries it, and EngineError is raised saying so.
    """
    gid = _gid(group)
    connection = get_connection(connection_ref)
    value = _clean_amount(amount)
    currency = str(currency or "USD").strip().upper()
    if not re.match(r"^[A-Z]{3}$", currency):
        raise EngineError("The currency must be a three-letter code such as USD.")
    tag_key = str(tag_key or DEFAULT_TAG_KEY).strip()
    if not tag_dimension_name(tag_key):
        raise EngineError("'{}' is not a tag key CloudBolt Engine can report on.".format(tag_key[:60]))
    budget_name = (name or "").strip() or default_budget_name(group)
    if len(budget_name) > 255:
        raise EngineError("The budget name can be at most 255 characters.")
    client = EngineClient(connection)
    tag_field = client.find_tag_field(tag_key)
    now = _now_iso()
    with _locked() as field:
        if _rows(field, gid):
            raise EngineError("Group {} already has a CloudBolt Engine budget.".format(gid))
        document = {
            "group": gid,
            "connection": connection.global_id,
            "budget_id": None,
            "tag_key": tag_key,
            "tag_value": default_tag_value(group),
            "tag_field": tag_field,
            "pending": {
                "name": budget_name,
                "amount": _money(value),
                "currency": currency,
                "description": (description or "").strip() or None,
            },
            "snapshot": None,
            "created_at": now,
            "updated_at": now,
        }
        _write(CustomFieldValue(field=field), document)
    if tag_field:
        try:
            return ensure_budget(gid)
        except EngineError as exc:
            raise EngineError(
                "{} The budget request is saved for this group and the scheduled refresh will retry it.".format(
                    str(exc).rstrip()
                ),
                status=exc.status,
                code=exc.code,
            )
    return document


def update_group_budget(group, amount=None, name=None, description=None):
    """
    Change the group's budget. None means unchanged for all three arguments;
    name "" is also unchanged; description "" clears the description. A new
    amount applies from the current month (UTC): that month's breakpoint is
    replaced or added and the full period list is sent, but only when it
    differs from the amount Engine already has for the month; nothing is sent
    to Engine when nothing changes. While the budget is still pending, only the
    stored request changes (no call to Engine). The snapshot is refreshed
    afterwards. Returns the registration document.
    """
    gid = _gid(group)
    _require_registration(gid)
    new_amount = _clean_amount(amount) if amount is not None else None
    new_name = (name or "").strip() if name is not None else ""
    if len(new_name) > 255:
        raise EngineError("The budget name can be at most 255 characters.")
    new_name = new_name or None
    new_description = (description or "").strip() if description is not None else None

    outcome = {}

    def apply(doc):
        # Decided under the lock: the job may have created the budget since
        # the document was first read.
        if doc.get("budget_id"):
            outcome["remote"] = True
            return False
        pending = doc.get("pending") or {}
        if new_amount is not None:
            pending["amount"] = _money(new_amount)
        if new_name is not None:
            pending["name"] = new_name
        if new_description is not None:
            pending["description"] = new_description or None
        doc["pending"] = pending
        snapshot = doc.get("snapshot")
        if snapshot and isinstance(snapshot.get("budget"), dict) and not snapshot["budget"].get("id"):
            snapshot["budget"]["name"] = pending.get("name")
            snapshot["budget"]["description"] = pending.get("description")
            snapshot["budget"]["amount"] = pending.get("amount")
        return True

    document = _save_registration(gid, apply)
    if not outcome.get("remote"):
        return document

    client = _client_for(document)
    current = _budget_call(client.get_budget, document["budget_id"])
    _sync_budget(client, document["budget_id"], current, new_name, new_description, new_amount)
    refresh_snapshot(gid)
    return get_registration(gid)


def remove_group_budget(group, delete_remote=True):
    """Remove the group's registration and, by default, its Engine budget.
    A budget that is already gone from Engine is not an error. When the Engine
    delete cannot be done (the connection is missing or unlabeled, the
    credentials fail, Engine errors) EngineError is raised and the
    registration is kept; delete_remote=False removes the registration
    locally and never touches the connection."""
    gid = _gid(group)
    document = get_registration(gid)
    if document is None:
        return
    if delete_remote and document.get("budget_id"):
        try:
            _client_for(document).delete_budget(document["budget_id"])
        except EngineNotFound:
            pass
        except EngineError as exc:
            raise EngineError(
                "The budget could not be deleted in CloudBolt Engine, so it was kept here: {} "
                "Fix the problem and try again, or remove it here only.".format(str(exc).rstrip()),
                status=exc.status,
                code=exc.code,
            )
    with _locked() as field:
        for cfv, _ in _rows(field, gid):
            cfv.delete()


def ensure_budget(group):
    """Create the Engine budget of a pending registration once the tag field
    exists in Engine. Returns the registration (still pending, without
    raising, when the field has not appeared yet). Pending values edited while
    the budget was being created are applied to the new budget."""
    gid = _gid(group)
    document = _require_registration(gid)
    if document.get("budget_id"):
        return document
    if not document.get("pending"):
        raise EngineError("Group {} has no budget to create.".format(gid))
    client = _client_for(document)
    tag_field = client.find_tag_field(document["tag_key"])
    if not tag_field:
        return document
    sent = dict(document["pending"])
    budget = _create_remote(client, dict(document, tag_field=tag_field))
    later = {}

    def apply(doc):
        doc["tag_field"] = tag_field
        doc["budget_id"] = budget["id"]
        if doc.get("pending") != sent:
            later.update(doc.get("pending") or {})
        doc["pending"] = None

    saved = _save_registration(gid, apply)
    if later:
        current = _budget_call(client.get_budget, budget["id"])
        _sync_budget(
            client,
            budget["id"],
            current,
            later.get("name"),
            later.get("description") or "",
            _clean_amount(later["amount"]) if later.get("amount") else None,
        )
    return saved


def _timestamp(value):
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def refresh_snapshot(group):
    """
    Fetch the budget, its performance for the trailing 12 months and the
    group's daily costs, compute the month-end trend and save the result in
    the registration. Returns the stored snapshot.

    It does not raise for Engine or upstream failures: the message goes to
    the snapshot's `error`, the previous data is kept, and that snapshot is
    returned. It raises EngineError only when the group has no registration.
    A registration still waiting for the tag field gets a snapshot built from
    its pending request (error null, trend status no_data).
    """
    gid = _gid(group)
    document = _require_registration(gid)
    seen_at = (document.get("snapshot") or {}).get("fetched_at")
    if not document.get("budget_id"):
        pending = document.get("pending") or {}
        budget_amount = _money(pending.get("amount"))
        snapshot = {
            "fetched_at": _now_iso(),
            "error": None,
            "budget": {
                "id": None,
                "name": pending.get("name"),
                "description": pending.get("description"),
                "currency": pending.get("currency") or "USD",
                "amount": budget_amount,
                "periods": [],
            },
            "months": [],
            "daily": [],
            "trend": compute_trend([], _utc_today(), _dec(budget_amount)),
        }
        return _store_snapshot(gid, snapshot, require_no_budget=True)
    try:
        snapshot = _build_snapshot(document)
    except EngineError as exc:
        return _record_error(gid, str(exc), seen_at)
    except Exception as exc:
        logger.exception("CloudBolt Engine snapshot refresh failed for %s", gid)
        return _record_error(
            gid, "Could not refresh the budget snapshot: {}.".format(type(exc).__name__), seen_at
        )
    return _store_snapshot(gid, snapshot)


def _store_snapshot(gid, snapshot, require_no_budget=False):
    """Save a snapshot unless a newer one is already stored (two refreshes
    can overlap), or, for a pending-style snapshot, the budget got created
    meanwhile. Returns what is stored."""
    def apply(doc):
        if require_no_budget and doc.get("budget_id"):
            return False
        stored = _timestamp((doc.get("snapshot") or {}).get("fetched_at"))
        mine = _timestamp(snapshot["fetched_at"])
        if stored is not None and mine is not None and stored > mine:
            return False
        doc["snapshot"] = snapshot

    return _save_registration(gid, apply).get("snapshot") or snapshot


def _record_error(gid, message, seen_at):
    """Put `message` on the stored snapshot, keeping its data. Never raises:
    if the error cannot be saved, the original message is still what the
    caller gets back."""
    fallback = {"fetched_at": None, "error": message, "budget": None, "months": [], "daily": [], "trend": None}

    def apply(doc):
        snapshot = doc.get("snapshot")
        if snapshot is not None and snapshot.get("fetched_at") != seen_at:
            # A newer refresh stored its result meanwhile; do not mark it.
            return False
        snapshot = snapshot or dict(fallback)
        snapshot["error"] = message
        doc["snapshot"] = snapshot

    try:
        return _save_registration(gid, apply).get("snapshot") or fallback
    except Exception:
        logger.exception("Could not record the CloudBolt Engine refresh error for %s", gid)
        return fallback


def _build_snapshot(document):
    client = _client_for(document)
    today = _utc_today()
    this_month = _month_key(today)
    first = today.replace(day=1)
    start_month = _month_key(_add_months(first, -(PERFORMANCE_MONTHS - 1)))

    budget = _budget_call(client.get_budget, document["budget_id"]) or {}
    performance = _budget_call(client.get_performance, document["budget_id"], start_month, this_month) or {}
    months = []
    for row in performance.get("months") or []:
        pct = row.get("variance_pct")
        months.append(
            {
                "month": row.get("month"),
                "budgeted": _money(row.get("budgeted")),
                "actual": _money(row.get("actual")),
                "variance": _money(row.get("variance")),
                "variance_pct": pct if isinstance(pct, (int, float)) else None,
                "in_progress": bool(row.get("in_progress")),
            }
        )
    current = next((m for m in months if m["month"] == this_month), None)
    if current:
        amount = current["budgeted"]
    else:
        amount = _money(_amount_for_month(budget.get("periods"), this_month))

    window_start = first - timedelta(days=TREND_RATE_DAYS)
    tag_field = document["tag_field"]
    body = {
        "dataSource": "FOCUS",
        "groupBy": [tag_field],
        "metrics": ["billed_cost"],
        "dateRange": {"from": window_start.isoformat(), "to": today.isoformat()},
        "granularity": "day",
        "filters": [{"member": tag_field, "operator": "equals", "values": [document["tag_value"]]}],
        "limit": COST_QUERY_LIMIT,
    }
    daily = _daily_from_rows((client.query_costs(body) or {}).get("data") or [])
    trend = compute_trend(daily, today, _dec(amount))
    return {
        "fetched_at": _now_iso(),
        "error": None,
        "budget": {
            "id": budget.get("id"),
            "name": budget.get("name"),
            "description": budget.get("description"),
            "currency": budget.get("currency") or performance.get("currency") or "USD",
            "amount": amount,
            "periods": budget.get("periods") or [],
        },
        "months": months,
        "daily": month_daily(daily, today),
        "trend": trend,
    }


def _daily_from_rows(rows):
    """[(date, Decimal)] from cost-explorer day rows, summed per day. A row's
    day is under "charge_period_start.day" (an ISO timestamp) and its cost
    under "billed_cost"; Cube sends measures as strings, numbers are accepted
    too."""
    totals = {}
    for row in rows:
        raw = row.get("charge_period_start.day") or row.get("charge_period_start")
        try:
            day = date.fromisoformat(str(raw)[:10])
        except ValueError:
            continue
        totals[day] = totals.get(day, Decimal(0)) + _dec(row.get("billed_cost"))
    return sorted(totals.items())


def refresh_all():
    """Per registered group: ensure_budget, then refresh_snapshot. One group's
    failure does not stop the others. A registration whose Group no longer
    exists in CMP is deleted locally (its Engine budget is left alone) and
    reported. Returns [{"group": "GRP-...", "ok": bool, "message": str}]."""
    registrations = list_registrations()
    existing = set()
    if registrations:
        existing = set(
            Group.objects.filter(global_id__in=[d["group"] for d in registrations]).values_list(
                "global_id", flat=True
            )
        )
    results = []
    for document in registrations:
        gid = document["group"]
        if gid not in existing:
            try:
                remove_group_budget(gid, delete_remote=False)
                note = "The group no longer exists in CloudBolt, so its budget registration was removed here."
                if document.get("budget_id"):
                    note += " The budget in CloudBolt Engine was not deleted."
                results.append({"group": gid, "ok": True, "message": note})
            except Exception as exc:
                logger.exception("Could not remove the registration of deleted group %s", gid)
                results.append(
                    {
                        "group": gid,
                        "ok": False,
                        "message": "The group no longer exists but its registration could not be removed: {}.".format(
                            type(exc).__name__
                        ),
                    }
                )
            continue
        try:
            ensured = ensure_budget(gid)
            snapshot = refresh_snapshot(gid)
            if not ensured.get("budget_id") and not snapshot.get("error"):
                results.append(
                    {
                        "group": gid,
                        "ok": True,
                        "message": "Waiting for the {} tag to appear in billing data; budget not created yet.".format(
                            ensured["tag_key"]
                        ),
                    }
                )
            elif snapshot.get("error"):
                results.append({"group": gid, "ok": False, "message": snapshot["error"]})
            else:
                trend = snapshot.get("trend") or {}
                results.append(
                    {
                        "group": gid,
                        "ok": True,
                        "message": "Refreshed: {} (spent {} of {}, projected {}).".format(
                            trend.get("status"),
                            trend.get("spend_to_date"),
                            trend.get("budget"),
                            trend.get("projected"),
                        ),
                    }
                )
        except EngineError as exc:
            results.append({"group": gid, "ok": False, "message": str(exc)})
        except Exception as exc:
            logger.exception("CloudBolt Engine refresh failed for %s", gid)
            results.append(
                {"group": gid, "ok": False, "message": "Unexpected {} while refreshing.".format(type(exc).__name__)}
            )
    return results


# =============================================================================
# Trend (pure: no I/O, no CloudBolt)
# =============================================================================
def month_daily(daily, today):
    """The current month's daily costs for the snapshot: one
    {"day", "cost"} per day from the 1st through the latest day with data,
    missing days "0". Empty when no data reaches the current month."""
    first = today.replace(day=1)
    through = max((d for d, _ in daily if d <= today), default=None)
    if through is None or through < first:
        return []
    by_day = {d: c for d, c in daily}
    out = []
    day = first
    while day <= through:
        cost = by_day.get(day)
        out.append({"day": day.isoformat(), "cost": _money(cost) if cost is not None else "0"})
        day += timedelta(days=1)
    return out


def compute_trend(daily, today, budget_amount):
    """
    Month-end trend for the month containing `today`.

    daily: [(date, Decimal)], one entry per day that has a cost row, possibly
        including days of the previous month (they feed the daily rate).
        Days after `today` are ignored.
    today: a date; its month is the budget month.
    budget_amount: the month's budget (Decimal, or anything Decimal accepts).

    data_through is the latest day with a cost row. spend_to_date sums the
    current month. daily_rate is the mean of the 7 calendar days ending at
    data_through, a day with no row counting as 0, except that days before the
    first day that has data are not counted (fewer days available means the
    mean of the days that exist). remaining_days is the number of days of the
    month after data_through (all of them when data_through is before the 1st).
    projected = spend_to_date + daily_rate * remaining_days; headroom =
    budget - projected. Status is no_data when there are no rows, over when
    spend_to_date exceeds the budget, at_risk when projected exceeds it, else
    on_track; it is decided on the amounts as shown (rounded to 2 places).
    """
    budget = _dec(budget_amount)
    first = today.replace(day=1)
    days_in_month = calendar.monthrange(today.year, today.month)[1]
    rows = {}
    for day, cost in daily:
        if day <= today:
            rows[day] = rows.get(day, Decimal(0)) + _dec(cost)
    if not rows:
        return {
            "status": "no_data",
            "data_through": None,
            "spend_to_date": _money(0),
            "daily_rate": _money(0),
            "remaining_days": days_in_month,
            "projected": _money(0),
            "budget": _money(budget),
            "headroom": _money(budget),
        }
    data_through = max(rows)
    earliest = min(rows)
    spend = sum((c for d, c in rows.items() if d >= first), Decimal(0))
    window_start = max(data_through - timedelta(days=TREND_RATE_DAYS - 1), earliest)
    window_days = (data_through - window_start).days + 1
    window_total = sum((c for d, c in rows.items() if window_start <= d <= data_through), Decimal(0))
    # Credits can make a window's mean negative; a projection never runs below
    # the spend so far, so the rate used (and shown) is at least 0.
    rate = max(window_total / Decimal(window_days), Decimal(0))
    remaining = days_in_month - data_through.day if data_through >= first else days_in_month
    projected = spend + rate * Decimal(remaining)
    spend_r = spend.quantize(CENT, rounding=ROUND_HALF_UP)
    rate_r = rate.quantize(CENT, rounding=ROUND_HALF_UP)
    projected_r = projected.quantize(CENT, rounding=ROUND_HALF_UP)
    budget_r = budget.quantize(CENT, rounding=ROUND_HALF_UP)
    if spend_r > budget_r:
        status = "over"
    elif projected_r > budget_r:
        status = "at_risk"
    else:
        status = "on_track"
    return {
        "status": status,
        "data_through": data_through.isoformat(),
        "spend_to_date": str(spend_r),
        "daily_rate": str(rate_r),
        "remaining_days": remaining,
        "projected": str(projected_r),
        "budget": str(budget_r),
        "headroom": str(budget_r - projected_r),
    }
