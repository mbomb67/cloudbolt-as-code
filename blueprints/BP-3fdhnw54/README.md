# Azure Network Security Group

Creates, deletes and discovers Azure Network Security Groups (NSGs) as CloudBolt Network resources. The companion extension [XUI-0jdk7t6h](../../extensions/XUI-0jdk7t6h/) adds a Security Rules tab to each NSG resource, and the optional Post-Provision action [HPA-vo2ghe4x](../../orchestration_actions/HPA-vo2ghe4x/) attaches newly provisioned Azure VMs to an NSG chosen on the order form.

## Contents
| Role | ID | Name |
|---|---|---|
| Build | OHK-7987st2p | Azure Network Security Group Build |
| Teardown | OHK-9jouejv8 | Network Security Group Teardown |
| Discovery | OHK-kdne1t5s | Azure Network Security Group Sync |
| Extension | XUI-0jdk7t6h | Azure NSG Management (Security Rules tab) |
| Orchestration action | HPA-vo2ghe4x | Azure NSG - Attach VM to NSG (optional, ships disabled) |
| Plugin | OHK-vswf8b1m | Azure NSG - Generate Options (populates the `azure_nsg` parameter used by HPA-vo2ghe4x) |

## Prerequisites
- An Azure (ARM) resource handler with at least one Environment the ordering group can use. The order form lists only Azure environments; the handler itself is never shown.
- The handler's service principal needs read/write/delete on `Microsoft.Network/networkSecurityGroups` in the target resource groups (Network Contributor is sufficient) and read on resource groups for discovery.
- The Resource Group dropdown is populated from the `resource_group_arm` parameter options synced onto the selected Environment; if none are synced the list is empty.
- Custom fields `azure_rh_id`, `azure_network_security_group`, `azure_network_security_group_id`, `azure_location` and `resource_group_name` are created automatically on first run.

## Setup
1. Sync the repository; the build, teardown and discovery plugins and the extension import with the blueprint.
2. On each Azure Environment users will order into, sync the Resource Group (`resource_group_arm`) parameter options (Environment > Parameters).
3. Optionally run discovery (Admin > Blueprints > Azure Network Security Group > Sync) to import existing NSGs from every Azure handler.
4. To attach VMs to an NSG at provision time, follow the setup in [HPA-vo2ghe4x](../../orchestration_actions/HPA-vo2ghe4x/README.md).

## Notes
- Order form: Environment, Resource Group (regenerated when the Environment changes) and NSG name. The NSG is created in the Environment's region (`node_location`).
- Build fails if an NSG with the same name already exists in the resource group; run discovery to adopt it instead.
- Teardown starts the Azure delete and returns SUCCESS without waiting for it to finish, so confirm in Azure before relying on the NSG being gone. If the resource has no `azure_network_security_group_id`, teardown returns SUCCESS without calling Azure.
- Discovery walks every resource group of every Azure handler and keys resources on `azure_network_security_group_id` plus `azure_location`.
- Security rules are managed from the extension's tab, not by this blueprint; edits there write directly to Azure with no CloudBolt job record.
