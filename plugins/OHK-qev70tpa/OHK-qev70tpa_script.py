"""
CloudBolt build plugin: Azure Storage Account.

Provisions an Azure Storage Account for the "Azure Storage Account" blueprint
and stores the identifying metadata on the CloudBolt Resource so the teardown
and discovery plugins can find it again.

Expected Action Inputs (declared in OHK-qev70tpa_metadata.json):
  - env_id (INT, required)               : Azure Environment (gates RBAC)
  - resource_group (STR, required)       : target resource group
  - storage_account_name (STR, required) : 3-24 chars, lowercase alphanumeric, globally unique
  - sku (STR, required)                  : e.g. "Standard_LRS"
  - account_kind (STR, optional)         : default "StorageV2"
  - access_tier (STR, optional)          : "Hot" | "Cool" (StorageV2/BlobStorage only)

The Azure region is NOT prompted: every CloudBolt Azure environment is bound to
a single region, so location is read from env.node_location at run time.

RBAC: end users select an Environment (env_id); the Azure resource handler is
derived from it inside run() via env.resource_handler.cast(). The handler is
never exposed on the order form. See docs/agents/rbac-and-security.md.

External API: Azure Storage Resource Provider via the azure-mgmt-storage SDK,
authenticated through CloudBolt's configure_arm_client wrapper. Operation shapes
anchored to Microsoft's current docs (cited at each call site) per
docs/agents/external-apis.md — not extrapolated from memory.

Returns a 3-tuple: (status, output_msg, error_msg)
  status: "SUCCESS" | "WARNING" | "FAILURE"
"""

import re

from accounts.models import Group
from common.methods import set_progress
from infrastructure.models import CustomField, Environment
from utilities.logger import ThreadLogger

logger = ThreadLogger(__name__)

# Azure storage account name rules:
# https://learn.microsoft.com/en-us/azure/storage/common/storage-account-overview#storage-account-name
_NAME_RE = re.compile(r"^[a-z0-9]{3,24}$")


def _ensure_custom_fields():
    """Pre-create the custom fields this blueprint persists on its Resource.

    get_or_create makes this idempotent. Discovery auto-creates the same
    namespaced fields, but build/teardown must create them explicitly.
    """
    fields = [
        ("azure_storage_account_name", "Azure Storage Account Name", "STR",
         "Name of the Azure storage account."),
        ("azure_storage_account_id", "Azure Storage Account Resource ID", "STR",
         "Full Azure Resource ID of the storage account."),
        ("azure_storage_account_rg", "Azure Storage Account Resource Group", "STR",
         "Resource group containing the storage account."),
        ("azure_storage_account_location", "Azure Storage Account Location", "STR",
         "Azure region of the storage account."),
        ("azure_storage_account_sku", "Azure Storage Account SKU", "STR",
         "Replication SKU of the storage account."),
        ("azure_storage_account_rh_id", "Azure Storage Account Resource Handler ID", "INT",
         "ID of the Azure resource handler that owns this storage account."),
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


def _resolve_group(group):
    """The group kwarg arrives as a Group or as its name depending on caller."""
    if group is None or isinstance(group, Group):
        return group
    return Group.objects.filter(name=str(group)).first()


def generate_options_for_env_id(field, **kwargs):
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


def generate_options_for_resource_group(field, control_value=None, **kwargs):
    """List resource groups in the selected environment's subscription.

    Docs: https://learn.microsoft.com/en-us/python/api/azure-mgmt-resource/azure.mgmt.resource.resources.operations.resourcegroupsoperations#list
    """
    if not control_value:
        return [("", "------ Select an environment first ------")]

    try:
        env = Environment.objects.get(id=control_value)
    except (Environment.DoesNotExist, ValueError):
        return [("", "------ Invalid environment ------")]

    from resourcehandlers.azure_arm.models import AzureARMHandler

    rh = env.resource_handler.cast()
    if not isinstance(rh, AzureARMHandler):
        return [("", "------ Environment has no Azure handler ------")]

    try:
        from azure.mgmt.resource import ResourceManagementClient
        from resourcehandlers.azure_arm.azure_wrapper import configure_arm_client

        wrapper = rh.get_api_wrapper()
        resource_client = configure_arm_client(wrapper, ResourceManagementClient)
        options = [(rg.name, rg.name) for rg in resource_client.resource_groups.list()]
    except Exception as exc:
        logger.warning("Failed to list resource groups for handler %s: %s", rh, exc)
        return [("", "------ Could not load resource groups ------")]

    options.sort(key=lambda x: x[1])
    return options


def generate_options_for_sku(field, **kwargs):
    """Common Azure Storage SKUs.

    Docs: https://learn.microsoft.com/en-us/rest/api/storagerp/srp_sku_types
    """
    return {
        "options": [
            ("Standard_LRS", "Standard LRS (locally redundant)"),
            ("Standard_ZRS", "Standard ZRS (zone redundant)"),
            ("Standard_GRS", "Standard GRS (geo redundant)"),
            ("Standard_RAGRS", "Standard RA-GRS (read-access geo redundant)"),
            ("Premium_LRS", "Premium LRS (locally redundant)"),
            ("Premium_ZRS", "Premium ZRS (zone redundant)"),
        ],
        "initial_value": "Standard_LRS",
        "sort": False,
    }


def generate_options_for_account_kind(field, **kwargs):
    """Azure Storage account kinds.

    Docs: https://learn.microsoft.com/en-us/python/api/azure-mgmt-storage/azure.mgmt.storage.v2023_01_01.models.kind
    """
    return {
        "options": [
            ("StorageV2", "StorageV2 (general purpose v2)"),
            ("StorageV1", "Storage (general purpose v1)"),
            ("BlobStorage", "BlobStorage"),
            ("BlockBlobStorage", "BlockBlobStorage"),
            ("FileStorage", "FileStorage"),
        ],
        "initial_value": "StorageV2",
        "sort": False,
    }


def generate_options_for_access_tier(field, **kwargs):
    """Blob access tier (StorageV2 / BlobStorage)."""
    return [("Hot", "Hot"), ("Cool", "Cool")]


def run(job, **kwargs):
    """Provision the Azure Storage Account."""
    set_progress("Starting Azure Storage Account build...")
    logger.info("Azure Storage Account build plugin started for job %s", job.id)

    _ensure_custom_fields()

    env_id_str = "{{ env_id }}".strip()
    resource_group = "{{ resource_group }}".strip()
    account_name = "{{ storage_account_name }}".strip().lower()
    sku = "{{ sku }}".strip() or "Standard_LRS"
    account_kind = "{{ account_kind }}".strip() or "StorageV2"
    access_tier = "{{ access_tier }}".strip()

    # ---- Validate inputs ------------------------------------------------
    if not env_id_str:
        return "FAILURE", "", "An Environment is required."
    try:
        env_id = int(env_id_str)
    except ValueError:
        return "FAILURE", "", f"Invalid env_id value '{env_id_str}'."

    if not resource_group:
        return "FAILURE", "", "A resource group is required."
    if not _NAME_RE.match(account_name):
        return (
            "FAILURE",
            "",
            "Storage account name must be 3-24 characters, lowercase letters and numbers only.",
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

    # Each CloudBolt Azure environment is bound to a single region; read it from
    # the environment rather than prompting the user.
    location = getattr(env, "node_location", None)
    if not location:
        return (
            "FAILURE",
            "",
            f"Environment '{env.name}' has no region set (node_location); "
            "configure the environment's location in CloudBolt.",
        )

    set_progress(
        f"Creating storage account '{account_name}' ({sku}, {account_kind}) "
        f"in resource group '{resource_group}' / {location}..."
    )

    # ---- Create via azure-mgmt-storage ---------------------------------
    # Docs: https://learn.microsoft.com/en-us/python/api/azure-mgmt-storage/azure.mgmt.storage.v2023_01_01.operations.storageaccountsoperations#begin-create
    #       StorageAccountsOperations.begin_create(resource_group_name, account_name, parameters) -> LROPoller[StorageAccount]
    try:
        from azure.mgmt.storage import StorageManagementClient
        from azure.mgmt.storage.models import (
            StorageAccountCreateParameters,
            Sku,
            StorageAccountCheckNameAvailabilityParameters,
        )
        from azure.core.exceptions import HttpResponseError
        from resourcehandlers.azure_arm.azure_wrapper import configure_arm_client
    except ImportError as exc:
        logger.exception("azure-mgmt-storage SDK not available")
        return "FAILURE", "", f"Azure Storage SDK is not installed: {exc}"

    wrapper = rh.get_api_wrapper()
    storage_client = configure_arm_client(wrapper, StorageManagementClient)

    # Pre-flight global name availability check.
    # Docs: https://learn.microsoft.com/en-us/python/api/azure-mgmt-storage/azure.mgmt.storage.v2023_01_01.operations.storageaccountsoperations#check-name-availability
    try:
        availability = storage_client.storage_accounts.check_name_availability(
            StorageAccountCheckNameAvailabilityParameters(name=account_name)
        )
        if not availability.name_available:
            return (
                "FAILURE",
                "",
                f"Storage account name '{account_name}' is not available: "
                f"{availability.message or availability.reason}",
            )
    except HttpResponseError as exc:
        logger.warning("Name availability check failed (continuing to create): %s", exc)

    create_params = {
        "sku": Sku(name=sku),
        "kind": account_kind,
        "location": location,
    }
    if access_tier and account_kind in {"StorageV2", "BlobStorage"}:
        create_params["access_tier"] = access_tier

    try:
        poller = storage_client.storage_accounts.begin_create(
            resource_group,
            account_name,
            StorageAccountCreateParameters(**create_params),
        )
        account = poller.result()  # blocks until provisioning completes
    except HttpResponseError as exc:
        logger.exception("Azure storage account creation failed")
        return "FAILURE", "", f"Failed to create storage account: {exc.message}"

    set_progress(f"Storage account '{account_name}' created successfully.")

    # ---- Persist metadata on the Resource ------------------------------
    resource = job.resource_set.first()
    if resource:
        resource.set_value_for_custom_field("azure_storage_account_name", account_name)
        resource.set_value_for_custom_field("azure_storage_account_id", account.id)
        resource.set_value_for_custom_field("azure_storage_account_rg", resource_group)
        resource.set_value_for_custom_field("azure_storage_account_location", location)
        resource.set_value_for_custom_field("azure_storage_account_sku", sku)
        resource.set_value_for_custom_field("azure_storage_account_rh_id", rh.id)
        set_progress("Stored storage account metadata on the resource.")

    return "SUCCESS", f"Azure Storage Account '{account_name}' is ready.", ""
