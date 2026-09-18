# Common CloudBolt Implementation Patterns

Real-world patterns and code snippets extracted from production plugins. Use these as copy-paste references when authoring scripts in `plugins/OHK-<id>/OHK-<id>_script.py` or `shared_modules/SHM-<id>/SHM-<id>_script.py`.

## Azure Authentication Patterns

### CRITICAL: Getting Azure Credentials from Handler

**WRONG — These attributes don't exist on wrapper:**
```python
wrapper = rh.get_api_wrapper()
client_id = wrapper.client_id  # ❌ AttributeError!
client_secret = wrapper.client_secret  # ❌ AttributeError!
```

**CORRECT — Get credentials from handler:**
```python
rh = env.resource_handler.cast()
client_id = rh.client_id  # ✅ From handler
client_secret = rh.secret  # ✅ Note: it's "secret", not "client_secret"
tenant_id = getattr(rh, "azure_tenant_id", None) or getattr(rh, "tenant_id", None)
```

### Using Azure SDK Clients (Recommended)

For Azure SDK clients, use `wrapper.credentials`:

```python
from azure.mgmt.keyvault import KeyVaultManagementClient
from resourcehandlers.azure_arm.azure_wrapper import configure_arm_client

rh = env.resource_handler.cast()
wrapper = rh.get_api_wrapper()

# Use wrapper.credentials for Azure SDK authentication
kv_client = configure_arm_client(wrapper, KeyVaultManagementClient)

# Get tenant ID from handler
tenant_id = rh.azure_tenant_id
```

### Using Direct REST API Calls

For direct REST API calls with `requests`, get raw credentials from handler. **Always reference Microsoft's current OAuth2/Azure REST documentation when constructing the token URL, scopes, and API versions — see [external-apis.md](external-apis.md):**

```python
import requests

rh = env.resource_handler.cast()
client_id = rh.client_id
client_secret = rh.secret
tenant_id = getattr(rh, "azure_tenant_id", None) or getattr(rh, "tenant_id", None)

# Get OAuth2 token — verify endpoint and scope against current Azure docs at the call site
token_url = f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token"
token_data = {
    "grant_type": "client_credentials",
    "client_id": client_id,
    "client_secret": client_secret,
    "scope": "https://management.azure.com/.default"
}
token_response = requests.post(token_url, data=token_data)
token = token_response.json()["access_token"]

# Make authenticated API call
headers = {"Authorization": f"Bearer {token}"}
response = requests.get(api_url, headers=headers)
```

## Custom Field Creation Patterns

### Using c2_wrapper (Recommended — Simpler)

```python
from c2_wrapper import create_custom_field

def create_custom_fields():
    """Pre-create custom fields before setting values."""
    create_custom_field("azure_key_vault_id", "Azure Resource ID", "STR")
    create_custom_field("azure_key_vault_vault_uri", "Vault URI", "STR")
    create_custom_field("azure_key_vault_rh_id", "Resource Handler ID", "STR")
    create_custom_field("azure_key_vault_name", "Vault Name", "STR")
    create_custom_field("azure_key_vault_location", "Location", "STR")
```

### Using CustomField.objects (More Verbose)

```python
from infrastructure.models import CustomField

def _ensure_custom_fields():
    CustomField.objects.get_or_create(
        name="aws_s3_bucket_name",
        defaults=dict(
            label="AWS S3 Bucket Name",
            description="Name of the AWS S3 bucket",
            type="STR",
            show_on_servers=False,
        ),
    )
```

## RBAC-Aware Environment Selection

**Standard — the only accepted pattern.** Build every Environment dropdown from `group.get_available_environments()`. It returns the environments explicitly entitled to the requesting group (and its ancestors) **plus** all unconstrained environments (no groups assigned), and users must be able to order into both. `Environment.objects.filter(group__in=[group])` is not an alternative: it silently drops unconstrained environments. Full rule and rationale: [rbac-and-security.md → Environment Dropdown Standard](rbac-and-security.md#environment-dropdown-standard).

```python
from accounts.models import Group
from infrastructure.models import Environment


def _resolve_group(group):
    """The group kwarg arrives as a Group or as its name depending on caller."""
    if group is None or isinstance(group, Group):
        return group
    return Group.objects.filter(name=str(group)).first()


def generate_options_for_env_id(field, **kwargs):
    group = _resolve_group(kwargs.get("group"))
    if not group:
        # Resolved convention: return [] (not None) when group is missing.
        return []

    # Entitlement from the platform; technology filter at the DB level.
    available_ids = [env.id for env in group.get_available_environments()]
    envs = Environment.objects.filter(
        id__in=available_ids,
        resource_handler__azurearmhandler__isnull=False,  # or awshandler, ovirthandler, ...
    ).order_by("name")
    if not envs.exists():
        return [("", "------ No Azure environments available ------")]

    return [(env.id, env.name) for env in envs]
```

The `group` kwarg arrives as a `Group` or as its name depending on the caller, which is why `_resolve_group()` accepts both. Live examples: `plugins/OHK-qev70tpa` (Azure), `plugins/OHK-7987st2p` (Azure), `plugins/OHK-prew0osh` (OpenShift Virtualization).

## Azure Handler Specifics

### Accessing Azure Subscription and Tenant

```python
from resourcehandlers.azure_arm.models import AzureARMHandler

def run(job, **kwargs):
    env = Environment.objects.get(id=env_id)
    handler = env.resource_handler.cast()  # Returns AzureARMHandler

    # Get wrapper
    wrapper = handler.get_api_wrapper()

    # Access Azure-specific attributes
    subscription_id = handler.serviceaccount  # CloudBolt stores subscription ID here
    tenant_id = handler.azure_tenant_id

    # Use in SDK calls
    from azure.mgmt.keyvault import KeyVaultManagementClient
    kv_client = configure_arm_client(wrapper, KeyVaultManagementClient)
```

### Using SubscriptionClient Directly

```python
from azure.mgmt.resource import SubscriptionClient

def generate_options_for_location(field, control_value=None, **kwargs):
    if not control_value:
        return [('', '------ Please select an environment first ------')]

    env = Environment.objects.get(id=control_value)
    rh = env.resource_handler.cast()
    wrapper = rh.get_api_wrapper()
    subscription_id = rh.serviceaccount

    # Use SubscriptionClient directly with credentials
    sub_client = SubscriptionClient(wrapper.credentials)
    locations = sub_client.subscriptions.list_locations(subscription_id)

    # Filter out staging locations
    options = [
        (loc.name, loc.display_name) for loc in locations
        if "stage" not in loc.name.lower()
    ]
    options.sort(key=lambda x: x[1])

    return options
```

### Parsing Azure Resource IDs

Azure Resource IDs follow this format:
`/subscriptions/{sub}/resourceGroups/{rg}/providers/{provider}/{type}/{name}`

```python
# Example: Parse resource group from vault ID
vault_id = "/subscriptions/abc-123/resourceGroups/my-rg/providers/Microsoft.KeyVault/vaults/my-vault"
rg_name = vault_id.split("/")[4]  # Gets "my-rg"

# Standard index positions:
# [0] = ""
# [1] = "subscriptions"
# [2] = subscription_id
# [3] = "resourceGroups"
# [4] = resource_group_name
# [5] = "providers"
# [6] = provider_namespace
# [7] = resource_type
# [8] = resource_name
```

## Discovery Hydration Pattern (Critical for Azure)

Many Azure SDK `list()` operations return "thin" objects without full properties. You MUST call `get()` to hydrate:

```python
from resourcehandlers.azure_arm.models import AzureARMHandler
from azure.mgmt.keyvault import KeyVaultManagementClient
from resourcehandlers.azure_arm.azure_wrapper import configure_arm_client

RESOURCE_IDENTIFIER = "azure_key_vault_id"

def discover_resources(**kwargs):
    discovered_vaults = []

    # Global Discovery: Loop through ALL handlers
    rhs = AzureARMHandler.objects.all()

    for rh in rhs:
        try:
            wrapper = rh.get_api_wrapper()
            kv_client = configure_arm_client(wrapper, KeyVaultManagementClient)

            # list() returns "thin" objects lacking properties
            for vault_thin in kv_client.vaults.list():
                v_name = vault_thin.name
                rg_name = vault_thin.id.split("/")[4]  # Parse RG from ID

                try:
                    # CRITICAL: Call get() to fetch full object with properties
                    vault = kv_client.vaults.get(rg_name, v_name)

                    discovered_vaults.append({
                        "name": v_name,  # REQUIRED
                        "azure_key_vault_id": vault.id,  # RESOURCE_IDENTIFIER
                        "azure_key_vault_vault_uri": vault.properties.vault_uri,
                        "azure_key_vault_rh_id": str(rh.id),
                        "azure_key_vault_name": v_name,
                        "azure_key_vault_location": vault.location,
                        "azure_key_vault_sku": vault.properties.sku.name if vault.properties.sku else "standard",
                    })
                except Exception as e:
                    # Log but continue - don't fail entire discovery for one vault
                    set_progress(f"Could not fetch full details for vault {v_name}: {str(e)}")
                    continue
        except Exception as e:
            set_progress(f"Error discovering vaults for handler {rh.name}: {str(e)}")
            continue

    return discovered_vaults
```

## Advanced Generate Options Patterns

### Returning Dict Format for Enhanced UI

```python
def generate_options_for_sku(field, **kwargs):
    """
    Return dict format for initial_value and sort control.
    """
    from azure.mgmt.keyvault.models import SkuName

    options = []
    for sku in SkuName:
        options.append((sku.value, sku.value.capitalize()))

    return {
        'options': options,
        'initial_value': 'standard',  # Pre-select this option
        'sort': True,  # Sort alphabetically
        'override': True,  # Optional: bypass validation
    }
```

### Dependent Options with Resource Group

```python
from resourcehandlers.azure_arm.models import AzureARMHandler
from azure.mgmt.resource import ResourceManagementClient
from resourcehandlers.azure_arm.azure_wrapper import configure_arm_client

def generate_options_for_resource_group(field, control_value=None, **kwargs):
    """
    Dynamically generate resource groups based on selected environment.
    """
    if not control_value:
        return [('', '------ Please select an environment first ------')]

    try:
        env = Environment.objects.get(id=control_value)
    except Environment.DoesNotExist:
        return []

    rh = env.resource_handler.cast()

    # Type check for safety
    if not isinstance(rh, AzureARMHandler):
        return []

    wrapper = rh.get_api_wrapper()
    resource_client = configure_arm_client(wrapper, ResourceManagementClient)

    # List all resource groups in subscription
    rgs = resource_client.resource_groups.list()
    return [(rg.name, rg.name) for rg in rgs]
```

### Multiple Dependencies with control_value_dict

```python
def generate_options_for_subnet(field, control_value_dict=None, **kwargs):
    """
    Options depend on both environment AND virtual network selection.
    """
    if not control_value_dict:
        return [('', '------ Please make all selections ------')]

    env_id = control_value_dict.get('env_id')
    vnet_name = control_value_dict.get('virtual_network')

    if not env_id or not vnet_name:
        return [('', '------ Please select environment and VNet ------')]

    # Fetch subnets for this VNet
    env = Environment.objects.get(id=env_id)
    rh = env.resource_handler.cast()
    wrapper = rh.get_api_wrapper()

    from azure.mgmt.network import NetworkManagementClient
    network_client = configure_arm_client(wrapper, NetworkManagementClient)

    # Parse resource group from VNet name if needed, or get from previous selection
    rg_name = control_value_dict.get('resource_group')
    vnet = network_client.virtual_networks.get(rg_name, vnet_name)

    return [(subnet.name, subnet.name) for subnet in vnet.subnets]
```

> **The generator alone does nothing.** `control_value` / `control_value_dict` only arrive populated if the field's controllers are declared as `REGENOPTIONS` field dependencies in the metadata. A method that reads `control_value_dict.get("virtual_network")` without a matching dependency on `virtual_network` will always receive an empty dict and stay stuck on its placeholder. Wire the metadata for every controller — see [metadata-schemas.md → Parameter dependencies](metadata-schemas.md#parameter-dependencies-field_dependency__set).

## Template Variable Fallback Handling

Sometimes template variables aren't rendered (like in async tasks). Handle gracefully:

```python
def run(job, **kwargs):
    vault_name = "{{ vault_name }}"
    sku_name = "{{ sku }}"

    # Handle template rendering fallback
    if not sku_name or sku_name == "sku":  # "sku" is the raw template string
        sku_name = "standard"  # Use default

    # Validate required fields
    if not vault_name or vault_name == "vault_name":
        return "FAILURE", "Vault name is required", ""
```

## Accessing Job and Resource Context

```python
def run(job, **kwargs):
    """
    job: The CloudBolt Job object
    kwargs: Contains additional context
    """
    # Get the resource being provisioned
    resource = job.resource_set.first()

    # Alternative: from kwargs
    resource = kwargs.get("resource")

    # Get the server (for server-level actions)
    server = kwargs.get("server")

    # Get the order (if part of a blueprint order)
    order = job.order if hasattr(job, 'order') else None

    # Access job properties
    job_id = job.id
    job_type = job.type
    job_owner = job.owner
```

## Error Handling Patterns

### Azure ClientError

```python
from azure.core.exceptions import ResourceNotFoundError, HttpResponseError

try:
    vault = kv_client.vaults.get(rg_name, vault_name)
except ResourceNotFoundError:
    msg = f"Vault '{vault_name}' not found; assuming already deleted."
    return "WARNING", msg, ""
except HttpResponseError as e:
    if e.status_code == 403:
        msg = "Insufficient permissions to access vault."
        return "FAILURE", msg, ""
    else:
        msg = f"Azure API error: {e.message}"
        return "FAILURE", msg, ""
```

### AWS ClientError

```python
from botocore.exceptions import ClientError

try:
    client.delete_bucket(Bucket=bucket_name)
except ClientError as exc:
    code = exc.response.get("Error", {}).get("Code")

    if code in {"NoSuchBucket", "404"}:
        return "WARNING", f"Bucket '{bucket_name}' not found; assuming deleted.", ""

    if code == "BucketNotEmpty":
        return "WARNING", f"Bucket '{bucket_name}' is not empty; manual cleanup required.", ""

    if code == "AccessDenied":
        return "FAILURE", "Insufficient permissions to delete bucket.", ""

    # Unknown error
    msg = f"Error deleting bucket: {exc}"
    logger.exception(msg)
    return "FAILURE", msg, ""
```

## XUI Tab Extension Pattern

```python
from django.shortcuts import render, get_object_or_404
from extensions.views import tab_extension, TabExtensionDelegate
from infrastructure.models import Server

class TabDelegate(TabExtensionDelegate):
    """Control when the tab should be displayed."""

    def should_display(self):
        # Only show for AWS servers
        server = self.instance  # self.instance is the Server object
        if not server.resource_handler:
            return False
        return server.resource_handler.resource_technology.slug == "aws"

@tab_extension(
    model=Server,
    title="My Tab",
    delegate=TabDelegate,
    description="Custom tab for servers"
)
def server_tab_my_extension(request, obj_id):
    """Render the tab content."""
    server = get_object_or_404(Server, pk=obj_id)

    # Fetch data for display
    data = get_my_data(server)

    return render(request, 'my_extension/templates/server_tab.html', {
        'server': server,
        'data': data,
    })
```

## Working with Server Objects

```python
from infrastructure.models import Server

# Access server info
server_name = server.hostname
server_ip = server.ip
power_status = server.power_status  # Values: POWON, POWOFF, PENDING, UNKNOWN

# Get tech-specific info
if hasattr(server, 'azurearmserverinfo'):
    vm_size = server.azurearmserverinfo.node_size
    resource_group = server.azurearmserverinfo.resource_group

if hasattr(server, 'ec2serverinfo'):
    instance_id = server.ec2serverinfo.instance_id
    instance_type = server.ec2serverinfo.instance_type
    region = server.ec2serverinfo.ec2_region

# Set custom field values
server.set_value_for_custom_field("node_size", "Standard_D4s_v3")

# Refresh server info from provider
server.refresh_info()
server.refresh_from_db()
```

## Common Import Patterns

```python
# Core CloudBolt
from common.methods import set_progress
from utilities.logger import ThreadLogger
from c2_wrapper import create_custom_field, create_hook

# Models
from accounts.models import Group, UserProfile
from infrastructure.models import CustomField, Environment, Server
from resources.models import Resource
from cbhooks.models import CloudBoltHook, OrchestrationHook
from utilities.models import ConnectionInfo

# Resource Handlers
from resourcehandlers.aws.models import AWSHandler
from resourcehandlers.azure_arm.models import AzureARMHandler
from resourcehandlers.azure_arm.azure_wrapper import configure_arm_client

# Azure SDKs
from azure.mgmt.keyvault import KeyVaultManagementClient
from azure.mgmt.resource import ResourceManagementClient, SubscriptionClient
from azure.mgmt.network import NetworkManagementClient
from azure.core.exceptions import ResourceNotFoundError, HttpResponseError

# AWS SDKs
import boto3
from botocore.exceptions import ClientError

# Django
from django.shortcuts import render, get_object_or_404
from django.http import HttpResponseRedirect
from django.urls import reverse

# XUI
from extensions.views import tab_extension, TabExtensionDelegate
from utilities.decorators import json_view, dialog_view
```

## Real-World Checklist Additions

When implementing a new blueprint, also ensure:

- [ ] Handle template variable fallback (check for raw template string values)
- [ ] For Azure: Call `get()` to hydrate thin objects from `list()` operations
- [ ] Parse Azure Resource IDs correctly when extracting components
- [ ] Filter out staging/test locations in region lists
- [ ] Build Environment dropdowns from `group.get_available_environments()` (entitled + unconstrained envs); never `group__in=[group]` alone
- [ ] Access handler-specific attributes (`azure_tenant_id`, `serviceaccount`)
- [ ] Return dict format from `generate_options` when you need `initial_value` or sort control
- [ ] Handle both `ResourceNotFoundError` and generic `HttpResponseError` for Azure
- [ ] Check specific error codes for AWS `ClientError` (`NoSuchBucket`, `BucketNotEmpty`, etc.)
- [ ] Use `isinstance()` checks before casting handlers to specific types
- [ ] Log and continue (don't fail) when individual items error during discovery
- [ ] Call `server.refresh_info()` and `server.refresh_from_db()` after provider API changes
- [ ] When calling any external vendor API, reference the vendor's current documentation — see [external-apis.md](external-apis.md). Do not extrapolate endpoint shapes from memory.
