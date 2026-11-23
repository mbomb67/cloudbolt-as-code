# Enable Apache Down Alert

Server action that makes OpenShift detect that Apache stopped answering inside an OpenShift Virtualization VM and raise an `ApacheDown` alert. It works on OpenShift Virtualization VMs only; any other server is refused. It is safe to run again.

## What it does
1. Adds a TCP readiness probe on port 443 to the VirtualMachine (first check after 120 seconds, then every 10 seconds, 3 failures).
2. Restarts the VM once, only when the probe was just added or changed, and waits up to 10 minutes for the new VM instance to be Running and Ready.
3. Creates or updates the PrometheusRule `apache-down-<vm>` in the VM's namespace. It is labelled `app.kubernetes.io/managed-by: cloudbolt` and `cloudbolt.io/resource: <server id>`, and it is left to Thanos Ruler so it can read platform metrics.
4. Prints a status block every run: the VM instance and its pod readiness, the rule's expression and the raw `kube_pod_status_ready` series from Thanos, and any pending or firing `ApacheDown` alert. Run it again after stopping Apache to see what the cluster sees.

## The alert
`ApacheDown` fires when the VM's virt-launcher pod has been not ready for 1 minute, with labels `severity: critical`, `name: <vm>` and `cloudbolt_server: <server global id>`.

## Delivery to CloudBolt
The alert reaches CloudBolt only through an Alertmanager webhook receiver that posts to the [Monitoring Alerts](../../webhooks/IWH-i7b44zvs/) webhook. That receiver is not created here.

## What it needs
The resource handler's account needs permission to edit the VM and the `monitoring-rules-edit` role in the VM's namespace. The status block also uses the Monitoring extension and `cluster-monitoring-view`; without them it prints that the metrics check is unavailable.

## Remove it by hand
`oc delete prometheusrule apache-down-<vm> -n <namespace>`, then remove `spec.template.spec.readinessProbe` from the VirtualMachine and restart the VM.
