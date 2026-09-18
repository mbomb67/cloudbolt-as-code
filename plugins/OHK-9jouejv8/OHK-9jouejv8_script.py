"""
Delete an Azure network security group.
"""
from azure.common.credentials import ServicePrincipalCredentials
from azure.mgmt.network import NetworkManagementClient
from msrestazure.azure_exceptions import CloudError

from common.methods import set_progress
from resourcehandlers.azure_arm.models import AzureARMHandler
from azure.identity import ClientSecretCredential

def get_tenant_id_for_azure(handler):
    '''
        Handling Azure RH table changes for older and newer versions (> 9.4.5)
    '''
    if hasattr(handler,"azure_tenant_id"):
        return handler.azure_tenant_id

    return handler.tenant_id


def run(job, **kwargs):
    resource = kwargs.pop("resources").first()
    
    cf_value = resource.get_cf_values_as_dict()
    
    if cf_value.get("azure_network_security_group_id", None) is None:
        return "SUCCESS", "Network security group deleted successfully.", ""
    
    azure_network_security_group = cf_value.get("azure_network_security_group", None)
    resource_group = cf_value.get("resource_group_name", None)
    rh_id = cf_value.get("azure_rh_id", None)
    rh = AzureARMHandler.objects.get(id=rh_id)

    set_progress("Connecting To Azure networking...")
    credentials = ClientSecretCredential(
        client_id=rh.client_id,
        client_secret=rh.secret,
        tenant_id=get_tenant_id_for_azure(rh)
    )
    network_client = NetworkManagementClient(credentials, rh.serviceaccount)
    set_progress("Connection to Azure networking established")

    set_progress("Deleting network security group %s..." % (azure_network_security_group))
    
    try:
        network_client.network_security_groups.begin_delete(
            resource_group_name=resource_group,
            network_security_group_name=azure_network_security_group,
        )
    except CloudError as e:
        set_progress("Azure Clouderror: {}".format(e))
        return "FAILURE", "Network security group could not be deleted", ""

    return "SUCCESS", "The network security group has been succesfully deleted", ""