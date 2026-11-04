# AWS Security Group

Creates, deletes and discovers AWS VPC security groups as CloudBolt Network resources, the same resource type as the [Azure Network Security Group](../BP-3fdhnw54/) blueprint. The companion extension [XUI-oj60n72c](../../extensions/XUI-oj60n72c/) adds a Security Rules tab to each security group resource for managing its inbound and outbound rules.

## Contents
| Role | ID | Name |
|---|---|---|
| Build | OHK-jb16kobi | AWS Security Group Build |
| Teardown | OHK-9y493oyz | AWS Security Group Teardown |
| Discovery | OHK-62awc6bl | AWS Security Group Sync |
| Extension | XUI-oj60n72c | AWS Security Group Management (Security Rules tab) |

## Prerequisites
- An AWS resource handler with at least one Environment the ordering group can use. The order form lists only AWS environments; the handler itself is never shown.
- The handler's IAM identity needs `ec2:CreateSecurityGroup`, `ec2:DeleteSecurityGroup`, `ec2:DescribeSecurityGroups`, `ec2:DescribeVpcs`, `ec2:DescribeNetworkInterfaces` and `ec2:CreateTags`.
- Custom fields `aws_rh_id`, `aws_region`, `aws_security_group_name`, `aws_security_group_id` and `aws_vpc_id` are created automatically by the first build.

## Setup
1. Sync the repository; the build, teardown and discovery plugins import with the blueprint. Sync the extension too, or import the [AWS Security Groups](../../packages/PKG-wik86fh7/) content package.
2. Optionally run discovery (Admin > Blueprints > AWS Security Group > Sync) to import existing security groups.

## Notes
- Order form: Environment, Security Group Name and Description. The security group is created in the Environment's VPC (`vpc_id`), or in the region's default VPC when the Environment names none, and gets a `Name` tag equal to its name. The description defaults to the name and cannot be changed later.
- Build fails if a security group with the same name already exists in the VPC; run discovery to adopt it instead.
- AWS gives a new security group no inbound rules and one outbound rule allowing all IPv4 traffic.
- Teardown fails with AWS's reason when the security group is still attached to a network interface or referenced by another security group's rule, naming the network interfaces; detach it and delete the resource again. A VPC's default security group cannot be deleted. A security group already gone returns WARNING.
- Discovery lists every security group in each region an AWS handler's environments use, across all VPCs, and keys resources on `aws_security_group_id` plus `aws_region`. It marks a tracked security group Historical only when its region was read, the group is missing, no job is running on it, and AWS answers that it does not exist; an unreachable handler or region retires nothing.
- Deleting a discovered resource deletes the security group in AWS.
- Rules are managed from the extension's tab, not by this blueprint.
