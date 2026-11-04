"""
Creates an Azure network security group in the chosen resource group, in the
region of the chosen Environment, and records it on the resource for the
teardown and discovery plugins and the Security Rules tab (XUI-0jdk7t6h).

Azure is reached through the Environment's resource handler API wrapper
(handler.get_api_wrapper().network_client), which carries CloudBolt's proxy,
SSL verification and sovereign cloud settings. The handler is never shown on
the order form.
  https://learn.microsoft.com/en-us/python/api/azure-mgmt-network/azure.mgmt.network.operations.networksecuritygroupsoperations
"""
import re

import settings
from azure.core.exceptions import HttpResponseError, ResourceNotFoundError
from azure.mgmt.network.models import NetworkSecurityGroup

from accounts.models import Group
from common.methods import is_version_newer, set_progress
from infrastructure.models import CustomField
from infrastructure.models import Environment


CB_VERSION_93_PLUS = is_version_newer(settings.VERSION_INFO["VERSION"], "9.2.2")

# NSG names: 1-80 alphanumerics, underscores, periods and hyphens, starting
# with an alphanumeric and ending with an alphanumeric or underscore.
# https://learn.microsoft.com/en-us/azure/azure-resource-manager/management/resource-name-rules#microsoftnetwork
NSG_NAME_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9_.-]{0,78}[A-Za-z0-9_])?$")


def _resolve_group(group):
    """The group kwarg arrives as a Group or as its name depending on caller."""
    if group is None or isinstance(group, Group):
        return group
    return Group.objects.filter(name=str(group)).first()


def generate_options_for_env_id(server=None, **kwargs):
    """RBAC-aware Environment selector, restricted to Azure-backed environments.

    Standard: Group.get_available_environments() returns the environments
    explicitly entitled to the requesting group (and its ancestors) PLUS any
    unconstrained environments (no groups assigned). Users must be able to
    order into both, so never filter Environment by group__in alone.
    See docs/agents/rbac-and-security.md.
    """
    group = _resolve_group(kwargs.get("group"))
    if not group:
        return []

    available_ids = [env.id for env in group.get_available_environments()]
    envs = Environment.objects.filter(
        id__in=available_ids,
        resource_handler__azurearmhandler__isnull=False,
    ).order_by("name")
    if not envs.exists():
        return [("", "------ No Azure environments available ------")]

    return [(env.id, env.name) for env in envs]


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


def _azure_message(exc):
    """Azure's error message and code from the ARM error body, else the SDK's text."""
    error = getattr(exc, "error", None)
    if error is not None and getattr(error, "message", None):
        return f"{error.message} ({error.code})" if getattr(error, "code", None) else error.message
    return getattr(exc, "message", None) or str(exc)


def run(job, **kwargs):
    create_custom_fields_as_needed()

    resource = kwargs.get("resource")
    env = Environment.objects.get(id="{{ env_id }}")
    resource_group = "{{ resource_group }}".strip()
    network_security_group_name = "{{ network_security_group_name }}".strip()

    if not NSG_NAME_RE.match(network_security_group_name):
        return (
            "FAILURE",
            "",
            f"'{network_security_group_name}' is not a valid network security group name: use 1-80 "
            "letters, digits, underscores, periods and hyphens, starting with a letter or digit and "
            "ending with a letter, digit or underscore.",
        )

    rh = env.resource_handler.cast()
    location = env.node_location
    set_progress(f"Connecting to Azure through resource handler '{rh.name}'")
    network_client = rh.get_api_wrapper().network_client

    # https://learn.microsoft.com/en-us/python/api/azure-mgmt-network/azure.mgmt.network.operations.networksecuritygroupsoperations#azure-mgmt-network-operations-networksecuritygroupsoperations-get
    try:
        network_client.network_security_groups.get(resource_group, network_security_group_name)
    except ResourceNotFoundError:
        pass
    except HttpResponseError as exc:
        return (
            "FAILURE",
            "",
            f"Could not check whether network security group '{network_security_group_name}' exists in "
            f"resource group '{resource_group}': {_azure_message(exc)}",
        )
    else:
        return (
            "FAILURE",
            "",
            f"Network security group '{network_security_group_name}' already exists in resource group "
            f"'{resource_group}'. Choose another name, or run discovery (Sync) on this blueprint to adopt it.",
        )

    # Record where the NSG goes before asking Azure for it, so a build that
    # fails after Azure accepted the request still leaves the teardown enough
    # to delete it. The ID follows Azure's resource ID format; the create below
    # replaces it with the ID Azure returns.
    resource.name = network_security_group_name
    resource.azure_network_security_group = network_security_group_name
    resource.resource_group_name = resource_group
    resource.azure_location = location
    resource.azure_rh_id = rh.id
    resource.azure_network_security_group_id = (
        f"/subscriptions/{rh.serviceaccount}/resourceGroups/{resource_group}"
        f"/providers/Microsoft.Network/networkSecurityGroups/{network_security_group_name}"
    )
    resource.save()

    set_progress(f"Creating network security group '{network_security_group_name}' in {location}")
    # https://learn.microsoft.com/en-us/python/api/azure-mgmt-network/azure.mgmt.network.operations.networksecuritygroupsoperations#azure-mgmt-network-operations-networksecuritygroupsoperations-begin-create-or-update
    try:
        nsg = network_client.network_security_groups.begin_create_or_update(
            resource_group, network_security_group_name, NetworkSecurityGroup(location=location)
        ).result()
    except HttpResponseError as exc:
        return (
            "FAILURE",
            "",
            f"Azure could not create network security group '{network_security_group_name}': "
            f"{_azure_message(exc)}",
        )

    resource.azure_network_security_group_id = nsg.id
    resource.save()

    return (
        "SUCCESS",
        f"Network security group {network_security_group_name} has been created in {location}.",
        "",
    )
