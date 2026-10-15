# AWS EKS Cluster (XUI)

Adds three tabs to Kubernetes Cluster resources created or discovered by the AWS EKS blueprint ([BP-r5apcf7e](../../blueprints/BP-r5apcf7e/)):

- **Cluster**: live status, Kubernetes and platform version with support status, API endpoint and access mode, OIDC issuer, control-plane logging, authentication mode, networking, the health issues AWS reports, the jobs running on the resource, and an update banner with an **Update EKS Version** button when EKS offers a newer Kubernetes version. An add-on table shows each add-on's installed and latest compatible version, status, and health, with an **Upgrade** button per row.
- **Node groups**: every managed node group with status, AMI type and release, Kubernetes version (flagged when behind the control plane), instance types, scaling configuration, in-service node count, and the health issues AWS reports; **Scale** and **Delete** buttons per row, **Add node group** and **Sync from AWS** in the toolbar.
- **Workloads**: read through the cluster's container orchestrator token: nodes with Ready state, pressure conditions, CPU and memory requests against allocatable capacity, and pod counts; namespaces with pod, deployment, and service counts.

Every button starts one of the blueprint's resource actions as a CloudBolt job (Update EKS Version, Scale AWS EKS Nodegroup, Delete AWS EKS Nodegroup, Update AWS EKS Add-on, Add AWS EKS Nodegroup, List AWS EKS Nodegroup), so approvals, permissions, and job history apply. The tab is named Cluster rather than Overview because every resource already has an Overview tab.

## Prerequisites
- CloudBolt 8.6 or later.
- Resources that carry `eks_cluster_name` plus `env_id` or `aws_rh_id` and `aws_region` (stamped by the blueprint's build and discovery plugins). The tabs only appear on those resources.
- On top of the blueprint's permissions, the handler credentials need `eks:ListAddons`, `eks:DescribeAddon`, `eks:DescribeAddonVersions`, `eks:DescribeUpdate`, `eks:UpdateAddon`, `eks:UpdateNodegroupConfig`, and `autoscaling:DescribeAutoScalingGroups` (node counts; without it the Nodes column shows a dash).
- The Workloads tab needs the container orchestrator the blueprint registers (`container_orchestrator_id`). Its bearer token is minted from the handler credentials, so that identity must be allowed to list nodes, pods, namespaces, services, and deployments; the cluster creator has this through the cluster admin access entry.

## Setup
1. Sync the repository, then restart the web server (`systemctl restart httpd` on the appliance): the extension adds URL paths, which CloudBolt loads only at startup, and Admin > Extensions flags it with the same reminder. Confirm the extension is enabled there.
2. Sync the blueprint as well, so its management actions include the two new ones: a button is hidden when its action is missing from the blueprint or the viewer may not run it. A blueprint sync resets the blueprint's group permissions to what the metadata lists (none, with any-group off), so restore them afterwards.

## Notes
- Viewing needs `resource.view` on the resource. Each button appears only when `resource_action_permitted` allows that action for the viewer, and the job runs as the viewer; an action that requires approval creates an order instead.
- Delete needs the node group's `eks_nodegroup` sub-resource; **Sync from AWS** (List AWS EKS Nodegroup) creates missing ones. Scale and Upgrade work from AWS directly.
- The latest add-on version is the newest one AWS publishes for the cluster's Kubernetes version. After Update EKS Version, recheck add-ons and node groups: their latest versions change and node groups show as behind the control plane until they are updated.
- Panels load asynchronously and read AWS and the cluster API on every load; nothing is cached in CloudBolt.
- Styling follows CloudBolt's own order and resource pages (theme tokens, Bootstrap 5, Bootstrap Icons) so Branded Themes apply.
