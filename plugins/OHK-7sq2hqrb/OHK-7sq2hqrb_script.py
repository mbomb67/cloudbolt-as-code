"""
This script will allow a user to ingest metadata from VMs in Aria (vRA8) in to
CloudBolt. This script will run against all servers in a group and will
update the metadata in CloudBolt.


Currently supported metadata types:
- Owner - find the owner of a VM in Aria and set it as the owner of the VM in
  CloudBolt. If the owner doesn't exist in CloudBolt, it will be created.
- Group - find the Project of a VM in Aria and set it as the group of the
  VM in CloudBolt. If the group doesn't exist in CloudBolt, it can be created.


Pre-Requisites:
- Configure a Connection Info object in CloudBolt with "vra8" as a label
- CloudBolt must be at least on version 2023.4.1
- If migrating groups metadata - the Groups should already exist in CloudBolt
- An LDAP connection must be configured in CloudBolt


Features:
- groups_map: Allows you to pass in a dict mapping Aria Business Group names to
  CloudBolt group names. This is useful if you want to use different names
  in CloudBolt than Aria. This would also allow several Aria Business Groups to
  map to the same CloudBolt group.
  Example: {"Aria Business Group Name": "CloudBolt Group Name"}
- create_groups: If a group isn't found with the same name as the Aria Business
  Group, a new group will be created in CloudBolt

Form:
    - Allow customer to select a Cloud Template in Aria (version optional),
    then migrate the Cloud Template and all sub-resources under it.
    - Allow customer to select corresponding CloudBolt Blueprint to migrate to.
    - Have an optional param for projects to migrate for the Cloud Template.
    - Allow the customer to input custom properties to migrate (or not)

Execution:
    - Query Aria to get all deployments for the CloudTemplate
    - For each deployment
        - Create a Resource in CloudBolt
            - Assign Owner and Group
            - Assign to CB Blueprint
        - get the resources and their properties

"""
import ast
import json
from urllib.parse import urlencode
import requests
import yaml
import time
from dateutil.parser import parse
from django.contrib.auth.models import User
from django.db.models import Q

from infrastructure.models import Server, CustomField, Environment
from orders.models import CustomFieldValue
from resourcehandlers.vmware.pyvmomi_wrapper import get_vm_by_uuid
from resources.models import Resource
from servicecatalog.models import ServiceBlueprint
from accounts.models import Group
from c2_wrapper import create_custom_field
from utilities.models import ConnectionInfo, LDAPUtility
from common.methods import set_progress
from utilities.logger import ThreadLogger
from shared_modules.aria_connection import (
    AriaAutomationConnection as VRealizeAutomation8Connection,
    generate_options_for_aria_connection as generate_options_for_vra_connection,
    generate_options_for_aria_projects as generate_options_for_vra_projects,
)

logger = ThreadLogger(__name__)

CONN_INFO_ID = "{{vra_connection}}"
VRA_BLUEPRINT_ID = "{{vra_blueprint}}"
CB_BLUEPRINT_ID = "{{cloudbolt_blueprint}}"

# Optional to filter migrated resources to a specific vRA Project
PROJECT_IDS = "{{vra_projects}}"
try:
    PROJECT_IDS = ast.literal_eval(PROJECT_IDS)
except Exception:
    PROJECT_IDS = []

# An optional dict mapping vRA Business Group names to CloudBolt group names
# Example: {"vRA Business Group Name": "CloudBolt Group Name"}
GROUPS_MAP = "{{groups_map}}"
logger.debug(f'GROUPS_MAP: {GROUPS_MAP}')
try:
    groups_map = ast.literal_eval(GROUPS_MAP)
    GROUPS_MAP = {}
    for group in groups_map:
        GROUPS_MAP[group["vra_project"]] = group["cb_group"]
except Exception:
    GROUPS_MAP = {}

logger.debug(f'GROUPS_MAP: {GROUPS_MAP}')
# The Deployment resources need to be mapped to CloudBolt Tiers in the
# destination CloudBolt Blueprint. This is a dict mapping the vRA Deployment
# resource names to the CloudBolt Tier names.
# Example: {"Cloud_Machine_1": "CloudBolt Server Tier"}
DEPLOYMENT_MAP = "{{deployment_map}}"
logger.debug(f'DEPLOYMENT_MAP: {DEPLOYMENT_MAP}')
try:
    deploy_array = ast.literal_eval(DEPLOYMENT_MAP)
    DEPLOYMENT_MAP = {}
    for deploy in deploy_array:
        DEPLOYMENT_MAP[deploy["vra_resource_name"]] = deploy["cb_si_name"]
except Exception:
    DEPLOYMENT_MAP = {}

logger.debug(f'DEPLOYMENT_MAP: {DEPLOYMENT_MAP}')

# A list of prefixes to Custom Property names that should be ignored if they
# start with these prefixes
IGNORE_PREFIXES = """{{prefixes_to_ignore}}"""
logger.debug(f'IGNORE_PREFIXES: {IGNORE_PREFIXES}')
try:
    prefix_strings = ast.literal_eval(IGNORE_PREFIXES)
    IGNORE_PREFIXES = []
    for prefix in prefix_strings:
        pfx = json.loads(prefix.replace("'", '"'))["ignored_prefixes"]
        IGNORE_PREFIXES.append(pfx)
except Exception:
    IGNORE_PREFIXES = []

MIGRATE_ONEFUSE_PROPERTIES = ast.literal_eval("{{migrate_onefuse_properties}}")
if not MIGRATE_ONEFUSE_PROPERTIES:
    # If the user does not want to migrate OneFuse properties, we will add the
    # OneFuse_ prefix to the list of prefixes to ignore
    IGNORE_PREFIXES.append("OneFuse_")
logger.debug(f'IGNORE_PREFIXES: {IGNORE_PREFIXES}')

# Explicit properties to ignore - most of these are vRA specific properties
# that do not need to be migrated to CloudBolt
IGNORE_PROPERTIES = ["resourceId", "zone_overlapping_migrated", "powerState",
                     "environmentName", "computeHostType", "id", "cpuCount",
                     "totalMemoryMB", "endpointType", "tags", "resourceLink",
                     "hostName", "networks", "_clusterAllocationSize",
                     "resourcePool", "componentType", "address",
                     "resourceGroupName", "datastoreName", "coreCount",
                     "accounts", "flavorMappingName", "vmFolderPath",
                     "resourceDescLink", "project", "zone", "memoryGB", "image",
                     "count", "resourceName", "cloneFromImage", "flavor",
                     "softwareName", "name", "region", "storage", "providerId",
                     "osType", "instanceUUID", "endpointId", "datacenter",
                     "primaryMAC", "computeHostRef", "account", "vcUuid",
                     "hasSnapshots", "countIndex",
                     ]

# Optional input allowing you to specify a custom prefix for the migrated
# Custom Properties. This is useful if you want to keep the vRA Custom
# Properties separate from the CloudBolt Custom Properties.
MIGRATE_PREFIX = "{{migrate_prefix}}"

# Input for the OneFuse Endpoint Map. This is an optional dict mapping the
# OneFuse Endpoint names in vRA to the OneFuse Endpoint names in CloudBolt. This
# is useful if you want to use different OneFuse endpoint names in CloudBolt
# than vRA.
ONEFUSE_ENDPOINT_MAP = "{{onefuse_endpoint_map}}"
logger.debug(f'ONEFUSE_ENDPOINT_MAP: {ONEFUSE_ENDPOINT_MAP}')
try:
    groups_map = ast.literal_eval(ONEFUSE_ENDPOINT_MAP)
    ONEFUSE_ENDPOINT_MAP = {}
    for group in groups_map:
        ONEFUSE_ENDPOINT_MAP[group["vra_endpoint"]] = group["cb_endpoint"]
except Exception:
    ONEFUSE_ENDPOINT_MAP = {}

logger.debug(f'ONEFUSE_ENDPOINT_MAP: {ONEFUSE_ENDPOINT_MAP}')


def generate_options_for_create_groups(**kwargs):
    return [
        ("True", "True"),
        ("False", "False")
    ]


def generate_options_for_vra_blueprint(field, control_value=None, **kwargs):
    if not control_value:
        return [("", "------First, Select a vRA Connection------")]
    vra = VRealizeAutomation8Connection(control_value)
    bps = vra.list_blueprints()["content"]
    return [(bp["id"], bp["name"]) for bp in bps]


def generate_options_for_cloudbolt_blueprint(field, **kwargs):
    bps = ServiceBlueprint.objects.filter(status="ACTIVE")
    return [(bp.id, bp.name) for bp in bps]


def create_custom_fields():
    description = ("The vRA ID of the resource. This should be unique across "
                   "CloudBolt Servers and Resources.")
    create_custom_field("vra_id", "vRA ID", "STR",
                        description=description)
    create_custom_field("vra_migrated", "Migrated from vRA",
                        "BOOL")


def run(job=None, logger=None, **kwargs):
    validate_deployment_map()
    create_custom_fields()
    # group = Group.objects.get(id=GROUP_ID)
    vra = VRealizeAutomation8Connection(CONN_INFO_ID)

    # Get all vRA Deployments for the Blueprint
    deployment_ids = vra.list_deployment_ids_for_blueprint(VRA_BLUEPRINT_ID,
                                                           project_ids=PROJECT_IDS)
    bp = ServiceBlueprint.objects.get(id=CB_BLUEPRINT_ID)
    if bp.resource_type is None:
        # Since a Blueprint was selected without a resource type, we will import
        # all resources as stand-alone servers in CloudBolt (not tied to a
        # parent resource)
        set_progress(f'No resource type selected for Blueprint: {bp.name}. Will'
                     f' import all resources as stand-alone servers.')
        servers = migrate_servers_metadata(deployment_ids, vra)

    else:
        # Since a Blueprint was selected with a resource type, we will import
        # all resources as child resources of the Blueprint
        set_progress(f'Importing all resources as child resources of the '
                     f'Blueprint: {bp.name}.')
        resources = migrate_resources(deployment_ids, vra, bp)

    return "SUCCESS", "", ""


def migrate_resources(deployment_ids, vra, bp):
    resources = []
    for deployment_id in deployment_ids:
        deployment = vra.get_deployment(deployment_id)
        owner = get_owner(deployment, vra)
        project_id = deployment["projectId"]
        group = get_group_from_vra_project_id(project_id, vra,
                                              deployment["name"])
        if not group:
            logger.warning(f'Group does not exist, skipping the '
                           f'migration of {deployment["name"]}.')
            continue
        parent_resource = create_deployment_resource(deployment, owner, bp, vra,
                                                     group)
        for deployment_resource in deployment["resources"]:
            resource = vra.get_resource(deployment_resource["id"])
            vra_resource_key = resource["properties"]["name"]
            cb_tier = DEPLOYMENT_MAP[vra_resource_key]
            si = bp.serviceitem_set.get(name=cb_tier).cast()
            resource_type = si.real_type.name
            if resource_type == "provision server service item":
                server = migrate_server_metadata(resource["id"], vra, owner)
                if server:
                    server.resource = parent_resource
                    server.save()
            elif resource_type == "blueprint service item":
                child_resource = create_cloudbolt_resource(
                    resource, bp, owner, group, parent_resource
                )
                resource.parent_resource = parent_resource
                resource.save()
                resources.append(child_resource)
            else:
                logger.warning(f'Unknown resource type: {resource_type} for '
                               f'service item: {si.name}. Skipping.')
                continue
        resources.append(parent_resource)
    return resources


def migrate_servers_metadata(deployment_ids, vra):
    servers = []
    for deployment_id in deployment_ids:
        deployment = vra.get_deployment(deployment_id)
        owner = get_owner(deployment, vra)
        for resource in deployment["resources"]:
            try:
                server = migrate_server_metadata(resource["id"], vra, owner)
            except Exception as e:
                logger.warning(f'Error migrating server metadata: {e}')
                continue
            if server:
                servers.append(server)
    return servers


def get_owner_metadata(deployment, vra):
    owner = deployment.get("ownedBy", None)
    org_id = deployment["orgId"]
    user = vra.get_user_by_username(owner, org_id)
    upn = f'{user["username"]}@{user["domain"]}'
    email = user.get("email", None)
    owner = {"upn": upn, "email": email}
    return owner


def migrate_server_metadata(resource_id, vra, owner):
    resource = vra.get_resource(resource_id)
    resource_type = resource["type"]
    name = resource["name"]
    if resource_type == "Cloud.vSphere.Machine":
        props = resource["properties"]
        uuid = props["instanceUUID"]
        try:
            server = Server.objects.get(vmwareserverinfo__instance_uuid=uuid)
        except Server.DoesNotExist:
            logger.warning(f'No CloudBolt Server found for server: {name} with '
                           f'instance_uuid: {uuid}. Skipping.')
            return None
        project_id = resource["projectId"]
        server = assign_server_to_group(server, project_id, vra)
        server = assign_server_to_owner(server, owner)
        server = assign_server_to_environment(server)
        migrate_custom_properties(server, props)
        return server
    else:
        logger.warning(f'Unknown resource type: {resource_type}. Skipping.')
        return None


def assign_server_to_environment(server):
    """
    Use the pyVmomi API to get the vCenter SDK server object, then get the name
    of the cluster where the server is located. Then get the CloudBolt
    Environment with the same name and assign the server to that Environment.
    """
    rh = server.resource_handler.cast()
    si = rh.get_api_wrapper()._get_connection()
    try:
        vm = get_vm_by_uuid(si, server.resource_handler_svr_id)
        cluster = vm.runtime.host.parent.name
        if not cluster:
            raise Exception(f'No cluster found for server: {server.hostname}')
        env = Environment.objects.filter(vmware_cluster=cluster,
                                         resource_handler=rh).first()
        server.environment = env
        server.save()
        return server
    except Exception as e:
        logger.warning(f'Unable to assign environment for server: '
                       f'{server.hostname}')
        return server


def get_group_name_from_project_id(project_id, vra):
    project = vra.get_project_name(project_id)
    try:
        group_name = GROUPS_MAP[project]
        return group_name
    except KeyError:
        return project


def create_deployment_resource(deployment, owner, bp, vra, group):
    resource = create_cloudbolt_resource(deployment, bp, owner, group)
    created_by = deployment.get("createdBy", None)
    created_at = deployment.get("createdAt", None)
    set_created_metadata(resource, created_by, created_at)
    return resource


def create_cloudbolt_resource(vra_resource, bp, owner, group,
                              parent_resource=None):
    """
    - Create a Resource in CloudBolt, Assign it to CB Blueprint
    - Assign Owner and Group
    - Migrate Custom Properties
    :param vra_resource: vRA Resource dict. Could either be for a resource or a
        deployment
    :param bp: CloudBolt Blueprint to assign the resource to
    :param owner: dict containing the owner metadata
    :param group: CloudBolt Group to assign the resource to
    :param parent_resource: CloudBolt Resource to assign the resource to
    :return: CloudBolt Resource
    """
    cf = CustomField.objects.get(name="vra_id")
    cfv, _ = CustomFieldValue.objects.get_or_create(field=cf,
                                                    value=vra_resource["id"])
    try:
        resource = Resource.objects.get(attributes=cfv)
    except Resource.DoesNotExist:
        resource, _ = Resource.objects.get_or_create(
            name=vra_resource["name"],
            defaults={
                "resource_type": bp.resource_type,
                "blueprint": bp,
                "group": group,
                "owner": owner.userprofile,
            }
        )
        resource.set_value_for_custom_field(cf.name, vra_resource["id"])
    if parent_resource:
        resource.parent_resource = parent_resource
    resource.vra_migrated = True
    resource.group = group
    resource.owner = owner.userprofile
    resource.status = "ACTIVE"
    resource.save()
    try:
        props = vra_resource["properties"]
        vra_type = vra_resource["type"]
        migrate_custom_properties(resource, props, vra_type)
    except KeyError:
        pass
    return resource


def set_created_metadata(resource, created_by, created_at):
    create_custom_field("vra_created_by", "Created By", "STR")
    create_custom_field("vra_created_at", "Created At", "DTM")
    resource.set_value_for_custom_field("vra_created_by", created_by)
    created_dtm = parse(created_at)
    resource.set_value_for_custom_field("vra_created_at", created_dtm)


def get_instance_uuid_from_pyvmomi(server):
    rh = server.resource_handler.cast()
    wrapper = rh.get_api_wrapper()
    vm = wrapper.get_vm_dict(server)
    return vm["instance_uuid"]


def assign_server_to_owner(server, owner):
    try:
        profile = owner.userprofile
        set_progress(f'Changing Owner for server: {server.hostname} to: '
                     f'{profile.full_name}')
        server.owner = profile
        server.save()
        return server
    except Exception as e:
        logger.warning(f'Ran in to issues attempting to assign Owner: {owner} '
                       f' to server: {server.hostname}. Error: {e}')
        raise


def get_owner(deployment, vra):
    owner = get_owner_metadata(deployment, vra)
    upn, email = owner["upn"], owner["email"]
    if upn.find('@') == -1:
        short_name = upn
        domain = None
    else:
        short_name, domain = upn.split('@')
    try:
        ldap = LDAPUtility.objects.get(ldap_domain=domain)
    except LDAPUtility.DoesNotExist:
        logger.info(f'No LDAPUtility found for domain: {domain}. Trying email')
        try:
            ldap = LDAPUtility.objects.get(ldap_domain=email.split('@')[1])

        except LDAPUtility.DoesNotExist:
            logger.error(f'No LDAPUtility found for email: {email} domain.')
            raise
    if ldap.ldap_username == "sAMAccountName":
        ldap_attributes = ldap.ldap_user_attributes(short_name)
    elif ldap.ldap_username == "userPrincipalName":
        ldap_attributes = ldap.ldap_user_attributes(upn)
    else:
        raise Exception(f'Invalid ldap_username: {ldap.ldap_username}. '
                        f'Only sAMAccountName and userPrincipalName are '
                        f'valid values.')
    if not ldap_attributes:
        raise Exception(f'No LDAP attributes found for {upn}')

    user = ldap.get_or_create_cb_user(ldap_attributes)
    return user


def assign_server_to_group(server, project_id, vra):
    # Also works for resources
    group = get_group_from_vra_project_id(project_id, vra, server)
    set_progress(f'Changing Group for resource: {server} to: {group}')
    server.group = group
    server.save()
    return server


def get_group_from_vra_project_id(project_id, vra, resource):
    """
    Get the CloudBolt Group from the vRA Project ID
    :param project_id: vRA Project ID
    :param vra: vRA Connection
    :param resource: Resource object - can either be the resource/server object
        or the name of the resource/server
    """
    project_name = get_group_name_from_project_id(project_id, vra)
    group_id = None
    if GROUPS_MAP:
        try:
            group_id = GROUPS_MAP[project_id]
        except KeyError:
            logger.debug(f'No group found for {project_name} in GROUPS_MAP, '
                         f'trying the group name from vRA.')
            return None

    try:
        if group_id:
            group = Group.objects.get(id=group_id)
            logger.info(f'Updating CB group from {project_name} in vRA '
                        f'to {group.name} in CB.')
        else:
            group = Group.objects.get(name=project_name)
    except Group.DoesNotExist:
        logger.warning(f'Group: {project_name} does not exist, skipping the '
                       f'assignment of server: {resource} to group: '
                       f'{project_name}.')
        return None
    return group


def migrate_custom_properties(resource, custom_properties, vra_type=None):
    # Works for a Resource or a Server
    for key, value in custom_properties.items():
        if key in IGNORE_PROPERTIES:
            continue
        if any(key.startswith(prefix) for prefix in IGNORE_PREFIXES):
            continue
        if not value:
            # If the value is None, we will skip it
            continue
        if type(value) is dict:
            # If the value is a dict, dump to json string
            if vra_type.startswith("Custom.onefuse."):
                # Checking if object is in the OneFuse Custom Resource namespace
                # If so, we need to separate the endpoint and IDs
                endpoint, onefuse_id = value["id"].split(":")
                value["id"] = onefuse_id
                value["endpoint"] = endpoint
            value = json.dumps(value)
        if key.startswith("OneFuse_"):
            # If the Custom Property is a OneFuse property, we need to translate
            # the name and value
            value = translate_onefuse_property_value(key, value)
            key = translate_onefuse_property_name(key)
        if value:
            cf_name = f'{MIGRATE_PREFIX}{key}'
            cf_label = key
            logger.debug(f'Creating Custom Field: {cf_name} for resource: '
                         f'{resource} with value: {value}')
            cf = create_cf_for_custom_property(cf_name, cf_label, value)
            resource.set_value_for_custom_field(cf.name, value)
    return None


def create_cf_for_custom_property(cf_name, cf_label, value):
    if len(cf_label) > 50:
        # Custom Field labels in CloudBolt are limited to 50 chars, if the
        # length of the label is greater than 50, we will truncate it
        logger.warning(f'Custom Field label: {cf_label} is greater than 50 '
                       f'chars. Truncating to 50 chars.')
        cf_label = cf_label[:50]
    if len(cf_name) > 255:
        # Custom Field names in CloudBolt are limited to 255 chars, if the
        # length of the name is greater than 255, we will truncate it
        logger.warning(f'Custom Field name: {cf_name} is greater than 255 '
                       f'chars. Truncating to 255 chars.')
        cf_name = cf_name[:255]
    cf_type = "STR"
    if len(value) > 400:
        # String fields in CloudBolt are limited to 975 chars, if the length
        # of the value is greater than 400, we will use a Text field
        # which is not limited on chars
        cf_type = "TXT"
    if cf_name == "Application":
        # Application is a reserved Custom Field name in CloudBolt, so we will
        # append an underscore to the name
        cf_name = f'{cf_name}_'
    cf = create_custom_field(cf_name, cf_label, cf_type,
                             description=f'vRA Custom Property: {cf_label}',
                             show_on_servers=True)
    return cf


def create_custom_field_set_value(field_name, field_type, value, server):
    cmp_type = get_cmp_type(field_type)
    create_custom_field(field_name, field_name, cmp_type)


def get_cmp_type(field_type):
    if field_type == 'string':
        return 'STR'
    elif field_type == 'boolean':
        return 'BOOL'
    elif field_type == 'integer':
        return 'INT'
    elif field_type == 'decimal':
        return 'DEC'
    else:
        raise Exception(f'Unknown field_type: {field_type}')


def validate_deployment_map():
    vra = VRealizeAutomation8Connection(CONN_INFO_ID)
    vra_content = vra.get_blueprint_content(VRA_BLUEPRINT_ID)
    content = yaml.load(vra_content, Loader=yaml.FullLoader)
    vra_resource_keys = content["resources"].keys()
    for key in vra_resource_keys:
        if key not in DEPLOYMENT_MAP.keys():
            raise Exception(f'No mapping found for vRA Deployment resource: '
                            f'{key}. Please add a mapping for this resource '
                            f'in the DEPLOYMENT_MAP.')
    bp = ServiceBlueprint.objects.get(id=CB_BLUEPRINT_ID)
    cb_keys = [DEPLOYMENT_MAP[key] for key in DEPLOYMENT_MAP.keys()]
    sis = get_supported_sis_for_blueprint(bp)
    si_names = [si.name for si in sis]
    for key in cb_keys:
        if key not in si_names:
            raise Exception(f'No matching Service Item found in Blueprint: '
                            f'{bp.name} for Deployment resource: {key}. '
                            f'Please add a matching Service Item to the '
                            f'Blueprint.')


def get_supported_sis_for_blueprint(bp):
    # Each Blueprint should have matching service items for each of the
    # Deployment resources. Really the only two valid sub-resources are
    # Servers and Blueprints.
    return bp.serviceitem_set.filter(
        Q(blueprintserviceitem__isnull=False) |
        Q(provisionserverserviceitem__isnull=False)
    )


def translate_onefuse_property_name(prop_name):
    """
    Take a OneFuse property name and value from vRA and translate it to the
    CloudBolt property name and value
    """
    ebs_map = get_ebs_map()
    if prop_name.startswith("OneFuse_"):
        for key, value in ebs_map.items():
            if prop_name.find(key) != -1:
                return prop_name.replace(key, value)
        return prop_name
    else:
        return prop_name


def get_ebs_map():
    """
    Returns a map of the EBS Execution String values from vRA to the CloudBolt
    Hook Point values
    """
    return {
        "ComputeAllocationPreEarly": "HostnameOverwrite",
        "ComputeAllocationPreLate": "HostnameOverwrite",
        "NetworkConfigureEarly": "PreCreateResource",
        "NetworkConfigureLate": "PreCreateResource",
        "ComputeProvisionPreEarly": "PreCreateResource",
        "ComputeProvisionPreLate": "PreCreateResource",
        "ComputeProvisionPostEarly": "PostProvision",
        "ComputeProvisionPostLate": "PostProvision",
    }


def get_bool_props():
    return ["OneFuse_AnsibleTowerBehavior_FailOnBlankHost",
            "OneFuse_AnsibleTowerBehavior_FailOnBlankLimit"]


def get_mo_props():
    return ["OneFuse_AnsibleTower_", "OneFuse_Dns_", "OneFuse_Ipam_",
            "OneFuse_Naming", "OneFuse_AD", "OneFuse_PluggableModule_",
            "OneFuse_Scripting_", "OneFuse_ServiceNowCmdb", "OneFuse_Vra_"]


def get_policy_props():
    return ["OneFuse_AnsibleTowerPolicy", "OneFuse_DnsPolicy",
            "OneFuse_IpamPolicy", "OneFuse_NamingPolicy", "OneFuse_ADPolicy",
            "OneFuse_PluggableModulePolicy", "OneFuse_PropertyToolkit",
            "OneFuse_ScriptingPolicy", "OneFuse_ServiceNowCmdbPolicy",
            "OneFuse_VraPolicy"]


def translate_onefuse_property_value(prop_name, prop_value):
    """
    Take a OneFuse property name and value from vRA and update the OneFuse
    endpoint name in the payload to the CloudBolt endpoint name
    """
    bool_props = get_bool_props()
    mo_props = get_mo_props()
    policy_props = get_policy_props()
    if not ONEFUSE_ENDPOINT_MAP:
        return prop_value
    if prop_name.startswith("OneFuse_"):
        if prop_name in bool_props:
            # If we make it here this should be a OneFuse boolean property,
            # value returned should be boolean
            if prop_value == "true" or prop_value == "True":
                return True
            else:
                return False
        elif prop_name.startswith(tuple(policy_props)):
            logger.debug(f'Found Policy property: {prop_name}')
            if prop_name.find("Trigger") != -1:
                # Don't want to update values for trigger properties
                return prop_value
            else:
                # If we make it here this should be a OneFuse Policy property,
                # and we need to replace the endpoint name in the policy string
                endpoint = prop_value.split(":")[0]
                if endpoint not in ONEFUSE_ENDPOINT_MAP.keys():
                    logger.debug(f'No endpoint mapping found for endpoint: '
                                 f'{endpoint}')
                    return prop_value
                return prop_value.replace(endpoint,
                                          ONEFUSE_ENDPOINT_MAP[endpoint], 1)
        elif prop_name.startswith(tuple(mo_props)):
            # If we make it here this should be a OneFuse Managed Object
            # property, and we may need to replace the endpoint name in the
            # payload. Will need to parse the string to a JSON object, replace
            # the endpoint name, then convert back to a string
            logger.debug(f'Found MO property: {prop_name}')
            try:
                prop_json = json.loads(prop_value)
            except Exception as e:
                logger.warning(f'Error converting property value to JSON: {e}')
                return prop_value
            if prop_json["endpoint"] in ONEFUSE_ENDPOINT_MAP.keys():
                prop_json["endpoint"] = ONEFUSE_ENDPOINT_MAP[
                    prop_json["endpoint"]]
            # Replace the EBS Execution String with the Hook Point
            ebs_value = prop_json.get("OneFuse_VraEbsExecutionString", None)
            if ebs_value:
                ebs_map = get_ebs_map()
                prop_json["OneFuse_CBHookPointString"] = ebs_map[ebs_value]
                del prop_json["OneFuse_VraEbsExecutionString"]
            return json.dumps(prop_json)

        else:
            logger.debug(f'Unknown OneFuse property: {prop_name}')
            return prop_value
    else:
        logger.debug(f'Not a OneFuse property: {prop_name}')
        return prop_value

