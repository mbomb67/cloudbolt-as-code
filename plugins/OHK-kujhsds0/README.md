# Node Size - Generate Options by Region, OS Image, Security, Networking and Storage

Generated Parameter Options plugin for the `node_size` parameter on Azure orders. It starts from the sizes an admin configured on `node_size` for the selected Environment (the same base set as CloudBolt's native `limit_azure_node_size_by_storage_type` hook) and removes any size Azure cannot deploy for the current order, using the live Azure Resource SKUs API instead of size-name patterns.

## Contents
| Role | ID | Name |
|---|---|---|
| Plugin | OHK-kujhsds0 | Node Size - Generate Options by Region, OS Image, Security, Networking and Storage (Azure SKU capabilities) |

## Prerequisites
- Azure (ARM) resource handler with `node_size` values configured per Environment. The Environment is the allow-list; nothing is hard-coded in the plugin.
- The handler's service principal needs `Microsoft.Compute/skus/read` and read access to the VM images your OS Builds use (marketplace, Compute Gallery or managed images).
- Optional global custom fields, each enabling one filter: `security_type_arm` (TrustedLaunch or ConfidentialVM), `availability_zone_arm` (1, 2 or 3), `enable_accelerated_networking` and `encryption_at_host` (BOOL), plus your storage-type field. Fields that are absent are simply not filtered on.

## Setup
1. Attach the plugin to the parameter: Admin > Parameters > node_size > Options > Generated, select this plugin.
2. Add REGENOPTIONS field dependencies with dependent field `node_size` and controlling fields `os_build`, your storage-type field, `security_type_arm`, `availability_zone_arm`, `enable_accelerated_networking` and `encryption_at_host`. Declaring all of them once is safe: controllers missing from a given form are ignored, and dependencies on fields that do not exist are dropped on import.
3. While troubleshooting, set `DEBUG_LOGGING = True` in the script; every decision is logged with an `azure_image` prefix.

## Notes
- Filters, each skipped when its data is unavailable: SKU offered and unrestricted in the region; CPU architecture and Hyper-V generation match the image; TrustedLaunch drops `TrustedLaunchDisabled` SKUs and ConfidentialVM keeps only confidential-capable SKUs; accelerated networking, encryption at host and availability zone require the matching SKU capability; Premium or Ultra storage requires `PremiumIO`.
- Security type comes from the `security_type_arm` parameter, not from the image; unset means Standard and imposes no constraint.
- Image data by type: marketplace (live lookup), Compute Gallery (definition architecture and generation), managed image (x64, real generation), VHD blob (x64, generation unknown). Arm64 is only possible via a Compute Gallery image.
- Not enforced: Ultra disk zonal availability beyond PremiumIO plus zone, ephemeral OS disk support, data-disk count and subscription quota.
- Returns `override: True` with the surviving sizes. SKU and image lookups are cached for the life of the call.
