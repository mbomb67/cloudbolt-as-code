# Compare Azure List Prices

MCP Tool Action published to agents as `custom_compare_azure_list_prices`. It queries the public [Azure Retail Prices API](https://learn.microsoft.com/en-us/rest/api/cost-management/retail-prices/azure-retail-prices) for the regions of the Azure environments the calling user can order into, and returns list prices with a monthly estimate where the meter unit allows one. Read-only. It never provisions or orders.

## What syncs

| Item | ID |
|---|---|
| MCP Tool Action | `MTA-ab0krash` |
| Plugin | [`OHK-r9cm4oar`](../../plugins/OHK-r9cm4oar/) |
| Shared module | [`env_options`](../../shared_modules/SHM-r0oq14r7/) (entitlement and region lookup) |

## After every sync

1. Edit the action and set **Synchronous Action = Yes**. Sync import does not restore it, so a freshly imported action runs asynchronously and returns a job ID instead of the result.
2. Assign the roles that may see and run the tool. With no roles it is open to every authenticated API consumer. Roles never sync.
3. Reconnect the MCP client; tool lists are cached per session.

The caller's MCP token needs the `read` and `write` scopes even though the tool is read-only.

## Inputs

Give at least one filter. Exact fields and `contains` matches are case-sensitive, as the API requires.

| Input | Type | Meaning |
|---|---|---|
| `service_name` | string | Exact `serviceName`, e.g. `Virtual Machines`, `Storage`, `Azure Database for PostgreSQL` |
| `service_contains` | string | Substring of `serviceName` |
| `product_contains` | string | Substring of `productName`; VM Windows rows end in ` Windows` |
| `sku_contains` | string | Substring of `skuName` |
| `meter_contains` | string | Substring of `meterName` |
| `arm_sku_name` | string | Exact `armSkuName`, e.g. `Standard_D4s_v5` |
| `price_type` | enum | `Consumption` (default), `DevTestConsumption`, `Reservation` |
| `include_spot` | boolean | Keep Spot and Low Priority meters (default false) |
| `hours_per_month` | integer | Multiplier for hourly meters (default 730) |
| `quantity_per_month` | number | Usage applied to per-unit meters such as GB/Month |
| `max_rows` | integer | Row cap across all regions (default 200, max 500) |

## Output

`rows` (one per meter and region, camelCase keys, `estMonthlyUsd` null when the unit needs a quantity), `cheapestRegionPerMeter`, `regions` (region to entitled environment IDs), `unavailable` (environments without a location, regions that failed or timed out), `hints` (distinct service, product, SKU, meter names and units seen, or from a service-only fallback query when nothing matched), `truncated`, `message`, `disclaimer`.

## Examples

| Ask | Filters |
|---|---|
| Linux D4s v5 VM | `service_name=Virtual Machines`, `arm_sku_name=Standard_D4s_v5` |
| Windows D4s v5 VM | same plus `product_contains=Windows` |
| P30 premium disk | `service_name=Storage`, `product_contains=Premium SSD Managed Disks`, `sku_contains=P30 LRS` |
| PostgreSQL flexible server, 4 vCores | `service_contains=PostgreSQL`, `product_contains=Flexible Server`, `sku_contains=4 vCore` |

## Limits

Per-request timeout 5 s connect and 15 s read, three pages per region, 40 s total. Regions past the budget are listed under `unavailable` with reason `timeout`. If the API fails for every region the run fails; prices are never fabricated. Environments whose `node_location` parameter is empty are reported under `unavailable` with reason `no_location_configured`.
