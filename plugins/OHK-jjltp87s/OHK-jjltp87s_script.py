"""Blueprint build item: makes OpenShift raise an ApacheDown alert for one tier's VM.

Finds the tier's VM and calls shared_modules/SHM-sud926fb (openshift_vm_alerts): a readiness probe on
the VirtualMachine, one restart when the running VM lacks it, a PrometheusRule, and delivery to the
Monitoring Alerts webhook. Put it last in the blueprint, because it restarts the VM.

Inputs: tier (the server build item's name).
"""

from shared_modules import openshift_vm_alerts
from shared_modules import vm_tier_scripts as tiers

TIER = tiers.clean("{{ tier }}")


def run(job, resource=None, **kwargs):
    if not TIER:
        return "FAILURE", "", "Set this build item's Tier."
    try:
        server = tiers.tier_server(resource, TIER)
    except tiers.TierError as error:
        return "FAILURE", "", str(error)
    return openshift_vm_alerts.enable_apache_alert(server, job)
