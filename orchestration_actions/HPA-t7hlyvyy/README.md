# Azure Resource Manager Rate Hook

Binds the Azure VM cost-estimation plugin to the "Compute Server Rate" hook point so order and blueprint cost previews for Azure servers use the customer's negotiated prices (EA/MCA Price Sheet, cached by the Azure Price Sheet Refresh recurring job) and fall back to public Retail Prices otherwise. Prices compute, OS disk, data disks, public IP and the separate RHEL/SLES license meter.

## Contents
| Role | ID | Name |
|---|---|---|
| Orchestration action | HPA-t7hlyvyy | Azure Resource Manager Rate Hook (Compute Server Rate, run_seq 5) |
| Plugin | OHK-vg0rmi7i | Azure Resource Manager Rate Hook |
| Shared module | SHM-6gtujb8t | azure_pricing |
| Companion job | RJB-reblryol | Azure Price Sheet Refresh |

## Prerequisites
- Azure Resource Manager resource handler; the plugin is filtered to `resource_technologies: ["Azure"]`.
- Outbound HTTPS from the appliance to `prices.azure.com` (retail fallback) and `management.azure.com`.
- For negotiated prices: the handler service principal holds a billing-scope role (EA `EnrollmentReader` on the enrollment, or MCA `Billing profile reader`) and RJB-reblryol has run at least once. CSP/Partner subscriptions always price at retail.
- Environments store a resolvable `node_location` (normalized to an ARM region such as `eastus`).

## Setup
1. Read [../../docs/azure-negotiated-rate-setup.md](../../docs/azure-negotiated-rate-setup.md).
2. Restart CloudBolt after any sync that changes SHM-6gtujb8t; shared modules are cached in the running process.
3. Set up RJB-reblryol and run it once manually to populate the negotiated-price cache.
5. If your blueprints use different field names for public IP (`public_ip`), Azure Hybrid Benefit (`hybrid_benefit`/`ahb`) or data disks, adjust `_collect_inputs` and `_DATA_DISK_RE` in `plugins/OHK-vg0rmi7i/OHK-vg0rmi7i_script.py`.

## Notes
- Fallback ladder per meter: negotiated sheet, then Retail Prices API, then CloudBolt `default_compute_rate`. A line priced at list when negotiated data was expected is labelled "(list price)" in the cost tooltip.
- Keeps `default_compute_rate`'s admin-configured Software > Applications and Extra rates; drops the default Software > OS Build line so an Azure-sourced OS license is not double-counted.
- Estimates only: Ultra Disk lines are "(capacity only)"; tiered disks (P/E/S) are approximated from the disk-size sum; Premium SSD v2 IOPS/throughput are not priced; retail lookups are USD.
- Sovereign clouds (US Gov, China, Germany) have no Retail Prices API; they need negotiated data or fall back to `default_compute_rate`.
