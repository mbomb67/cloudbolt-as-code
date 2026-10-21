# Azure Kubernetes Service

Creates an AKS cluster in an Azure environment the ordering group can use. The cluster follows Microsoft's baseline for a shared cluster: system-assigned managed identity, AKS-managed Microsoft Entra ID with Azure RBAC for Kubernetes authorization, Azure CNI Overlay with the Cilium dataplane and network policy engine, automatic Kubernetes upgrades on the chosen channel, and weekly node image updates. The build registers the cluster in CloudBolt as a Kubernetes container orchestrator with its own environment and records each node pool as an AKS Node Pool sub-resource. Day-2 actions manage node pools, the cluster's Kubernetes version, namespace landing zones, and the CloudBolt groups that may order namespaces on the cluster.

## Contents
| Role | ID | Name |
|---|---|---|
| Build | OHK-x13tmam0 | AKS Cluster Build |
| Teardown | OHK-27oemiwb | AKS Cluster Teardown |
| Discovery | OHK-uof2rcaa | AKS Cluster Discovery |
| Shared module | SHM-ptvp0km4 | azure_aks |
| Day-2 action | RSA-wxd1o7s8 | Add AKS Node Pool (hook OHK-b7poza8x) |
| Day-2 action | RSA-v9717mtc | List AKS Node Pools (hook OHK-0fp008gh) |
| Day-2 action | RSA-ehxck39g | Scale AKS Node Pool (hook OHK-80c4ibdb) |
| Day-2 action | RSA-wsre115s | Upgrade AKS Node Pool (hook OHK-t0j1w09c) |
| Day-2 action | RSA-j4f98ti1 | Delete AKS Node Pool (hook OHK-czexzrl1) |
| Day-2 action | RSA-gp2aywcz | Upgrade AKS Version (hook OHK-ojf2vvq1) |
| Day-2 action | RSA-jttbwkm5 | Create AKS Namespace (hook OHK-kb4pv2yq, shared module SHM-cozv547n) |
| Day-2 action | RSA-my8fifei | Manage AKS Cluster Groups (hook OHK-yzmq0kzu) |
| Options hook | HPA-qb0w86mi | Generate options for 'Expiration Date' (hook OHK-cfciy0fo) |
| UI extension | [XUI-nr5iiu60](../../extensions/XUI-nr5iiu60/) | Azure AKS Cluster tabs (Cluster, Node pools, Workloads, Groups) |

Order form: Cluster Name, Environment, Resource Group, Kubernetes Version, System Node Size, System Node Count, Pricing Tier, Automatic Upgrade (required), and Autoscale Maximum, Private Cluster, Cluster Admin Entra Groups, Entitled CloudBolt Groups, Expiration Date (optional).

## Prerequisites
- An Azure resource handler with at least one environment. Resource Group lists the environment's resource groups and System Node Size its Node Size options (B-series sizes are left out; Microsoft requires at least 4 vCPUs for system pools). The order form lists environments, not handlers.
- The handler's service principal needs Contributor (or Azure Kubernetes Service Contributor) on the resource groups it deploys into, and Microsoft.ContainerService read on the subscription for discovery. Namespace access grants additionally need permission to create role assignments (User Access Administrator or Role Based Access Control Administrator) on the clusters; see the AKS Namespace blueprint ([BP-dogqi827](../BP-dogqi827/)).
- The appliance reaches the cluster's API server over HTTPS. A private cluster is created as asked, but CloudBolt can only manage its Kubernetes objects when the appliance can resolve and route to the private endpoint.

## Setup
1. Sync the repo, then restart CloudBolt: every plugin here imports the `azure_aks` shared module, which running processes cache.
2. Entitle the ordering groups to the Azure environments. `any_group_can_deploy` is true, so every group may order the blueprint itself.
3. Choose who may order namespaces on a cluster with Entitled CloudBolt Groups on the order form, or later on the cluster's Groups tab (Manage AKS Cluster Groups); no environment administrator is needed. Both change the groups of the cluster's `Resource-<cluster> Environment#<region>` environment. On an appliance that restricts new environments, that environment starts in the Unassigned group, and no group sees the cluster until groups are entitled or all groups are allowed.

## Notes
- Kubernetes Version lists the generally available minor versions AKS offers in the environment's region (`kubernetesVersions`), newest first, with AKS's default preselected; AKS installs the latest patch of the chosen minor version.
- The system node pool is named `systempool`. When Autoscale Maximum is greater than System Node Count, the cluster autoscaler runs between the two. Add User node pools for workloads with Add AKS Node Pool.
- Local accounts stay enabled: CloudBolt reaches the API server with the cluster's admin credential (`listClusterAdminCredential`), using its client certificate and verifying the server against the cluster CA. Disabling local accounts on the cluster breaks the orchestrator. Cluster Admin Entra Groups become `aadProfile.adminGroupObjectIDs`.
- Creation takes 5 to 10 minutes; the job follows the operation Azure returns.
- Node pools are AKS Node Pool sub-resources of the cluster. Every node pool action reads the pools from Azure, so stale sub-resources never block an action; List AKS Node Pools refreshes the sub-resources and the cluster's details.
- Upgrade AKS Version offers the versions in the cluster's upgrade profile. "Control plane and all node pools" upgrades the control plane, then each pool; "Control plane only" is refused while an automatic upgrade channel is set, as AKS requires. Upgrade AKS Node Pool then moves a pool to the control plane's version or to the latest node image.
- AKS manages add-on versions itself, so there is no counterpart to the EKS Update Add-on action; the Cluster tab lists the add-ons and cluster extensions with their state.
- Manage AKS Cluster Groups adds or removes groups on the cluster's environment, or removes them all (Allow all groups). An environment with no groups is unconstrained: every group may order namespaces on the cluster. Removing the last group has the same effect; CloudBolt's Unassigned placeholder is never put back. Subgroups inherit the environment, the environment's history records each change, and namespaces already ordered keep their own environments.
- Create AKS Namespace is the platform-admin path to a namespace landing zone: it builds the namespace exactly as the AKS Namespace blueprint ([BP-dogqi827](../BP-dogqi827/)) does and files it as a resource of that blueprint owned by the chosen group.
- Teardown refuses while namespace landing zones remain on the cluster, naming them and their groups. It then deletes the cluster (AKS removes its node pools and node resource group), the CloudBolt orchestrator and its environments, and the node pool sub-resources.
- Discovery adopts every AKS cluster in each Azure handler's subscription, matched by Azure resource ID, into the first group whose name starts with `Unassigned`, refreshes clusters it already tracks, records node pools, and registers or refreshes the orchestrator. Stopped clusters keep their existing orchestrator credentials.
- Expiration Date defaults to 7 days out. Nothing in this blueprint acts on it.
