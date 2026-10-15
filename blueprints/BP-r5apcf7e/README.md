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
| Options hook | HPA-qb0w86mi | Generate options for 'Expiration Date' (hook OHK-cfciy0fo) |

Order form: Environment, Cluster Name, Kubernetes Version, Security Groups, Subnet 1, Subnet 2 (all required) and an optional Expiration Date.

## Prerequisites
- An AWS resource handler with at least one environment that has a region and a VPC set. The subnet and security-group dropdowns read that environment's VPC, and the order form lists environments, not handlers.
- Handler credentials permitted to create, describe, update, and delete EKS clusters and node groups; create IAM roles, attach policies to them, and delete them; and describe EC2 subnets, security groups, instance types, and instance type offerings.
- Subnets in two different availability zones. Subnet 2 excludes whichever subnet was chosen as Subnet 1.

## Setup
1. Sync the repo. Nothing in this blueprint carries a `FILL-ME` placeholder or a secret.
2. Entitle the ordering groups to the blueprint and to the AWS environments; `any_group_can_deploy` is false.
3. To change the Kubernetes versions offered, edit `generate_options_for_k8s_version` in `plugins/OHK-u4k6dg46/OHK-u4k6dg46_script.py` and `generate_options_for_kubernetes_version` in `plugins/OHK-rvuo63r2/OHK-rvuo63r2_script.py`. Both lists are hardcoded and ship 1.31 through 1.34; the order form defaults to 1.33.

## Notes
- Cluster creation takes 10 to 15 minutes; the job polls AWS once a minute. The cluster is created with a public API endpoint.
- The build creates a per-cluster IAM role named `<cluster>-<random>-Role` with `AmazonEKSClusterPolicy`. Add AWS EKS Nodegroup creates or reuses one shared `CustomAmazonEKSNodeRole` for every cluster. Teardown deletes the cluster role and leaves the node role in place.
- Node groups are stored as `eks_nodegroup` sub-resources of the cluster. AWS allows 10 managed node groups per cluster and 100 nodes per node group; the form limits min, max, and desired nodes to 2 through 100 and disk size to 20 through 100 GiB. AMI Type offers Amazon Linux 2 x86_64 or Arm, and the instance-type list is filtered to that architecture and to types offered in both of the cluster's availability zones.
- Delete AWS EKS Nodegroup asks for a node group status first (ACTIVE, UPDATING, CREATE_FAILED, DELETE_FAILED, DEGRADED) and then lists the sub-resources in that status. Run List AWS EKS Nodegroup first if the sub-resources are stale; it also removes sub-resources for node groups AWS no longer has.
- Update EKS Version upgrades the control plane only, one minor version at a time as AWS requires. Node groups are not upgraded.
- Teardown deletes every node group, then the cluster and its IAM role, then the CloudBolt orchestrator and the sub-resources. A resource with no stored AWS handler ID is removed from CloudBolt without touching AWS.
- Discovery scans every AWS environment, creates a resource for each cluster CloudBolt does not already track by name, assigns it to the first group whose name contains `unassigned`, and registers it as a container orchestrator. It does not import node groups; run List AWS EKS Nodegroup on each discovered cluster.
- Expiration Date defaults to 7 days out. Nothing in this blueprint acts on it; the Expire Servers recurring job (`recurring_jobs/RJB-nsx4v2s1`) expires servers, not resources.
