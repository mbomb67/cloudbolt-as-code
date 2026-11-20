"""Blueprint teardown item: removes what a 3-tier web app created outside its VMs.

Deletes every Route, Service and NetworkPolicy labelled with the resource
(shared_modules/SHM-5t7bom9y) and the web tier's certificate in Azure Key Vault
(shared_modules/SHM-ny8h0nx0). CloudBolt deletes the VMs itself. Safe to run twice: whatever is
already gone is skipped, and a failed part is reported as a warning so the VMs are still removed.
"""

from common.methods import set_progress
from infrastructure.models import Environment
from shared_modules import azure_key_vault_certificates as kv
from shared_modules import openshift_vm_network as vmnet


def _delete_cluster_objects(resource):
    # Teardown runs before CloudBolt deletes the VMs, so they say where the objects are; the
    # values the Route step stored cover a resource whose VMs are already gone.
    targets = set()
    for server in resource.server_set.all():
        info = getattr(server, "ovirtserverinfo", None)
        if info is not None and info.namespace and server.resource_handler_id:
            targets.add((server.resource_handler_id, info.namespace))
    namespace = resource.get_value_for_custom_field("web_app_namespace")
    handler_id = resource.get_value_for_custom_field("web_app_openshift_rh_id")
    if namespace and handler_id:
        targets.add((int(handler_id), namespace))
    if not targets:
        return "This resource has no OpenShift VMs, so there are no Services, Routes or NetworkPolicies to delete."

    messages = []
    for handler_id, namespace in sorted(targets):
        deleted = vmnet.delete_owned(vmnet.for_handler_id(handler_id), namespace, resource)
        messages.append(f"Deleted in namespace {namespace}: {', '.join(deleted) or 'nothing left to delete'}.")
    return " ".join(messages)


def _delete_certificate(resource):
    certificate_id = resource.get_value_for_custom_field("web_app_certificate")
    vault = resource.get_value_for_custom_field("web_app_key_vault")
    env_id = resource.get_value_for_custom_field("web_app_azure_env_id")
    if not certificate_id or not vault or not env_id:
        return "No Key Vault certificate was recorded for this resource."
    environment = Environment.objects.filter(id=int(env_id)).first()
    if not environment or not environment.resource_handler:
        raise kv.KeyVaultError(f"The Azure environment that issued {certificate_id} no longer exists.")
    # A certificate id is <vault>/certificates/<name>/<version>.
    name = certificate_id.split("/certificates/")[1].split("/")[0]
    if kv.delete_certificate(environment.resource_handler.cast(), vault, name):
        return f"Deleted certificate {name} from {vault}."
    return f"Certificate {name} was already gone from {vault}."


def run(job, resource=None, **kwargs):
    if resource is None:
        return "WARNING", "", "No resource to tear down."
    done, problems = [], []
    for step in (_delete_cluster_objects, _delete_certificate):
        try:
            message = step(resource)
            set_progress(message)
            done.append(message)
        except Exception as error:  # noqa: BLE001 - report it and let the VMs be deleted
            set_progress(f"Could not finish: {error}")
            problems.append(str(error))
    if problems:
        return "WARNING", " ".join(done), " ".join(problems)
    return "SUCCESS", " ".join(done), ""
