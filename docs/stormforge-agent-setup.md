# StormForge agent on EKS, AKS and GKE clusters

The AWS EKS ([BP-r5apcf7e](../blueprints/BP-r5apcf7e/)), Azure Kubernetes Service ([BP-20q58q9z](../blueprints/BP-20q58q9z/)) and Google Kubernetes Engine ([BP-g8g8aecx](../blueprints/BP-g8g8aecx/)) blueprints install the StormForge agent on every cluster they build and register the cluster with StormForge. Deleting the cluster removes it from StormForge. The Install or Upgrade StormForge Agent action installs, upgrades or reinstalls the agent on any of these clusters, including discovered ones.

CloudBolt talks to StormForge only through the StormForge API. It installs the agent with Helm, which runs as a short-lived Job inside the cluster, so neither the appliance nor the cluster needs Helm or the StormForge CLI.

## Prerequisites
- A StormForge credential (client ID and client secret) with StormForge's default permissions. CloudBolt uses it for its own StormForge API calls, and every cluster's agent runs on it: StormForge does not let an API credential create other credentials, so CloudBolt cannot make one per cluster.
- The appliance reaches your StormForge API host (`api.stormforge.io` unless StormForge gave you another) and `registry.stormforge.io` on port 443.
- Each cluster reaches the API host, the matching ingest host (`in.` in place of `api.`, for example `in.stormforge.io`), `registry.stormforge.io`, `quay.io` and Docker Hub (`registry-1.docker.io`, `auth.docker.io`, `production.cloudflare.docker.com`) on port 443, directly or through a proxy. Docker Hub serves the Helm image, which the `stormforge_helm_image` parameter can point elsewhere.

## Setup
1. Create the credential with the StormForge CLI. `stormforge login` signs in to `api.stormforge.io`; for another endpoint, pass its URL, for example `stormforge login https://api.example.com/`.
   ```
   stormforge login
   stormforge auth create cloudbolt-cmp -o json
   ```
   The output holds the client ID and client secret.
2. In CloudBolt, go to **Admin > Connection Info** and add a connection info:
   - **Name:** `StormForge`
   - **IP/Hostname:** your StormForge API host, for example `api.stormforge.io`. CloudBolt and the agent use only this endpoint.
   - **Username:** the client ID
   - **Password:** the client secret
3. Sync the repo, then restart CloudBolt (the job engine and the web server) so running processes load the `stormforge` shared module.
4. Optional, per environment: create these parameters under **Admin > Parameters** and set them on a cluster environment's **Parameters** tab:
   - `stormforge_proxy_url`: HTTPS proxy for the agent and for Helm.
   - `stormforge_no_proxy`: the agent's NO_PROXY list.
   - `stormforge_helm_image`: a Helm 4 image, for example a copy in your own registry.

   The first install creates the three parameters if they are missing, so this step can also come later.

## What runs
- **Build:** the Install StormForge Agent item runs after the cluster is built.
  1. It writes the connection info's credential to the Secret `stormforge-system/stormforge-auth`.
  2. It runs `helm upgrade --install` of `oci://registry.stormforge.io/library/stormforge` at the latest released version.
  3. It waits for the agent to check in.

  A problem is a warning, so the cluster order still completes; run the action to retry.
- **Install or Upgrade StormForge Agent:** a dropdown shows the installed version and what each choice changes it to (newer versions only: StormForge charts cannot be downgraded), for example `Upgrade 3.4.0 → 3.5.0 (latest)`. It rewrites the Secret from the connection info, so after you change the credential, run the action on each cluster to rotate it.
- **Teardown:** the Remove from StormForge item runs after the cluster is deleted. It deletes the cluster in StormForge. A cluster that fails to delete stays registered.

## Notes
- **Shared credential:** every cluster holds the same credential, so anyone who can read Secrets in `stormforge-system` on one cluster can act on every cluster in your StormForge org. Limit who can read Secrets there.
- **Cluster name:** the StormForge name is the cloud cluster name in lowercase, with characters other than letters, digits and `-` replaced, cut to 63 characters. If StormForge already has a different cluster by that name, which CloudBolt checks by comparing the `kube-system` namespace UID, the install stops and says so.
- **Nodes:** the agent and Helm run as pods. A cluster with no ready nodes, such as an EKS cluster before its first node group, gets a warning; add nodes, then run the action.
- **Helm Job permissions:** the Job runs `docker.io/alpine/helm:4.3.1`, pinned by digest, as a non-root user under a temporary ServiceAccount bound to `cluster-admin`, because the chart creates CRDs and ClusterRoles. CloudBolt deletes the Job, its values ConfigMap, the ServiceAccount and the binding when Helm finishes. Helm's output appears in the CloudBolt job log.
- **Resource attributes:** the cluster resource shows StormForge Cluster, StormForge Agent Version and StormForge Status.
- **StormForge tabs:** the StormForge Optimize Live extension ([XUI-wyauaxbl](../extensions/XUI-wyauaxbl/)) adds Clusters, Namespaces and Workloads tabs to these clusters. It reads StormForge with the same connection info and needs the `view:clusters` and `view:workloads` scopes, which a credential made with `stormforge auth create` has.
- **Existing installs:** on a cluster where StormForge was installed by hand, the action keeps the existing release's cluster name and the credential in its Secret.
