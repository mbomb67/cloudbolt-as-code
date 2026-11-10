"""
CloudBolt day-2 resource action: Apply StormForge Recommendation.

One action on the AWS EKS Namespace (BP-e4c0q58z), AKS Namespace
(BP-dogqi827) and GKE Namespace (BP-c9ca1i3z) blueprints, for namespaces on a
cluster that runs the StormForge agent. For one workload in the namespace it
either:

  - applies StormForge's latest recommendation now: the patches StormForge
    publishes for the workload are sent to the Kubernetes API, the same
    strategic merge patch as the kubectl command StormForge shows; or
  - turns automatic deployment on or off, by setting the workload's
    live.stormforge.io/auto-deploy annotation, so StormForge applies each new
    recommendation itself (or stops doing so).

Only workloads StormForge reports in the resource's own namespace can be
chosen, and every Kubernetes call is addressed inside that namespace. The
StormForge tab on the namespace (extensions/XUI-wyauaxbl) starts this action
from a workload's page.

All of the work is in shared_modules/SHM-2fd3bhsz (stormforge).

Vendor references:
  https://docs.stormforge.io/optimize-live/recommendations/apply/
  https://docs.stormforge.io/docs/configure/settings/apply-settings/
  https://kubernetes.io/docs/reference/using-api/api-concepts/#patch-and-apply

Action Inputs:
  - stormforge_workload (STR, required): the workload, as type/name.
  - stormforge_apply_mode (STR, required): now, auto_on or auto_off.

Entry point: run(job, resource, **kwargs) -> (status, output_msg, error_msg)
"""

from common.methods import set_progress
from utilities.logger import ThreadLogger

from shared_modules import stormforge

logger = ThreadLogger(__name__)


def generate_options_for_stormforge_workload(field=None, **kwargs):
    resource = kwargs.get("resource")
    if resource is None:
        return []
    try:
        options = stormforge.workload_options(resource)
    except Exception as exc:  # noqa: BLE001 - show the reason in the dropdown instead of a blank form
        logger.exception("Could not list StormForge workloads for resource %s", resource.id)
        return [("", f"Cannot list the namespace's workloads: {exc}")]
    if not options:
        return [("", "StormForge reports no workloads in this namespace yet")]
    return {"options": options, "sort": False}


def generate_options_for_stormforge_apply_mode(field=None, **kwargs):
    return {"options": list(stormforge.APPLY_MODES), "initial_value": stormforge.APPLY_NOW, "sort": False}


def run(job, resource=None, **kwargs):
    resource = stormforge._resolve_resource(job, resource, kwargs)
    if resource is None:
        return "FAILURE", "", "No namespace resource was supplied."
    workload = "{{ stormforge_workload }}".strip()
    mode = "{{ stormforge_apply_mode }}".strip()
    try:
        return stormforge.apply_to_workload(resource, workload, mode, progress=set_progress)
    except stormforge.StormForgeError as exc:
        return "FAILURE", "", str(exc)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Apply StormForge Recommendation failed for resource %s", resource.id)
        return "FAILURE", "", f"Applying the StormForge recommendation failed: {exc}"
