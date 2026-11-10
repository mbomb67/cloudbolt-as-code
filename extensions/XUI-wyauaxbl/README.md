# StormForge Optimize Live (XUI)

Shows StormForge in CloudBolt on Kubernetes clusters that run the StormForge agent, and on the namespaces teams own on those clusters.

- A cluster gets three tabs, marked with the StormForge icon: **Clusters**, **Namespaces** and **Workloads**. Clicking a namespace or workload anywhere opens it in its tab.
- A namespace (AWS EKS Namespace, AKS Namespace or GKE Namespace resource) gets one tab, **Optimization**, with that namespace only. See [Namespaces](#namespaces).

## Cluster tabs

- **Clusters**: the optimization score, recommendation summary and recommendation impact cards, **More Information** (waste and risk, agent versions, status messages), and four views:
  - **Namespaces**: StormForge's namespace table, with search, sorting and pages.
  - **Nodes**: the cluster as it is now. CPU, memory and pods (used, allocated and capacity), node count by type, and every node.
  - **Config**: cluster labels, node instance categories and cost estimates.
  - **Agent**: StormForge's record of the cluster (state, last seen, conditions, workloads and nodes reported, agent version, and whether it is the same cluster by `kube-system` UID), the agent pods in `stormforge-system` (status, restarts, start time, a warning on pods older than the `stormforge-auth` credential), and the last 200 lines of an agent container's log. An **Install or Upgrade StormForge Agent** button opens the action for people who may run it.
- **Namespaces**: a namespace's cards and its workload table. With no namespace chosen, the namespace table.
- **Workloads**: a workload's score, potential savings and recommendation status, then four views:
  - **Summary**: attributes, status messages, per-pod and total requests with their impact, and usage against requests and limits.
  - **Recommendation**: each container's current and recommended requests and limits, with a chart, and the recommended instance category.
  - **Patches**: the kubectl command and YAML to copy or download, with the node affinity preview.
  - **Settings**: Goals, Schedule, Apply Settings, Reliability, CPU and Memory, HPA, Runtime and Pod Scheduling, each with StormForge's annotation examples.

  With no workload chosen, the cluster's workload table with a namespace filter.

The cluster tabs are read-only. StormForge settings are applied as `live.stormforge.io/*` annotations in Kubernetes, so the Settings pages show the annotations that change them.

## Namespaces
The **Optimization** tab appears on a namespace resource when its cluster runs the StormForge agent and this CloudBolt has the `StormForge` connection info. It shows the namespace's optimization score, recommendation summary and impact, and its workload table. Clicking a workload opens its Summary, Recommendation, Patches and Settings in the same tab.

- **One namespace only:** the namespace comes from the resource (`eks_namespace_name`, `aks_namespace_name` or `gke_namespace_name`), never from the request. The cluster, nodes, agent and namespace-list views are not served to a namespace resource.
- **Apply recommendation:** a workload's page has an **Apply recommendation** button for people who may run **Apply StormForge Recommendation** ([RSA-d2zufawc](../../resource_actions/RSA-d2zufawc/)) on the namespace. It runs that action as a CloudBolt job, with three choices:
  - **Apply the latest recommendation now** sends StormForge's patches for the workload to the cluster, the same patch as the `kubectl patch` command on the Patches view.
  - **Turn automatic deployment on** sets `live.stormforge.io/auto-deploy: "Enabled"` on the workload, so StormForge applies each new recommendation after the learning period.
  - **Turn automatic deployment off** sets it to `"Disabled"`.

  The same action is in the namespace's **Actions** menu, with a workload dropdown.
- **What can be applied:** Deployments, StatefulSets, DaemonSets, ReplicaSets, CronJobs, Jobs and their HorizontalPodAutoscalers. For other workload types (for example Argo Rollouts), apply the patch with kubectl from the Patches view.

## Prerequisites
- CloudBolt 8.6 or later.
- The StormForge agent set up as in [StormForge agent on EKS, AKS and GKE clusters](../../docs/stormforge-agent-setup.md). The tabs appear only on clusters where the agent install succeeded: they need both `stormforge_cluster_name` and `stormforge_agent_version`, which the install writes. The extension uses the `stormforge` shared module ([SHM-2fd3bhsz](../../shared_modules/SHM-2fd3bhsz/)) and the `StormForge` connection info.
- The StormForge credential needs the `view:clusters` and `view:workloads` scopes. A credential made with `stormforge auth create` has them.
- Namespace tabs need the cluster to be a CloudBolt resource: the namespace's parent resource, or the one in its `*_namespace_cluster_resource_id`. Namespaces ordered from or created on a CloudBolt-built cluster have it.
- The **Nodes** view reads the cluster through the container orchestrator CloudBolt registered for it (`container_orchestrator_id`). Used CPU and memory need metrics-server in the cluster. GKE and AKS run it by default; EKS clusters need it installed.

## Setup
1. Sync the repository.
2. Restart the web server (`systemctl restart httpd`). The extension adds URL paths, which CloudBolt loads only at startup.
3. Confirm the extension is enabled under **Admin > Extensions**.

## Notes
- **Charts:** StormForge draws usage and node history from its own UI backend, which accepts only signed-in users, not API credentials. The tabs show the latest recommendation's average usage against requests and limits, and the nodes as they are now. **Open in StormForge** links each view to the same page in StormForge for the history.
- **Tab names:** the tabs are titled "SF Clusters", "SF Namespaces", "SF Workloads" and "SF Optimization", and the page shows the icon in place of "SF". The EKS, AKS and GKE extensions already add a Workloads tab, and CloudBolt builds tab IDs from titles, so the prefix keeps the IDs apart.
- **Access:** viewing needs `resource.view` on the cluster or namespace the tab is on. StormForge is read with the connection info's credential, not the viewer's, so every panel re-checks the viewer's permission. The agent log is shown only to CloudBolt admins and people who may run Install or Upgrade StormForge Agent on the cluster. Applying needs permission to run Apply StormForge Recommendation on the namespace; give the action **Requires approval** to send every apply through an order.
- **Applied patches and GitOps:** a patch changes the workload in the cluster. If a pipeline or GitOps controller deploys the workload from its own manifests, its next deployment puts the old requests back unless automatic deployment is on.
- **No data:** when StormForge knows a cluster but shows no namespaces, open **Clusters > Agent**: it shows whether the agent registered, whether its pods run on the current credential, and what the agent logged.
- **Loading:** a tab loads only when it is opened. StormForge responses are cached for a minute, and **Refresh** reads them again.
