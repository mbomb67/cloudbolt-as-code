"""
CloudBolt day-2 resource action: Install or Upgrade StormForge Agent.

One action on the AWS EKS (BP-r5apcf7e), Azure Kubernetes Service
(BP-20q58q9z) and Google Kubernetes Engine (BP-g8g8aecx) cluster blueprints.
It installs the StormForge agent on a cluster that does not have it (a
discovered cluster, or one whose build item warned), upgrades or downgrades
it to the chosen chart version, or reinstalls the same version. The version
dropdown shows what is installed now and what each choice changes it to, for
example "Upgrade 3.4.0 -> 3.5.0 (latest)".

All of the work is in shared_modules/SHM-2fd3bhsz (stormforge): StormForge
API calls, the agent's credential, and the in-cluster Helm job.

Action Inputs:
  - stormforge_target_version (STR, required): StormForge chart version.

Entry point: run(job, resource, **kwargs) -> (status, output_msg, error_msg)
"""

from common.methods import set_progress
from utilities.logger import ThreadLogger

from shared_modules import stormforge

logger = ThreadLogger(__name__)


def generate_options_for_stormforge_target_version(field=None, **kwargs):
    resource = kwargs.get("resource")
    if resource is None:
        return []
    try:
        options = stormforge.version_options(resource)
    except Exception as exc:  # noqa: BLE001 - show the reason in the dropdown instead of a blank form
        logger.exception("Could not list StormForge versions for resource %s", resource.id)
        return [("", f"Cannot list StormForge versions: {exc}")]
    return {"options": options, "initial_value": options[0][0], "sort": False}


def run(job, resource=None, **kwargs):
    resource = stormforge._resolve_resource(job, resource, kwargs)
    if resource is None:
        return "FAILURE", "", "No cluster resource was supplied."
    version = "{{ stormforge_target_version }}".strip()
    if not version:
        return "FAILURE", "", "Choose a StormForge version."
    try:
        return stormforge.install_or_upgrade(resource, version, progress=set_progress)
    except stormforge.StormForgeError as exc:
        return "FAILURE", "", str(exc)
    except Exception as exc:  # noqa: BLE001
        logger.exception("StormForge install or upgrade failed for resource %s", resource.id)
        return "FAILURE", "", f"StormForge install or upgrade failed: {exc}"
