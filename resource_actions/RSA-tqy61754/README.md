# Launch Helm Chart

Standalone day-2 resource action that runs `helm install <release> <chart> --namespace <namespace> --create-namespace` on the resource's server tagged `controller`. Intended for resources that represent a Kubernetes cluster whose control-plane node is a CloudBolt-managed server.

## Contents
| Role | ID | Name |
|---|---|---|
| Plugin | OHK-tb703wqx | Launch Helm Chart (2) |

Action inputs (all required STR): Chart, Namespace, Release Name.

## Prerequisites
- The target resource must have at least one server carrying a CloudBolt tag named exactly `controller`; otherwise the action fails with "No controller node found". No content in this repo applies that tag, so add it to the server manually or in your cluster blueprint.
- `helm` installed on that server and a kubeconfig usable by the CloudBolt remote-execution account (`server.execute_script` runs without sudo). The chart reference must resolve from the server: a repo already added for that account, an OCI reference, or a local path.

## Setup
1. Attach the action to the relevant resource type or a blueprint's management actions in the UI; as a standalone action it has no parent blueprint after import.
2. Nothing else to configure; the action imports enabled and does not require approval.

## Notes
- Runs asynchronously (`is_synchronous` false) and returns the helm output as the job message. A non-zero `helm` exit raises from `execute_script` and fails the job.
- Not idempotent: re-running with the same release name fails because the script uses `helm install`, not `upgrade --install`.
- The plugin is named `Launch Helm Chart (2)` in metadata while the action label is `Launch Helm Chart`.
