# Azure Resource Group

Creates, deletes and inventories Azure Resource Groups as CloudBolt resources, with day-2 actions for tags, delete locks and contents. The order form asks only for Environment, group name and optional extra tags; region and subscription come from the Environment, and governance tags are generated from the resource handler's Taggable Attributes by a shared first build step.

## Contents
| Role | ID | Name |
|---|---|---|
| Build (runs first, hidden on order form) | OHK-xoajww7v | Generate Tags from Resource Handler Tag Map (shared) |
| Build | OHK-7c5xywbx | Azure Resource Group |
| Teardown | OHK-4xqzbdtx | Teardown Azure Resource Group |
| Discovery | OHK-e5a4m2bm | Discover Azure Resource Groups |
| Day-2 action | RSA-qwku9lip | Manage Delete Lock (OHK-sf6w5pfn) |
| Day-2 action | RSA-a38h23ms | List Resources in Group (OHK-iy92hhvh) |
| Day-2 action | RSA-kbikieh7 | Update Tags (OHK-nwcyoto7) |
| Shared module | SHM-i1oshqxg | azure_management_locks (REST helpers used by the lock action, teardown and discovery) |

## Prerequisites
- An Azure (ARM) resource handler with at least one Environment the ordering group can use (entitled or unconstrained environments are listed).
- The handler's service principal needs read/write/delete on `Microsoft.Resources/subscriptions/resourceGroups` and `Microsoft.Resources/subscriptions/resources/read`. Manage Delete Lock additionally needs `Microsoft.Authorization/locks/*`, which among built-in roles only Owner and User Access Administrator grant.
- For generated tags: Taggable Attributes on the Azure handler (Admin > Resource Handlers > handler > Tags), each mapping a CloudBolt parameter to a cloud tag name.

## Setup
1. Sync the repository; both build steps, teardown, discovery and the three resource actions import with the blueprint.
2. Configure Taggable Attributes on the Azure handler for the parameters you want stamped as tags (Group, Owner, Environment or any custom field set on the order). Without a tag map, step 1 returns WARNING and only tags typed in Additional Tags are applied.
3. Optionally run discovery to adopt existing resource groups, including their tags and lock state.

## Notes
- Step 1 reads values from the Resource's parameters, then arguments captured on the order, then Environment and Group parameter defaults, and stores the result as JSON in the `cb_generated_tags` custom field. It has `continue_on_failure` set, so the group is still created if tag generation fails. Generated tags win over Additional Tags on a case-insensitive name conflict, and Azure tag limits are enforced.
- OHK-xoajww7v declares no action inputs and is shared; it can be reused as the first build item of any blueprint whose build plugin reads `cb_generated_tags`.
- Teardown deletes everything in the group. It refuses to run while a CanNotDelete or ReadOnly lock exists (release it with Manage Delete Lock first), logs the group's contents before deleting, and returns WARNING when the group is already gone.
- Manage Delete Lock only creates or removes the lock named `cloudbolt-lock`; locks created elsewhere are reported but left in place.
- Lock operations call the Azure Management Locks REST API (api-version 2016-09-01) through the shared module; the `azure.mgmt.resource.locks` SDK client is not required on the appliance.
- Update Tags: merge adds or overwrites the given keys and keeps the rest; replace makes them the complete set.
