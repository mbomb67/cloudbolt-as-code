# Azure Price Sheet Refresh

Recurring job that downloads each Azure subscription's negotiated Price Sheet (EA or MCA) with the resource handler's service principal and caches it on disk, keyed by meterId, for the Azure Resource Manager Rate Hook (HPA-t7hlyvyy) to read at order time. Price Sheet generation is an asynchronous bulk export that can take minutes, so it runs on a schedule rather than inline in the cost preview.

## Contents
| Role | ID | Name |
|---|---|---|
| Recurring job | RJB-reblryol | Azure Price Sheet Refresh (schedule `0 2 * * *`, daily at 02:00) |
| Plugin | OHK-bjgpsxoq | Azure Price Sheet Refresh |
| Shared module | SHM-6gtujb8t | azure_pricing |
| Consumer | HPA-t7hlyvyy | Azure Resource Manager Rate Hook |

## Prerequisites
- One or more Azure Resource Manager resource handlers.
- Handler service principal holds a billing-scope role: EA `EnrollmentReader` on the enrollment (sheet downloaded by billing account) or MCA/MPA `Billing profile reader` (downloaded by billing profile). CSP, MOSP and subscriptions without billing access are skipped; those price at retail.
- Outbound HTTPS to `management.azure.com` and to the Azure Storage SAS URL the download API returns.
- Write access to `<PROSERV_DIR>/azure_pricing_cache/` (PROSERV_DIR defaults to `/var/opt/cloudbolt/proserv/`).

## Setup
1. Read [../../docs/azure-negotiated-rate-setup.md](../../docs/azure-negotiated-rate-setup.md).
2. Restart CloudBolt after any sync that changes SHM-6gtujb8t; shared modules are cached in the running process.
3. After granting the billing role, run the job once manually to populate the cache instead of waiting for the schedule.
5. Adjust the schedule if needed; price sheets change roughly monthly, so daily is ample.

## Notes
- Writes one file per subscription, `pricesheet_<subscriptionId>.json`. Only base-tier `Consumption` rows (`tierMinimumUnits == 0`) are kept, so cached prices are the on-demand rate; reservation and savings-plan rows are discarded.
- Handlers are processed serially; each poll is bounded by `PRICE_SHEET_MAX_WAIT` (default 420 s) in SHM-6gtujb8t. A missing billing role, an unresolvable MCA billing profile, or a 4xx on the download POST fail fast without polling.
- EA downloads need a billing-period name, derived as `YYYYMM`. If an EA download returns 404, check your enrollment's period label format.
- `allow_parallel_jobs` is false, so an overlapping run is not started while one is in progress.
