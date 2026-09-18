"""
CloudBolt Day-2 resource action: Update Tags.

Updates the Azure tags on the resource group backing this resource, in either
merge mode (add/overwrite the supplied keys, leave the rest alone) or replace
mode (the supplied set becomes the complete tag set).

Action Inputs:
  - rg_tags (TXT, required)    : one "key=value" per line (commas also accepted)
  - rg_tag_mode (STR, required): "merge" | "replace"

Azure's PATCH on a resource group replaces the whole tags collection when tags
are supplied, so merge mode reads the current tags first and sends the union.

External API: Azure Resource Manager via azure-mgmt-resource. Operation shapes
anchored to Microsoft's current docs (cited at the call site) per
docs/agents/external-apis.md.

Entry point: run(job, resource, **kwargs) -> (status, output_msg, error_msg)
"""

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


def _resource_client(rh):
    from azure.mgmt.resource import ResourceManagementClient
    from resourcehandlers.azure_arm.azure_wrapper import configure_arm_client

    return configure_arm_client(rh.get_api_wrapper(), ResourceManagementClient)


def _parse_tags(raw):
    """Parse a free-text tag block into an Azure tags dict.

    Accepts one "key=value" per line, or a comma-separated list. Blank entries
    and lines without '=' are ignored. Values may contain '=' (split once).
    """
    tags = {}
    if not raw:
        return tags
    entries = []
    for line in str(raw).splitlines():
        entries.extend(line.split(","))
    for entry in entries:
        entry = entry.strip()
        if not entry or "=" not in entry:
            continue
        key, value = entry.split("=", 1)
        key = key.strip()
        if key:
            tags[key] = value.strip()
    return tags


def _render_tags(tags):
    """Render a tags dict back to the 'key=value' block stored on the Resource."""
    if not tags:
        return ""
    return "\n".join(f"{key}={value}" for key, value in sorted(tags.items()))


def generate_options_for_rg_tag_mode(field, **kwargs):
    """How the supplied tags combine with the tags already on the group."""
    return {
        "options": [
            ("merge", "Merge - add or overwrite these keys, keep the others"),
            ("replace", "Replace - these tags become the complete set"),
        ],
        "initial_value": "merge",
        "sort": False,
    }


def run(job=None, resource=None, **kwargs):
    """Apply the requested tag changes to the resource group."""
    set_progress("Starting Update Tags...")

    resource = _resolve_resource(job, resource, kwargs)

    new_tags = _parse_tags("""{{ rg_tags }}""")
    mode = "{{ rg_tag_mode }}".strip().lower() or "merge"

    if mode not in {"merge", "replace"}:
        return "FAILURE", "", f"Tag mode must be 'merge' or 'replace', got '{mode}'."
    if mode == "merge" and not new_tags:
        return (
            "FAILURE",
            "",
            "No valid tags were supplied. Enter one key=value per line, "
            "or use replace mode to clear all tags.",
        )

    try:
        rg_name, rh = _load_group(resource)
    except Exception as exc:
        logger.warning("Could not resolve resource group from resource: %s", exc)
        return "FAILURE", "", f"Could not resolve the Azure resource group: {exc}"

    try:
        from azure.core.exceptions import HttpResponseError, ResourceNotFoundError
        from azure.mgmt.resource.resources.models import ResourceGroupPatchable
    except ImportError as exc:
        return "FAILURE", "", f"Azure Resource Management SDK is not installed: {exc}"

    resource_client = _resource_client(rh)

    # Azure PATCH replaces the whole tags collection, so merge mode has to read
    # the current tags and send the union.
    # Docs: https://learn.microsoft.com/en-us/python/api/azure-mgmt-resource/azure.mgmt.resource.resources.operations.resourcegroupsoperations#get
    #       ResourceGroupsOperations.get(resource_group_name) -> ResourceGroup
    if mode == "merge":
        try:
            current = resource_client.resource_groups.get(rg_name)
        except ResourceNotFoundError:
            return "FAILURE", "", f"Resource group '{rg_name}' no longer exists in Azure."
        except HttpResponseError as exc:
            logger.exception("Failed to read current tags")
            return "FAILURE", "", f"Failed to read the current tags: {exc.message}"
        final_tags = dict(current.tags or {})
        final_tags.update(new_tags)
    else:
        final_tags = dict(new_tags)

    set_progress(
        f"Applying {len(final_tags)} tag(s) to resource group '{rg_name}' ({mode} mode)..."
    )

    # Docs: https://learn.microsoft.com/en-us/python/api/azure-mgmt-resource/azure.mgmt.resource.resources.operations.resourcegroupsoperations#update
    #       ResourceGroupsOperations.update(resource_group_name, parameters: ResourceGroupPatchable) -> ResourceGroup
    try:
        updated = resource_client.resource_groups.update(
            rg_name,
            ResourceGroupPatchable(tags=final_tags),
        )
    except ResourceNotFoundError:
        return "FAILURE", "", f"Resource group '{rg_name}' no longer exists in Azure."
    except HttpResponseError as exc:
        logger.exception("Failed to update resource group tags")
        return "FAILURE", "", f"Failed to update tags: {exc.message}"

    resource.set_value_for_custom_field(
        "azure_resource_group_tags", _render_tags(updated.tags)
    )

    msg = f"Resource group '{rg_name}' now carries {len(updated.tags or {})} tag(s)."
    logger.info(msg)
    set_progress(msg)
    return "SUCCESS", msg, ""
