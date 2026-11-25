"""
CloudBolt day-2 resource action: Set StormForge Optimization Settings.

One action on the AWS EKS Namespace (BP-e4c0q58z), AKS Namespace
(BP-dogqi827) and GKE Namespace (BP-c9ca1i3z) blueprints, for namespaces on a
cluster that runs the StormForge agent. It sets the chosen optimization
settings as live.stormforge.io annotations, either on the namespace, where
they apply to every workload in it, or on one workload. A workload's own
value overrides its namespace's, which overrides the cluster's defaults.

Only the resource's own namespace and workloads StormForge reports in it can
be chosen, and every Kubernetes call is addressed inside that namespace.

All of the work is in shared_modules/SHM-2fd3bhsz (stormforge).

Vendor references:
  https://docs.stormforge.io/docs/configure/annotate-namespaces/
  https://docs.stormforge.io/docs/configure/annotate-workloads/
  https://docs.stormforge.io/docs/configure/settings/

Action Inputs:
  - stormforge_settings_target (STR, required): namespace, or a workload as
    type/name.
  - stormforge_cpu_goal, stormforge_memory_goal (STR): Savings, Balanced or Reliability.
  - stormforge_schedule (STR): a schedule macro, an ISO 8601 duration or a cron expression.
  - stormforge_auto_deploy (STR): Enabled or Disabled.
  - stormforge_cpu_policy, stormforge_memory_policy (STR): the optimization policy.
  - stormforge_cpu_requests_min, stormforge_cpu_requests_max,
    stormforge_memory_requests_min, stormforge_memory_requests_max (STR):
    Kubernetes quantities, optionally followed by container=quantity entries.
  Every setting is optional. A dropdown left on Leave as it is, or a text
  field left empty, keeps the current value; Remove, or a - in a text field,
  removes the setting.

Entry point: run(job, resource, **kwargs) -> (status, output_msg, error_msg)
"""

from common.methods import set_progress
from utilities.logger import ThreadLogger

from shared_modules import stormforge

logger = ThreadLogger(__name__)


def _choices(name):
    try:
        return {"options": stormforge.setting_options(name), "initial_value": stormforge.SETTING_KEEP, "sort": False}
    except AttributeError:
        # The web server still runs the shared module from before this action was synced.
        return [("", "Restart the CloudBolt web server to load the updated StormForge shared module")]


def generate_options_for_stormforge_settings_target(field=None, **kwargs):
    resource = kwargs.get("resource")
    if resource is None:
        return []
    try:
        options = stormforge.settings_target_options(resource)
    except AttributeError:
        return [("", "Restart the CloudBolt web server to load the updated StormForge shared module")]
    except Exception as exc:  # noqa: BLE001 - show the reason in the dropdown instead of a blank form
        logger.exception("Could not list StormForge settings targets for resource %s", resource.id)
        return [("", f"Cannot list the namespace's workloads: {exc}")]
    return {"options": options, "initial_value": stormforge.TARGET_NAMESPACE, "sort": False}


def generate_options_for_stormforge_cpu_goal(field=None, **kwargs):
    return _choices("stormforge_cpu_goal")


def generate_options_for_stormforge_memory_goal(field=None, **kwargs):
    return _choices("stormforge_memory_goal")


def generate_options_for_stormforge_auto_deploy(field=None, **kwargs):
    return _choices("stormforge_auto_deploy")


def generate_options_for_stormforge_cpu_policy(field=None, **kwargs):
    return _choices("stormforge_cpu_policy")


def generate_options_for_stormforge_memory_policy(field=None, **kwargs):
    return _choices("stormforge_memory_policy")


def run(job, resource=None, **kwargs):
    resource = stormforge._resolve_resource(job, resource, kwargs)
    if resource is None:
        return "FAILURE", "", "No namespace resource was supplied."
    target = "{{ stormforge_settings_target }}".strip()
    values = {
        "stormforge_cpu_goal": "{{ stormforge_cpu_goal }}",
        "stormforge_memory_goal": "{{ stormforge_memory_goal }}",
        "stormforge_schedule": "{{ stormforge_schedule }}",
        "stormforge_auto_deploy": "{{ stormforge_auto_deploy }}",
        "stormforge_cpu_policy": "{{ stormforge_cpu_policy }}",
        "stormforge_memory_policy": "{{ stormforge_memory_policy }}",
        "stormforge_cpu_requests_min": "{{ stormforge_cpu_requests_min }}",
        "stormforge_cpu_requests_max": "{{ stormforge_cpu_requests_max }}",
        "stormforge_memory_requests_min": "{{ stormforge_memory_requests_min }}",
        "stormforge_memory_requests_max": "{{ stormforge_memory_requests_max }}",
    }
    try:
        return stormforge.set_namespace_settings(resource, target, values, progress=set_progress)
    except stormforge.StormForgeError as exc:
        return "FAILURE", "", str(exc)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Set StormForge Optimization Settings failed for resource %s", resource.id)
        return "FAILURE", "", f"Setting the StormForge optimization settings failed: {exc}"
