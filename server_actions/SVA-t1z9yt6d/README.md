# Enable Apache Down Alert

Server action that makes OpenShift detect that Apache stopped answering inside an OpenShift Virtualization VM and raise an `ApacheDown` alert. It works on OpenShift Virtualization VMs only; any other server is refused. It is safe to run again. The work is in the `openshift_vm_alerts` shared module ([SHM-sud926fb](../../shared_modules/SHM-sud926fb/)), which the [3-tier web app blueprint](../../blueprints/BP-sy5pb325/) also calls from its last build step; this action is a thin wrapper around it.

## What it does
1. Adds a TCP readiness probe on port 443 to the VirtualMachine (first check after 30 seconds, then every 10 seconds, 3 failures).
2. Restarts the VM once, only when the running VM does not yet have the probe, and waits up to 10 minutes for the new VM instance to be Running and Ready.
3. Creates or updates the PrometheusRule `apache-down-<vm>` in the VM's namespace. It is labelled `app.kubernetes.io/managed-by: cloudbolt` and `cloudbolt.io/resource: <server id>`, and it is left to Thanos Ruler so it can read platform metrics.
4. Sets up delivery: stores the Monitoring Alerts webhook's URL (with its token) in the Secret `cloudbolt-alert-webhook` and creates the AlertmanagerConfig `cloudbolt-alerts`, which sends alerts that carry a `cloudbolt_server` label to that URL. Cluster-wide monitoring settings are never changed.
5. Prints a status block every run: the VM instance and its pod readiness, the rule's expression, the pod's age and the raw `kube_pod_status_ready` series from Thanos, any pending or firing `ApacheDown` alert, and a Delivery part (Secret, AlertmanagerConfig, the webhook host, whether user-defined alert routing is on). Run it again after stopping Apache to see what the cluster sees.

## The alert
`ApacheDown` fires when the VM's virt-launcher pod has been not ready for 1 minute and is older than 5 minutes (so a booting VM does not alert), with labels `severity: critical`, `name: <vm>` and `cloudbolt_server: <server global id>`.

## Delivery to CloudBolt
Two prerequisites: a token set on the [Monitoring Alerts](../../webhooks/IWH-i7b44zvs/) webhook (without one the action prints "Delivery not configured" and skips delivery; run it again after setting one), and user-defined alert routing switched on by a cluster admin (`alertmanager.enabled` and `enableAlertmanagerConfig` true in `user-workload-monitoring-config`). The action only reports whether routing is on and prints the fix when it is not. The cluster must also be able to reach CloudBolt's URL, taken from the default portal's site URL.

## What it needs
The resource handler's account needs permission to edit the VM, the `monitoring-rules-edit` role and the `alert-routing-edit` role in the VM's namespace. The status block also uses the Monitoring extension and `cluster-monitoring-view`; without them it prints that the metrics check is unavailable.

## Remove it by hand
`oc delete prometheusrule apache-down-<vm> -n <namespace>`, `oc delete alertmanagerconfig cloudbolt-alerts -n <namespace>` and `oc delete secret cloudbolt-alert-webhook -n <namespace>` (the last two are shared by every VM in the namespace), then remove `spec.template.spec.readinessProbe` from the VirtualMachine and restart the VM.
