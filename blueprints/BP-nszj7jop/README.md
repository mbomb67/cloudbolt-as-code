# Azure Storage Account

Provisions, deletes and inventories Azure Storage Accounts as CloudBolt resources, with day-2 actions for blob containers, access tier and replication SKU. Users pick an Environment; region and subscription come from it and the resource handler is never shown on the order form.

## Contents
| Role | ID | Name |
|---|---|---|
| Build | OHK-qev70tpa | Azure Storage Account |
| Teardown | OHK-8px9e3ws | Teardown Azure Storage Account |
| Discovery | OHK-h9lmvlkb | Discover Azure Storage Accounts |
| Day-2 action | RSA-xzs5f3a2 | Create Blob Container (OHK-l25x6wd8) |
| Day-2 action | RSA-h93k53ld | List Blob Containers (OHK-3kp5czyb) |
| Day-2 action | RSA-zmi15uts | Delete Blob Container (OHK-y1120h61) |
| Day-2 action | RSA-0ldsokuc | Change Access Tier (OHK-stdxttnw) |
| Day-2 action | RSA-gr2wsfzx | Change SKU (OHK-fel441xh) |

## Prerequisites
- An Azure (ARM) resource handler with at least one Environment the ordering group can use. The Environment dropdown lists Azure environments entitled to the group plus unconstrained ones.
- The handler's service principal needs read/write/delete on `Microsoft.Storage/storageAccounts` and on `blobServices/containers` in the target resource groups (Storage Account Contributor covers this), plus `Microsoft.Resources/subscriptions/resourceGroups/read` to populate the Resource Group dropdown.
- The `azure-mgmt-storage` SDK on the CloudBolt server; build returns FAILURE if it is missing.

## Setup
1. Sync the repository; the build, teardown and discovery plugins and the five resource actions import with the blueprint.
2. Optionally run discovery (Admin > Blueprints > Azure Storage Account > Sync) to adopt existing storage accounts from every Azure handler.

## Notes
- Order form: Environment, Resource Group (listed live from the subscription), Storage Account Name (3-24 lowercase letters and digits, globally unique), Account Kind (default StorageV2), Access Tier (Hot/Cool, StorageV2 and BlobStorage only) and SKU (default Standard_LRS; Standard LRS/ZRS/GRS/RA-GRS, Premium LRS/ZRS).
- Build checks name availability first and fails with Azure's reason if the name is taken. Metadata is stored in `azure_storage_account_*` custom fields; discovery keys on `azure_storage_account_id`.
- Teardown is idempotent: it returns WARNING (not FAILURE) when the account, its metadata or its handler is already gone.
- Container actions use the management plane, so no account keys are needed. Delete Blob Container permanently removes every blob in the container and returns WARNING if the container is already gone. A Public Access Level other than `none` on Create Blob Container requires the account to allow public access.
- Change SKU passes Azure's rejection through as FAILURE for unsupported conversions (for example Standard to Premium). Change Access Tier applies only to StorageV2 and BlobStorage accounts.
