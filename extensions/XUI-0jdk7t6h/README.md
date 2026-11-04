# Azure NSG Management (XUI)

Adds a **Security Rules** tab to Azure Network Security Group resources of the companion blueprint ([BP-3fdhnw54](../../blueprints/BP-3fdhnw54/)). The tab follows the Azure portal's NSG Overview: essentials (resource group, location, subscription, provisioning state, custom rule counts, associations, tags), the subnets and network interfaces the NSG is associated with, and Inbound and Outbound rule tables sorted by priority (Priority, Name, Port, Protocol, Source, Destination, Action), each with a filter and an Add rule button. Custom rules have Edit and Delete; Azure default rules are read-only.

## Prerequisites
- A CloudBolt release with the Bootstrap 5 UI.
- NSG resources created by the companion blueprint's build or discovery plugin. The tab only appears on resources that carry the `azure_network_security_group_id` custom field.
- The Azure resource handler's service principal must be able to read NSGs and write their security rules (`Microsoft.Network/networkSecurityGroups/securityRules/*`; Network Contributor is sufficient).

## Setup
1. Sync the repository and confirm the extension is enabled under Admin > UI Extensions.
2. Restart the web server after the first sync of this version (`systemctl restart httpd`), because it adds a URL route.

## Notes
- Viewing the tab needs the View Resources permission on the resource; Add, Edit and Delete need Change Resource Attributes.
- Changes are written to Azure as soon as a dialog is saved, with no approval or job, and each one is recorded in the resource's History.
- Add refuses a name or a priority that another rule already uses. Rules that use application security groups can be deleted here but are edited in the Azure portal.
- Rules are read from Azure each time the tab opens or Refresh is clicked.
