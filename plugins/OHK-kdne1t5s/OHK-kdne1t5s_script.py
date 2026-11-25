"""
Discovery plugin for the Azure Network Security Group blueprint: returns one
dictionary per network security group in every Azure resource handler's
subscription, and CloudBolt's discovery sync creates, updates or reactivates
the matching resources from them (servicecatalog/models.py,
ServiceBlueprint.sync_resources). The plugin never creates resources or
parameters itself.

NSGs are matched on their Azure resource ID and location. Azure resource IDs
compare case-insensitively, so an NSG CloudBolt already tracks is returned
with the identifier values it stores.

Retirement is the plugin's call; the blueprint's auto_historical_resources
stays off, so a handler that cannot be reached retires nothing. A tracked
Active NSG is marked Historical only when its handler's listing succeeded, the
NSG is missing from it, no job is running on it, and a GET of the NSG answers
that it does not exist. Anything else returns the identifier alone, which
changes nothing. The sync reactivates a Historical NSG when it is found again.
  https://learn.microsoft.com/en-us/python/api/azure-mgmt-network/azure.mgmt.network.operations.networksecuritygroupsoperations
  https://learn.microsoft.com/en-us/azure/azure-resource-manager/troubleshooting/error-not-found
"""
from azure.core.exceptions import ResourceNotFoundError

from common.methods import set_progress
from jobs.models import Job
from resourcehandlers.azure_arm.models import AzureARMHandler
from utilities.logger import ThreadLogger

logger = ThreadLogger(__name__)

RESOURCE_IDENTIFIER = ["azure_network_security_group_id", "azure_location"]

# Lifecycles the build gives a resource while it runs and when it fails
# (jobengine/jobmodules/deploy_blueprint_job.py).
BUILDING = "PROV"
BUILD_FAILED = "PROVFAILED"

# 404 error codes that prove the NSG is gone. SubscriptionNotFound is a 404
# too, but it says nothing about the NSG.
# https://learn.microsoft.com/en-us/azure/azure-resource-manager/troubleshooting/error-not-found
GONE_ERROR_CODES = ("ResourceNotFound", "NotFound", "ResourceGroupNotFound")


def _key(resource_id):
    return str(resource_id or "").lower()


def _stored(resource):
    return resource.get_cf_values_as_dict()


def _known_resources(blueprint):
    """The blueprint's resources by lower-cased NSG ID, an Active one winning."""
    known = {}
    if blueprint is None:
        return known
    for resource in blueprint.resource_set.all().order_by("id"):
        key = _key(_stored(resource).get("azure_network_security_group_id"))
        if key and (key not in known or resource.lifecycle == "ACTIVE"):
            known[key] = resource
    return known


def _untouched(resource):
    """A dictionary that matches the resource and changes nothing."""
    values = _stored(resource)
    return {name: values.get(name) for name in RESOURCE_IDENTIFIER}


def _retired(resource):
    values = _untouched(resource)
    values["lifecycle"] = "HISTORICAL"
    return values


def _has_running_job(resource):
    """A running build, action or teardown may be about to create or delete the NSG."""
    return (
        resource.jobs.filter(status__in=Job.ACTIVE_STATUSES).exists()
        or Job.objects.filter(
            status__in=Job.ACTIVE_STATUSES,
            job_parameters__hookparameters__resources=resource,
        ).exists()
    )


def _confirmed_gone(network_client, resource):
    """True only when Azure answers that the NSG does not exist."""
    values = _stored(resource)
    # https://learn.microsoft.com/en-us/python/api/azure-mgmt-network/azure.mgmt.network.operations.networksecuritygroupsoperations#azure-mgmt-network-operations-networksecuritygroupsoperations-get
    try:
        network_client.network_security_groups.get(
            values.get("resource_group_name"), values.get("azure_network_security_group")
        )
        return False
    except ResourceNotFoundError as exc:
        code = getattr(getattr(exc, "error", None), "code", None)
        if code in GONE_ERROR_CODES:
            return True
        logger.info("Not retiring %s: Azure answered 404 with code %s", resource, code)
        return False
    except Exception as exc:
        logger.info("Could not confirm whether %s still exists: %s", resource, exc)
        return False


def _resource_group(nsg_id, rg_names):
    """The resource group in the NSG's ID, spelled as Azure lists the group."""
    parts = str(nsg_id).split("/")
    lowered = [p.lower() for p in parts]
    if "resourcegroups" not in lowered or lowered.index("resourcegroups") + 1 >= len(parts):
        return ""
    name = parts[lowered.index("resourcegroups") + 1]
    return rg_names.get(name.lower(), name)


def nsg_dict(nsg, rh, rg_names, resource):
    """The values the sync sets on an NSG's resource, matching what the build stores."""
    values = {
        "name": nsg.name,
        "azure_network_security_group": nsg.name,
        "azure_network_security_group_id": nsg.id,
        "azure_location": nsg.location,
        "azure_rh_id": str(rh.id),
        "resource_group_name": _resource_group(nsg.id, rg_names),
    }
    if resource is not None:
        values.update(_untouched(resource))
        if resource.lifecycle == BUILD_FAILED:
            # The sync only reactivates Historical resources by itself.
            values["lifecycle"] = "ACTIVE"
            set_progress(f"Recovering '{nsg.name}': its CloudBolt build failed after Azure created it.")
    return values


def discover_resources(**kwargs):
    known = _known_resources(kwargs.get("blueprint"))
    discovered = []
    returned = set()
    # Handlers whose listing succeeded, by ID, with their network client.
    clients = {}

    for rh in AzureARMHandler.objects.all():
        set_progress(f"Listing network security groups for Azure handler '{rh.name}'")
        try:
            wrapper = rh.get_api_wrapper()
            network_client = wrapper.network_client
            # https://learn.microsoft.com/en-us/rest/api/resources/resource-groups/list
            rg_names = {rg.name.lower(): rg.name for rg in wrapper.resource_client.resource_groups.list()}
            # https://learn.microsoft.com/en-us/python/api/azure-mgmt-network/azure.mgmt.network.operations.networksecuritygroupsoperations#azure-mgmt-network-operations-networksecuritygroupsoperations-list-all
            nsgs = list(network_client.network_security_groups.list_all())
        except Exception as exc:
            set_progress(f"Skipping Azure handler '{rh.name}': {exc}")
            continue
        clients[str(rh.id)] = network_client
        set_progress(f"Azure handler '{rh.name}': {len(nsgs)} network security group(s).")

        for nsg in nsgs:
            key = _key(nsg.id)
            # Two handlers on the same subscription see the same NSGs.
            if not key or key in returned:
                continue
            returned.add(key)
            resource = known.get(key)
            if resource is not None and resource.lifecycle == BUILDING:
                set_progress(f"Skipping '{nsg.name}': its CloudBolt build is still running.")
                discovered.append(_untouched(resource))
                continue
            discovered.append(nsg_dict(nsg, rh, rg_names, resource))

    # Tracked Active NSGs no listing returned.
    for key, resource in known.items():
        if resource.lifecycle != "ACTIVE" or key in returned:
            continue
        network_client = clients.get(str(_stored(resource).get("azure_rh_id")))
        if (
            network_client is not None
            and not _has_running_job(resource)
            and _confirmed_gone(network_client, resource)
        ):
            set_progress(f"Network security group '{resource.name}' no longer exists; marking it Historical.")
            discovered.append(_retired(resource))
        else:
            discovered.append(_untouched(resource))

    return discovered
