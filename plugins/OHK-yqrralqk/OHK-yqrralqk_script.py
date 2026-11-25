"""Server action: runs a shell script on an OpenShift Virtualization VM over SSH tunnelled through the cluster API.

The tunnel lives in the kubevirt_api_tunnel shared module (shared_modules/SHM-wkubiao7). It needs no
route from CloudBolt to the guest, but the VM needs a pod network (masquerade) NIC as its first interface.
"""

from common.methods import set_progress
from shared_modules import kubevirt_api_tunnel

SCRIPT = """{{ script }}"""


def run(job, server=None, **kwargs):
    set_progress(f"Opening the API tunnel to {server.hostname}")
    try:
        exit_code, output = kubevirt_api_tunnel.run_script(server, SCRIPT)
    except kubevirt_api_tunnel.TunnelError as error:
        return "FAILURE", "", str(error)

    set_progress(output)
    if exit_code:
        return "FAILURE", output, f"The script exited with code {exit_code}"
    return "SUCCESS", output, ""
