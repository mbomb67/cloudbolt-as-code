"""Makes OpenShift raise an ApacheDown alert for an OpenShift Virtualization VM, and takes it away again.

    from shared_modules import openshift_vm_alerts as alerts

    status, output, errors = alerts.enable_apache_alert(server, job)   # build item or server action
    notes, failed = alerts.remove_apache_alert(server)                          # teardown; returns (notes, failed), never raises

enable_apache_alert works through the handler's own credential, on objects in the VM's namespace:
  1. a readiness probe on the VirtualMachine (TCP port 443), so the virt-launcher pod goes not-ready
     when nothing listens on the port;
  2. a restart of the VM, only when the running instance does not have the probe yet, so the template
     change takes effect;
  3. a PrometheusRule named apache-down-<vm>, evaluated by Thanos Ruler, that fires ApacheDown with the
     labels the Monitoring Alerts webhook (webhooks/IWH-i7b44zvs) maps back to this server;
  4. delivery: a Secret holding the webhook's URL and an AlertmanagerConfig that routes the alert to it, so
     the cluster's own Alertmanager posts the alert to CloudBolt. Cluster-wide monitoring settings are
     never changed; the status block says whether user-defined alert routing is switched on.

Every run ends with a status block that shows what the cluster sees. Safe to run again. The Secret and
the AlertmanagerConfig are shared by every VM of the namespace, so remove_apache_alert deletes only the
per-VM PrometheusRule.
"""

import re
import time
from urllib.parse import quote

from common.methods import set_progress
from shared_modules import openshift_vm_network as vmnet

PORT = 443
ALERT_NAME = "ApacheDown"
FOR = "1m"
PROBE_TIMINGS = {"initialDelaySeconds": 30, "periodSeconds": 10, "timeoutSeconds": 5, "failureThreshold": 3}
BOOT_GRACE_SECONDS = 300
WEBHOOK_URI_PATH = "monitoring-alerts"
WEBHOOK_RUN_PATH = "/api/v3/cmp/inboundWebHooks/monitoring-alerts/run/"
SECRET_NAME = "cloudbolt-alert-webhook"
ALERTMANAGER_CONFIG_NAME = "cloudbolt-alerts"
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
SECRETS_PATH = "/api/v1/namespaces/%s/secrets"
MONITORING_GROUP_PATH = "/apis/monitoring.coreos.com"
# https://prometheus-operator.dev/docs/api-reference/api/ (AlertmanagerConfig, v1beta1 and v1alpha1)
ALERTMANAGER_CONFIGS_PATH = "/apis/monitoring.coreos.com/%s/namespaces/%s/alertmanagerconfigs"
CONFIGMAP_PATH = "/api/v1/namespaces/%s/configmaps/%s"
USER_WORKLOAD_NAMESPACE = "openshift-user-workload-monitoring"

RULE_PERMISSION = (
    "The resource handler's account is not allowed to manage PrometheusRules in this namespace. "
    "Grant it the monitoring-rules-edit role in the namespace: "
    "oc adm policy add-role-to-user monitoring-rules-edit <account> -n <namespace>. "
    "https://docs.redhat.com/en/documentation/openshift_container_platform/4.19/html/monitoring/managing-alerts"
)
ROUTING_PERMISSION = (
    "The resource handler's account is not allowed to manage this object in the namespace. "
    "Grant it the alert-routing-edit role in the namespace: "
    "oc adm policy add-role-to-user alert-routing-edit <account> -n <namespace>. "
    "https://docs.redhat.com/en/documentation/openshift_container_platform/4.19/html/monitoring/managing-alerts"
)
SECRET_PERMISSION = (
    "The resource handler's account is not allowed to manage Secrets in this namespace. "
    "Grant it the edit role in the namespace, or any role that may create and update Secrets: "
    "oc adm policy add-role-to-user edit <account> -n <namespace>."
)
ROUTING_FIX = (
    "A cluster admin turns user-defined alert routing on in the ConfigMap user-workload-monitoring-config "
    "(namespace openshift-user-workload-monitoring), under config.yaml:\n"
    "alertmanager:\n  enabled: true\n  enableAlertmanagerConfig: true\n"
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


def pod_age(namespace, vm_name):
    """Seconds since the VM's pod started. kube_pod_start_time carries the same namespace and pod labels, see
    https://github.com/kubernetes/kube-state-metrics/blob/main/docs/metrics/workload/pod-metrics.md"""
    return (
        'time() - kube_pod_start_time{namespace="' + promql_string(namespace)
        + '", pod=~"' + promql_string(pod_pattern(vm_name)) + '"}'
    )


def rule_expression(namespace, vm_name):
    """Pod not ready, and older than the boot grace period so a VM that is still starting does not alert."""
    return (
        "(" + ready_series(namespace, vm_name) + " == 0) and on (namespace, pod) ("
        + pod_age(namespace, vm_name) + " > %d)" % BOOT_GRACE_SECONDS
    )


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
    if current and all(current.get(key) == value for key, value in wanted.items()):
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
        try:
            vmi = cluster.request("GET", VMI_PATH % (namespace, vm_name), allow_404=True)
        except vmnet.OpenShiftApiError as error:
            state = "the VM could not be read (%s)" % error
            set_progress("Waiting for the restarted VM: " + state)
            continue
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


def webhook_url():
    """(url with token, url without query string, problem). The token is read from the Monitoring Alerts webhook."""
    from cbhooks.models import InboundWebHook
    from shared_modules import alert_escalation

    hook = InboundWebHook.objects.filter(uri_path=WEBHOOK_URI_PATH).first()
    token = (getattr(hook, "token", "") or "").strip()
    if not token:
        return None, None, "Delivery not configured: set a token on the Monitoring Alerts webhook, then run this action again"
    base = alert_escalation.absolute_url(WEBHOOK_RUN_PATH)
    if "://" not in base:
        return None, None, "Delivery not configured: CloudBolt has no site URL (Admin > Portals), so the webhook address is unknown"
    return base + "?token=" + quote(token, safe=""), base, None


def alertmanager_version(cluster):
    """The AlertmanagerConfig API version the cluster serves: v1beta1, else v1alpha1."""
    group = cluster.request("GET", MONITORING_GROUP_PATH)
    served = [item.get("version") for item in group.get("versions", [])]
    for version in ("v1beta1", "v1alpha1"):
        if version in served:
            return version
    return None


def alertmanager_config(version, namespace):
    """Both versions use the same field names for what is set here: route with matchers (name, value,
    matchType) and receivers with webhookConfigs (urlSecret, sendResolved).
    https://prometheus-operator.dev/docs/api-reference/api/"""
    return {
        "apiVersion": "monitoring.coreos.com/" + version,
        "kind": "AlertmanagerConfig",
        "metadata": {
            "name": ALERTMANAGER_CONFIG_NAME,
            "namespace": namespace,
            "labels": {vmnet.MANAGED_BY_LABEL: vmnet.MANAGED_BY},
        },
        "spec": {
            "route": {
                "receiver": "cloudbolt",
                "groupBy": ["alertname", "cloudbolt_server"],
                "groupWait": "10s",
                "groupInterval": "1m",
                "repeatInterval": "1h",
                "matchers": [{"name": "cloudbolt_server", "value": ".+", "matchType": "=~"}],
            },
            "receivers": [{
                "name": "cloudbolt",
                "webhookConfigs": [{
                    "urlSecret": {"name": SECRET_NAME, "key": "url"},
                    "sendResolved": False,
                }],
            }],
        },
    }


def configmap_text(cluster, namespace, name):
    """config.yaml of a ConfigMap; None when it does not exist, False when it cannot be read."""
    try:
        item = cluster.request("GET", CONFIGMAP_PATH % (namespace, name), allow_404=True)
    except vmnet.OpenShiftApiError:
        return False
    if item is None:
        return None
    return (item.get("data") or {}).get("config.yaml", "")


def routing_state(cluster):
    """(on, off or unknown, detail lines). User-defined alert routing needs the user-workload Alertmanager.
    https://docs.redhat.com/en/documentation/openshift_container_platform/4.19/html/monitoring/managing-alerts"""
    details = []
    pods = None
    try:
        listing = cluster.request(
            "GET", "/api/v1/namespaces/%s/pods" % USER_WORKLOAD_NAMESPACE,
            params={"labelSelector": "app.kubernetes.io/name=alertmanager"})
        pods = [item["metadata"]["name"] for item in listing.get("items", [])]
        details.append("alertmanager-user-workload pods: %s" % (", ".join(pods) if pods else "none"))
    except vmnet.OpenShiftApiError:
        details.append("alertmanager-user-workload pods: not readable with this account")
    user_text = configmap_text(cluster, USER_WORKLOAD_NAMESPACE, "user-workload-monitoring-config")
    platform_text = configmap_text(cluster, "openshift-monitoring", "cluster-monitoring-config")
    for label, text in (("user-workload-monitoring-config", user_text), ("cluster-monitoring-config", platform_text)):
        if text is False:
            details.append("%s: not readable with this account" % label)
        elif text is None:
            details.append("%s: not present" % label)
        else:
            flags = [key for key in ("alertmanager", "enableAlertmanagerConfig", "enableUserAlertmanagerConfig")
                     if re.search(key + ":", text)]
            enabled = re.search(r"(enableAlertmanagerConfig|enableUserAlertmanagerConfig):\s*true", text)
            details.append("%s: %s%s" % (label, ", ".join(flags) if flags else "no routing settings",
                                          " (routing enabled)" if enabled else ""))
    if pods:
        return "on", details
    if pods is not None and user_text is not False:
        return "off", details
    return "unknown", details


def deliver(cluster, namespace):
    """Create the Secret and AlertmanagerConfig; never raises. Returns a short outcome for the summary."""
    try:
        url, shown, problem = webhook_url()
    except Exception as error:
        url, shown, problem = None, None, "Delivery not configured: the Monitoring Alerts webhook could not be read (%s)" % error
    if problem:
        set_progress(problem)
        return "not configured"
    stage = "secret"
    try:
        set_progress("Creating or replacing the Secret %s holding the webhook URL (%s)" % (SECRET_NAME, shown))
        cluster.apply(SECRETS_PATH % namespace, {
            "apiVersion": "v1", "kind": "Secret",
            "metadata": {"name": SECRET_NAME, "namespace": namespace, "labels": {vmnet.MANAGED_BY_LABEL: vmnet.MANAGED_BY}},
            "type": "Opaque",
            "stringData": {"url": url},
        })
        stage = "config"
        version = alertmanager_version(cluster)
        if version is None:
            set_progress("Delivery not set up: the cluster serves no AlertmanagerConfig API (monitoring.coreos.com v1beta1 or v1alpha1)")
            return "no AlertmanagerConfig API"
        set_progress("Creating or replacing the AlertmanagerConfig %s (%s)" % (ALERTMANAGER_CONFIG_NAME, version))
        cluster.apply(ALERTMANAGER_CONFIGS_PATH % (version, namespace), alertmanager_config(version, namespace))
    except Exception as error:
        # The URL carries the webhook token; keep it out of the job log.
        text = str(error).replace(url, shown)
        if "(403)" in text:
            text += " " + (SECRET_PERMISSION if stage == "secret" else ROUTING_PERMISSION)
        set_progress("Delivery not set up: " + text)
        return "refused"
    set_progress("Delivery set up: Alertmanager will post ApacheDown to %s" % shown)
    return "configured"


def report_delivery(cluster, namespace):
    """The Delivery part of the status block; best effort."""
    set_progress("Delivery:")
    try:
        found = cluster.request("GET", SECRETS_PATH % namespace + "/" + SECRET_NAME, allow_404=True)
        set_progress("  Secret %s: %s" % (SECRET_NAME, "present" if found is not None else "missing"))
    except Exception as error:
        set_progress("  Secret %s: not readable (%s)" % (SECRET_NAME, error))
    try:
        version = alertmanager_version(cluster)
        found = None
        if version:
            found = cluster.request(
                "GET", ALERTMANAGER_CONFIGS_PATH % (version, namespace) + "/" + ALERTMANAGER_CONFIG_NAME, allow_404=True)
        set_progress("  AlertmanagerConfig %s: %s" % (ALERTMANAGER_CONFIG_NAME, "present" if found is not None else "missing"))
    except Exception as error:
        set_progress("  AlertmanagerConfig %s: not readable (%s)" % (ALERTMANAGER_CONFIG_NAME, error))
    try:
        from urllib.parse import urlparse

        _, shown, _ = webhook_url()
        set_progress("  Webhook host the cluster posts to: %s" % (urlparse(shown).netloc if shown else "not configured"))
    except Exception as error:
        set_progress("  Webhook host: unknown (%s)" % error)
    try:
        state, details = routing_state(cluster)
    except Exception as error:
        state, details = "unknown", ["could not be checked (%s)" % error]
    set_progress("  User-defined alert routing: %s" % state)
    for line in details:
        set_progress("    " + line)
    if state != "on":
        set_progress(ROUTING_FIX)


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

    report_delivery(cluster, namespace)
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
        set_progress("Rule expression: " + ("true, the pod is not ready" if firing else "false or no data, the pod is ready or still booting"))
        age = thanos.instant_value(pod_age(namespace, vm_name))
        set_progress("Pod age (the rule waits for %ds): %s" % (
            BOOT_GRACE_SECONDS, "no data" if age is None else "%ds" % float(age)))
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


def enable_apache_alert(server, job=None):
    """Set up the probe, rule and delivery for one VM; return (status, output, errors) like a plug-in's run()."""
    try:
        cluster, namespace = vmnet.for_server(server)
    except Exception as error:
        return "FAILURE", "", "This works on OpenShift Virtualization VMs only: %s" % error
    vm_name = server.hostname
    set_progress("Enabling the %s alert for %s in namespace %s" % (ALERT_NAME, vm_name, namespace))

    timeout = None
    try:
        probe_changed = ensure_probe(cluster, namespace, vm_name)
        restarted = False
        # The running instance, not the template, decides: a probe patched in by an earlier run that
        # could not restart the VM is still not in effect.
        running = cluster.request("GET", VMI_PATH % (namespace, vm_name), allow_404=True)
        in_effect = (((running or {}).get("spec", {}).get("readinessProbe") or {}).get("tcpSocket") or {}).get("port")
        if running is not None and str(in_effect) != str(PORT):
            timeout, restarted = restart_and_wait(cluster, namespace, vm_name)
        rule = apply_rule(cluster, namespace, vm_name, server)
    except vmnet.OpenShiftApiError as error:
        return failure(error)
    except Exception as error:
        return "FAILURE", "", "%s: %s" % (type(error).__name__, error)

    delivery = deliver(cluster, namespace)
    report_status(cluster, server, namespace, vm_name)
    if timeout:
        return "FAILURE", "", timeout
    summary = "Probe: %s. Rule: %s. Delivery: %s.\nRestarted: %s." % (
        "updated" if probe_changed else "already set", rule, delivery, "yes" if restarted else "no")
    return "SUCCESS", summary, ""


def remove_apache_alert(server):
    """Delete the per-VM PrometheusRule; return (note lines, failed). A missing rule is fine; nothing is raised."""
    try:
        cluster, namespace = vmnet.for_server(server)
        name = "apache-down-" + server.hostname
        cluster.request("DELETE", RULES_PATH % namespace + "/" + name, allow_404=True)
        return ["Removed the PrometheusRule %s from %s if it existed" % (name, namespace)], False
    except Exception as error:
        return ["Could not remove the Apache alert rule of %s: %s" % (getattr(server, "hostname", "?"), error)], True
