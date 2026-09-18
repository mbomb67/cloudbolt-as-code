"""
CloudBolt build plugin: Azure Resource Group.

Creates an Azure Resource Group for the "Azure Resource Group" blueprint and
stores the identifying metadata on the CloudBolt Resource so the teardown,
discovery, and day-2 plugins can find it again.

Expected Action Inputs (declared in OHK-7c5xywbx_metadata.json):
  - env_id (INT, required)              : Azure Environment (gates RBAC)
  - resource_group_name (STR, required) : 1-90 chars, may not end with a period
  - rg_tags (TXT, optional)             : ADDITIONAL tags, one "key=value" per line
                                          (or comma separated)

Tags: governance tags are not typed by the user. The shared build plugin
"Generate Tags from Resource Handler Tag Map" (OHK-xoajww7v) runs first in the
blueprint, evaluates the Taggable Attributes configured on the Azure resource
handler against the parameters set on this order/Resource, and stores the
result as JSON in the Resource custom field cb_generated_tags. This plugin
merges that dict with any additional rg_tags (generated tags win on a
case-insensitive name conflict), enforces Azure's tag limits, and applies the
union to the new resource group. The plugin still works stand-alone: when the
generator has not run, cb_generated_tags is absent and only rg_tags apply.

The Azure region is NOT prompted: every CloudBolt Azure environment is bound to
a single region, so location is read from the environment's node_location
parameter at run time. The subscription likewise comes from the environment's
resource handler, never from user input.

RBAC: end users select an Environment (env_id); the Azure resource handler is
derived from it inside run() via env.resource_handler.cast(). The handler is
never exposed on the order form. The Environment dropdown lists the Azure
environments entitled to the ordering group plus any Unconstrained environments
(no group entitlement at all), via Group.get_available_environments().
See docs/agents/rbac-and-security.md.

External API: Azure Resource Manager via the azure-mgmt-resource SDK,
authenticated through CloudBolt's configure_arm_client wrapper. Operation shapes
anchored to Microsoft's current docs (cited at each call site) per
docs/agents/external-apis.md — not extrapolated from memory.

Returns a 3-tuple: (status, output_msg, error_msg)
  status: "SUCCESS" | "WARNING" | "FAILURE"
"""

import json
import re

from common.methods import set_progress
from infrastructure.models import CustomField, Environment
from utilities.logger import ThreadLogger

logger = ThreadLogger(__name__)

# Azure resource group naming rules:
# https://learn.microsoft.com/en-us/azure/azure-resource-manager/management/resource-name-rules#microsoftresources
# 1-90 chars; alphanumerics, underscores, parentheses, hyphens, periods; may not end with a period.
_RG_NAME_RE = re.compile(r"^[-\w\._\(\)]{1,90}$", re.UNICODE)

# Resource custom field written by the shared tag-generator plugin (OHK-xoajww7v).
GENERATED_TAGS_CF = "cb_generated_tags"

# Azure tag limits:
# https://learn.microsoft.com/en-us/azure/azure-resource-manager/management/tag-resources#limitations
# - at most 50 tag name/value pairs per resource or resource group
# - tag name <= 512 characters, tag value <= 256 characters
# - tag names may not contain < > % & \ ? /  and are case-insensitive
_AZURE_MAX_TAGS = 50
_AZURE_MAX_TAG_NAME = 512
_AZURE_MAX_TAG_VALUE = 256
_AZURE_TAG_NAME_BAD_CHARS = re.compile(r"[<>%&\\?/]")


def _ensure_custom_fields():
    """Pre-create the custom fields this blueprint persists on its Resource.

    get_or_create makes this idempotent. Discovery auto-creates the same
    namespaced fields, but build/teardown must create them explicitly.
    """
    fields = [
        ("azure_resource_group_name", "Azure Resource Group Name", "STR",
         "Name of the Azure resource group."),
        ("azure_resource_group_id", "Azure Resource Group Resource ID", "STR",
         "Full Azure Resource ID of the resource group."),
        ("azure_resource_group_location", "Azure Resource Group Location", "STR",
         "Azure region of the resource group."),
        ("azure_resource_group_tags", "Azure Resource Group Tags", "TXT",
         "Tags applied to the resource group, one key=value per line."),
        ("azure_resource_group_lock", "Azure Resource Group Lock", "STR",
         "Management lock level on the resource group: None, CanNotDelete, or ReadOnly."),
        ("azure_resource_group_resource_count", "Azure Resource Group Resource Count", "INT",
         "Number of Azure resources contained in the resource group."),
        ("azure_resource_group_rh_id", "Azure Resource Group Resource Handler ID", "INT",
         "ID of the Azure resource handler that owns this resource group."),
    ]
    for name, label, cf_type, description in fields:
        CustomField.objects.get_or_create(
            name=name,
            defaults=dict(
                label=label,
                description=description,
                type=cf_type,
                show_on_servers=False,
            ),
        )


def _env_location(env):
    """Resolve the ARM region name (e.g. 'eastus') from the environment.

    CloudBolt stores an environment's region in the node_location parameter and
    may hold the display form ('East US'); normalize to the ARM form.
    """
    raw = None
    try:
        cfv = env.get_cfv_for_custom_field("node_location")
        raw = getattr(cfv, "value", cfv)
    except Exception:
        raw = None
    if not raw:
        raw = getattr(env, "node_location", None)
    if not raw:
        return None
    return str(raw).strip().lower().replace(" ", "")


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


def _load_generated_tags(resource):
    """Tags produced by the shared tag-generator plugin, or {} when it did not run."""
    if resource is None:
        return {}
    try:
        raw = resource.get_value_for_custom_field(GENERATED_TAGS_CF)
    except Exception:
        return {}
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        logger.warning("Ignoring unparsable %s on resource %s", GENERATED_TAGS_CF, resource.id)
        return {}
    return data if isinstance(data, dict) else {}


def _merge_tags(generated, additional):
    """Union of generated (tag-map) tags and user-supplied additional tags.

    Azure tag names are case-insensitive, so the merge is too. Generated tags
    are governance tags and win on a conflict.
    """
    merged = {}
    seen = set()
    for source in (generated, additional):
        for key, value in source.items():
            lowered = str(key).lower()
            if lowered in seen:
                continue
            seen.add(lowered)
            merged[str(key)] = "" if value is None else str(value)
    return merged


def _sanitize_azure_tags(tags):
    """Apply Azure's tag limits. Returns (clean_tags, warnings)."""
    clean = {}
    warnings = []
    for key, value in tags.items():
        if not key or _AZURE_TAG_NAME_BAD_CHARS.search(key) or len(key) > _AZURE_MAX_TAG_NAME:
            warnings.append("dropped tag '%s': invalid Azure tag name" % key)
            continue
        if len(value) > _AZURE_MAX_TAG_VALUE:
            warnings.append(
                "truncated value of tag '%s' to %d characters" % (key, _AZURE_MAX_TAG_VALUE)
            )
            value = value[:_AZURE_MAX_TAG_VALUE]
        clean[key] = value
    if len(clean) > _AZURE_MAX_TAGS:
        dropped = sorted(clean)[_AZURE_MAX_TAGS:]
        warnings.append(
            "dropped %d tag(s) over Azure's limit of %d: %s"
            % (len(dropped), _AZURE_MAX_TAGS, ", ".join(dropped))
        )
        clean = {k: clean[k] for k in sorted(clean)[:_AZURE_MAX_TAGS]}
    return clean, warnings


def _render_tags(tags):
    """Render a tags dict back to the 'key=value' block stored on the Resource."""
    if not tags:
        return ""
    return "\n".join(f"{key}={value}" for key, value in sorted(tags.items()))


def _resolve_group(group):
    """Return a Group instance from whatever CloudBolt hands the option generator.

    The action-input options path passes the Group object itself; older Forge
    examples assume a group name. Accept an instance, a primary key, or a name.
    """
    if group is None or hasattr(group, "get_available_environments"):
        return group

    from accounts.models import Group

    try:
        return Group.objects.get(pk=int(group))
    except (TypeError, ValueError, Group.DoesNotExist):
        pass
    try:
        return Group.objects.get(name=str(group))
    except Group.DoesNotExist:
        return None


def generate_options_for_env_id(field, **kwargs):
    """RBAC-aware Environment selector, restricted to Azure-backed environments.

    Offers every environment the ordering group is allowed to deploy into:

      * environments explicitly entitled to the group (including entitlements
        inherited from a parent group), and
      * Unconstrained environments -- those with no group entitlement at all,
        which CloudBolt makes available to every group.

    Group.get_available_environments() is the platform's own implementation of
    that union, so the plugin defers to it instead of re-deriving the
    entitlement rules. The tenant/profile arguments scope Unconstrained
    environments the same way the native order form does.
    """
    group = _resolve_group(kwargs.get("group"))
    if group is None:
        return []

    profile = kwargs.get("profile")
    tenant = getattr(profile, "tenant", None) if profile is not None else None
    available = group.get_available_environments(tenant=tenant, profile=profile)
    env_ids = [env.id for env in available]

    envs = Environment.objects.filter(
        id__in=env_ids,
        resource_handler__azurearmhandler__isnull=False,
    ).order_by("name")
    if not envs.exists():
        return [("", "------ No Azure environments available ------")]

    return [(env.id, env.name) for env in envs]


def run(job, **kwargs):
    """Create the Azure Resource Group."""
    set_progress("Starting Azure Resource Group build...")
    logger.info("Azure Resource Group build plugin started for job %s", job.id)

    _ensure_custom_fields()

    env_id_str = "{{ env_id }}".strip()
    rg_name = "{{ resource_group_name }}".strip()
    additional_tags = _parse_tags("""{{ rg_tags }}""")

    # ---- Assemble tags: tag-map generated + additional ----------------------
    resource = kwargs.get("resource") or job.resource_set.first()
    generated_tags = _load_generated_tags(resource)
    tags, tag_warnings = _sanitize_azure_tags(_merge_tags(generated_tags, additional_tags))
    for warning in tag_warnings:
        logger.warning("Tag sanitation: %s", warning)
    if generated_tags:
        set_progress(
            "Using %d tag(s) from the resource handler tag map and %d additional tag(s)."
            % (len(generated_tags), len(additional_tags))
        )

    # ---- Validate inputs ------------------------------------------------
    if not env_id_str:
        return "FAILURE", "", "An Environment is required."
    try:
        env_id = int(env_id_str)
    except ValueError:
        return "FAILURE", "", f"Invalid env_id value '{env_id_str}'."

    if not rg_name:
        return "FAILURE", "", "A resource group name is required."
    if not _RG_NAME_RE.match(rg_name) or rg_name.endswith("."):
        return (
            "FAILURE",
            "",
            "Resource group name must be 1-90 characters of alphanumerics, "
            "underscores, parentheses, hyphens, or periods, and may not end with a period.",
        )

    # ---- Resolve handler from environment (RBAC gate) -------------------
    try:
        env = Environment.objects.get(id=env_id)
    except Environment.DoesNotExist:
        return "FAILURE", "", f"Environment with id={env_id} not found."

    from resourcehandlers.azure_arm.models import AzureARMHandler

    rh = env.resource_handler.cast()
    if not isinstance(rh, AzureARMHandler):
        return "FAILURE", "", "Selected environment is not backed by an Azure handler."

    location = _env_location(env)
    if not location:
        return (
            "FAILURE",
            "",
            f"Environment '{env.name}' has no region set (node_location); "
            "configure the environment's location in CloudBolt.",
        )

    try:
        from azure.core.exceptions import HttpResponseError
        from azure.mgmt.resource import ResourceManagementClient
        from azure.mgmt.resource.resources.models import ResourceGroup
        from resourcehandlers.azure_arm.azure_wrapper import configure_arm_client
    except ImportError as exc:
        logger.exception("azure-mgmt-resource SDK not available")
        return "FAILURE", "", f"Azure Resource Management SDK is not installed: {exc}"

    wrapper = rh.get_api_wrapper()
    resource_client = configure_arm_client(wrapper, ResourceManagementClient)

    # ---- Refuse to adopt a pre-existing group ---------------------------
    # create_or_update is an upsert, so without this check a typo'd name would
    # silently re-tag someone else's resource group and CloudBolt would then
    # delete it on teardown.
    # Docs: https://learn.microsoft.com/en-us/python/api/azure-mgmt-resource/azure.mgmt.resource.resources.operations.resourcegroupsoperations#check-existence
    #       ResourceGroupsOperations.check_existence(resource_group_name) -> bool
    try:
        if resource_client.resource_groups.check_existence(rg_name):
            return (
                "FAILURE",
                "",
                f"A resource group named '{rg_name}' already exists in this subscription. "
                "Choose a different name, or import the existing group with the blueprint's "
                "discovery plugin instead of creating it.",
            )
    except HttpResponseError as exc:
        logger.warning("Existence check for '%s' failed (continuing): %s", rg_name, exc)

    set_progress(f"Creating resource group '{rg_name}' in {location}...")

    # Docs: https://learn.microsoft.com/en-us/python/api/azure-mgmt-resource/azure.mgmt.resource.resources.operations.resourcegroupsoperations#create-or-update
    #       ResourceGroupsOperations.create_or_update(resource_group_name, parameters: ResourceGroup) -> ResourceGroup.
    #       Synchronous — resource group creation is not a long-running operation.
    try:
        resource_group = resource_client.resource_groups.create_or_update(
            rg_name,
            ResourceGroup(location=location, tags=tags or None),
        )
    except HttpResponseError as exc:
        logger.exception("Azure resource group creation failed")
        return "FAILURE", "", f"Failed to create resource group: {exc.message}"

    set_progress(f"Resource group '{rg_name}' created successfully.")

    # ---- Persist metadata on the Resource ------------------------------
    if resource:
        # Name the CloudBolt resource after the Azure object it represents so the
        # resource list maps 1:1 to what is in the portal.
        resource.name = rg_name
        resource.set_value_for_custom_field("azure_resource_group_name", rg_name)
        resource.set_value_for_custom_field("azure_resource_group_id", resource_group.id)
        resource.set_value_for_custom_field("azure_resource_group_location", location)
        resource.set_value_for_custom_field(
            "azure_resource_group_tags", _render_tags(resource_group.tags or tags)
        )
        resource.set_value_for_custom_field("azure_resource_group_lock", "None")
        resource.set_value_for_custom_field("azure_resource_group_resource_count", 0)
        resource.set_value_for_custom_field("azure_resource_group_rh_id", rh.id)
        resource.save()
        set_progress("Stored resource group metadata on the resource.")

    tag_summary = f" with {len(tags)} tag(s)" if tags else ""
    if generated_tags:
        tag_summary += f" ({len(generated_tags)} from the resource handler tag map)"
    if tag_warnings:
        return (
            "WARNING",
            f"Azure Resource Group '{rg_name}' is ready in {location}{tag_summary}.",
            "Some tags were adjusted to fit Azure limits: " + "; ".join(tag_warnings),
        )
    return (
        "SUCCESS",
        f"Azure Resource Group '{rg_name}' is ready in {location}{tag_summary}.",
        "",
    )
