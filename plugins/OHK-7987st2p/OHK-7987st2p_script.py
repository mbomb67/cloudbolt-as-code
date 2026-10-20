"""
Creates an Azure network security group
"""
import settings

from azure.common.credentials import ServicePrincipalCredentials
from azure.mgmt.network import NetworkManagementClient
from msrestazure.azure_exceptions import CloudError

from common.methods import is_version_newer, set_progress
from infrastructure.models import CustomField
from infrastructure.models import Environment
from accounts.models import Group
from azure.identity import ClientSecretCredential


CB_VERSION_93_PLUS = is_version_newer(settings.VERSION_INFO["VERSION"], "9.2.2")


def get_tenant_id_for_azure(handler):
    '''
        Handling Azure RH table changes for older and newer versions (> 9.4.5)
    '''
    if hasattr(handler,"azure_tenant_id"):
        return handler.azure_tenant_id

    return handler.tenant_id


def generate_options_for_env_id(server=None, **kwargs):
    group_name = kwargs["group"]
    
    try:
        group = Group.objects.get(name=group_name)
    except Exception as err:
        return []
    
    # fetch all group environment
    envs = group.get_available_environments()
    azure_envs= [env for env in envs if env.resource_handler.resource_technology.name == "Azure"]
    
    options = [(env.id, env.name) for env in azure_envs]
    
    return options


def generate_options_for_resource_group(control_value=None, **kwargs):
    """Dynamically generate options for resource group form field based on the user's selection for Environment.
    
    This method requires the user to set the resource_group parameter as dependent on environment.
    """
    if control_value is None:
        return []

    env = Environment.objects.get(id=control_value)

    if CB_VERSION_93_PLUS:
        # Get the Resource Groups as defined on the Environment. The Resource Group is a
        # CustomField that is only updated on the Env when the user syncs this field on the
        # Environment specific parameters.
        resource_groups = env.custom_field_options.filter(
            field__name="resource_group_arm"
        )
        return [rg.str_value for rg in resource_groups]
    else:
        rh = env.resource_handler.cast()
        groups = rh.armresourcegroup_set.all()
        return [g.name for g in groups]


def create_custom_fields_as_needed():
    CustomField.objects.get_or_create(
        name="azure_rh_id",
        type="STR",
        defaults={
            "label": "Azure RH ID",
            "description": "Used by the Azure blueprints"
        },
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
            "description": "Used by the Azure blueprints"
        },
    )


def validate_network_security_group_name(name, resource_group_name, network_client):
    try:
        rsp = network_client.network_security_groups.get(network_security_group_name=name, resource_group_name=resource_group_name)
    except Exception as err:
        pass
    else:
        return False
    
    return True
        
def run(job, **kwargs):
    
    # get or create custom fields if needed
    create_custom_fields_as_needed()
    
    resource = kwargs.get("resource")

    env = Environment.objects.get(id="{{ env_id }}")
    resource_group = "{{ resource_group }}"
    network_security_group_name = "{{ network_security_group_name }}"
    
    rh = env.resource_handler.cast()

    set_progress("Connecting To Azure Network Service...")
    credentials = ClientSecretCredential(
        client_id=rh.client_id,
        client_secret=rh.secret,
        tenant_id=get_tenant_id_for_azure(rh)
    )
    network_client = NetworkManagementClient(credentials, rh.serviceaccount)
    
    set_progress("Connection to Azure networks established")

    # validate network security group is exist or not
    if not validate_network_security_group_name(network_security_group_name, resource_group, network_client):
        
        failure_error_msg = f"Entered network security group {network_security_group_name} has already existed on the Azure portal, please enter a different name or sync the resources"
        set_progress(failure_error_msg)
        return "FAILURE", "", failure_error_msg
    
    
    set_progress("Creating the network security group...")
    
    try:
        async_vnet_creation = network_client.network_security_groups.begin_create_or_update(
            resource_group, network_security_group_name, {"location": env.node_location}
        )
        nsg_info = async_vnet_creation.result()
    except CloudError as e:
        set_progress("Azure Clouderror: {}".format(e))

    assert nsg_info.name == network_security_group_name

    resource.name = network_security_group_name
    resource.azure_network_security_group = network_security_group_name
    resource.resource_group_name = resource_group
    resource.azure_location = env.node_location
    resource.azure_rh_id = rh.id
    resource.azure_network_security_group_id = nsg_info.id
    resource.save()

    return ( "SUCCESS", "Network security group {} has been created in Location {}.".format(network_security_group_name, env.node_location), "",)