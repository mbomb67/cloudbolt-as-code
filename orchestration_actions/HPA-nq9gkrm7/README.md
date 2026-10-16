# List available azure node sizes based on storage type

Replaces the code of CloudBolt's built-in Generated Parameter Options action of the same name, which supplies the `node_size` dropdown on Azure orders. The built-in code narrows the environment's configured sizes with a name regex. This version keeps only the sizes Azure can deploy for the order's region, subscription, OS image (architecture and Hyper-V generation), storage type, security type, availability zone, accelerated networking and encryption at host. It reads those capabilities from the Azure Resource SKUs API.

## Contents
| Role | ID | Name |
|---|---|---|
| Plugin | OHK-mhul31ct | List available azure node sizes based on storage type |

## How it takes over the built-in action
The importer matches CloudBolt's existing action and plugin by name and updates them in place. The `node_size` parameter keeps its link to the plugin, so nothing needs re-wiring after a sync. Upgrades leave the synced code alone, because CloudBolt only refreshes built-in plugin code that was never changed.

## Caching
A region's SKU list is about 2.7 MB and takes 3–4 s to fetch. CloudBolt runs this plugin once per Azure environment on every custom-form validate and submit, so the plugin caches each region's SKU capabilities and each image's requirements in CloudBolt's cache (memcached) for 6 hours. Failed lookups are not cached. To pick up a SKU change sooner, bump `CACHE_VERSION` in the plugin, or lower `SKU_CACHE_SECONDS`.

## Setup
1. Sync `orchestration_actions/HPA-nq9gkrm7`.
2. If the action has a Source Code URL (Admin > Actions, edit form), clear it and sync again. A Source Code URL overrides synced code.
3. Optional: wire `os_build`, the storage type parameter, `security_type_arm`, `availability_zone_arm`, `enable_accelerated_networking` and `encryption_at_host` as Regenerate Options controllers of `node_size`. See the setup notes at the end of the plugin.

## Notes
- Any capability the plugin cannot read is not filtered on, so orders stay orderable.
- Set `DEBUG_LOGGING = True` in the plugin to log every decision with the prefix `azure_image`.
