# Azure NSG Management (XUI)

Adds a **Security Rules** tab to Azure Network Security Group resources. The tab shows Inbound and Outbound rule tables (Priority, Name, Port, Protocol, Source, Destination, Action) read live from Azure, with Add, Edit and Delete dialogs for custom rules; Azure default rules are shown read-only. Companion to the Azure Network Security Group blueprint ([BP-3fdhnw54](../../blueprints/BP-3fdhnw54/)).

## Prerequisites
- CloudBolt 8.6 or later.
- NSG resources created by the companion blueprint (build plugin OHK-7987st2p or discovery plugin OHK-kdne1t5s). The tab only appears on resources that carry the `azure_network_security_group_id` and `azure_rh_id` custom fields.
- The Azure ARM resource handler's service principal must be able to read and write NSG security rules (`Microsoft.Network/networkSecurityGroups/securityRules/*`).

## Setup
1. Sync the repository and confirm the extension is enabled under Admin > Extensions. No further configuration is required.

## Notes
- Add/Edit/Delete write directly to Azure through the `azure.mgmt.network` SDK; there is no CloudBolt-side approval or job record.
- Rule tables are fetched from Azure on every tab load.

See [package readme](azure_nsg_management/readme.md) for details.
