"""
Compare Azure List Prices (MCP Tool Action plugin).

Looks up public Azure list prices from the Azure Retail Prices API for the
regions of the Azure environments the calling user can order into, and
estimates a monthly cost where the meter's unit allows it. Read-only: it never
provisions, orders, or changes anything in CloudBolt or Azure.

Entitlement is enforced here, not by the platform. MCP Tool Actions run with
CloudBolt's own access, so the environment list is built from
group.get_available_environments() for every group the caller belongs to, the
same query the built-in fetch_orderable_environments reaches through
ServiceItem.enabled_envs_for_group.

Every declared input is read from its rendered template token in
_read_inputs(); keep each one referenced that way or CloudBolt's token scan
deletes the input when the script is saved. Output keys are camelCase so a
synchronous run and an asynchronous fetch_job read identically.

Azure Retail Prices API reference (endpoint, filters, pagination, case
sensitivity, savings plans):
https://learn.microsoft.com/en-us/rest/api/cost-management/retail-prices/azure-retail-prices
"""
import datetime
import re
import time

import requests

from common.methods import get_proxies, set_progress
from infrastructure.models import Environment
from shared_modules.env_options import available_environments, azure_handler
from utilities.helpers import get_ssl_verification
from utilities.logger import ThreadLogger

logger = ThreadLogger(__name__)

API_URL = "https://prices.azure.com/api/retail/prices"
API_VERSION = "2023-01-01-preview"
PRICE_TYPES = ("Consumption", "DevTestConsumption", "Reservation")
FILTER_INPUTS = (
    "service_name",
    "service_contains",
    "product_contains",
    "sku_contains",
    "meter_contains",
    "arm_sku_name",
)
NARROWING_INPUTS = ("product_contains", "sku_contains", "meter_contains", "arm_sku_name")

DEFAULT_HOURS = 730
DEFAULT_MAX_ROWS = 200
HARD_MAX_ROWS = 500
MAX_PAGES_PER_REGION = 3
TOTAL_BUDGET_SECONDS = 40
REQUEST_TIMEOUT = (5, 15)  # (connect, read) seconds per request
HINT_LIMIT = 50

DISCLAIMER = (
    "Public Azure list prices in USD excluding discounts, credits, and tax. "
    "Monthly figures are estimates from the meter unit and the supplied hours "
    "or quantity. Not a quote."
)

# unitOfMeasure looks like "1 Hour", "1/Month", "1 GB/Month", "10K", "100".
UNIT_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)?\s*(.*?)\s*$")


# --------------------------------------------------------------------------
# Inputs
# --------------------------------------------------------------------------


def _text(value):
    return (value or "").strip()


def _bool(value):
    return _text(value).lower() in ("true", "1", "yes", "y", "on")


def _int(value, default, low, high):
    text = _text(value)
    if not text:
        return default
    try:
        number = int(float(text))
    except ValueError:
        return default
    return max(low, min(high, number))


def _float(value):
    text = _text(value)
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _read_inputs():
    """
    Read the declared inputs from their rendered template tokens. An omitted
    input renders as an empty string; a BOOL renders as True or False.
    """
    raw = {
        "service_name": """{{ service_name }}""",
        "service_contains": """{{ service_contains }}""",
        "product_contains": """{{ product_contains }}""",
        "sku_contains": """{{ sku_contains }}""",
        "meter_contains": """{{ meter_contains }}""",
        "arm_sku_name": """{{ arm_sku_name }}""",
        "price_type": """{{ price_type }}""",
        "include_spot": """{{ include_spot }}""",
        "hours_per_month": """{{ hours_per_month }}""",
        "quantity_per_month": """{{ quantity_per_month }}""",
        "max_rows": """{{ max_rows }}""",
    }
    inputs = {name: _text(raw[name]) for name in FILTER_INPUTS}

    price_type = _text(raw["price_type"]) or PRICE_TYPES[0]
    canonical = {p.lower(): p for p in PRICE_TYPES}.get(price_type.lower())
    if canonical is None:
        return None, "price_type must be one of {}; got '{}'.".format(
            ", ".join(PRICE_TYPES), price_type
        )
    inputs["price_type"] = canonical
    inputs["include_spot"] = _bool(raw["include_spot"])
    inputs["hours_per_month"] = _int(raw["hours_per_month"], DEFAULT_HOURS, 1, 744)
    inputs["quantity_per_month"] = _float(raw["quantity_per_month"])
    inputs["max_rows"] = _int(raw["max_rows"], DEFAULT_MAX_ROWS, 1, HARD_MAX_ROWS)

    if not any(inputs[name] for name in FILTER_INPUTS):
        return None, "Give at least one filter: {}.".format(", ".join(FILTER_INPUTS))
    return inputs, None


# --------------------------------------------------------------------------
# Entitled environments and regions
# --------------------------------------------------------------------------


def _entitled_azure_environments(profile):
    """
    Map environment id to {"env": Environment, "groups": set of group names}
    for every Azure environment the caller may order into. Uses the platform
    entitlement query (group.get_available_environments, wrapped by
    env_options.available_environments) for each of the caller's groups. A
    CloudBolt admin is entitled to every environment through every group, so
    the loop is skipped for them.
    """
    found = {}
    if profile.is_cbadmin:
        queryset = Environment.objects.filter(
            resource_handler__azurearmhandler__isnull=False
        ).order_by("name")
        for env in queryset:
            found[env.id] = {"env": env, "groups": {"all groups (CloudBolt admin)"}}
        return found

    for group in profile.get_groups(include_global_roles=True):
        for env in available_environments(group, profile=profile, azure_only=True):
            entry = found.setdefault(env.id, {"env": env, "groups": set()})
            entry["groups"].add(group.name)
    return found


def _region_of(env):
    """The environment's Azure region in ARM short form (e.g. eastus), or ""."""
    handler = azure_handler(env)
    if handler is None:
        return ""
    return _text(handler.get_env_location(env))


def _group_by_region(entitled):
    """
    Return ({region: [environment dicts]}, [unavailable dicts]) from the
    entitled environments. Environments without a location are reported, not
    silently dropped.
    """
    regions = {}
    unavailable = []
    for entry in entitled.values():
        env = entry["env"]
        summary = {
            "id": env.global_id,
            "name": env.name,
            "groups": sorted(entry["groups"]),
        }
        region = _region_of(env)
        if not region:
            unavailable.append(
                {
                    "environmentId": env.global_id,
                    "environmentName": env.name,
                    "region": "",
                    "reason": "no_location_configured",
                }
            )
            continue
        regions.setdefault(region, []).append(summary)
    for members in regions.values():
        members.sort(key=lambda item: item["name"].lower())
    return regions, unavailable


# --------------------------------------------------------------------------
# Retail Prices API
# --------------------------------------------------------------------------


class _Budget:
    def __init__(self, seconds):
        self.deadline = time.monotonic() + seconds

    def exhausted(self):
        return time.monotonic() >= self.deadline


def _odata_quote(value):
    # OData string literal: single quotes, embedded quotes doubled.
    return "'" + str(value).replace("'", "''") + "'"


def _filter_string(inputs, region, service_only=False):
    """
    Build the $filter for one region. Exact fields (serviceName, armSkuName)
    and contains() substrings are case-sensitive in this API version.
    """
    parts = [
        "armRegionName eq " + _odata_quote(region),
        "priceType eq " + _odata_quote(inputs["price_type"]),
    ]
    if inputs["service_name"]:
        parts.append("serviceName eq " + _odata_quote(inputs["service_name"]))
    if inputs["service_contains"]:
        parts.append("contains(serviceName, " + _odata_quote(inputs["service_contains"]) + ")")
    if service_only:
        return " and ".join(parts)
    if inputs["product_contains"]:
        parts.append("contains(productName, " + _odata_quote(inputs["product_contains"]) + ")")
    if inputs["sku_contains"]:
        parts.append("contains(skuName, " + _odata_quote(inputs["sku_contains"]) + ")")
    if inputs["meter_contains"]:
        parts.append("contains(meterName, " + _odata_quote(inputs["meter_contains"]) + ")")
    if inputs["arm_sku_name"]:
        parts.append("armSkuName eq " + _odata_quote(inputs["arm_sku_name"]))
    return " and ".join(parts)


def _fetch_items(filter_string, budget, needed, max_pages=MAX_PAGES_PER_REGION):
    """
    GET the filter, following NextPageLink (1,000 rows per page) up to
    max_pages. Returns (items, more): more is True when rows were left unread
    because of the page cap, the row cap, or the time budget. Raises
    requests.RequestException or ValueError (bad JSON) on failure.
    """
    proxies = get_proxies(API_URL)
    verify = get_ssl_verification()
    items = []
    url = API_URL
    params = {"api-version": API_VERSION, "$filter": filter_string}
    pages = 0
    while url:
        response = requests.get(
            url, params=params, timeout=REQUEST_TIMEOUT, proxies=proxies, verify=verify
        )
        response.raise_for_status()
        payload = response.json()
        items.extend(payload.get("Items") or [])
        pages += 1
        url = payload.get("NextPageLink") or ""
        params = None  # NextPageLink carries the filter and $skip
        if url and (pages >= max_pages or len(items) >= needed or budget.exhausted()):
            return items, True
    return items, False


def _is_spot(item):
    text = " ".join(str(item.get(key) or "") for key in ("meterName", "skuName")).lower()
    return "spot" in text or "low priority" in text


def _number(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _monthly(item, hours, quantity):
    """
    Return (usageBasis, estMonthlyUsd) for one meter:
      reservation_term  Reservation rows: term price / 12 or / 36
      hourly            "1 Hour": price / count * hours
      per_month         "1/Month": price / count
      per_unit          "1 GB/Month", "10K", "100": price / count * quantity, or None
      unknown           anything unparseable
    """
    price = _number(item.get("retailPrice"))
    if price is None:
        return "unknown", None

    if item.get("type") == "Reservation":
        term = str(item.get("reservationTerm") or "")
        months = 12 if term.startswith("1") else 36 if term.startswith("3") else None
        return "reservation_term", (round(price / months, 4) if months else None)

    unit = str(item.get("unitOfMeasure") or "")
    match = UNIT_RE.match(unit)
    count = _number(match.group(1)) if match and match.group(1) else None
    unit_text = (match.group(2) if match else unit).strip()
    lowered = unit_text.lower()
    if count is None:
        count = 1.0
        counted = False
    else:
        counted = True
    if lowered in ("k", "m"):  # "10K" means 10,000 units
        count *= 1000.0 if lowered == "k" else 1000000.0
        lowered = "unit"
    if not lowered and counted:
        lowered = "unit"

    if lowered in ("hour", "hours"):
        return "hourly", round(price / count * hours, 4)
    if lowered in ("/month", "month", "months"):
        return "per_month", round(price / count, 4)
    if lowered:
        if quantity is None:
            return "per_unit", None
        return "per_unit", round(price / count * quantity, 4)
    return "unknown", None


def _row(item, region, environments, basis, monthly):
    plans = item.get("savingsPlan")
    savings = None
    if isinstance(plans, list) and plans:
        savings = [
            {"term": plan.get("term"), "retailPrice": plan.get("retailPrice")}
            for plan in plans
            if isinstance(plan, dict)
        ]
    return {
        "region": region,
        "environments": environments,
        "serviceName": item.get("serviceName"),
        "productName": item.get("productName"),
        "skuName": item.get("skuName"),
        "meterName": item.get("meterName"),
        "armSkuName": item.get("armSkuName"),
        "type": item.get("type"),
        "reservationTerm": item.get("reservationTerm"),
        "unitOfMeasure": item.get("unitOfMeasure"),
        "retailPrice": item.get("retailPrice"),
        "usageBasis": basis,
        "estMonthlyUsd": monthly,
        "savingsPlan": savings,
    }


def _meter_key(row):
    return " | ".join(str(row.get(key) or "") for key in ("productName", "skuName", "meterName"))


def _hints(items, source):
    def distinct(key):
        seen = []
        for item in items:
            value = item.get(key)
            if value and value not in seen:
                seen.append(value)
                if len(seen) >= HINT_LIMIT:
                    break
        return seen

    return {
        "serviceNames": distinct("serviceName"),
        "productNames": distinct("productName"),
        "skuNames": distinct("skuName"),
        "meterNames": distinct("meterName"),
        "units": distinct("unitOfMeasure"),
        "hintsFrom": source,
    }


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------


def _failure(message):
    return {"status": "FAILURE", "error_message": message}


def run(job, *args, **kwargs):
    profile = kwargs.get("profile")
    if profile is None:
        return _failure(
            "No calling user profile was supplied. Run this tool through the MCP "
            "server or the API as an authenticated user."
        )

    inputs, error = _read_inputs()
    if error:
        return _failure(error)

    started = datetime.datetime.now(datetime.timezone.utc)
    filters_echo = {name: inputs[name] for name in FILTER_INPUTS}
    filters_echo.update(
        {
            "price_type": inputs["price_type"],
            "include_spot": inputs["include_spot"],
            "hours_per_month": inputs["hours_per_month"],
            "quantity_per_month": inputs["quantity_per_month"],
            "max_rows": inputs["max_rows"],
        }
    )
    outputs = {
        "filters": filters_echo,
        "regions": {},
        "currency": "USD",
        "retrievedAt": started.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "rows": [],
        "cheapestRegionPerMeter": {},
        "truncated": False,
        "hints": _hints([], "results"),
        "unavailable": [],
        "message": "",
        "disclaimer": DISCLAIMER,
    }

    entitled = _entitled_azure_environments(profile)
    if not entitled:
        outputs["message"] = "You are not entitled to any Azure environments."
        return {"status": "SUCCESS", "output_message": outputs["message"], "outputs": outputs}

    regions, unavailable = _group_by_region(entitled)
    outputs["regions"] = {region: [env["id"] for env in members] for region, members in regions.items()}
    outputs["unavailable"] = unavailable
    if not regions:
        outputs["message"] = (
            "Your Azure environments have no Azure location (node_location) configured, "
            "so there is no region to price."
        )
        return {"status": "SUCCESS", "output_message": outputs["message"], "outputs": outputs}

    set_progress(
        "Azure list-price lookup for {} as {} across {} region(s): {}".format(
            {name: inputs[name] for name in FILTER_INPUTS if inputs[name]},
            profile.user.username,
            len(regions),
            ", ".join(sorted(regions)),
        ),
        job,
    )

    budget = _Budget(TOTAL_BUDGET_SECONDS)
    rows = []
    kept_items = []
    dropped_spot = 0
    failures = 0
    truncated = False
    max_rows = inputs["max_rows"]

    for region in sorted(regions):
        if len(rows) >= max_rows:
            truncated = True
            break
        if budget.exhausted():
            unavailable.append(
                {
                    "environmentId": None,
                    "environmentName": None,
                    "region": region,
                    "reason": "timeout",
                    "message": "Time budget of {} s exhausted before this region was queried.".format(
                        TOTAL_BUDGET_SECONDS
                    ),
                }
            )
            continue

        filter_string = _filter_string(inputs, region)
        try:
            items, more = _fetch_items(filter_string, budget, max_rows - len(rows))
        except (requests.RequestException, ValueError) as exc:
            failures += 1
            logger.warning("Retail Prices API call failed for %s: %s", region, exc)
            unavailable.append(
                {
                    "environmentId": None,
                    "environmentName": None,
                    "region": region,
                    "reason": "api_error",
                    "message": str(exc)[:300],
                }
            )
            continue

        truncated = truncated or more
        for item in items:
            if not inputs["include_spot"] and _is_spot(item):
                dropped_spot += 1
                continue
            if len(rows) >= max_rows:
                truncated = True
                break
            basis, monthly = _monthly(item, inputs["hours_per_month"], inputs["quantity_per_month"])
            rows.append(_row(item, region, regions[region], basis, monthly))
            kept_items.append(item)

    if failures and failures == len(regions):
        return _failure(
            "The Azure Retail Prices API could not be reached for any region; no prices "
            "were retrieved. See 'unavailable' in the job log for details: {}".format(
                "; ".join(entry.get("message", "") for entry in unavailable if entry.get("reason") == "api_error")
            )
        )

    rows.sort(
        key=lambda row: (
            _meter_key(row),
            row["estMonthlyUsd"] is None,
            row["estMonthlyUsd"] if row["estMonthlyUsd"] is not None else (_number(row["retailPrice"]) or 0.0),
        )
    )
    cheapest = {}
    for row in rows:
        if row["estMonthlyUsd"] is None:
            continue
        key = _meter_key(row)
        if key not in cheapest or row["estMonthlyUsd"] < cheapest[key]["estMonthlyUsd"]:
            cheapest[key] = {"region": row["region"], "estMonthlyUsd": row["estMonthlyUsd"]}

    hints = _hints(kept_items, "results")
    message = ""
    if not rows:
        narrowed = any(inputs[name] for name in NARROWING_INPUTS)
        if narrowed and not budget.exhausted():
            # Discovery fallback: the service alone, first region, first page,
            # hints only, so the agent can see the real product/SKU/meter names.
            first_region = sorted(regions)[0]
            try:
                fallback_items, _ = _fetch_items(
                    _filter_string(inputs, first_region, service_only=True), budget, 1000, max_pages=1
                )
                fallback_items = [i for i in fallback_items if inputs["include_spot"] or not _is_spot(i)]
                hints = _hints(fallback_items, "fallback")
            except (requests.RequestException, ValueError) as exc:
                logger.warning("Discovery fallback failed: %s", exc)
        if dropped_spot and not inputs["include_spot"]:
            message = "Only Spot or Low Priority meters matched; set include_spot to true to see them."
        elif hints["hintsFrom"] == "fallback" and hints["productNames"]:
            message = (
                "No meters matched the narrowing filters. The hints list the product, SKU and "
                "meter names that exist for this service in {}; refine and call again.".format(first_region)
            )
        else:
            message = (
                "No meters matched. Filter values are exact and case-sensitive (for example "
                "'Virtual Machines', 'Storage', 'Azure Database for PostgreSQL'); try "
                "service_contains or product_contains with a shorter value."
            )
    elif truncated:
        message = (
            "Result truncated at {} rows; narrow the filters using the hints and call again.".format(
                len(rows)
            )
        )

    outputs.update(
        {
            "rows": rows,
            "cheapestRegionPerMeter": cheapest,
            "truncated": truncated,
            "hints": hints,
            "unavailable": unavailable,
            "message": message,
        }
    )
    summary = "{} price row(s) across {} region(s); {} Spot/Low Priority row(s) dropped; truncated={}".format(
        len(rows), len(regions), dropped_spot, truncated
    )
    set_progress(summary, job)
    return {"status": "SUCCESS", "output_message": summary, "outputs": outputs}
