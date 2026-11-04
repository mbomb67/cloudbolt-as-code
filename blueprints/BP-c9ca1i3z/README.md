# GKE Namespace

Creates a governed namespace for a team on an existing GKE cluster: a ResourceQuota sized by tier, default container limits, and NetworkPolicies for the chosen ingress and egress policy, which GKE Dataplane V2 enforces. CloudBolt then creates a CloudBolt environment on the cluster's container orchestrator pinned to the namespace, entitles the ordering group (and optionally one more) to it, and mirrors the tier into the environment quota, so the team orders container blueprints into their namespace without touching the cluster. Team access is a Kubernetes RoleBinding for Google accounts, groups, and service accounts. This is the GKE twin of the AWS EKS Namespace ([BP-e4c0q58z](../BP-e4c0q58z/)) and AKS Namespace blueprints.

## Contents
| Role | ID | Name |
|---|---|---|
| Build | OHK-32pzj98f | GKE Namespace |
| Teardown | OHK-jm8j1513 | Teardown GKE Namespace |
| Discovery | OHK-ml7ee42y | Discover GKE Namespaces |
| Options hook | HPA-qb0w86mi | Generate options for 'Expiration Date' (hook OHK-cfciy0fo) |
| Shared module | SHM-kephq8iq | gke_namespace (uses SHM-4yw92b8x gcp_gke) |
| Day-2 | RSA-x1658l9r / OHK-xg47m1x2 | Request GKE Namespace Quota Change |
| Day-2 | RSA-mucwia5b / OHK-vdhjpyg2 | Manage GKE Namespace Access |
| Day-2 | RSA-1qdgexp2 / OHK-vtuzh4nh | Extend GKE Namespace Expiration |
| Day-2 | RSA-0f3na70j / OHK-egyf9q28 | GKE Namespace Connection Details |
| Day-2 | RSA-0k2xcobm / OHK-l3j30r89 | Manage GKE Namespace Pipeline Identity |
| Day-2 | RSA-ah76u4qg / OHK-ljgoxser | Deploy Test Workload |

Order form: GKE Cluster, Namespace Name, Size Tier, Ingress Policy, Egress Policy (required), Additional CloudBolt Group, Team Google Principals, Team Access Level, and an optional Expiration Date.

## Prerequisites
- A GKE cluster created or discovered by the Google Kubernetes Engine blueprint ([BP-g8g8aecx](../BP-g8g8aecx/)), so the cluster is a Kubernetes Cluster resource with a Kubernetes container orchestrator and a per-cluster environment. The GKE Cluster dropdown lists the GKE clusters whose per-cluster environment the ordering group can use: the groups chosen on the cluster's Groups tab, or every group when none are chosen. On an appliance that restricts new environments (Admin > Miscellaneous Settings), no team sees a cluster until its owners choose groups.
- GKE Dataplane V2 on the cluster (the Google Kubernetes Engine blueprint enables it). On a cluster without a network policy enforcer the NetworkPolicies are created but not enforced.
- Google Groups for RBAC on the cluster for `group:` principals (the cluster blueprint's RBAC Security Group).

## Setup
1. Sync the repo, then restart CloudBolt: every plugin here imports the `gke_namespace` and `gcp_gke` shared modules, which running processes cache.
2. To change the size tiers (small, medium, large, xlarge: 4/8/16/32 cores, 16/32/64/128 GiB, 100/250/500/1000 GiB, 20/40/80/160 pods), edit `TIERS` in `shared_modules/SHM-kephq8iq/SHM-kephq8iq_script.py` and restart.

## Day-2 actions
- **Request GKE Namespace Quota Change** moves the namespace to another size tier once the group's approvers accept it: the ResourceQuota, the tier label, and the environment quota.
- **Manage GKE Namespace Access** grants or revokes a Google account, Google Group, or Google service account's access (Edit, View, or Admin), a CloudBolt group's entitlement to the namespace environment, or both. The owning group cannot be revoked.
- **Extend GKE Namespace Expiration** adds days to the expiration date and the `cloudbolt.io/lease-expires` annotation.
- **Manage GKE Namespace Pipeline Identity** gives a GitHub Actions workflow access to the namespace without a stored key, or removes it; see Pipeline identities below.
- **GKE Namespace Connection Details** returns the commands and kubeconfig that connect kubectl to the namespace and who has access. The Google GKE Cluster extension ([XUI-8egdz3e9](../../extensions/XUI-8egdz3e9/)) shows the same on a Connect tab.
- **Deploy Test Workload** runs a two-pod unprivileged nginx Deployment and a ClusterIP Service named `cb-test-workload` in the namespace, so the Workloads tab has something to show, or removes them. The same action serves the EKS, AKS and GKE namespace and OpenShift project blueprints.

## Access
Team Google Principals takes one principal per line: `user:EMAIL` for a Google account, `group:EMAIL` for a Google Group, or `serviceAccount:EMAIL` for a Google service account (a bare email is a user, or a service account when it ends in `.gserviceaccount.com`). Each gets a RoleBinding in the namespace to Kubernetes' built-in `edit`, `view`, or `admin` ClusterRole; granting another level replaces the previous one.
- RoleBindings CloudBolt makes carry the label `app.kubernetes.io/managed-by: cloudbolt`; revoke deletes only those and reports any others.
- People connect with `gcloud container clusters get-credentials`, which needs `container.clusters.get` on the project (Kubernetes Engine Cluster Viewer), and `gke-gcloud-auth-plugin`. Without that permission they use the kubeconfig the Connect tab offers, which holds no secret.
- IAM roles on the project, such as Kubernetes Engine Developer or Admin, reach every namespace whatever the RoleBindings say, and are not listed here.
- A grant that fails during the order fails the build with the namespace already created; fix the principals and run Manage GKE Namespace Access, or delete the resource.

## Pipeline identities
Manage GKE Namespace Pipeline Identity lets GitHub Actions workflows from one repository and one branch or GitHub environment deploy into the namespace through Workload Identity Federation, with no service account key. In the cluster's project it creates an OIDC provider for GitHub in the `cloudbolt-github` workload identity pool (created on first use and kept), trusting only that repository's subject, and an IAM service account `cb-gh-<hash>` the workflow impersonates; the service account gets the chosen access in the namespace through a RoleBinding. Google documents RBAC subjects for IAM service accounts but not for federated identities, hence the service account. The job log and the Connect tab show a workflow that signs in with `google-github-actions/auth` and runs `kubectl` against the cluster endpoint with the cluster CA, so the workflow needs no IAM role on the project.
- The handler's identity needs Workload Identity Pool Admin and Service Account Admin on the project, and the project needs the IAM, Security Token Service, and IAM Service Account Credentials APIs enabled. The GitHub runner must reach the cluster's endpoint (control plane authorized networks can block it).
- GitHub sends a name-based subject (`repo:owner/repo:ref:refs/heads/main`) for older repositories and an ID-based one (`repo:owner@ID/repo@ID:...`) for repositories created, renamed, or transferred after July 15, 2026. For a private repository, give it as `owner@OWNER-ID/repo@REPO-ID`; the appliance needs HTTPS to `api.github.com` to look up the IDs of a public one.
- Remove takes the same repository and branch or environment, revokes the access, and deletes the provider and service account; teardown does the same for every pipeline identity on the namespace. Only a provider or service account whose description names the namespace resource is deleted. Google keeps a deleted provider for 30 days, so adding the same pipeline again restores it. The namespace access goes at once; Google notes that deleting a provider does not revoke credentials it already issued.

## Deploying into the namespace
The pinned environment is named `<cluster> / <namespace>#<location>`, points at the cluster's orchestrator, and carries the namespace as its `kubernetes_namespace` value. Only the entitled groups see it on order forms.
- A plugin-based container blueprint ordered into that environment reads the namespace with `env.kubernetes_namespace` and sets `metadata.namespace` on the objects it creates.
- CloudBolt's native Container Object item deploys into the namespace named in its YAML, so a manifest template sets `namespace: "{{ job.job_parameters.cast.environment.kubernetes_namespace }}"` in each object's metadata; a manifest without a namespace lands in `default`.

## Notes
- Each namespace resource is a sub-resource of its Kubernetes Cluster resource while staying owned by the ordering group, so the cluster's Workloads tab lists it and the cluster's teardown refuses while any namespace remains. Discovery adopts every namespace labelled `cloudbolt.io/landing-zone=true` on the GKE clusters CloudBolt tracks, with the same parent link, owned by the group in its `cloudbolt.io/group-id` annotation (else Unassigned). It retires a namespace only on proof: its cluster was listed without it and a GET answers 404, or GKE reports the cluster itself gone. Create GKE Namespace files the record in PROV until the namespace exists, so discovery never acts on it mid-build.
- Ingress Policy defaults to Allow same namespace (pods in the namespace and in kube-system); choose Allow all when a load balancer or an ingress controller in another namespace must reach the team's pods. Egress defaults to Allow all. Allow same namespace and Deny all egress still allow DNS lookups to kube-dns.
- The ResourceQuota `cloudbolt-landing-zone` caps CPU and memory requests and limits, persistent volume claim storage and count, and pods; the LimitRange gives containers that set no requests or limits 100m/128Mi requests and 500m/512Mi limits. Namespace names must be 1-63 lowercase letters, digits, or hyphens; Kubernetes and GKE system namespaces (`kube-`, `gke-`, `gmp-`, and others) are refused, and an existing namespace is never adopted by an order.
- Expiration Date defaults to 7 days out and is mirrored to the namespace as the `cloudbolt.io/lease-expires` annotation. Nothing in this blueprint acts on it.
