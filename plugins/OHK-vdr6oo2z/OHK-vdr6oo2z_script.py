"""
Teardown item shared by the AWS EKS (BP-r5apcf7e), Azure Kubernetes Service
(BP-20q58q9z) and Google Kubernetes Engine (BP-g8g8aecx) blueprints: after the
cluster is deleted, remove it from StormForge.

Runs after the blueprint's cluster teardown item, so a cluster that failed to
delete stays registered. A StormForge problem is a warning, since the cluster
is already gone. All of the work is in shared_modules/SHM-2fd3bhsz
(stormforge).

Entry point: run(job, resource, **kwargs) -> (status, output_msg, error_msg)
"""

from shared_modules import stormforge


def run(job, resource=None, **kwargs):
    return stormforge.run_teardown_step(job, resource, kwargs)
