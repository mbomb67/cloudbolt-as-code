# OpenShift Project (XUI)

Adds Connect and Workloads tabs to OpenShift Project resources of the OpenShift Project Landing Zone blueprint ([BP-tikkhf2y](../../blueprints/BP-tikkhf2y/)). They are the OpenShift counterpart of the namespace tabs in the AWS EKS ([XUI-8a1jocmr](../XUI-8a1jocmr/)), Azure AKS ([XUI-nr5iiu60](../XUI-nr5iiu60/)), and Google GKE ([XUI-8egdz3e9](../XUI-8egdz3e9/)) extensions.

- **Connect**: the project's status, display name, cluster, API server, CloudBolt environment, requester, size tier, network isolation (its NetworkPolicies), expiration, and **Open in OpenShift console**; the OpenShift users, groups, and service accounts a RoleBinding in the project names, and the cluster admins; the CloudBolt groups that may order into the project; and the `oc login --web`, `oc project`, and `oc get` commands with copy buttons. **Manage access** in the toolbar.
- **Workloads**: a read-only inventory: CloudBolt's quota as a row of meters (any other quota object in the project is listed below it), virtual machines with status, IP address, node, and a link to their CloudBolt server, Deployments, StatefulSets and DaemonSets, pods, services, Routes and ingresses, persistent volume claims, and recent warning events. **Request quota change** in the toolbar. It never shows pod logs, Secrets, or ConfigMaps, and has no buttons that change the cluster.

Each button opens CloudBolt's own dialog for the blueprint's Manage Team Access or Request Quota Change action, so approvals, permissions, and job history apply.

## Prerequisites
- CloudBolt 8.6 or later.
- The `openshift_landing_zone` shared module ([SHM-qmiweowv](../../shared_modules/SHM-qmiweowv/)) at the version in this repository: the tabs use its `get` and `list_items` reads. Without it each panel shows an alert.
- Project resources that carry `openshift_project_name` and `openshift_project_rh_id` (stamped by the blueprint's build and discovery). The tabs only appear on those resources.
- The OpenShift Virtualization resource handler's credentials can read the project's objects. Listing cluster admins also needs read on ClusterRoleBindings; without it the Access table says so and lists the project's RoleBindings only.

## Setup
1. Sync the extension and the blueprint (which brings the shared module), then restart the web server (`systemctl restart httpd` on the appliance): the extension adds URL paths, which CloudBolt loads only at startup, and web processes cache shared module code.
2. Confirm the extension is enabled in Admin > UI Extensions.

## Notes
- Viewing needs `resource.view` on the resource. A button appears only when its action is on the resource's blueprint and `resource_action_permitted` allows it for the viewer.
- OpenShift is read with the resource handler's credentials, never the viewer's, so every panel re-checks the viewer's CloudBolt permission and the tabs stay read-only.
- There is no kubeconfig to download: OpenShift issues each person a token when they sign in with `oc login`.
- A Route that OpenShift generated for an Ingress is shown once, as the Ingress.
- Panels load asynchronously and read the cluster API on every load; nothing is cached in CloudBolt.
