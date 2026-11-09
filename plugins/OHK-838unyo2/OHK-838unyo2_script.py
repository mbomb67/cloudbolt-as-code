"""
Teardown item for the Azure Kubernetes Service blueprint (BP-20q58q9z): after the cluster is
deleted, revoke the StormForge credential CloudBolt registered for it and
delete the cluster in StormForge.

Runs after the cluster teardown (AKS Cluster Teardown), so a cluster that failed
to delete stays registered. A StormForge problem is a warning, since the
cluster is already gone. All of the work is in shared_modules/SHM-2fd3bhsz
(stormforge).

Entry point: run(job, resource, **kwargs) -> (status, output_msg, error_msg)
"""

from shared_modules import stormforge


def run(job, resource=None, **kwargs):
    return stormforge.run_teardown_step(job, resource, kwargs)
