"""Server action: makes OpenShift raise an ApacheDown alert when Apache stops answering inside a VM.

All of the work is in shared_modules/SHM-sud926fb (openshift_vm_alerts): a readiness probe on the
VirtualMachine, one restart when the running VM lacks it, a PrometheusRule, and delivery to the
Monitoring Alerts webhook. Safe to run again.
"""

from shared_modules import openshift_vm_alerts


def run(job, server=None, **kwargs):
    return openshift_vm_alerts.enable_apache_alert(server, job)
