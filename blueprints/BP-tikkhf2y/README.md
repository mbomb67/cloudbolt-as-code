# OpenShift Project Landing Zone

Creates a governed OpenShift project for a team: a namespace with a tiered ResourceQuota, a LimitRange, optional multitenant-isolation NetworkPolicies, and an optional admin RoleBinding for an OpenShift group. It then creates a CloudBolt environment pinned to that namespace, entitles the ordering group (and optionally one more) to it, and mirrors the quota into the environment so the team can order VM and container blueprints into the project.

## Contents
| Role | ID | Name |
|---|---|---|
| Build | OHK-prew0osh | OpenShift Project Landing Zone |
| Teardown | OHK-mpe8fl3d | Teardown OpenShift Project Landing Zone |
| Discovery | OHK-e7albpni | Discover OpenShift Project Landing Zones |
| Day-2 action | RSA-yj1c4b5s | Manage Team Access (hook OHK-9zzqqz7t) |
| Day-2 action | RSA-4e8lmj2r | Request Quota Change (hook OHK-ug53cdbx) |
| Day-2 action | RSA-kx7mdgva | Extend Expiration (hook OHK-3w9nejn3) |
| Options hook | HPA-qb0w86mi | Generate options for 'Expiration Date' (hook OHK-cfciy0fo) |
| Shared module | SHM-qmiweowv | openshift_landing_zone |

## Prerequisites
- An OpenShift Virtualization resource handler with at least one environment the ordering group can use. The order form lists environments, not handlers; the handler is derived server-side.
- Handler credentials permitted to create and delete namespaces, ResourceQuotas, LimitRanges, NetworkPolicies, and RoleBindings, and to list groups, users, and service accounts.

## Setup
1. Sync the repo, then restart CloudBolt. Shared-module code is cached in-process and the plugins import `openshift_landing_zone`.
2. To change the size tiers (small, medium, large, xlarge: 4/8/16/32 cores, 16/32/64/128 GiB, 100/250/500/1000 GiB, 20/40/80/160 pods, 4/8/16/32 VMs), edit `TIERS` in `shared_modules/SHM-qmiweowv/SHM-qmiweowv_script.py` and restart.

## Notes
- Project names must be 1-63 lowercase letters, digits, or hyphens; names starting with `openshift-` or `kube-` and the built-in namespaces are refused.
- Expiration Date defaults to 7 days out and is mirrored to the namespace as the `cloudbolt.io/lease-expires` annotation. Extend Expiration adds days to the current date, or to today if it has passed or was never set.
- Request Quota Change requires approval from the group's approvers. Kubernetes accepts a quota below current usage; CloudBolt's environment quota refuses to drop below what is in use and reports a warning.
- Manage Team Access works on two planes: CloudBolt group entitlement to the environment, and an OpenShift RoleBinding (admin, edit, or view) for a Group, User, or ServiceAccount. Supply either or both.
- Teardown deletes the namespace and everything in it, then removes the CloudBolt environment. It refuses to run while CloudBolt still tracks active servers in that environment; decommission them first. Already-deleted namespaces return WARNING.
- Discovery imports only namespaces labeled `cloudbolt.io/landing-zone=true` across all OpenShift Virtualization handlers; plain namespaces are ignored.
- `any_group_can_deploy` is true, so every group can order this blueprint.
