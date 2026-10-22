# Azure Network Security Groups

Orders, discovers, and deletes Azure network security groups, adds a Security Rules tab for viewing and editing their rules, and can attach new Azure VMs to a chosen network security group at provisioning.

Content package `PKG-gqw2jqgj`, version 1.0.0. Sync it from the Content Packages admin page (Content Package Manager UI extension).

| Type | Name | Folder |
|---|---|---|
| Plugin | Azure NSG - Generate Options | [plugins/OHK-vswf8b1m](../../plugins/OHK-vswf8b1m/) |
| UI extension | Azure NSG Management | [extensions/XUI-0jdk7t6h](../../extensions/XUI-0jdk7t6h/) |
| Orchestration action | Azure NSG - Attach VM to NSG | [orchestration_actions/HPA-vo2ghe4x](../../orchestration_actions/HPA-vo2ghe4x/) |
| Blueprint | Azure Network Security Group | [blueprints/BP-3fdhnw54](../../blueprints/BP-3fdhnw54/) |

## After the first sync
1. Restart the web server (`systemctl restart httpd`): the UI extension adds URL paths.
2. The Attach VM to NSG action syncs disabled. To use it, set up the `azure_nsg` parameter and enable the action as its [README](../../orchestration_actions/HPA-vo2ghe4x/README.md) describes; the parameter does not travel with the package.
