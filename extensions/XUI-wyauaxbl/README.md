# StormForge Optimize Live (XUI)

Shows StormForge in CloudBolt on Kubernetes clusters that run the StormForge agent. Each cluster gets three tabs, marked with the StormForge icon: **Clusters**, **Namespaces** and **Workloads**. Clicking a namespace or workload anywhere opens it in its tab.

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

The tabs are read-only. StormForge settings are applied as `live.stormforge.io/*` annotations in Kubernetes, so the Settings pages show the annotations that change them.

## Prerequisites
- CloudBolt 8.6 or later.
- The StormForge agent set up as in [StormForge agent on EKS, AKS and GKE clusters](../../docs/stormforge-agent-setup.md). The tabs appear only on clusters that have the `stormforge_cluster_name` parameter, which the agent install writes. The extension uses the `stormforge` shared module ([SHM-2fd3bhsz](../../shared_modules/SHM-2fd3bhsz/)) and the `StormForge` connection info.
- The StormForge credential needs the `view:clusters` and `view:workloads` scopes. A credential made with `stormforge auth create` has them.
- The **Nodes** view reads the cluster through the container orchestrator CloudBolt registered for it (`container_orchestrator_id`). Used CPU and memory need metrics-server in the cluster. GKE and AKS run it by default; EKS clusters need it installed.

## Setup
1. Sync the repository.
2. Restart the web server (`systemctl restart httpd`). The extension adds URL paths, which CloudBolt loads only at startup.
3. Confirm the extension is enabled under **Admin > Extensions**.

## Notes
- **Charts:** StormForge draws usage and node history from its own UI backend, which accepts only signed-in users, not API credentials. The tabs show the latest recommendation's average usage against requests and limits, and the nodes as they are now. **Open in StormForge** links each view to the same page in StormForge for the history.
- **Tab names:** the tabs are titled "SF Clusters", "SF Namespaces" and "SF Workloads", and the page shows the icon in place of "SF". The EKS, AKS and GKE extensions already add a Workloads tab, and CloudBolt builds tab IDs from titles, so the prefix keeps the IDs apart.
- **Access:** viewing needs `resource.view` on the cluster. StormForge is read with the connection info's credential, not the viewer's, so every panel re-checks the viewer's permission. The agent log is shown only to CloudBolt admins and people who may run Install or Upgrade StormForge Agent on the cluster.
- **No data:** when StormForge knows a cluster but shows no namespaces, open **Clusters > Agent**: it shows whether the agent registered, whether its pods run on the current credential, and what the agent logged.
- **Loading:** a tab loads only when it is opened. StormForge responses are cached for a minute, and **Refresh** reads them again.
