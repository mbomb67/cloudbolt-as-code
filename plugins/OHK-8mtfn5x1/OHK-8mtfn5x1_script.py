"""
Build item for the AWS EKS blueprint (BP-r5apcf7e): install the StormForge agent
on the new cluster and register it with StormForge.

Runs after the cluster build (Deploy EKS Cluster) with continue on failure: any
problem is reported as a warning so the cluster order still completes, and
the Install or Upgrade StormForge Agent action (RSA-76jmctg6) retries it.
All of the work is in shared_modules/SHM-2fd3bhsz (stormforge).

Entry point: run(job, resource, **kwargs) -> (status, output_msg, error_msg)
"""

from shared_modules import stormforge


def run(job, resource=None, **kwargs):
    return stormforge.run_build_step(job, resource, kwargs)
