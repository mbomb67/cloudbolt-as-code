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
- Custom fields `azure_rh_id`, `azure_network_security_group`, `azure_network_security_group_id`, `azure_location` and `resource_group_name` are created automatically by the first build or discovery.

## Setup
1. Sync the repository; the build, teardown and discovery plugins and the extension import with the blueprint.
2. On each Azure Environment users will order into, sync the Resource Group (`resource_group_arm`) parameter options (Environment > Parameters).
3. Optionally run discovery (Admin > Blueprints > Azure Network Security Group > Sync) to import existing NSGs from every Azure handler.
4. To attach VMs to an NSG at provision time, follow the setup in [HPA-vo2ghe4x](../../orchestration_actions/HPA-vo2ghe4x/README.md).

## Notes
- Order form: Environment, Resource Group (regenerated when the Environment changes) and NSG name. The NSG is created in the Environment's region (`node_location`). Names must follow Azure's rules (1-80 letters, digits, underscores, periods and hyphens), or the build fails before calling Azure.
- Build fails if an NSG with the same name already exists in the resource group; run discovery to adopt it instead.
- Teardown waits for Azure to delete the NSG. Azure refuses to delete an NSG still associated with a subnet or network interface, and the teardown then fails with Azure's message; dissociate it and delete the resource again. An NSG already gone returns WARNING. If the resource has no `azure_network_security_group_id`, teardown returns SUCCESS without calling Azure.
- Discovery lists every NSG in each Azure handler's subscription and keys resources on `azure_network_security_group_id` plus `azure_location`. It marks a tracked NSG Historical only when its handler was read, the NSG is missing, no job is running on it, and Azure answers that it does not exist; an unreachable handler retires nothing.
- Security rules are managed from the extension's tab, not by this blueprint; edits there write directly to Azure and are recorded in the resource's History.
