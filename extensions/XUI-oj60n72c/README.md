# AWS Security Group Management (XUI)

Adds a **Security Rules** tab to AWS security group resources of the companion blueprint ([BP-ofwowhqd](../../blueprints/BP-ofwowhqd/)). The tab follows the AWS console's security group page: details (name, ID, description, VPC, owner, region, rule counts, tags), Inbound and Outbound rule tables (Name, Security group rule ID, IP version, Type, Protocol, Port range, Source or Destination, Description), each with a filter and an Add rule button, and the network interfaces the group is attached to. Every rule has Edit and Delete.

The rule dialog works like the console's Edit inbound rules page: choosing a Type (SSH, HTTPS, RDP, All traffic, ...) fills and locks the protocol and port range, Custom TCP and Custom UDP take a port or range, Custom ICMP takes a type and code, and Anywhere-IPv4 or Anywhere-IPv6 fills the source. The Source or Destination field suggests the security groups in the VPC and the region's prefix lists, and Add accepts several sources separated by commas, one rule each.

## Prerequisites
- A CloudBolt release with the Bootstrap 5 UI.
- Security group resources created by the companion blueprint's build or discovery plugin. The tab only appears on resources that carry the `aws_security_group_id` custom field.
- The AWS resource handler's IAM identity needs `ec2:DescribeSecurityGroups`, `ec2:DescribeSecurityGroupRules`, `ec2:DescribeNetworkInterfaces`, `ec2:DescribeManagedPrefixLists`, `ec2:AuthorizeSecurityGroupIngress`, `ec2:AuthorizeSecurityGroupEgress`, `ec2:ModifySecurityGroupRules`, `ec2:RevokeSecurityGroupIngress`, `ec2:RevokeSecurityGroupEgress`, `ec2:CreateTags` and `ec2:DeleteTags`.

## Setup
1. Sync the repository and confirm the extension is enabled under Admin > UI Extensions.
2. Restart the web server after the first sync of this version (`systemctl restart httpd`), because it adds URL routes.

## Notes
- Viewing the tab needs the View Resources permission on the resource; Add, Edit and Delete need Change Resource Attributes.
- Changes are written to AWS as soon as a dialog is saved, with no approval or job, and each one is recorded in the resource's History.
- AWS cannot change what kind of source a rule has (a CIDR block, a security group or a prefix list). Editing a rule to another kind adds a new rule and deletes the old one, so its rule ID changes.
- Rules that reference a security group in another account or through a VPC peering connection can be deleted here but are edited in the AWS console.
- Rules are read from AWS each time the tab opens or Refresh is clicked.
