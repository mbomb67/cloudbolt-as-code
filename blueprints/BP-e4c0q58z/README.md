# AWS EKS Namespace

Creates a governed namespace for a team on an existing EKS cluster: a namespace with a tiered ResourceQuota, a LimitRange, and a default-deny NetworkPolicy set, all enforced by the cluster. It then creates a CloudBolt environment on the cluster's container orchestrator pinned to that namespace, entitles the ordering group (and optionally one more) to it, and mirrors the tier into the environment quota, so the team orders container blueprints into their namespace without touching the cluster. This is the EKS twin of the OpenShift Project Landing Zone ([BP-tikkhf2y](../BP-tikkhf2y/)).

## Contents
| Role | ID | Name |
|---|---|---|
| Build | OHK-u9jm7p8h | AWS EKS Namespace |
| Teardown | OHK-bnano0nc | Teardown AWS EKS Namespace |
| Discovery | OHK-ow9x0amc | Discover AWS EKS Namespaces |
| Options hook | HPA-qb0w86mi | Generate options for 'Expiration Date' (hook OHK-cfciy0fo) |
| Shared module | SHM-sa5yqm9j | eks_namespace |

Order form: EKS Cluster, Namespace Name, Size Tier (required), Additional CloudBolt Group, Allow Ingress From Other Namespaces, and an optional Expiration Date.

## Prerequisites
- An EKS cluster created or discovered by the AWS EKS blueprint ([BP-r5apcf7e](../BP-r5apcf7e/)), so the cluster is a Kubernetes Cluster resource with a Kubernetes container orchestrator and a per-cluster environment. The EKS Cluster dropdown lists the clusters whose orchestrator backs an environment the ordering group can use. The per-cluster environment is unconstrained, so every group sees the cluster, unless the appliance restricts new environments (Admin > Miscellaneous Settings), in which case it belongs to the Unassigned group and no team sees the cluster until a platform team assigns groups to that environment.
- The orchestrator's identity (the AWS handler's, through the EKS token it mints) allowed to create and delete namespaces, ResourceQuotas, LimitRanges, and NetworkPolicies; the cluster creator has this through the cluster admin access entry.
- NetworkPolicy enforcement on the cluster: the Amazon VPC CNI network policy feature (VPC CNI 1.21.0 or later, enabled in the add-on configuration) or another policy engine. Without it the policies are created but not enforced ([AWS documentation](https://docs.aws.amazon.com/eks/latest/userguide/cni-network-policy.html)).

## Setup
1. Sync the repo, then restart the CloudBolt job engine. Shared-module code is cached in the job engine's worker processes (plugins reload on every job, shared modules do not) and every plugin here imports `eks_namespace`.
2. To change the size tiers (small, medium, large, xlarge: 4/8/16/32 cores, 16/32/64/128 GiB, 100/250/500/1000 GiB, 20/40/80/160 pods), edit `TIERS` in `shared_modules/SHM-sa5yqm9j/SHM-sa5yqm9j_script.py` and restart.

## Deploying into the namespace
The pinned environment is named `<cluster> / <namespace>#<region>`, points at the cluster's orchestrator, and carries the namespace as its `kubernetes_namespace` value. Only the entitled groups see it on order forms. Keep the `#<region>` suffix if you rename it: CloudBolt mints the cluster's EKS token with the region parsed from the orchestrator's first environment name after `#`, so an environment without it breaks every call to the cluster through CloudBolt.
- A plugin-based container blueprint ordered into that environment reads the namespace with `env.kubernetes_namespace` (or `env.get_cfv("kubernetes_namespace")`) and sets `metadata.namespace` on the objects it creates.
- CloudBolt's native Container Object item deploys into the namespace named in its YAML, so a manifest template sets `namespace: "{{ job.job_parameters.cast.environment.kubernetes_namespace }}"` in each object's metadata; a manifest without a namespace lands in `default`.

## Notes
- Namespace names must be 1-63 lowercase letters, digits, or hyphens; names starting with `kube-` and the built-in namespaces are refused. An existing namespace is never adopted by an order; import it with discovery instead.
- The ResourceQuota caps CPU and memory requests and limits, persistent volume claim storage and count, and pod count; the LimitRange gives containers that set no requests or limits 100m/128Mi requests and 500m/512Mi limits, which a quota requires. Both are enforced by the cluster's API server.
- Leave Allow Ingress From Other Namespaces unchecked to isolate the namespace: ingress is denied except from pods in the same namespace and in `kube-system`. Load balancers in IP target mode and other namespaces need their own allow policies.
- The environment quota mirror is informational for container orders, which CloudBolt does not count against server quotas.
- Expiration Date defaults to 7 days out and is mirrored to the namespace as the `cloudbolt.io/lease-expires` annotation. Nothing in this blueprint acts on it.
- Teardown removes the CloudBolt environment first, then deletes the namespace and everything in it, waits for it to terminate, and drops the orchestrator's namespace record. An already-deleted namespace returns WARNING.
- Discovery imports only namespaces labeled `cloudbolt.io/landing-zone=true` across every EKS cluster's orchestrator; plain namespaces are ignored. It links an imported namespace to its environment when one exists but does not create environments.
- `any_group_can_deploy` is true, so every group can order this blueprint; the EKS Cluster dropdown is what limits where a group can order a namespace. Namespace environments do not count toward that gate: holding one namespace on a cluster does not let a group order more there.
- Platform admins can also create a namespace from the cluster resource with the AWS EKS blueprint's Create AWS EKS Namespace action, choosing the owning group; the result is a resource of this blueprint, identical to an ordered one except that it has no expiration date.
