"""
CloudBolt Day-2 resource action: List Resources in Group.

Read-only inventory of everything inside the Azure resource group backing this
resource. Useful before decommissioning — the teardown plugin deletes the group
and everything in it — and as a quick drift check against what CloudBolt knows.

Records the count on the resource (azure_resource_group_resource_count) and
returns a grouped-by-type summary as the job output.

Action Inputs: none.

External API: Azure Resource Manager via azure-mgmt-resource. Operation shapes
anchored to Microsoft's current docs (cited at the call site) per
docs/agents/external-apis.md.

Entry point: run(job, resource, **kwargs) -> (status, output_msg, error_msg)
"""

from collections import defaultdict

from common.methods import set_progress
from utilities.logger import ThreadLogger

logger = ThreadLogger(__name__)


def _resolve_resource(job, resource, kwargs):
    """Return the Resource this action targets.

    CloudBolt hands plugins their context as keyword arguments, and which key
    carries the resource depends on how the action was invoked: a single-resource
    run supplies `resource`, the bulk/list path supplies `resources`, and any
    job-backed run has it on the job. Accept all three rather than trusting one
    slot — the same defensive read other day-2 plugins in this repo use.
    """
    if resource is not None:
        return resource
    if kwargs.get("resource") is not None:
        return kwargs["resource"]
    for candidate in kwargs.get("resources") or []:
        if candidate is not None:
            return candidate
    if job is not None:
        return job.resource_set.first()
    return None


def _load_group(resource):
    """Return (rg_name, handler) from the resource's custom field values.

    Raises ValueError naming the specific missing piece, so a failure message
    identifies the cause instead of collapsing every cause into one string.
    """
    if resource is None:
        raise ValueError("no Resource was supplied to this action")

    rg_name = resource.get_value_for_custom_field("azure_resource_group_name")
    if not rg_name:
        raise ValueError("resource has no 'azure_resource_group_name' value")

    rh_id = resource.get_value_for_custom_field("azure_resource_group_rh_id")
    if not rh_id:
        raise ValueError("resource has no 'azure_resource_group_rh_id' value")

    from resourcehandlers.azure_arm.models import AzureARMHandler

    try:
        return rg_name, AzureARMHandler.objects.get(id=rh_id)
    except AzureARMHandler.DoesNotExist:
        raise ValueError(f"no Azure resource handler with id={rh_id}")


def run(job=None, resource=None, **kwargs):
    """Inventory the contents of the resource group."""
    set_progress("Starting List Resources in Group...")

    resource = _resolve_resource(job, resource, kwargs)
    try:
        rg_name, rh = _load_group(resource)
    except Exception as exc:
        logger.warning("Could not resolve resource group from resource: %s", exc)
        return "FAILURE", "", f"Could not resolve the Azure resource group: {exc}"

    try:
        from azure.core.exceptions import HttpResponseError, ResourceNotFoundError
        from azure.mgmt.resource import ResourceManagementClient
        from resourcehandlers.azure_arm.azure_wrapper import configure_arm_client
    except ImportError as exc:
        return "FAILURE", "", f"Azure Resource Management SDK is not installed: {exc}"

    resource_client = configure_arm_client(rh.get_api_wrapper(), ResourceManagementClient)

    set_progress(f"Listing resources in '{rg_name}'...")

    # Docs: https://learn.microsoft.com/en-us/python/api/azure-mgmt-resource/azure.mgmt.resource.resources.operations.resourcesoperations#list-by-resource-group
    #       ResourcesOperations.list_by_resource_group(resource_group_name) -> ItemPaged[GenericResourceExpanded].
    #       The paged iterator is fully consumed here so the count is exact.
    try:
        contents = list(resource_client.resources.list_by_resource_group(rg_name))
    except ResourceNotFoundError:
        return "FAILURE", "", f"Resource group '{rg_name}' no longer exists in Azure."
    except HttpResponseError as exc:
        logger.exception("Failed to list resources in group")
        return "FAILURE", "", f"Failed to list resources: {exc.message}"

    resource.set_value_for_custom_field(
        "azure_resource_group_resource_count", len(contents)
    )

    if not contents:
        msg = f"Resource group '{rg_name}' is empty."
        logger.info(msg)
        set_progress(msg)
        return "SUCCESS", msg, ""

    by_type = defaultdict(list)
    for item in contents:
        by_type[item.type or "(unknown type)"].append(item.name)

    lines = [f"Resource group '{rg_name}' contains {len(contents)} resource(s):"]
    for azure_type in sorted(by_type):
        names = sorted(by_type[azure_type])
        lines.append(f"{azure_type} ({len(names)}): {', '.join(names)}")

    for line in lines:
        set_progress(line)

    logger.info("Inventoried %s resource(s) in group %s", len(contents), rg_name)
    return "SUCCESS", "\n".join(lines), ""
