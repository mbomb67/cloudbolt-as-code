# AKS Namespace

Creates a governed namespace for a team on an existing AKS cluster as an AKS [managed namespace](https://learn.microsoft.com/en-us/azure/aks/concepts-managed-namespaces): AKS creates the namespace and enforces its CPU and memory quota and its ingress and egress network policies from the Azure resource, and the team's access is Azure RBAC on that resource. CloudBolt adds a quota for storage, volume claims, and pods and default container limits, then creates a CloudBolt environment on the cluster's container orchestrator pinned to the namespace, entitles the ordering group (and optionally one more) to it, and mirrors the tier into the environment quota, so the team orders container blueprints into their namespace without touching the cluster. This is the AKS twin of the AWS EKS Namespace blueprint ([BP-e4c0q58z](../BP-e4c0q58z/)).

## Contents
| Role | ID | Name |
|---|---|---|
| Build | OHK-735mhtvh | AKS Namespace |
| Teardown | OHK-wmdr2ef5 | Teardown AKS Namespace |
| Discovery | OHK-tybit8su | Discover AKS Namespaces |
| Options hook | HPA-qb0w86mi | Generate options for 'Expiration Date' (hook OHK-cfciy0fo) |
| Shared module | SHM-cozv547n | aks_namespace (uses SHM-ptvp0km4 azure_aks) |
| Day-2 | RSA-7wdigcpr / OHK-jeighfcq | Request AKS Namespace Quota Change |
| Day-2 | RSA-1amyol6t / OHK-cqkwd0mq | Manage AKS Namespace Access |
| Day-2 | RSA-nvwe7u4b / OHK-t1r0y1yd | Extend AKS Namespace Expiration |
| Day-2 | RSA-2f01ppuc / OHK-vi009xb8 | AKS Namespace Connection Details |
| Day-2 | RSA-rwaszep5 / OHK-4rucefwc | Manage AKS Namespace Pipeline Identity |

Order form: AKS Cluster, Namespace Name, Size Tier, Ingress Policy, Egress Policy (required), Additional CloudBolt Group, Team Entra Principals, Team Access Level, and an optional Expiration Date.

## Prerequisites
- An AKS cluster created or discovered by the Azure Kubernetes Service blueprint ([BP-20q58q9z](../BP-20q58q9z/)), so the cluster is a Kubernetes Cluster resource with a Kubernetes container orchestrator and a per-cluster environment. The AKS Cluster dropdown lists the AKS clusters whose orchestrator backs an environment the ordering group can use. That environment is unconstrained, so every group sees the cluster, unless the appliance restricts new environments (Admin > Miscellaneous Settings), in which case no team sees the cluster until a platform team assigns groups to it.
- Microsoft Entra ID with Azure RBAC for Kubernetes authorization on the cluster (the Azure Kubernetes Service blueprint enables both) and a network policy engine for the network policies (it builds clusters with Cilium).
- The Azure handler's service principal allowed to manage managed namespaces (`Microsoft.ContainerService/managedClusters/managedNamespaces/*`, in Contributor or Azure Kubernetes Service Namespace Contributor) and `Microsoft.Resources/deployments/*` on the cluster's resource group, and to create role assignments on the cluster (User Access Administrator or Role Based Access Control Administrator).

## Setup
1. Sync the repo, then restart CloudBolt: every plugin here imports the `aks_namespace` and `azure_aks` shared modules, which running processes cache.
2. To change the size tiers (small, medium, large, xlarge: 4/8/16/32 cores, 16/32/64/128 GiB, 100/250/500/1000 GiB, 20/40/80/160 pods), edit `TIERS` in `shared_modules/SHM-cozv547n/SHM-cozv547n_script.py` and restart.

## Day-2 actions
- **Request AKS Namespace Quota Change** moves the namespace to another size tier once the group's approvers accept it: the managed namespace's CPU and memory quota, CloudBolt's storage and pod quota, the tier label, and the environment quota.
- **Manage AKS Namespace Access** grants or revokes a Microsoft Entra user, group, or service principal's access (Writer, Reader, or Admin), a CloudBolt group's entitlement to the namespace environment, or both. The owning group cannot be revoked.
- **Extend AKS Namespace Expiration** adds days to the expiration date and the `cloudbolt.io/lease-expires` annotation.
- **Manage AKS Namespace Pipeline Identity** gives a GitHub Actions workflow access to the namespace without stored credentials, or removes it; see Pipeline identities below.
- **AKS Namespace Connection Details** returns the commands that connect kubectl to the namespace and who has access. The Azure AKS Cluster extension ([XUI-nr5iiu60](../../extensions/XUI-nr5iiu60/)) shows the same on a Connect tab.

## Access
Team Entra Principals takes Microsoft Entra object IDs, one per line, each optionally prefixed with its type (`Group:`, `User:`, or `ServicePrincipal:`; a type avoids a failed lookup for a principal created moments ago). Each gets two role assignments scoped to the managed namespace, as Microsoft documents: Azure Kubernetes Service Namespace User (see the namespace and download its kubeconfig) and the access level's data-plane role (Azure Kubernetes Service RBAC Writer, Reader, or Admin). Granting another level replaces the previous one.
- Assignments CloudBolt makes carry the description `Managed by CloudBolt`; revoke and teardown delete only those and report any others.
- People connect with Azure CLI 2.80.0 or later and kubelogin: `az aks namespace get-credentials --name <namespace> --cluster-name <cluster> --resource-group <rg>`, then `kubelogin convert-kubeconfig -l azurecli`. The kubeconfig carries no secret; kubectl signs in with the person's own Entra identity. Role assignments can take up to five minutes to apply.
- A grant that fails during the order fails the build with the namespace already created; fix the IDs and run Manage AKS Namespace Access, or delete the resource.

## Pipeline identities
Manage AKS Namespace Pipeline Identity creates a user-assigned managed identity in the cluster's resource group that only GitHub Actions workflows from one repository and one branch or GitHub environment can sign in as (federated credentials take one exact subject, no wildcards), and grants it the chosen access on the namespace. The job log and the Connect tab show a workflow that uses it through `azure/login` and `azure/use-kubelogin`.
- GitHub sends a name-based subject (`repo:owner/repo:ref:refs/heads/main`) for older repositories and an ID-based one (`repo:owner@ID/repo@ID:...`) for repositories created, renamed, or transferred after July 15, 2026. The identity trusts both: the action reads the ids from the GitHub API. For a private repository, give it as `owner@OWNER-ID/repo@REPO-ID`.
- Remove takes the same repository and branch or environment, revokes the access, and deletes the identity with its credentials; teardown does the same for every pipeline identity on the namespace. Identities are named `cb-aks-<namespace>-<hash>` and tagged with the namespace resource id; only identities with that tag are deleted.
- The handler's service principal also needs Managed Identity Contributor on the cluster's resource group, and the appliance needs HTTPS to `api.github.com` for the id lookup.

## Deploying into the namespace
The pinned environment is named `<cluster> / <namespace>#<region>`, points at the cluster's orchestrator, and carries the namespace as its `kubernetes_namespace` value. Only the entitled groups see it on order forms.
- A plugin-based container blueprint ordered into that environment reads the namespace with `env.kubernetes_namespace` and sets `metadata.namespace` on the objects it creates.
- CloudBolt's native Container Object item deploys into the namespace named in its YAML, so a manifest template sets `namespace: "{{ job.job_parameters.cast.environment.kubernetes_namespace }}"` in each object's metadata; a manifest without a namespace lands in `default`.

## Notes
- Each namespace resource is a sub-resource of its Kubernetes Cluster resource while staying owned by the ordering group, so the cluster's Workloads tab lists it and the cluster's teardown refuses while any namespace remains. Discovery adopts every managed namespace on the AKS clusters CloudBolt tracks, with the same parent link, owned by the group in its `cloudbolt.io/group-id` annotation (else Unassigned). It marks a tracked namespace Historical only when Azure answers a GET of the managed namespace with not found (the namespace, its cluster, or its resource group is gone), so leave the blueprint's `auto_historical_resources` off; a busy namespace or an unreadable cluster retires nothing. Namespaces still being created are skipped, and one whose creation failed after Azure made it is set back to Active.
- AKS blocks changes to a managed namespace through the Kubernetes API, so labels, annotations, quota, and network policy change only through these actions (or Azure). Teams may still add their own NetworkPolicies; an allow policy overrides a Deny all default.
- Ingress Policy defaults to Allow same namespace; choose Allow all when an ingress controller in another namespace must reach the team's services. Egress defaults to Allow all.
- The managed quota caps CPU and memory requests and limits; CloudBolt's `cloudbolt-landing-zone` ResourceQuota caps persistent volume claim storage and count and pods, and its LimitRange gives containers that set no requests or limits 100m/128Mi requests and 500m/512Mi limits. Namespace names must be 1-63 lowercase letters, digits, or hyphens; Kubernetes and AKS system namespaces are refused, and an existing namespace is never adopted by an order.
- Expiration Date defaults to 7 days out and is mirrored to the namespace as the `cloudbolt.io/lease-expires` annotation. Nothing in this blueprint acts on it.
