# AWS EKS

Creates an Amazon EKS cluster in an AWS environment the ordering group can use. The build plugin creates an IAM cluster role, calls `create_cluster` with the chosen Kubernetes version, two subnets, and a security group, waits for the cluster to become active, then registers it in CloudBolt as a Kubernetes container orchestrator with its own environment. Day-2 actions manage managed node groups and the control-plane version. Teardown removes the node groups, the cluster, its IAM role, and the orchestrator.

## Contents
| Role | ID | Name |
|---|---|---|
| Build | OHK-u4k6dg46 | Deploy EKS Cluster |
| Teardown | OHK-pgbigmd9 | Delete Cluster |
| Discovery | OHK-e9qppbei | Discover EKS |
| Day-2 action | RSA-f9ivvn5q | Add AWS EKS Nodegroup (hook OHK-bfhdr8vx) |
| Day-2 action | RSA-0g4pfrde | List AWS EKS Nodegroup (hook OHK-prji4iqt) |
| Day-2 action | RSA-rrbh1ntm | Delete AWS EKS Nodegroup (hook OHK-39mebtxi) |
| Day-2 action | RSA-dhgwoj3z | Update EKS Version (hook OHK-rvuo63r2) |
| Day-2 action | RSA-inhs50gi | Scale AWS EKS Nodegroup (hook OHK-x1984ik1) |
| Day-2 action | RSA-xdlxetyc | Update AWS EKS Add-on (hook OHK-f0775rga) |
| Day-2 action | RSA-p2fnjwj3 | Upgrade AWS EKS Nodegroup (hook OHK-el9giuz3) |
| Day-2 action | RSA-5c06txyi | Create AWS EKS Namespace (hook OHK-oguk4oad, shared module SHM-sa5yqm9j) |
| Options hook | HPA-qb0w86mi | Generate options for 'Expiration Date' (hook OHK-cfciy0fo) |
| UI extension | [XUI-8a1jocmr](../../extensions/XUI-8a1jocmr/) | AWS EKS Cluster tabs (Cluster, Node groups, Workloads, Groups) |

Order form: Environment, Cluster Name, Kubernetes Version, Security Groups, Subnet 1, Subnet 2 (all required), and optional Entitled CloudBolt groups and Expiration Date.

## Prerequisites
- An AWS resource handler with at least one environment that has a region and a VPC set. The subnet and security-group dropdowns read that environment's VPC, and the order form lists environments, not handlers.
- Handler credentials permitted to create, describe, update, and delete EKS clusters and node groups (including `eks:UpdateNodegroupVersion`), to describe cluster versions and updates, to list, describe, and update add-ons and describe add-on versions, and to describe Auto Scaling groups (node counts on the Node groups tab); create IAM roles, attach policies to them, and delete them; describe EC2 subnets, security groups, instance types, and instance type offerings; and `ssm:GetParameters` on the public `/aws/service/eks` and `/aws/service/bottlerocket` parameters (without it the AMI Type list falls back to Amazon Linux 2023 and Bottlerocket).
- Subnets in two different availability zones. Subnet 2 excludes whichever subnet was chosen as Subnet 1.

## Setup
1. Sync the repo. Nothing in this blueprint carries a `FILL-ME` placeholder or a secret.
2. Entitle the ordering groups to the AWS environments. `any_group_can_deploy` is true, so every group may order the blueprint itself.

Nothing version-specific is hardcoded: every list below is read from AWS when the form opens.

## Notes
- Kubernetes Version lists the versions EKS offers in the environment's region (`DescribeClusterVersions`, standard and extended support, newest first) and preselects the version AWS marks as default.
- Cluster creation takes 10 to 15 minutes; the job polls AWS once a minute. The cluster is created with a public API endpoint and with authentication mode `API_AND_CONFIG_MAP`, so access entries work alongside the `aws-auth` ConfigMap and the handler's identity is the cluster admin.
- The build creates a per-cluster IAM role named `<cluster>-<random>-Role` with `AmazonEKSClusterPolicy`. Add AWS EKS Nodegroup creates or reuses one shared `CustomAmazonEKSNodeRole` for every cluster. Teardown deletes the cluster role only when its name follows that pattern, so a discovered cluster's role is left alone, and it leaves the node role in place.
- Node groups are stored as `eks_nodegroup` sub-resources of the cluster. AWS allows 10 managed node groups per cluster and 100 nodes per node group; the form limits min, max, and desired nodes to 2 through 100 and disk size to 20 through 100 GiB. AMI Type offers the families AWS publishes an AMI for at the cluster's Kubernetes version (Amazon Linux 2023, Bottlerocket, and Amazon Linux 2 up to 1.32), checked through the SSM parameters AWS documents for AMI lookup. Instance Type is filtered to that architecture and to types offered in every availability zone the cluster's subnets span; both EC2 lookups are paginated.
- Delete AWS EKS Nodegroup lists the cluster's node group sub-resources with their last known status. Run List AWS EKS Nodegroup first if the sub-resources are stale; it also removes sub-resources for node groups AWS no longer has.
- Update EKS Version offers exactly the next minor version EKS makes available, because AWS updates a control plane one minor version at a time, waits for the update to finish, and records the new version on the resource. Node groups are not upgraded by it; run Upgrade AWS EKS Nodegroup on each one afterwards, because AWS requires their minor version to match the control plane before the next control-plane update.
- Upgrade AWS EKS Nodegroup offers one dropdown entry per node group and target: the cluster's Kubernetes version when the node group is behind, and the latest AMI release for the version it already runs. It calls `UpdateNodegroupVersion` with the version only, so AWS picks the latest AMI release for that version; EKS then replaces the nodes in batches, draining pods and honouring Pod disruption budgets unless Force Update is checked. The job waits for the update, reports the resulting Kubernetes and AMI release versions, and stores them on the sub-resource. Node groups with a custom AMI from a launch template are listed but cannot be updated this way, and the action never rolls a node group back.
- Scale AWS EKS Nodegroup lists the node groups AWS has (no sub-resource needed), changes min, desired, and max nodes with `UpdateNodegroupConfig`, waits for the update, and refreshes the sub-resource when one exists.
- Update AWS EKS Add-on offers one dropdown entry per add-on and newer version AWS publishes for the cluster's Kubernetes version (`DescribeAddonVersions`), with Preserve, Overwrite, or None for settings changed on the cluster, and waits for the update to finish.
- Create AWS EKS Namespace is the platform-admin path to a namespace landing zone: it takes a namespace name, size tier, and owning CloudBolt group, builds the namespace exactly as the AWS EKS Namespace blueprint ([BP-e4c0q58z](../BP-e4c0q58z/)) does, and files it as a resource of that blueprint owned by the chosen group, so the team gets the same pinned environment, day-2 actions, and teardown as if they had ordered it. The blueprint must be synced, and the action needs the blueprint's shared module, so restart the job engine after the first sync.
- The groups that may order AWS EKS Namespace landing zones on a cluster are the groups its per-cluster environment is available to. Entitled CloudBolt groups lists every group except Unassigned; when any are chosen, the build makes them the environment's only groups (dropping the Unassigned placeholder a restrict-new-environments appliance adds), so they and their subgroups can order namespaces. Left empty, the environment keeps CloudBolt's default: unconstrained (every group) or, with new environments restricted, Unassigned (no group). Change the groups later on the cluster's Groups tab.
- The AWS EKS Cluster UI extension ([XUI-8a1jocmr](../../extensions/XUI-8a1jocmr/)) adds Cluster, Node groups, Workloads, and Groups tabs to the resource and starts these actions from buttons.
- Teardown refuses while namespace landing zones (sub-resources from the AWS EKS Namespace blueprint) remain on the cluster, naming them and their groups; delete those first. It then deletes every node group, waits for the cluster to be gone, deletes the cluster's IAM role, then removes the CloudBolt orchestrator, the environment created for it, and the sub-resources. A resource with no stored AWS handler ID is removed from CloudBolt without touching AWS.
- Discovery returns every EKS cluster in every AWS environment, matched to resources by cluster ARN; CloudBolt's sync creates a resource in the Unassigned group for a new cluster, updates a tracked one, and reactivates a Historical one. Each cluster is registered once as a container orchestrator with its own environment. A handler whose credentials cannot list or describe EKS clusters is skipped, with an info line in the job log, and its tracked clusters are left as they are. It does not import node groups; run List AWS EKS Nodegroup on each discovered cluster.
- Expiration Date defaults to 7 days out. Nothing in this blueprint acts on it; the Expire Servers recurring job (`recurring_jobs/RJB-nsx4v2s1`) expires servers, not resources.
