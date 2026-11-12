"""
CloudBolt day-2 resource action: Set StormForge Cluster Defaults.

One action on the AWS EKS (BP-r5apcf7e), Azure Kubernetes Service
(BP-20q58q9z) and Google Kubernetes Engine (BP-g8g8aecx) blueprints, for
clusters that run the StormForge agent. It writes the chosen optimization
settings to the cluster-defaults ConfigMap in the stormforge-system namespace
and restarts the StormForge cluster agent, which reads that ConfigMap only
when it starts. The values are the cluster's defaults: a namespace or a
workload that sets its own value overrides them.

Settings already in the ConfigMap that this action has no input for stay as
they are; comments in the file are not kept.

All of the work is in shared_modules/SHM-2fd3bhsz (stormforge).

Vendor references:
  https://docs.stormforge.io/docs/configure/cluster-defaults/
  https://docs.stormforge.io/docs/configure/configure-opt-settings/
  https://docs.stormforge.io/docs/configure/settings/

Action Inputs:
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
        return "FAILURE", "", "No cluster resource was supplied."
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
        return stormforge.set_cluster_defaults(resource, values, progress=set_progress)
    except stormforge.StormForgeError as exc:
        return "FAILURE", "", str(exc)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Set StormForge Cluster Defaults failed for resource %s", resource.id)
        return "FAILURE", "", f"Setting the StormForge cluster defaults failed: {exc}"
