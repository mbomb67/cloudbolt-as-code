# Node Size - Generate Options by OS Build Architecture

Generated Parameter Options plugin for the `node_size` parameter on Azure orders. It filters a customer-edited allow-list of VM sizes down to those matching the processor architecture (Arm64 or x64) of the selected OS Build's Azure image, so users cannot pick an Arm size for an x64 image or vice versa.

## Contents
| Role | ID | Name |
|---|---|---|
| Plugin | OHK-9csbq3zd | Node Size - Generate Options by OS Build Architecture |

## Prerequisites
- Azure (ARM) resource handler with OS Builds mapped to Azure images for each Environment. Architecture is read live through the handler's API wrapper, so the service principal must be able to read marketplace VM images (the same lookup CloudBolt uses when syncing images).
- The `node_size` custom field and the built-in `os_build` field on the provisioning order form.

## Setup
1. Edit `ALLOWED_NODE_SIZES` at the top of `OHK-9csbq3zd_script.py`. It ships with four example sizes per bucket (`arm`, `amd`, `x64`); replace them with the exact Azure VM size names you want to offer. The plugin never returns a size outside this list.
2. Attach the plugin to the parameter: Admin > Parameters > node_size > Options > Generated, select this plugin.
3. Add a REGENOPTIONS field dependency with controlling field `os_build` and dependent field `node_size` so the list regenerates when the OS Build changes. Without it the plugin receives no OS Build and always returns the full allow-list.

## Notes
- Arm64 image: `arm` bucket only. x64 image: `amd` and `x64` buckets merged, since AMD and Intel sizes both run x64 images.
- Fail-open: if no OS Build is selected, the Environment is unknown or the architecture lookup fails, the full allow-list is returned so orders stay orderable.
- Marketplace images are classified from Azure's `architecture` attribute. Custom or private images (no publisher/offer/sku) fall back to a name heuristic that looks for "arm64" or "aarch64" and otherwise assumes x64.
- Returns `override: True`, so sizes configured on the Environment for `node_size` are ignored in favour of this list; the first size in the result is the initial value.
- Image lookups are cached per (publisher, offer, sku, version, region) for the life of the call.
