"""Server action: makes OpenShift raise an ApacheDown alert when Apache stops answering inside a VM.

Three objects on the cluster, all created through the handler's own credential:
  1. a readiness probe on the VirtualMachine (TCP port 443), so the virt-launcher pod goes not-ready
     when nothing listens on the port;
  2. a restart of the VM, only when the probe was just added or changed, so the template change takes effect;
  3. a PrometheusRule named apache-down-<vm> in the VM's namespace, evaluated by Thanos Ruler, that fires
     ApacheDown with the labels the Monitoring Alerts webhook (webhooks/IWH-i7b44zvs) maps back to this server.

Every run ends with a status block that shows what the cluster sees. Safe to run again.
"""

import re
import time

from common.methods import set_progress
from shared_modules import openshift_vm_network as vmnet

PORT = 443
ALERT_NAME = "ApacheDown"
FOR = "1m"
PROBE_TIMINGS = {"initialDelaySeconds": 120, "periodSeconds": 10, "timeoutSeconds": 5, "failureThreshold": 3}
POLL_SECONDS = 10
WAIT_SECONDS = 600

# https://kubevirt.io/api-reference/ (VirtualMachine, kubevirt.io/v1)
VM_PATH = "/apis/kubevirt.io/v1/namespaces/%s/virtualmachines/%s"
VMI_PATH = "/apis/kubevirt.io/v1/namespaces/%s/virtualmachineinstances/%s"
# https://kubevirt.io/api-reference/ , operation PUT .../virtualmachines/{name}/restart (virtctl restart)
RESTART_PATH = "/apis/subresources.kubevirt.io/v1/namespaces/%s/virtualmachines/%s/restart"
PODS_PATH = "/api/v1/namespaces/%s/pods"
# https://prometheus-operator.dev/docs/api-reference/api/
RULES_PATH = "/apis/monitoring.coreos.com/v1/namespaces/%s/prometheusrules"

RULE_PERMISSION = (
    "The resource handler's account is not allowed to manage PrometheusRules in this namespace. "
    "Grant it the monitoring-rules-edit role in the namespace: "
    "oc adm policy add-role-to-user monitoring-rules-edit <account> -n <namespace>. "
    "https://docs.redhat.com/en/documentation/openshift_container_platform/4.19/html/monitoring/managing-alerts"
)


def wanted_probe():
    probe = {"tcpSocket": {"port": PORT}}
    probe.update(PROBE_TIMINGS)
    return probe


def promql_string(value):
    """Escape a value for use inside a double-quoted PromQL string."""
    return str(value).replace("\\", "\\\\").replace('"', '\\"')


def pod_pattern(vm_name):
    """Regex for the virt-launcher pod name of this VM (the pod gets a 5 character suffix)."""
    return "virt-launcher-" + re.escape(vm_name) + "-[a-z0-9]{5}"


def ready_series(namespace, vm_name):
    """kube_pod_status_ready of the VM's pod. https://github.com/kubernetes/kube-state-metrics/blob/main/docs/metrics/workload/pod-metrics.md"""
    return (
        'kube_pod_status_ready{namespace="' + promql_string(namespace)
        + '", pod=~"' + promql_string(pod_pattern(vm_name))
        + '", condition="true"}'
    )


def rule_expression(namespace, vm_name):
    return ready_series(namespace, vm_name) + " == 0"


def condition(obj, kind):
    for item in (obj or {}).get("status", {}).get("conditions", []) or []:
        if item.get("type") == kind:
            return item
    return None


def describe_condition(item):
    if item is None:
        return "no condition reported"
    text = "%s=%s" % (item.get("type"), item.get("status"))
    if item.get("reason"):
        text += ", reason " + str(item["reason"])
    if item.get("message"):
        text += ", " + str(item["message"])
    return text


def vmi_ready(vmi):
    ready = condition(vmi, "Ready")
    return (vmi or {}).get("status", {}).get("phase") == "Running" and ready is not None and ready.get("status") == "True"


def vmi_state(vmi):
    if vmi is None:
        return "no VirtualMachineInstance exists"
    return "phase %s; %s" % (vmi.get("status", {}).get("phase"), describe_condition(condition(vmi, "Ready")))


def failure(error):
    text = str(error)
    if "prometheusrules" in text and "(403)" in text:
        text += " " + RULE_PERMISSION
    return "FAILURE", "", text


def ensure_probe(cluster, namespace, vm_name):
    """Patch the wanted readiness probe into the VM template; return True when it changed."""
    vm = cluster.request("GET", VM_PATH % (namespace, vm_name))
    current = ((vm.get("spec", {}).get("template", {}) or {}).get("spec", {}) or {}).get("readinessProbe")
    wanted = wanted_probe()
    if current == wanted:
        set_progress("The VM already has the readiness probe on port %d" % PORT)
        return False
    # A merge patch merges objects, so null removes any other handler the old probe used.
    probe = {"httpGet": None, "exec": None, "guestAgentPing": None}
    probe.update(wanted)
    set_progress("Setting the readiness probe on port %d (TCP, first check after %ds)" % (PORT, PROBE_TIMINGS["initialDelaySeconds"]))
    cluster.request(
        "PATCH", VM_PATH % (namespace, vm_name),
        body={"spec": {"template": {"spec": {"readinessProbe": probe}}}},
        content_type="application/merge-patch+json",
    )
    return True


def restart_and_wait(cluster, namespace, vm_name):
    """Restart the VM and wait for a new VMI that is Running and Ready; return an error text or None."""
    old = cluster.request("GET", VMI_PATH % (namespace, vm_name), allow_404=True)
    if old is None:
        set_progress("The VM is not running, so it is not restarted; the probe applies when it next starts")
        return None, False
    old_uid = old.get("metadata", {}).get("uid")
    set_progress("Restarting the VM so the probe takes effect")
    # The subresource API declares no response type, so "Accept: application/json" (the shared client's
    # default) is refused with 406; any type must be accepted.
    path = RESTART_PATH % (namespace, vm_name)
    try:
        response = cluster.session.put(
            cluster.api_url + path, json={},
            headers={"Accept": "*/*", "Content-Type": "application/json"}, timeout=60,
        )
    except Exception as error:
        raise vmnet.OpenShiftApiError("PUT %s could not reach the cluster: %s" % (path, type(error).__name__))
    if not response.ok:
        raise vmnet.OpenShiftApiError("PUT %s failed (%s): %s" % (path, response.status_code, response.text[:500]))
    deadline = time.time() + WAIT_SECONDS
    state = "no VirtualMachineInstance yet"
    while time.time() < deadline:
        time.sleep(POLL_SECONDS)
        vmi = cluster.request("GET", VMI_PATH % (namespace, vm_name), allow_404=True)
        if vmi is not None and vmi.get("metadata", {}).get("uid") == old_uid:
            state = "the old VirtualMachineInstance is still shutting down"
        elif vmi is None:
            state = "no VirtualMachineInstance yet"
        else:
            state = vmi_state(vmi)
            if vmi_ready(vmi):
                set_progress("The VM is back: " + state)
                return None, True
        set_progress("Waiting for the restarted VM: " + state)
    return "The VM did not become Ready within %d minutes after the restart; last state: %s" % (WAIT_SECONDS // 60, state), True


def apply_rule(cluster, namespace, vm_name, server):
    """Create or update the PrometheusRule; return 'created' or 'updated'."""
    name = "apache-down-" + vm_name
    collection = RULES_PATH % namespace
    existed = cluster.request("GET", collection + "/" + name, allow_404=True) is not None
    set_progress("%s the PrometheusRule %s in %s" % ("Updating" if existed else "Creating", name, namespace))
    # The label openshift.io/prometheus-rule-evaluation-scope: leaf-prometheus is left off on purpose:
    # Thanos Ruler evaluates the rule, which lets it read the platform metrics kube_pod_status_ready.
    # https://docs.redhat.com/en/documentation/openshift_container_platform/4.19/html/monitoring/managing-alerts
    cluster.apply(collection, {
        "apiVersion": "monitoring.coreos.com/v1",
        "kind": "PrometheusRule",
        "metadata": {
            "name": name,
            "namespace": namespace,
            "labels": {
                vmnet.MANAGED_BY_LABEL: vmnet.MANAGED_BY,
                vmnet.OWNER_LABEL: server.global_id,
                vmnet.VM_LABEL: vm_name,
            },
        },
        "spec": {"groups": [{
            "name": name,
            "rules": [{
                "alert": ALERT_NAME,
                "expr": rule_expression(namespace, vm_name),
                "for": FOR,
                "labels": {"severity": "critical", "name": vm_name, "cloudbolt_server": server.global_id},
                "annotations": {
                    "summary": "Apache is not answering on " + vm_name,
                    "description": "Port %d on %s stopped answering the readiness probe." % (PORT, vm_name),
                },
            }],
        }]},
    })
    return "updated" if existed else "created"


def report_status(cluster, server, namespace, vm_name):
    """Print what the cluster sees. Every part is best effort and never fails the job."""
    set_progress("---- Status ----")
    try:
        vmi = cluster.request("GET", VMI_PATH % (namespace, vm_name), allow_404=True)
        set_progress("VirtualMachineInstance: " + vmi_state(vmi))
    except Exception as error:
        set_progress("VirtualMachineInstance: could not be read (%s)" % error)
    try:
        pods = cluster.request("GET", PODS_PATH % namespace, params={"labelSelector": vmnet.VM_LABEL + "=" + vm_name})
        items = pods.get("items", [])
        if not items:
            set_progress("virt-launcher pod: none found")
        for pod in items:
            set_progress("virt-launcher pod %s: %s" % (pod["metadata"]["name"], describe_condition(condition(pod, "Ready"))))
    except Exception as error:
        set_progress("virt-launcher pod: could not be read (%s)" % error)

    try:
        from xui.io_cloudbolt_prometheus import kubevirt_metrics
    except ImportError:
        set_progress("Metrics check unavailable: the Monitoring UI extension (XUI-60382c9x) is not installed")
        return
    try:
        thanos = kubevirt_metrics.ThanosClient(server.resource_handler.cast(), vm_name)
        series = thanos.instant(ready_series(namespace, vm_name))
        if series:
            set_progress("kube_pod_status_ready: " + ", ".join(
                "%s = %s" % (item["metric"].get("pod", "?"), item["value"][1]) for item in series))
        else:
            set_progress("kube_pod_status_ready: no series (the pod is gone or metrics are not available)")
        firing = thanos.instant(rule_expression(namespace, vm_name))
        set_progress("Rule expression: " + ("true, the pod is not ready" if firing else "false or no data, the pod is ready"))
        data = thanos.get_data("/alerts")
        if data is not None:
            alerts = [
                (item.get("labels", {}), item.get("state"))
                for item in data.get("alerts", [])
            ]
        else:
            data = thanos.get_data("/query", {"query": 'ALERTS{alertname="' + ALERT_NAME + '", namespace="' + promql_string(namespace) + '"}'})
            if data is None:
                set_progress("Metrics check unavailable: Thanos could not be queried (the account may lack cluster-monitoring-view)")
                return
            alerts = [(item["metric"], item["metric"].get("alertstate")) for item in data.get("result", [])]
        mine = [state for labels, state in alerts
                if labels.get("alertname") == ALERT_NAME and labels.get("namespace") == namespace
                and labels.get("name") == vm_name]
        set_progress("%s alert: %s" % (ALERT_NAME, ", ".join(mine) if mine else "not pending or firing"))
    except Exception as error:
        set_progress("Metrics check unavailable: %s" % error)


def run(job, server=None, **kwargs):
    try:
        cluster, namespace = vmnet.for_server(server)
    except Exception as error:
        return "FAILURE", "", "This action works on OpenShift Virtualization VMs only: %s" % error
    vm_name = server.hostname
    set_progress("Enabling the %s alert for %s in namespace %s" % (ALERT_NAME, vm_name, namespace))

    try:
        probe_changed = ensure_probe(cluster, namespace, vm_name)
        restarted = False
        # The running instance, not the template, decides: a probe patched in by an earlier run that
        # could not restart the VM is still not in effect.
        running = cluster.request("GET", VMI_PATH % (namespace, vm_name), allow_404=True)
        in_effect = (((running or {}).get("spec", {}).get("readinessProbe") or {}).get("tcpSocket") or {}).get("port")
        if probe_changed or (running is not None and str(in_effect) != str(PORT)):
            problem, restarted = restart_and_wait(cluster, namespace, vm_name)
            if problem:
                report_status(cluster, server, namespace, vm_name)
                return "FAILURE", "", problem
        rule = apply_rule(cluster, namespace, vm_name, server)
    except vmnet.OpenShiftApiError as error:
        return failure(error)

    report_status(cluster, server, namespace, vm_name)
    summary = "Probe: %s. Rule: %s.\nRestarted: %s." % (
        "added" if probe_changed else "already set", rule, "yes" if restarted else "no")
    return "SUCCESS", summary, ""
