# Azure Negotiated-Rate Cost Preview — Setup & Operations

This content estimates the cost of an Azure VM (compute, OS + data disks, public
IP, and the separate RHEL/SLES license) for CloudBolt's order and blueprint cost
previews, using each customer's **negotiated** Azure prices where possible and
falling back to **public retail** prices otherwise.

## Content in this feature

| Item | ID | Role |
|---|---|---|
| Azure Pricing (shared module) | `SHM-6gtujb8t` | Pricing engine: token, billing detection, Price Sheet download/parse, Retail API, meter mapping, rate_dict assembly |
| Azure Price Sheet Refresh (plugin) | `OHK-bjgpsxoq` | Recurring-job hook that downloads + caches each subscription's negotiated Price Sheet |
| Azure Price Sheet Refresh (recurring job) | `RJB-reblryol` | Schedules the refresh (daily, `0 2 * * *`) |
| Azure Resource Manager Rate Hook (plugin) | `OHK-vg0rmi7i` | The "Compute Server Rate" hook; reads the cache / retail and returns the rate_dict |
| Rate hook wiring (orchestration action) | `HPA-t7hlyvyy` | Binds the rate hook to the "Compute Server Rate" hook point |

## How pricing is sourced (fallback ladder)

Per subscription, per meter:

1. **EA / MCA with a cached negotiated sheet** → negotiated `unitPrice` (keyed by
   Azure `meterId`).
2. **EA / MCA with no usable sheet** (no billing role, not yet downloaded, or a
   specific meter missing) → public **Azure Retail Prices API**.
3. **CSP / Partner Center** → public Retail Prices API (the customer cannot read
   their partner-negotiated/marked-up rate via any Azure API).
4. **Retail unreachable** (e.g. sovereign cloud, no egress) → CloudBolt
   `default_compute_rate` (admin-configured rates only).

A line that falls back to list price while negotiated data was expected is
labelled `… (list price)` so the basis is visible in the cost tooltip.

## Prerequisite: grant the handler service principal a billing role

Negotiated pricing reuses the **Resource Handler's existing service principal**,
but the Price Sheet APIs live at the *billing* scope (separate from subscription
RBAC). Grant the SPN one of:

- **EA:** `EnrollmentReader` on the enrollment (billing account), via the EA
  portal / Cost Management. Price Sheet is downloaded *by billing account*.
- **MCA:** `Billing profile reader` (or Owner/Contributor/Invoice Manager) on the
  billing profile. Price Sheet is downloaded *by billing profile*.
- **CSP / Partner Center:** not applicable — no negotiated rate is reachable from
  the customer side; these subscriptions price via retail automatically.

No new credentials are stored in CloudBolt or this repo; the SPN secret is read
from the handler at runtime.

## Operation

- `RJB-reblryol` runs daily. For each Azure handler it detects the agreement type
  (`GET /providers/Microsoft.Billing/billingAccounts`), and for EA/MCA/MPA
  downloads the Price Sheet (async: POST → poll → SAS blob → CSV/ZIP), parses it
  to `{meterId: unitPrice}`, and writes a cache file under
  `<PROSERV_DIR>/azure_pricing_cache/pricesheet_<subscriptionId>.json`.
- The rate hook reads that cache at order time (process-memoized by file mtime),
  so previews are fast and never trigger the slow async download inline.
- After granting billing roles, run the recurring job once manually to populate
  the cache rather than waiting for the schedule.

## Runtime / timeouts

The Azure Price Sheet API is an asynchronous bulk export — generation routinely
takes minutes — so the refresh job POSTs and then polls. Each handler's poll is
bounded by `PRICE_SHEET_MAX_WAIT` (default 420s) in `SHM-6gtujb8t`, with the poll
interval clamped to `PRICE_SHEET_POLL_INTERVAL_MAX` (30s) so completion/failure is
detected promptly. Deterministic failures fail fast *before* any polling: no
billing-role access, an unresolvable MCA billing profile, and any `4xx` on the
download POST all return immediately. Handlers are processed serially, so total
job time ≈ the sum of per-handler waits; lower `PRICE_SHEET_MAX_WAIT` if you have
many handlers and prefer the job to give up sooner (at the cost of falling back to
retail for an enrollment whose sheet legitimately takes longer to generate).

## Price sheet has many rows per meter

The EA/MCA price sheet lists **each `meterId` many times** — one row per `priceType`
(`Consumption`, `ReservedInstance`, `Savings Plan`) and per pricing tier
(`tierMinimumUnits`). The refresh job keeps **only base-tier `Consumption`** rows
(`tierMinimumUnits == 0`), one per meter, so cached prices are the on-demand rate.
Without this filter a last-write-wins cache stores an arbitrary row (e.g. a
reservation total), producing absurd per-VM costs. Reservation/Savings-Plan pricing
is out of scope for the on-demand preview.

## Caveats / verify against your tenant

These are intentional v1 simplifications or items to confirm with live data:

- **EA billing period:** the EA Price Sheet URI needs a billing-period name. The
  Web-Direct `billingPeriods` API doesn't apply to EA, so the code derives the
  current period as `YYYYMM`. If an EA download 404s, confirm your enrollment's
  period label format.
- **Region:** resolved from the environment's `node_location` and normalized to
  the ARM region (e.g. `East US` → `eastus`). Confirm your environments store a
  resolvable location.
- **RHEL/SLES vCPU bands:** the per-vCPU license band sku label (e.g.
  `1-4 vCPU VM`) and the `5+` band string should be verified against the live
  Retail/Price Sheet meters; vCPU count comes from the Compute Resource SKUs API.
- **OS disk** is priced from `os_disk_size_arm` only. **Size 0** means "keep the
  image/template disk" (don't resize), so it's priced at the image's template size
  (`AzureARMImage.total_disk_size` — for OEL8, 49 GiB). If the template size isn't
  recorded, it falls back to the marketplace **base default (30 GiB Linux / 127 GiB
  Windows)** rather than skipping the OS disk. Size and source are logged.
- **Data disks** come from CloudBolt's **`disk_size`** aggregate — but note CloudBolt
  **folds the template OS-disk size into `disk_size`**. So the data-disk total is
  `disk_size − AzureARMImage.total_disk_size`, which prevents double-counting the OS
  disk (priced separately from `os_disk_size_arm`). Priced as one `Data Disks` line
  at the selected `storage_account_type_arm`: **exact for per-GiB types** (Premium
  SSD v2, Ultra); **approximate for tiered** types (P/E/S), since per-disk tier
  granularity and count aren't recoverable from a sum. If the template size can't be
  determined, the subtraction is skipped (logged) and the OS disk may be
  double-counted. Fallbacks used only if `disk_size` is unset: flat `disk_<N>_size`
  CFVs, then repeating `pcvss` groups (field maps logged under `[azure_pricing]`).
- **Disks:** the OS disk (`os_disk_size_arm`) and each data disk are priced. All
  disks use the single `storage_account_type_arm` selection as their disk type
  (unless a data disk carries its own type). Supported pricing models:
  - **Premium SSD / Standard SSD / Standard HDD** (`Premium_LRS`/`_ZRS`,
    `StandardSSD_LRS`/`_ZRS`, `Standard_LRS`) — billed **per disk per month** by
    provisioned tier (P/E/S). The retail `skuName` includes the redundancy suffix
    (e.g. `P10 LRS`) and the base meter is `<sku> Disk`.
  - **Premium SSD v2** (`PremiumV2_LRS`) — billed **per provisioned GiB**; only the
    capacity component is estimated. Provisioned IOPS/throughput default to the
    free baseline (3000 IOPS / 125 MBps) and are not estimated (no order params).
  - **Ultra Disk** (`UltraSSD_LRS`) — billed per provisioned GiB **plus mandatory
    IOPS/throughput with no free baseline**. Only capacity is estimated, so the
    line is labelled **"(capacity only)"** to flag it as a floor; actual Ultra cost
    is higher once IOPS/throughput are provisioned (not available as order params).
  - Any other/unmapped type is logged and skipped.
  If your blueprints name data disks differently, adjust `_DATA_DISK_RE` /
  `_collect_inputs` in `OHK-vg0rmi7i`. Per-disk storage types (a
  `disk_<N>_storage_account_type` convention) are not read — all disks assume
  `storage_account_type_arm`.
- **Currency:** retail lookups default to **USD**. Wire CloudBolt's configured
  currency through if non-USD previews are needed.
- **Sovereign clouds:** the public Retail Prices API is commercial-cloud only, so
  US Gov / China / Germany handlers rely on negotiated data (no retail fallback).
- **Public IP / AHB:** detected from order CFVs whose names contain `public_ip`
  and `hybrid_benefit`/`ahb`. Adjust `_collect_inputs` in `OHK-vg0rmi7i` if your
  blueprints use different field names.

## Shared-module reload

`SHM-6gtujb8t` is cached in the running CloudBolt process. After a repo sync that
changes it, **restart CloudBolt** for the change to take effect.
