# RBAC and Security Patterns for CloudBolt

Critical security and RBAC patterns that MUST be followed in all CloudBolt plugins, regardless of how the repo is laid out.

## RBAC: Environment-Gated ResourceHandler Access

**CRITICAL RULE**: NEVER expose `ResourceHandler` directly to end users. ALWAYS gate access through Environments.

### Why This Matters

CloudBolt's RBAC model controls which **Environments** users can access. Environments belong to Groups, and users have permissions based on their Group membership. If you expose ResourceHandlers directly, you bypass CloudBolt's RBAC system and potentially allow users to access resources they shouldn't see.

### The Correct Pattern

#### ❌ WRONG: Direct ResourceHandler Selection
```python
# NEVER DO THIS - bypasses RBAC
def generate_options_for_resource_handler(**kwargs):
    from resourcehandlers.aws.models import AWSHandler
    handlers = AWSHandler.objects.all()
    return [(h.id, h.name) for h in handlers]

def run(job, **kwargs):
    rh_id = int("{{ resource_handler }}")
    rh = AWSHandler.objects.get(id=rh_id)
    # User may not have permission to this handler!
```

#### ❌ WRONG: Reimplementing Entitlement with `group__in`
```python
# NEVER DO THIS - drops unconstrained environments (no groups assigned at all),
# which every group is entitled to order into. Also misses ancestor-group grants.
def generate_options_for_env_id(**kwargs):
    group = kwargs.get("group")
    envs = Environment.objects.filter(group__in=[group])
    return [(env.id, env.name) for env in envs]
```

#### ✅ CORRECT: Environment-Gated Access via `group.get_available_environments()`
```python
from accounts.models import Group
from infrastructure.models import Environment


def _resolve_group(group):
    """The group kwarg arrives as a Group or as its name depending on caller."""
    if group is None or isinstance(group, Group):
        return group
    return Group.objects.filter(name=str(group)).first()


def generate_options_for_env_id(field, **kwargs):
    """
    RBAC-aware environment selection.

    Group.get_available_environments() is the platform's entitlement query. It
    returns the environments explicitly entitled to the requesting group (and
    its ancestor groups) PLUS every unconstrained environment (no groups
    assigned). Users must be able to order into both kinds.
    """
    group = _resolve_group(kwargs.get("group"))
    if not group:
        return []

    # Entitlement comes from the platform; narrow by technology at the DB level.
    available_ids = [env.id for env in group.get_available_environments()]
    envs = Environment.objects.filter(
        id__in=available_ids,
        resource_handler__awshandler__isnull=False,  # Optional: filter by technology
    ).order_by("name")
    if not envs.exists():
        return [("", "------ No AWS environments available ------")]

    return [(env.id, env.name) for env in envs]

def run(job, **kwargs):
    """
    Convert Environment to ResourceHandler inside the plugin.
    User selected an env they have access to, RBAC is honored.
    """
    env_id = int("{{ env_id }}")
    env = Environment.objects.get(id=env_id)

    # NOW get the ResourceHandler - user has proven access via env selection
    rh = env.resource_handler.cast()  # Cast to specific type (AWSHandler, AzureARMHandler, etc.)

    # Use rh for API operations
    client = rh.get_boto3_client("us-east-1", "s3")
```

### Environment Dropdown Standard

Every Environment selector — build plugins, Day-2 actions, generated-options plugins — follows the same three steps:

1. **Resolve the `group` kwarg.** It arrives as a `Group` instance *or* as the group's name depending on the caller (order form vs. API), so use `_resolve_group()` above rather than assuming either.
2. **Get the entitled set from the platform:** `group.get_available_environments()`. Do not reimplement entitlement in the plugin — `Environment.objects.filter(group__in=[group])` drops unconstrained environments, and `Q(group__in=[group]) | Q(group__isnull=True)` misses ancestor-group entitlements and any future platform rules.
3. **Narrow by technology at the DB level:** `Environment.objects.filter(id__in=available_ids, resource_handler__<handler>__isnull=False)`. Reverse names in use in this repo: `azurearmhandler`, `awshandler`, `ovirthandler` (OpenShift Virtualization).

Return `[]` when there is no group, and a single placeholder row when the entitled set is empty after narrowing.

### Additional Environment Filtering

Further restrictions are handler-attribute filters layered on top of the entitled set — the entitlement query itself never changes:

```python
def generate_options_for_env_id(field, **kwargs):
    """Filter to AWS management account environments only."""
    group = _resolve_group(kwargs.get("group"))
    if not group:
        return []

    available_ids = [env.id for env in group.get_available_environments()]
    envs = Environment.objects.filter(
        id__in=available_ids,
        resource_handler__awshandler__is_management_account=True,
    ).order_by("name")

    return [(env.id, env.name) for env in envs]
```

## Parameter Security

### `{{ }}` Only for Declared Inputs — Never in Comments or Prose

CloudBolt renders the **entire** plugin script through its template engine *before* the Python ever executes — comments and docstrings are not exempt. Every `{{ ... }}` token is treated as a variable to resolve, so a stray pair anywhere will be substituted (often with an empty string) or break rendering outright.

```python
# ❌ WRONG — the engine tries to resolve {{ outputs.build.bucket }} in this comment
# The bucket name comes from the build step via {{ outputs.build.bucket }}.
bucket = "{{ bucket_name }}"

# ✅ CORRECT — describe the variable in plain words; only the real input uses braces
# The bucket name comes from the build step's outputs.
bucket = "{{ bucket_name }}"
```

**Rule:** only ever write `{{ }}` to inject a parameter you actually declared as a plugin input. In comments, log strings, and docstrings, refer to a value by name in plain English — never with literal `{{ }}` braces.

### Always Quote Template Variables

**CRITICAL**: Template variables are user-controlled input. ALWAYS quote them to prevent Python injection.

#### ❌ WRONG: Unquoted Variables
```python
# DANGEROUS - allows arbitrary Python code injection
api_key = {{ api_key }}
port = {{ port }}
```

If user enters `__import__('os').system('rm -rf /')`, it will execute!

#### ✅ CORRECT: Quoted Variables
```python
# SAFE - variables are strings, can't execute code
api_key = "{{ api_key }}"
port = int("{{ port }}")
enabled = "{{ enabled }}" == "true"
```

### Safe List Parsing

For list/dict parameters from multi-select or complex inputs:

```python
import ast

# SAFE - ast.literal_eval only evaluates literals, not code
items = ast.literal_eval("""{{ items }}""")  # Triple quotes for multi-line

# items is now a Python list/dict that can be used safely
for item in items:
    process(item)
```

### Type Casting

Always cast to the expected type:

```python
# Strings (ALWAYS quote)
bucket_name = "{{ bucket_name }}"
region = "{{ region }}"

# Integers
port = int("{{ port }}")
count = int("{{ count }}")

# Booleans
enabled = "{{ enabled }}".strip().lower() == "true"
is_public = "{{ is_public }}" == "true"

# Lists
tags = ast.literal_eval("""{{ tags }}""")

# Dicts
config = ast.literal_eval("""{{ config }}""")
```

## Custom Field Security

### Namespace to Avoid Collisions

Always prefix custom fields with service name:

```python
# GOOD - namespaced
CustomField.objects.get_or_create(
    name="aws_s3_bucket_name",
    defaults=dict(label="AWS S3 Bucket Name", type="STR")
)

# BAD - generic, could collide with other blueprints
CustomField.objects.get_or_create(
    name="bucket_name",  # Too generic!
    defaults=dict(label="Bucket Name", type="STR")
)
```

### Store Resource Handler ID, Not Environment ID

For teardown and Day 2 operations, you need the ResourceHandler to make API calls. Environment IDs can change or become inaccessible.

```python
# CORRECT - store RH ID
def run(job, **kwargs):
    # In build plugin
    env_id = int("{{ env_id }}")
    env = Environment.objects.get(id=env_id)
    rh = env.resource_handler.cast()

    # Store RH ID for later use
    resource = job.resource_set.first()
    resource.set_value_for_custom_field("aws_s3_bucket_rh_id", rh.id)

# Later in teardown
def run(job, **kwargs):
    resource = job.resource_set.first()
    rh_id = resource.get_value_for_custom_field("aws_s3_bucket_rh_id")

    from resourcehandlers.aws.models import AWSHandler
    rh = AWSHandler.objects.get(id=rh_id)
    # Now can make API calls
```

### Pre-Create Custom Fields in Build/Teardown

Discovery auto-creates fields, but Build and Teardown must pre-create them:

```python
def _ensure_custom_fields():
    """
    Pre-create custom fields before use.
    Using get_or_create makes this idempotent.
    """
    CustomField.objects.get_or_create(
        name="azure_key_vault_id",
        defaults=dict(
            label="Azure Key Vault ID",
            description="Azure Resource ID for the Key Vault",
            type="STR",
            show_on_servers=False,
        ),
    )
    # Create all fields the plugin will use
    # ...

def run(job, **kwargs):
    _ensure_custom_fields()  # Call BEFORE setting values

    resource = job.resource_set.first()
    resource.set_value_for_custom_field("azure_key_vault_id", vault_id)
```

## Sensitive Data Handling

### Password-Type Fields

For API keys, passwords, or other secrets:

```python
CustomField.objects.get_or_create(
    name="my_service_api_key",
    defaults=dict(
        label="API Key",
        description="API key for My Service",
        type="PWD",  # PWD fields are encrypted in database
    ),
)
```

### ConnectionInfo for External Credentials

For external system credentials, use `ConnectionInfo` instead of custom fields:

```python
from utilities.models import ConnectionInfo

# Create/get connection info
conn, created = ConnectionInfo.objects.get_or_create(
    name="My External System",
    defaults=dict(
        ip="api.example.com",
        port=443,
        protocol="https",
        username="api_user",
        password="api_password",  # Stored encrypted
    )
)

# Use in plugins
def run(job, **kwargs):
    conn = ConnectionInfo.objects.get(name="My External System")
    auth = (conn.username, conn.password)
    response = requests.get(f"{conn.protocol}://{conn.ip}", auth=auth)
```

### Secrets and Source Control Repo Sync

When CloudBolt exports content to a repo, **secrets are redacted to placeholder strings** and not round-tripped on import:

| Placeholder string | Affected fields |
|---|---|
| `"YOUR_CREDENTIALS"` | `RemoteScriptHook.credentials`, `CopyFileAction.credentials` |
| `"YOUR_AUTH_INFO"` | `WebHook.auth_header_value` |
| `"YOUR_EMAIL_INFO"` | `EmailHook.from_address`, `EmailHook.send_to_address` |
| `"SOURCE_CODE_URL Redacted"` | Any URL-sourced script's `source_code_url` |

The importer skips re-applying placeholder values, so **secrets must be re-entered after each sync**. This is intentional: the repo is the source of truth for code and metadata structure, not for credentials. For secrets bound to the running CloudBolt instance, use `ConnectionInfo` or `PWD`-type custom fields populated post-sync.

## Logging Security

### Don't Log Sensitive Data

```python
# BAD - logs API key
logger.info(f"Using API key: {api_key}")

# GOOD - redact or omit sensitive data
logger.info("Using configured API key")

# BAD - logs full error with potential secrets
logger.error(f"Auth failed: {response.text}")

# GOOD - log error type without exposing data
logger.error("Authentication failed with status 401")
```

### User-Facing vs Background Logs

```python
from common.methods import set_progress
from utilities.logger import ThreadLogger

logger = ThreadLogger(__name__)

# User sees this in UI - keep it friendly, no secrets
set_progress("Creating S3 bucket 'my-bucket'...")

# Developers see this in logs - can include more detail (but no secrets!)
logger.info("Creating S3 bucket in region us-east-1 for handler ID %s", rh.id)
```

## Discovery RBAC

### Discover Across ALL Handlers (But Results Are Filtered)

Discovery plugins enumerate resources across ALL handlers:

```python
def discover_resources(**kwargs):
    """
    Discover across all handlers.
    CloudBolt will filter results based on user's environment access.
    """
    from resourcehandlers.aws.models import AWSHandler

    discovered = []

    # Loop through ALL handlers
    for handler in AWSHandler.objects.all():
        try:
            client = handler.get_boto3_client("us-east-1", "s3")
            response = client.list_buckets()

            for bucket in response.get("Buckets", []):
                discovered.append({
                    "name": bucket["Name"],
                    "aws_s3_bucket_name": bucket["Name"],
                    "aws_s3_bucket_rh_id": handler.id,
                })
        except Exception as exc:
            logger.warning("Skipping handler %s: %s", handler, exc)
            continue

    # CloudBolt filters these based on user's environment access
    return discovered
```

**Why enumerate all handlers?**
- Users may have access to multiple environments across different handlers.
- CloudBolt automatically filters discovered resources based on the user's permissions.
- This ensures complete coverage without manually implementing RBAC filtering.

**Store Handler ID:**
- Each discovered resource should include the handler ID.
- This allows proper filtering and later operations.
- Use namespaced field: `aws_s3_bucket_rh_id`, `azure_key_vault_rh_id`, etc.

## Error Messages and Security

### Don't Expose Internal Paths or Secrets

```python
# BAD - exposes internal CloudBolt installation details
return "FAILURE", f"Failed to load config from /etc/cloudbolt/config.json", ""

# GOOD - generic message
return "FAILURE", "Failed to load configuration", ""

# BAD - exposes database query
return "FAILURE", f"Database error: {sql_exception.message}", ""

# GOOD - sanitized error
return "FAILURE", "Unable to retrieve data from database", ""
```

### Idempotent Teardown Without Information Leaks

```python
def run(job, **kwargs):
    """Teardown must be idempotent but not leak information."""
    resource = job.resource_set.first()
    bucket_name = resource.get_value_for_custom_field("aws_s3_bucket_name")

    if not bucket_name:
        # Don't reveal why we're skipping - could expose security info
        msg = "Resource metadata missing; assuming already deleted."
        return "WARNING", msg, ""

    try:
        client.delete_bucket(Bucket=bucket_name)
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code")

        if code in {"NoSuchBucket", "404"}:
            # Safe to reveal - bucket not found
            return "WARNING", f"Bucket '{bucket_name}' not found; assuming already deleted.", ""

        if code == "AccessDenied":
            # Don't expose permission details
            return "FAILURE", "Insufficient permissions to delete bucket.", ""

        # Generic error for unexpected issues
        return "FAILURE", "Failed to delete bucket.", ""

    return "SUCCESS", f"Bucket '{bucket_name}' deleted successfully.", ""
```

## Summary Checklist

- [ ] NEVER expose ResourceHandler directly — always use `env_id`
- [ ] Convert Environment to ResourceHandler in `run()` using `rh = env.resource_handler.cast()`
- [ ] Build Environment dropdowns from `group.get_available_environments()` (entitled + unconstrained envs) — never `Environment.objects.filter(group__in=[group])` alone
- [ ] ALWAYS quote template variables: `"{{ var }}"` not `{{ var }}`
- [ ] Use `ast.literal_eval("""{{ list }}""")` for safe list/dict parsing
- [ ] Cast variables to expected types: `int("{{ port }}")`
- [ ] Namespace custom fields: `aws_s3_bucket_name` not `bucket_name`
- [ ] Store RH ID not Environment ID: `azure_rh_id` not `env_id`
- [ ] Pre-create custom fields in Build/Teardown with `get_or_create`
- [ ] Use `PWD` type for sensitive fields
- [ ] Don't log sensitive data (API keys, passwords, tokens)
- [ ] Don't expose internal paths or detailed errors to users
- [ ] Discovery enumerates ALL handlers (CloudBolt filters by user permissions)
- [ ] Make teardown idempotent with `WARNING` (not `FAILURE`) for missing resources
- [ ] Re-enter redacted secrets after every Source Control Repo sync (see [metadata-schemas.md §0](metadata-schemas.md#0-universal-rules) for placeholder strings)
