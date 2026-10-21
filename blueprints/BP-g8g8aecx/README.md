# Google Kubernetes Engine

Creates a GKE Standard cluster in a GCP environment the ordering group can use. The cluster follows Google's hardening guidance for a shared cluster: a release channel that upgrades the control plane and nodes, VPC-native networking, GKE Dataplane V2 (which enforces Kubernetes network policies), Workload Identity Federation for GKE, Shielded GKE nodes on Container-Optimized OS with auto-upgrade and auto-repair, and no legacy ABAC or client certificate. The build registers the cluster in CloudBolt as a Kubernetes container orchestrator with its own environment and records each node pool as a GKE Node Pool sub-resource. Day-2 actions manage node pools, the cluster's Kubernetes version, namespace landing zones, and the CloudBolt groups that may order namespaces on the cluster.

## Contents
| Role | ID | Name |
|---|---|---|
| Build | OHK-shr5y3i6 | GKE Cluster Build |
| Teardown | OHK-y9wx9bga | GKE Cluster Teardown |
| Discovery | OHK-6l86xugc | GKE Cluster Discovery |
| Shared module | SHM-4yw92b8x | gcp_gke |
| Day-2 action | RSA-hg13ra5x | Add GKE Node Pool (hook OHK-aaltlvmb) |
| Day-2 action | RSA-afixcgmf | List GKE Node Pools (hook OHK-niw4hc9m) |
| Day-2 action | RSA-vz9sbjto | Scale GKE Node Pool (hook OHK-m0s3j1ax) |
| Day-2 action | RSA-gpa7ywnn | Upgrade GKE Node Pool (hook OHK-2rpjqm7u) |
| Day-2 action | RSA-kfd31bur | Delete GKE Node Pool (hook OHK-qxdrryt6) |
| Day-2 action | RSA-wlihlt2z | Upgrade GKE Version (hook OHK-91bx99mg) |
| Day-2 action | RSA-yrnl5j6j | Create GKE Namespace (hook OHK-cco2r73h, shared module SHM-kephq8iq) |
| Day-2 action | RSA-gqwszlzf | Manage GKE Cluster Groups (hook OHK-m8iwylq8) |
| Options hook | HPA-qb0w86mi | Generate options for 'Expiration Date' (hook OHK-cfciy0fo) |
| UI extension | [XUI-8egdz3e9](../../extensions/XUI-8egdz3e9/) | Google GKE Cluster tabs (Cluster, Node pools, Workloads, Groups) |

Order form: Cluster Name, Environment, Region, Availability, Subnetwork, Release Channel, Kubernetes Version, Machine Type, Nodes per Zone (required), and Autoscale Maximum, Private Nodes, Cluster Admin Principals, RBAC Security Group, Entitled CloudBolt Groups, Expiration Date (optional).

## Prerequisites
- A GCP resource handler with at least one environment whose zone (`gcp_zone`) and machine type (`gcp_node_size`) options are imported. Region lists the regions of the environment's zones, Machine Type its x86 general-purpose and compute-optimized machine types (accelerator, memory-optimized, Arm, and shared-core types are left out). The order form lists environments, not handlers.
- The handler's identity needs Kubernetes Engine Admin (`roles/container.admin`) on the environment's project: it creates clusters and node pools, and creates the cluster-admin binding for CloudBolt's ServiceAccount inside each cluster. It also needs `compute.subnetworks.list` and `compute.subnetworks.use` on the subnetwork's project (a Shared VPC host project additionally needs the roles GKE documents for Shared VPC), and `iam.serviceAccounts.actAs` on the node service account (the Compute Engine default service account).
- The appliance reaches the cluster's control plane endpoint over HTTPS.

## Setup
1. Sync the repo, then restart CloudBolt (the job engine and the web server): every plugin here imports the `gcp_gke` shared module, which running processes cache.
2. Entitle the ordering groups to the GCP environments. `any_group_can_deploy` is true, so every group may order the blueprint itself.
3. Choose who may order namespaces on a cluster with Entitled CloudBolt Groups on the order form, or later on the cluster's Groups tab (Manage GKE Cluster Groups); no environment administrator is needed. Both change the groups of the cluster's `Resource-<cluster> Environment#<location>` environment. On an appliance that restricts new environments, that environment starts in the Unassigned group, and no group sees the cluster until groups are entitled or all groups are allowed.

## Notes
- A regional cluster runs the control plane and nodes in several zones of the region; a zonal cluster runs in the first of the region's zones the environment offers. Node counts are per zone, so a regional cluster's default pool runs Nodes per Zone in each of its zones. When Autoscale Maximum is greater, the cluster autoscaler runs between the two per zone.
- Kubernetes Version lists the release channel's valid versions in the region, newest first, after the channel default, which is preselected.
- The build creates the node pool `default-pool` on Container-Optimized OS with containerd and the cloud-platform scope; IAM roles on the node service account decide what nodes may do. Private Nodes gives nodes internal addresses only, so they need Cloud NAT to reach the internet.
- CloudBolt reaches the API server as the `cloudbolt-admin` ServiceAccount in `kube-system`, bound to cluster-admin: the build creates it with the handler's Google credentials, and the container orchestrator holds the token of its `cloudbolt-admin-token` Secret, which does not expire. A GKE credential rotation (which GKE also starts itself before the cluster CA expires) invalidates the token and changes the CA, and deleting the ServiceAccount or Secret breaks it too; the next discovery or List GKE Node Pools replaces the token and CA. Google recommends short-lived IAM credentials for automation, but CloudBolt's Kubernetes orchestrator refreshes tokens only for EKS. This content verifies TLS against the cluster's own CA. CloudBolt's own Kubernetes features (the orchestrator's pages, the Container Object item) verify TLS with the appliance trust store, so with SSL verification on, add the cluster CA under Admin > SSL Certificates.
- Cluster Admin Principals are bound to cluster-admin through the `cloudbolt-cluster-admins` ClusterRoleBinding: `user:EMAIL` for a Google account, `serviceAccount:EMAIL` for a Google service account, and `group:EMAIL` for a Google Group, which works only with Google Groups for RBAC (RBAC Security Group, `gke-security-groups@YOUR-DOMAIN`). People still need `container.clusters.get` on the project (Kubernetes Engine Cluster Viewer) to fetch credentials with gcloud.
- Creation takes several minutes; the job follows the operation GKE returns.
- Subnetwork lists the subnetworks the handler may use in the region, Shared VPC subnetworks included. In its own project GKE creates the secondary ranges for Pods and Services; a Shared VPC subnetwork must already have them, and the build picks an unused range for Services (or one other clusters share) and an unused or shareable range for Pods, failing when there is none.
- Node pools are GKE Node Pool sub-resources of the cluster. Every node pool action reads the pools from GKE, so stale sub-resources never block an action; List GKE Node Pools refreshes the sub-resources (node counts come from the pools' managed instance groups) and the cluster's details, and registers the orchestrator when the cluster has none. Deleting a node pool sub-resource in CloudBolt only removes a record whose pool GKE no longer has; delete a live pool with Delete GKE Node Pool, which keeps the cluster's last pool. Scale GKE Node Pool keeps at least one node per zone in the last pool, and sets per-zone autoscaling limits even on a pool that had whole-pool limits.
- Upgrade GKE Version offers the versions the release channel accepts above the cluster's version, one minor version at a time. "Control plane and all node pools" upgrades the control plane, then each node pool behind it; "Control plane only" leaves the pools for Upgrade GKE Node Pool or for the release channel's own node upgrades.
- GKE manages add-on versions itself, so there is no counterpart to the EKS Update Add-on action.
- Manage GKE Cluster Groups adds or removes groups on the cluster's environment, or removes them all (Allow all groups). An environment with no groups is unconstrained: every group may order namespaces on the cluster. Removing the last group has the same effect; CloudBolt's Unassigned placeholder is never put back. Subgroups inherit the environment, the environment's history records each change, and namespaces already ordered keep their own environments.
- Create GKE Namespace is the platform-admin path to a namespace landing zone: it builds the namespace exactly as the GKE Namespace blueprint ([BP-c9ca1i3z](../BP-c9ca1i3z/)) does and files it as a resource of that blueprint owned by the chosen group.
- Teardown refuses while namespace landing zones remain on the cluster, naming them and their groups. It then deletes the cluster (GKE removes its node pools and nodes), the CloudBolt orchestrator and its environments, and the node pool sub-resources.
- Discovery returns every GKE cluster in the projects of the GCP environments, matched by GKE's cluster ID; new ones land in the Unassigned group. It registers or refreshes each running cluster's orchestrator, recovers a resource whose build failed after GKE created the cluster, and leaves a cluster whose build is still running alone. A project a handler cannot list is skipped and its clusters left unchanged.
- Expiration Date defaults to 7 days out. Nothing in this blueprint acts on it.
