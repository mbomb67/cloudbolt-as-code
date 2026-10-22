"""
Deletes the Azure network security group behind the resource and waits for
Azure to finish, so an NSG Azure refuses to delete (one still associated with
a subnet or network interface) fails the teardown with Azure's reason instead
of being left behind unnoticed. An NSG that is already gone counts as deleted.

Azure is reached through the API wrapper of the resource handler recorded on
the resource (azure_rh_id).
  https://learn.microsoft.com/en-us/python/api/azure-mgmt-network/azure.mgmt.network.operations.networksecuritygroupsoperations#azure-mgmt-network-operations-networksecuritygroupsoperations-begin-delete
  https://learn.microsoft.com/en-us/azure/virtual-network/manage-network-security-group#delete-a-network-security-group
"""
from azure.core.exceptions import HttpResponseError, ResourceNotFoundError

from common.methods import set_progress
from resourcehandlers.azure_arm.models import AzureARMHandler


def _resource(job, kwargs):
    resource = kwargs.get("resource")
    if resource is None and kwargs.get("resources") is not None:
        resource = kwargs["resources"].first()
    return resource or job.resource_set.first()


def _azure_message(exc):
    """Azure's error message and code from the ARM error body, else the SDK's text."""
    error = getattr(exc, "error", None)
    if error is not None and getattr(error, "message", None):
        return f"{error.message} ({error.code})" if getattr(error, "code", None) else error.message
    return getattr(exc, "message", None) or str(exc)


def run(job, **kwargs):
    resource = _resource(job, kwargs)
    if resource is None:
        return "FAILURE", "", "No resource is attached to this teardown job."

    values = resource.get_cf_values_as_dict()
    if not values.get("azure_network_security_group_id"):
        return "SUCCESS", "No Azure network security group is recorded on this resource; nothing to delete.", ""

    name = values.get("azure_network_security_group")
    resource_group = values.get("resource_group_name")
    rh = AzureARMHandler.objects.filter(id=values.get("azure_rh_id")).first()
    if rh is None:
        return (
            "FAILURE",
            "",
            f"The Azure resource handler recorded on this resource (ID {values.get('azure_rh_id')}) no longer "
            f"exists, so network security group '{name}' cannot be deleted. Delete it in Azure, then delete "
            "this resource again.",
        )

    set_progress(f"Deleting network security group '{name}' from resource group '{resource_group}'")
    network_client = rh.get_api_wrapper().network_client
    try:
        network_client.network_security_groups.begin_delete(resource_group, name).result()
    except ResourceNotFoundError:
        return "WARNING", f"Network security group '{name}' was already gone from Azure.", ""
    except HttpResponseError as exc:
        return "FAILURE", "", f"Azure could not delete network security group '{name}': {_azure_message(exc)}"

    return "SUCCESS", f"Network security group '{name}' has been deleted.", ""
