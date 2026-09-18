"""
Runs a script on the node labeled as "controller" to launch a Helm chart
"""
from common.methods import set_progress
from utilities.logger import ThreadLogger

logger = ThreadLogger(__name__)


def run(job, resource=None, *args, **kwargs):
    logger.debug(f'kwargs: {kwargs}')
    release_name = "{{release_name}}"
    namespace = "{{namespace}}"
    chart = "{{chart}}"
    server = resource.server_set.filter(tags__name="controller").first()
    if not server:
        set_progress("No controller node found for this resource.")
        return "FAILURE", "", "No controller node found."
    helm_script = get_script(release_name, namespace, chart)
    set_progress("Executing script on controller node to install Helm chart.")
    result = server.execute_script(script_contents=helm_script)
    return "SUCCESS", result, ""


def get_script(release_name, namespace, chart):
    return f"""#!/bin/bash
    helm install "{release_name}" "{chart}" --namespace "{namespace}" --create-namespace
    """