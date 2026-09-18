"""
Synchronize Azure network security groups
"""
import azure.mgmt.resource.resources as resources
from azure.mgmt.network import NetworkManagementClient
from msrestazure.azure_exceptions import CloudError
from azure.core.exceptions import AzureError

from infrastructure.models import CustomField
from common.methods import set_progress
from resourcehandlers.azure_arm.models import AzureARMHandler



RESOURCE_IDENTIFIER = ['azure_network_security_group_id', 'azure_location']


def get_tenant_id_for_azure(handler):
    '''
        Handling Azure RH table changes for older and newer versions (> 9.4.5)
    '''
    if hasattr(handler,"azure_tenant_id"):
        return handler.azure_tenant_id

    return handler.tenant_id

def create_custom_fields_as_needed():
    CustomField.objects.get_or_create(
        name="azure_rh_id",
        type="STR",
        defaults={
            "label": "RH ID",
            "description": "Used by the Azure blueprints"}
    )

    CustomField.objects.get_or_create(
        name="azure_network_security_group",
        type="STR",
        defaults={
            "label": "Network Security Group Name",
            "description": "Used by the Azure NSG",
            "show_as_attribute": True,
        },
    )

    CustomField.objects.get_or_create(
        name="azure_location",
        type="STR",
        defaults={
            "label": "Location",
            "description": "Used by the Azure blueprints",
            "show_as_attribute": True,
        },
    )

    CustomField.objects.get_or_create(
        name="resource_group_name",
        type="STR",
        defaults={
            "label": "Resource Group",
            "description": "Used by the Azure blueprints",
            "show_as_attribute": True,
        },
    )
    
    CustomField.objects.get_or_create(
        name="azure_network_security_group_id",
        type="STR",
        defaults={
            "label": "Network Security Group ID",
            "description": "Used by the Azure blueprints",
            "show_as_attribute": True,
        },
    )

def discover_resources(**kwargs):
    
    create_custom_fields_as_needed()
    
    discovered_virtual_nets = []
    
    for handler in AzureARMHandler.objects.all():
        
        set_progress("Connecting to Azure networks for handler: {}".format( handler))
        
        wrapper = handler.get_api_wrapper()
        
        
        network_client = wrapper.network_client

        azure_resources_client = wrapper.resource_client
        

        for resource_group in azure_resources_client.resource_groups.list():
            try:
                for security_group in network_client.network_security_groups.list(
                    resource_group_name=resource_group.name
                ):
                    resource = {
                            "name": security_group.as_dict()["name"],
                            "azure_network_security_group": security_group.as_dict()[
                                "name"
                            ],
                            "azure_location": security_group.as_dict()["location"],
                            "azure_rh_id": handler.id,
                            "resource_group_name": resource_group.name,
                            "azure_network_security_group_id": security_group.id
                        }
                        
                    if resource not in discovered_virtual_nets:
                        discovered_virtual_nets.append(
                            resource
                        )
            except (CloudError,AzureError) as e:
                set_progress("Azure error: {}".format(e))
                continue

    return discovered_virtual_nets