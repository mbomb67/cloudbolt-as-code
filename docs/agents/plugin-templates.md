# CloudBolt Plugin Templates

Comprehensive reference for the Python entry points and templates that go inside `plugins/OHK-<id>/<OHK-id>_script.py` (and the equivalent inline scripts on other content types). The metadata layout that wires these plugins to blueprints, RSAs, recurring jobs, etc. lives in [metadata-schemas.md](metadata-schemas.md).

## Plugin Architecture

CloudBolt CMP is a Django-based hybrid cloud automation platform. In the **Source Control Repos** layout this repo uses, every plugin script lives at `plugins/OHK-<id>/OHK-<id>_script.py` next to its `OHK-<id>_metadata.json`. The plugin is wired into other content by another content unit's `dependencies.hook → "plugins/OHK-<id>"` cross-reference — there is no `<provider>/<service>/` directory structure or `build_<service>.py` naming convention anymore.

**Key Principles:**
- Use small, composable functions.
- Separate CloudBolt orchestration logic from provider/client integration logic.
- Don't introduce new frameworks unless standard or already used by CloudBolt.
- Make all progress messages descriptive and user-friendly.
- **Prefer f-strings for string formatting** — `f"Created bucket '{name}' in {region}"` over `.format()`, `%`, or `+` concatenation; they read best and keep the value next to its placeholder. The one exception is `logger.*` calls, which idiomatically use lazy `%s` args (`logger.info("Discovering buckets for handler %s", handler)`) so the message is only built when that log level is active — that pattern is used throughout these templates and should stay.
- Always consult the `typings/` stubs at the repo root as the **Source of Truth** for CloudBolt API methods. `typings/` is copied from `/var/opt/cloudbolt/proserv/typings` on your appliance and gitignored (see [docs/dev-environment-setup.md](../dev-environment-setup.md)). If it is missing, copy it; never guess the API.

## 1. Build Plugins

**Purpose:** Provision new resources in cloud providers.

**Location:** `plugins/OHK-<id>/OHK-<id>_script.py`. Wired into a blueprint via `blueprints/BP-<id>/BP-<id>_metadata.json.deployment_items[].dependencies.hook = "plugins/OHK-<id>"`.

**Entry Point:** `run(job, **kwargs)`.

**Return Format:** 3-tuple `(status, output_message, errors)`.
- Status: `"SUCCESS"`, `"FAILURE"`, or `"WARNING"`.
- Output message: user-facing.
- Errors: error details (usually empty string).

**Alternative Return:** Dictionary format
```python
return {
    "status": "SUCCESS",
    "output_message": "Resource created",
    "outputs": {"resource_id": "xyz"}  # Available to subsequent steps via {{ outputs.step_name.key }}
}
```

**Name the resource after what it provisions.** When a build plugin takes the provisioned object's name as an action input (e.g. `storage_account_name`, `bucket_name`, `vm_name`), set that value as the CloudBolt resource's own `name` before returning — not just as a custom field:

```python
resource = job.resource_set.first()
resource.name = storage_account_name
resource.set_value_for_custom_field("azure_storage_account_name", storage_account_name)  # still store it for discovery/teardown
resource.save()
```

Without this, the resource keeps the generic blueprint-derived default and the resource list no longer maps 1:1 to the real cloud objects. Store the name in a custom field **as well** — the custom field is what discovery/teardown look up; the `name` is for the human-facing list.

### Build Plugin Template

```python
"""
CloudBolt build plugin for provisioning <Service Name>.

This plugin:
- Describes what it does
- Lists key features
- Notes any special requirements
"""

import boto3  # or appropriate SDK
from botocore.exceptions import ClientError

from accounts.models import Group
from common.methods import set_progress
from infrastructure.models import CustomField, Environment
from utilities.logger import ThreadLogger

logger = ThreadLogger(__name__)


def _ensure_custom_fields():
    """
    Create custom fields needed by this blueprint.
    Uses get_or_create so this is idempotent.
    """
    CustomField.objects.get_or_create(
        name="aws_s3_bucket_name",
        defaults=dict(
            label="AWS S3 Bucket Name",
            description="Name of the AWS S3 bucket backing this resource.",
            type="STR",
        ),
    )
    CustomField.objects.get_or_create(
        name="aws_s3_bucket_region",
        defaults=dict(
            label="AWS S3 Bucket Region",
            description="AWS region where the S3 bucket is created.",
            type="STR",
        ),
    )
    CustomField.objects.get_or_create(
        name="aws_s3_bucket_rh_id",
        defaults=dict(
            label="AWS S3 Bucket Resource Handler ID",
            description="ID of the AWS resource handler used for this bucket.",
            type="INT",
        ),
    )


def _resolve_group(group):
    """The group kwarg arrives as a Group or as its name depending on caller."""
    if group is None or isinstance(group, Group):
        return group
    return Group.objects.filter(name=str(group)).first()


def generate_options_for_env_id(field, **kwargs):
    """
    RBAC-aware Environment selector for this blueprint.

    CRITICAL PATTERN: Always expose env_id (not resource_handler) to end users.
    ResourceHandler access MUST be gated through Environments to honor RBAC.

    - Entitlement comes from group.get_available_environments(): environments
      explicitly entitled to the requesting Group (and its ancestors) PLUS all
      unconstrained environments (no groups assigned). Never group__in alone.
    - Further restricts to Environments backed by an AWS handler.
    """
    group = _resolve_group(kwargs.get("group"))
    if not group:
        # Resolved convention: return [] (not None) when group is missing.
        return []

    available_ids = [env.id for env in group.get_available_environments()]
    envs = Environment.objects.filter(
        id__in=available_ids,
        resource_handler__awshandler__isnull=False,
    ).order_by("name")
    if not envs.exists():
        return [("", "------ No AWS environments available ------")]

    return [(env.id, env.name) for env in envs]


def generate_options_for_region(field, control_value=None, **kwargs):
    """
    Generate region options for the environment's AWS resource handler.

    control_value is the selected env_id.
    """
    if not control_value:
        return [("", "------ Select an environment first ------")]

    try:
        env = Environment.objects.get(id=control_value)
    except (Environment.DoesNotExist, ValueError):
        return [("", "------ Invalid environment ------")]

    rh = getattr(env, "resource_handler", None)
    if not rh or not getattr(rh, "awshandler", None):
        return [("", "------ Environment has no AWS handler ------")]

    aws_rh = rh.cast()
    try:
        client = aws_rh.get_boto3_client("us-east-1", "ec2")
        response = client.describe_regions()
    except Exception as exc:
        logger.warning("Failed to list regions for handler %s: %s", aws_rh, exc)
        return [("", "------ Could not load regions ------")]

    options = [(r["RegionName"], r["RegionName"]) for r in response.get("Regions", [])]
    options.sort(key=lambda x: x[0])
    options.insert(0, ("", "------ Select a region (optional) ------"))
    return options


def run(job, **kwargs):
    """
    Build entry point.

    Expected Action Inputs (auto-discovered from {{ }} templates):
    - env_id (INT, required): Environment ID
    - bucket_name (STR, required)
    - region (STR, optional)
    - enable_versioning (BOOL, optional)
    """
    set_progress("Starting AWS S3 bucket provisioning.")
    logger.info("Starting S3 bucket build plugin for job %s", job.id)

    _ensure_custom_fields()

    # Template-driven inputs (CloudBolt creates action inputs for these)
    env_id_str = "{{ env_id }}".strip()
    bucket_name = "{{ bucket_name }}".strip()
    region_input = "{{ region }}".strip()
    enable_versioning_str = "{{ enable_versioning }}".strip().lower()
    enable_versioning = enable_versioning_str == "true"

    if not env_id_str:
        msg = "Environment is required; please select an Environment."
        logger.error(msg)
        return "FAILURE", msg, ""

    try:
        env_id = int(env_id_str)
    except ValueError:
        msg = f"Invalid env_id value '{env_id_str}'."
        logger.error(msg)
        return "FAILURE", msg, ""

    if not bucket_name:
        msg = "S3 bucket name is required."
        logger.error(msg)
        return "FAILURE", msg, ""

    try:
        env = Environment.objects.get(id=env_id)
    except Environment.DoesNotExist:
        msg = f"Environment with id={env_id} not found."
        logger.error(msg)
        return "FAILURE", msg, ""

    # CRITICAL PATTERN: Convert Environment to ResourceHandler
    # This ensures RBAC is honored - user selected an env they have access to
    rh = env.resource_handler.cast()  # Cast to specific handler type (AWSHandler)
    region = region_input or getattr(env, "aws_region", None) or "us-east-1"

    set_progress(f"Using AWS handler '{rh}' in region '{region}'.")

    # Create boto3 client via handler (AWS pattern)
    client = rh.get_boto3_client(region, "s3")

    set_progress(f"Creating S3 bucket '{bucket_name}' in region '{region}'.")

    create_kwargs = {"Bucket": bucket_name}
    if region != "us-east-1":
        create_kwargs["CreateBucketConfiguration"] = {"LocationConstraint": region}

    try:
        client.create_bucket(**create_kwargs)
    except ClientError as exc:
        error_code = exc.response.get("Error", {}).get("Code")
        if error_code in {"BucketAlreadyOwnedByYou", "BucketAlreadyExists"}:
            msg = f"S3 bucket '{bucket_name}' already exists."
            logger.warning(msg)
            set_progress(msg)
        else:
            msg = f"Error creating S3 bucket '{bucket_name}': {exc}"
            logger.exception(msg)
            return "FAILURE", msg, ""
    else:
        set_progress(f"S3 bucket '{bucket_name}' created successfully.")

    if enable_versioning:
        set_progress(f"Enabling versioning on bucket '{bucket_name}'.")
        client.put_bucket_versioning(
            Bucket=bucket_name,
            VersioningConfiguration={"Status": "Enabled"},
        )

    # Persist metadata on the resource for discovery/teardown
    resource = job.resource_set.first()
    if resource:
        # Name the CloudBolt resource after the thing it represents.
        # When a build plugin takes the provisioned object's name as an input,
        # set it as the resource name too so the resource list reflects reality
        # instead of the generic blueprint-derived default.
        resource.name = bucket_name
        resource.set_value_for_custom_field("aws_s3_bucket_name", bucket_name)
        resource.set_value_for_custom_field("aws_s3_bucket_region", region)
        resource.set_value_for_custom_field("aws_s3_bucket_rh_id", rh.id)
        resource.save()
        set_progress("Stored S3 bucket metadata on the resource.")

    msg = f"AWS S3 bucket '{bucket_name}' is ready."
    logger.info(msg)
    return "SUCCESS", msg, ""
```

## 2. Discovery Plugins

**Purpose:** Inventory existing resources from cloud providers and sync them into CloudBolt.

**Location:** `plugins/OHK-<id>/OHK-<id>_script.py`. Wired into a blueprint via `blueprints/BP-<id>/BP-<id>_metadata.json.discovery_plugin.dependencies.hook = "plugins/OHK-<id>"`.

**Entry Point:** `discover_resources(**kwargs)`.

**Return Format:** List of dictionaries, each representing a resource.

**Required Module-Level Variable:** `RESOURCE_IDENTIFIER` (namespaced field containing the cloud-native unique ID).

### Discovery Plugin Template

```python
"""
CloudBolt discovery plugin for AWS S3 buckets.

This plugin discovers S3 buckets across all configured AWS resource handlers
and returns data suitable for creating/updating CloudBolt Resources.
"""

from typing import Any
import boto3
from botocore.exceptions import ClientError

from utilities.logger import ThreadLogger

logger = ThreadLogger(__name__)

# CloudBolt uses this to know which custom field uniquely identifies the resource
RESOURCE_IDENTIFIER = "aws_s3_bucket_name"


def _get_all_aws_handlers():
    """
    Return all AWS resource handlers known to CloudBolt.

    We import lazily to avoid issues if AWS is not configured.
    """
    from resourcehandlers.aws.models import AWSHandler

    return AWSHandler.objects.all()


def _get_s3_client(handler) -> Any:
    """
    Obtain an S3 client from an AWS handler.
    """
    if hasattr(handler, "get_boto3_client"):
        return handler.get_boto3_client("us-east-1", "s3")

    session = boto3.Session(
        aws_access_key_id=getattr(handler, "serviceaccount", None),
        aws_secret_access_key=getattr(handler, "servicepasswd", None),
        region_name="us-east-1",
    )
    return session.client("s3")


def discover_resources(**kwargs):
    """
    Discover existing S3 buckets across all AWS handlers.

    Returns:
        list[dict]: Each dict describes a potential CloudBolt Resource and MUST include:
          - "name": Display name for the resource (REQUIRED)
          - "aws_s3_bucket_name": Unique identifier (RESOURCE_IDENTIFIER)
          - "aws_s3_bucket_rh_id": ID of the AWS Resource Handler for the bucket
          - All other keys should be namespaced custom fields
    """
    discovered = []

    for handler in _get_all_aws_handlers():
        try:
            client = _get_s3_client(handler)
        except Exception as exc:
            logger.warning("Skipping AWS handler %s due to client error: %s", handler, exc)
            continue

        logger.info("Discovering S3 buckets for AWS handler %s", handler)
        try:
            resp = client.list_buckets()
        except ClientError as exc:
            logger.warning("Error listing buckets for handler %s: %s", handler, exc)
            continue

        for bucket in resp.get("Buckets", []):
            name = bucket.get("Name")
            if not name:
                continue

            # Optionally get region (non-fatal if it fails)
            region = None
            try:
                loc = client.get_bucket_location(Bucket=name)
                region = loc.get("LocationConstraint") or "us-east-1"
            except ClientError:
                region = "us-east-1"

            discovered.append({
                "name": name,  # REQUIRED
                "aws_s3_bucket_name": name,  # RESOURCE_IDENTIFIER
                "aws_s3_bucket_region": region,
                "aws_s3_bucket_rh_id": handler.id,
            })

    return discovered
```

### Discovery Best Practices

1. **Global Discovery:** Loop through ALL resource handlers, not just one.
2. **Mandatory `name`:** Every dict must include a `name` key.
3. **Namespace Fields:** All keys except `name` should be namespaced (e.g., `aws_s3_bucket_*`).
4. **Cloud IDs:** Use cloud-native unique IDs (ARN, Azure Resource ID) as `RESOURCE_IDENTIFIER`.
5. **Hydration:** If `list()` returns "thin" objects, call `get()` for each to fetch full properties.
6. **Auto-Creation:** Discovery auto-creates custom fields from returned dictionaries.
7. **`RESOURCE_IDENTIFIER`:** Must match a custom field name that contains the unique cloud ID.

## 3. Teardown Plugins

**Purpose:** Delete/deprovision resources from cloud providers.

**Location:** `plugins/OHK-<id>/OHK-<id>_script.py`. Wired into a blueprint via `blueprints/BP-<id>/BP-<id>_metadata.json.teardown_items[].dependencies.hook = "plugins/OHK-<id>"`.

**Entry Point:** `run(job, **kwargs)`.

**Critical Requirement:** MUST be idempotent — return `WARNING` (not `FAILURE`) if already deleted.

### Teardown Plugin Template

```python
"""
CloudBolt teardown plugin for deleting an AWS S3 bucket.

This plugin:
- Looks up the bucket name and AWS handler from Resource custom fields.
- Attempts to delete the S3 bucket.
- Returns WARNING (not FAILURE) if the bucket is already gone or not empty.
"""

import boto3
from botocore.exceptions import ClientError

from common.methods import set_progress
from utilities.logger import ThreadLogger

logger = ThreadLogger(__name__)


def _get_resource(job):
    resource = job.resource_set.first()
    if resource is None:
        raise RuntimeError("No resource associated with this job.")
    return resource


def _get_bucket_metadata(resource):
    name = resource.get_value_for_custom_field("aws_s3_bucket_name")
    region = resource.get_value_for_custom_field("aws_s3_bucket_region") or "us-east-1"
    rh_id = resource.get_value_for_custom_field("aws_s3_bucket_rh_id")
    return name, region, rh_id


def _get_s3_client(handler, region: str):
    if hasattr(handler, "get_boto3_client"):
        return handler.get_boto3_client(region, "s3")

    session = boto3.Session(
        aws_access_key_id=getattr(handler, "serviceaccount", None),
        aws_secret_access_key=getattr(handler, "servicepasswd", None),
        region_name=region,
    )
    return session.client("s3")


def run(job, **kwargs):
    """
    Teardown entry point.
    """
    set_progress("Starting teardown of AWS S3 bucket.")
    logger.info("Starting S3 bucket teardown plugin for job %s", job.id)

    try:
        resource = _get_resource(job)
    except Exception as exc:
        msg = f"Unable to locate resource for job: {exc}"
        logger.exception(msg)
        return "WARNING", msg, ""

    bucket_name, region, rh_id = _get_bucket_metadata(resource)

    if not bucket_name:
        msg = "Resource is missing 'aws_s3_bucket_name'; assuming bucket is already deleted."
        logger.warning(msg)
        set_progress(msg)
        return "WARNING", msg, ""

    if not rh_id:
        msg = "Resource is missing 'aws_s3_bucket_rh_id'; cannot rehydrate AWS handler. Assuming bucket already deleted."
        logger.warning(msg)
        set_progress(msg)
        return "WARNING", msg, ""

    set_progress(f"Preparing to delete S3 bucket '{bucket_name}' in region '{region}'.")

    try:
        from resourcehandlers.aws.models import AWSHandler
        handler = AWSHandler.objects.get(id=rh_id)
    except Exception as exc:
        msg = f"Failed to load AWS handler (id={rh_id}): {exc}"
        logger.warning(msg)
        return "WARNING", msg, ""

    try:
        client = _get_s3_client(handler, region)
    except Exception as exc:
        msg = f"Failed to create S3 client for handler {handler}: {exc}"
        logger.exception(msg)
        return "WARNING", msg, ""

    try:
        client.delete_bucket(Bucket=bucket_name)
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code")
        if code in {"NoSuchBucket", "404"}:
            msg = f"S3 bucket '{bucket_name}' not found; assuming already deleted."
            logger.warning(msg)
            set_progress(msg)
            return "WARNING", msg, ""
        if code == "BucketNotEmpty":
            msg = f"S3 bucket '{bucket_name}' is not empty; manual clean-up required."
            logger.warning(msg)
            set_progress(msg)
            return "WARNING", msg, ""

        msg = f"Error deleting S3 bucket '{bucket_name}': {exc}"
        logger.exception(msg)
        return "FAILURE", msg, ""

    msg = f"S3 bucket '{bucket_name}' deleted successfully."
    logger.info(msg)
    set_progress(msg)
    return "SUCCESS", msg, ""
```

## 4. Day 2 Resource Actions

**Purpose:** Manage resources post-provisioning (scale, update, configure, etc.).

**Location:** Plugin script at `plugins/OHK-<id>/OHK-<id>_script.py`; the action itself at `resource_actions/RSA-<id>/RSA-<id>_metadata.json` with `dependencies.hook = "plugins/OHK-<id>"`. The RSA is wired into a blueprint via the blueprint's `management_actions[].dependencies.resource_action = "resource_actions/RSA-<id>"`, or it can be standalone (no blueprint reference).

**Entry Point:** `run(job, resource, **kwargs)` — note the `resource` parameter.

### Day 2 Action Template

```python
"""
A CloudBolt resource action to resize an Azure VM.
"""

def run(job, resource, logger=None, **kwargs):
    """
    Resize the VM.

    Note: resource parameter is provided directly.
    """
    new_size = "{{ new_size }}"
    if new_size is None:
        return "FAILURE", "", "ERROR, did not receive a new size"

    server = kwargs.get("server", None)
    if server is None:
        return (
            "FAILURE",
            "",
            "This plugin only runs against a Server, no Server object given",
        )

    rh = server.resource_handler.cast()
    rh.resize_server(server, new_size)
    server.refresh_info()
    server.refresh_from_db()

    updated_size = server.azurearmserverinfo.node_size
    if updated_size != new_size:
        return (
            "FAILURE",
            "",
            f"After resize, server size is {updated_size}, though we requested {new_size}",
        )

    # Update value of Node Size parameter on the server in CB Database
    server.set_value_for_custom_field("node_size", new_size)

    return (
        "SUCCESS",
        f"VM size updated to {updated_size}",
        "",
    )


def generate_options_for_new_size(**kwargs):
    """
    Get available VM sizes from the server's environment.
    """
    server = kwargs.get("server", None)
    if server:
        env = server.environment
        cfvs = env.get_cfvs_for_custom_field("node_size")
        return [cfv.value for cfv in cfvs]
    return None
```

## 5. XUI (eXtended User Interface) Extensions

**Purpose:** Custom Django-based UI extensions for CloudBolt.

**Location:** `extensions/XUI-<id>/` containing `XUI-<id>_metadata.json` and a nested Django package directory named by metadata `name`. On sync, CloudBolt unpacks the package into `proserv/xui/<name>/` on the running server. The metadata `package_contents` array lists every file in the package; default exported extensions are `.py`, `.html`, `.png`, `.svg`, `.jpg`, `.js`, `.css`, `.json`, `.yaml`, `.yml`, `.rst`, `.md`, `.xml`.

### XUI Package Directory Structure

The package directory inside `extensions/XUI-<id>/<name>/` mirrors the Django app shape CloudBolt installs:

```
extensions/XUI-<id>/
├── XUI-<id>_metadata.json
├── <name>.png                              # optional icon (matches metadata.icon)
└── <name>/                                 # package_contents lists every file below
    ├── __init__.py
    ├── views.py
    ├── urls.py
    ├── forms.py                            # optional
    ├── templates/
    │   └── <name>/
    │       └── *.html
    ├── static/
    │   └── <name>/
    │       ├── css/
    │       ├── js/
    │       └── images/
    └── README.md
```

### XUI URLs Pattern

CloudBolt registers an XUI's URLs at install time; you do not edit a global `xui/urls.py`. The extension's own `urls.py` exports `xui_urlpatterns`:

```python
from django.conf.urls import url
from . import views

xui_urlpatterns = [
    url(
        r"^my-extension/(?P<server_id>\d+)/data-json/$",
        views.data_json,
        name="my_extension_data_json",
    ),
    url(
        r"^my-extension/(?P<server_id>\d+)/action/$",
        views.perform_action,
        name="my_extension_action",
    ),
]
```

### XUI Views Pattern

```python
import time
from django.shortcuts import render, get_object_or_404
from django.contrib import messages
from django.urls import reverse
from django.http import HttpResponseRedirect
from django.utils.html import format_html
from django.utils.translation import ugettext as _

from extensions.views import tab_extension, TabExtensionDelegate
from infrastructure.models import Server
from utilities.decorators import json_view, dialog_view
from utilities.logger import ThreadLogger
from utilities.templatetags import helper_tags

logger = ThreadLogger(__name__)


class TabDelegate(TabExtensionDelegate):
    """Controls when the tab should be displayed."""
    def should_display(self):
        # Add logic to determine if tab should show
        # Example: only show for AWS servers
        return self.instance.resource_handler.resource_technology.slug == "aws"


@tab_extension(
    model=Server,
    title="My Tab",
    delegate=TabDelegate,
    description="Custom tab for servers"
)
def server_tab_my_extension(request, obj_id):
    """Renders the main tab view."""
    server = get_object_or_404(Server, pk=obj_id)

    return render(request, 'my_extension/templates/server_tab.html', dict(
        server=server,
    ))


@json_view
def data_json(request, server_id):
    """Returns JSON data for DataTables or AJAX requests."""
    server = get_object_or_404(Server, pk=server_id)

    # Get data
    data = get_my_data(server)

    # DataTables pagination
    start = int(request.GET.get('iDisplayStart', 0))
    length = int(request.GET.get('iDisplayLength', 10))
    search = request.GET.get('sSearch', '')

    # Filter and paginate
    filtered_data = [row for row in data if search.lower() in str(row).lower()]
    paged_data = filtered_data[start:start + length]

    return {
        "sEcho": int(request.GET.get("sEcho", 1)),
        "iTotalRecords": len(data),
        "iTotalDisplayRecords": len(filtered_data),
        "aaData": paged_data,
    }


@dialog_view
def perform_action(request, server_id):
    """Renders a dialog form for user actions."""
    server = get_object_or_404(Server, pk=server_id)
    action_url = reverse("my_extension_action", args=[server_id])

    if request.method == "POST":
        form = MyActionForm(request.POST)
        if form.is_valid():
            # Process the form
            result = form.save()

            # Create a job
            hook = get_my_hook()
            job = hook.run_as_job(server=server, **result)[0]

            msg = format_html(
                _("Job {job_name} has been created.").format(
                    job_name=helper_tags.render_simple_link(job)
                )
            )
            messages.info(request, msg)
            return HttpResponseRedirect(request.META["HTTP_REFERER"])
    else:
        form = MyActionForm()

    return {
        "title": "Perform Action",
        "form": form,
        "use_ajax": True,
        "action_url": action_url,
        "submit": "Execute",
    }
```

## 6. Inbound Webhook Plugins

**Purpose:** Synchronous HTTP endpoints (`webhooks/IWH-*` → `dependencies.hook` → `plugins/OHK-*`). Typical use in this repo: serving dropdown options to SurveyJS custom forms via `choicesByUrl`, for fields inside a Dynamic Panel that are not plugin inputs (so `generate_options_for_*` cannot serve them). Runtime contract and auth: [metadata-schemas.md §9](metadata-schemas.md#runtime-contract-verified-against-cloudbolt-source). Working example: `plugins/OHK-fx500o2r` (Form Options) with `shared_modules/SHM-r0oq14r7` (`env_options`).

```python
from utilities.logger import ThreadLogger
from shared_modules.env_options import (
    entitled_environment, options_for, profile_may_act_for_group, resolve_group,
)

logger = ThreadLogger(__name__)


def _fail(status, message):
    # Non-200: return BOTH keys; anything else in the dict is dropped.
    return {"iwh_status_code": status, "iwh_embedded_response": {"options": [], "error": message}}


def inbound_web_hook_get(*args, parameters=None, profile=None, **kwargs):
    """GET: `parameters` is request.GET (strings). `profile` is the caller's
    UserProfile, or None for token-mode/anonymous calls. No request object."""
    if profile is None:
        return _fail(403, "Authentication required.")
    group = resolve_group(parameters.get("group"))          # href, global ID or name
    if group is None or not profile_may_act_for_group(profile, group):
        return _fail(403, "Not a member of that group.")
    if not parameters.get("env_id"):
        # SurveyJS fires every choicesByUrl on load, before controllers have
        # values: return an empty list. (An empty-value "hint" option renders
        # as "[object Object]" in the dropdown, so don't.)
        return {"options": []}
    env = entitled_environment(group, parameters.get("env_id"), profile=profile)
    if env is None:
        return _fail(403, "Group is not entitled to that environment.")
    try:
        return {"options": options_for(parameters.get("source"), env)}
    except Exception as exc:  # noqa: BLE001 -- an uncaught exception is a 500 whose text reaches the browser
        logger.exception("webhook failed")
        return _fail(400, str(exc))
```

Rules:
- The IWH endpoint does **no RBAC** — any authenticated user (normal mode) or anyone with the token (token mode) can call it. Enforce membership and entitlement yourself, as above.
- Query-string names `filter` and `last` are reserved; do not use them. Values arrive as strings.
- Return a plain dict for 200; the `iwh_status_code` / `iwh_embedded_response` pair for anything else.
- `action_inputs` on the IWH or its plugin have no runtime effect. Read every input from `parameters`.
- Prefer `authentication_method: "normal"` for browser-called hooks. Token mode has no user, and a missing `token` in metadata imports as an empty token that an empty `?token=` satisfies.
- The form side: `choicesByUrl: {"url": "/api/v3/cmp/inboundWebHooks/<uri_path>/run/?source=…&group={group}&env_id={plugin-bdi-<id>.env_id}", "path": "options", "valueName": "value", "titleName": "title", "allowEmptyResponse": true}`. `{group}` is the relative href the standard group dropdown submits (`/api/v3/cmp/groups/GRP-…/`).
- To read a blueprint's pinned `parameter_defaults` server-side instead of duplicating them as hidden form fields: `ServiceItem.objects.get(global_id="BDI-…").cast().input_mappings` → `{m.hook_input.name: m.default_value.value}`; names carry an `_a<hookid>` suffix. `env_options.service_item_defaults()` wraps this.

## Parameter Handling Patterns

### Custom forms and pinned defaults

**Observed on a live instance (2026-09): when a custom form is attached, a deployment item's `parameter_defaults` are not applied to the plugin's inputs.** Every pinned coordinate must therefore also appear in the form as a hidden text question with a `defaultValue`, e.g. `{"type": "text", "name": "plugin-bdi-<id>.tfc_project", "visible": false, "defaultValue": "...", "isRequired": true}` (see `forms/FRM-t3v8zpb7`, `forms/FRM-84n18crj`). A BDI `parameter_defaults` copy is optional once the form carries the values (`BP-b0qm83lh` omits it); if you keep one, keep the two copies identical. Server-side code that needs a pinned value (e.g. an inbound webhook) should read the BDI's `input_mappings` rather than trust the form.

Two ways a custom form fills a dropdown:
- **A declared plugin input** → `/api/v3/cmp/customForms/{custom_form_id}/parameterOptions/plugin-bdi-<id>.<input>/?group={group}&blueprint={blueprint_id}&service_item=BDI-<id>[&inputs={"env_id": "{plugin-bdi-<id>.env_id}"}]`, which runs the plugin's `generate_options_for_<input>`. `inputs` is parsed into `control_value_dict`; `control_value` is set only when exactly one controller exists, and only REGENOPTIONS dependencies count.
- **A field inside a Dynamic Panel** (not a plugin input) → an inbound webhook (section 6 above).

### Basic Template Variables
```python
# String
api_key = "{{ api_key }}"

# Integer
port = int("{{ port }}")

# Boolean
enabled = "{{ enabled }}".lower() == "true"

# List (from multi-select)
import ast
items = ast.literal_eval("""{{ items }}""")
```

### Generate Options Functions

**Basic Options**:
```python
def generate_options_for_field_name(**kwargs):
    """Return list of tuples (value, label)."""
    return [
        ("value1", "Label 1"),
        ("value2", "Label 2"),
    ]
```

**With Dependencies**:
```python
def generate_options_for_region(control_value=None, **kwargs):
    """
    control_value is the value of the controlling field.
    """
    if not control_value:
        return [("", "------ Select environment first ------")]

    env = Environment.objects.get(id=control_value)
    # ... fetch regions for this environment
    return [(r.name, r.name) for r in regions]
```

**Multiple Dependencies**:
```python
def generate_options_for_disk_size(control_value_dict=None, **kwargs):
    """
    control_value_dict contains multiple controlling fields.
    """
    if not control_value_dict:
        return []

    node_size = control_value_dict.get('node_size')
    os_build = control_value_dict.get('os_build')

    # Logic using both values
    return options
```

> Both dependency forms above require a matching `REGENOPTIONS` field dependency in the action's metadata for **each** controller — otherwise `control_value`/`control_value_dict` arrives empty and the generator never re-fires. Declare them per [metadata-schemas.md → Parameter dependencies](metadata-schemas.md#parameter-dependencies-field_dependency__set).

**Rich UI Options**:
```python
def get_options_list(field, **kwargs):
    """Advanced return format for rich UI."""
    options = [
        {
            'value': 'v1',
            'label': 'Standard',
            'description': 'Standard performance tier',
            'icon': 'fa-server'
        },
        {
            'value': 'v2',
            'label': 'Premium',
            'description': 'High performance tier',
            'icon': 'fa-bolt'
        },
    ]
    return {
        'options': options,
        'initial_value': 'v1',
        'sort': False,  # Don't sort alphabetically
        'override': True,  # Bypass standard constraints
    }
```

## Custom Field Management

### Creating Custom Fields

```python
from infrastructure.models import CustomField

# Method 1: get_or_create (idempotent)
CustomField.objects.get_or_create(
    name="aws_s3_bucket_name",
    defaults=dict(
        label="AWS S3 Bucket Name",
        description="Name of the AWS S3 bucket backing this resource.",
        type="STR",
        show_on_servers=False,
    ),
)

# Method 2: Using c2_wrapper (for hooks)
from c2_wrapper import create_custom_field

create_custom_field(
    "azure_key_vault_id",
    "Azure Key Vault ID",
    "STR",
    description="Azure Resource ID for the Key Vault",
    show_on_servers=False,
)
```

### Setting Custom Field Values

```python
# On a Resource
resource.set_value_for_custom_field("aws_s3_bucket_name", "my-bucket")

# On a Server
server.set_value_for_custom_field("node_size", "Standard_D2s_v3")
```

### Getting Custom Field Values

```python
# Get casted value (STR, INT, etc.)
bucket_name = resource.get_value_for_custom_field("aws_s3_bucket_name")

# Get CustomFieldValue object
cfv = resource.get_cfv_for_custom_field("aws_s3_bucket_name")
value = cfv.value if cfv else None
```

### Custom Field Types
- `STR`: String
- `INT`: Integer
- `DT`: DateTime
- `BOOL`: Boolean
- `TXT`: Text (long string)
- `PWD`: Password (encrypted)
- `URL`: URL
- `IP`: IP Address
- `CODE`: Code (monospace)

Full enum: see [metadata-schemas.md §0 Universal Rules](metadata-schemas.md#0-universal-rules) for the complete CustomField `ATTR_TYPES` set.

## Common Import Patterns

```python
# Progress and Logging
from common.methods import set_progress
from utilities.logger import ThreadLogger

# Models
from accounts.models import Group
from infrastructure.models import CustomField, Environment, Server
from resources.models import Resource
from resourcehandlers.aws.models import AWSHandler
from resourcehandlers.azure_arm.models import AzureARMHandler

# Utilities
from utilities.decorators import json_view, dialog_view
from c2_wrapper import create_custom_field, create_hook

# Cloud SDKs
import boto3
from botocore.exceptions import ClientError
from azure.mgmt.keyvault import KeyVaultManagementClient
from resourcehandlers.azure_arm.azure_wrapper import configure_arm_client
```

## Testing and Debugging

### Logging Best Practices

```python
from utilities.logger import ThreadLogger
from common.methods import set_progress

logger = ThreadLogger(__name__)

# User-facing progress (shows in UI)
set_progress("Creating S3 bucket...")

# Background logging (for debugging)
logger.info("Starting bucket creation for job %s", job.id)
logger.warning("Bucket already exists: %s", bucket_name)
logger.error("Failed to create bucket: %s", exc)
logger.exception("Unhandled exception occurred")  # Includes traceback
```

### Error Handling

```python
try:
    # Cloud operation
    client.create_bucket(Bucket=bucket_name)
except ClientError as exc:
    error_code = exc.response.get("Error", {}).get("Code")

    # Handle specific errors
    if error_code == "BucketAlreadyExists":
        return "WARNING", "Bucket already exists", ""
    elif error_code == "AccessDenied":
        return "FAILURE", "Insufficient permissions", ""
    else:
        logger.exception("Unexpected error")
        return "FAILURE", f"Failed to create bucket: {exc}", ""
```

## Blueprint Metadata JSON

For the authoritative metadata JSON shape of a blueprint (and every other content type), see [metadata-schemas.md](metadata-schemas.md). The blueprint's `BP-<id>_metadata.json` declares the build/teardown plugin wiring, day-2 actions, parameters, and resource type — but the Python code remains here as templates.

## Type Stubs and API Discovery

**Location:** `typings/` at the repo root.

These are the **Source of Truth** for CloudBolt's internal API. When you need to know what methods or fields are available on CloudBolt models, check here.

**Common Type Stub Paths**:
- `typings/resources/models.pyi` — Resource model
- `typings/infrastructure/models.pyi` — Server, Environment, CustomField models
- `typings/resourcehandlers/*/models.pyi` — Resource handler models
- `typings/cbhooks/models.pyi` — Hook models
- `typings/jobs/models.pyi` — Job models

**Example Usage**:
```bash
# Find methods available on Resource
grep "def " typings/resources/models.pyi
```

## Provider-Specific Integration Patterns

### Azure Integration

```python
from resourcehandlers.azure_arm.models import AzureARMHandler
from resourcehandlers.azure_arm.azure_wrapper import configure_arm_client
from azure.mgmt.keyvault import KeyVaultManagementClient

def run(job, **kwargs):
    # Get handler from environment
    env_id = int("{{ env_id }}")
    env = Environment.objects.get(id=env_id)
    rh = env.resource_handler.cast()  # Returns AzureARMHandler

    # Get API wrapper
    wrapper = rh.get_api_wrapper()

    # Configure specific ARM client
    kv_client = configure_arm_client(wrapper, KeyVaultManagementClient)

    # Get subscription and tenant from handler (never hard-code)
    subscription_id = wrapper.subscription_id
    tenant_id = wrapper.tenant_id

    # Use client
    vaults = kv_client.vaults.list_by_subscription()
```

**Key Points:**
- Use `handler.get_api_wrapper()` for Azure operations.
- Use `configure_arm_client(wrapper, ClientClass)` to get SDK clients.
- Get tenant/subscription from handler, never hard-code.
- If `list()` returns thin objects, call `get()` to hydrate full properties.

### AWS Integration

```python
from resourcehandlers.aws.models import AWSHandler

def run(job, **kwargs):
    # Get handler from environment
    env_id = int("{{ env_id }}")
    env = Environment.objects.get(id=env_id)
    rh = env.resource_handler.cast()  # Returns AWSHandler

    # Get region from env or input
    region = "{{ region }}" or env.aws_region or "us-east-1"

    # Get boto3 client via handler
    client = rh.get_boto3_client(region, "s3")

    # For paginated results, use paginators
    paginator = client.get_paginator('list_objects_v2')
    for page in paginator.paginate(Bucket=bucket_name):
        for obj in page.get('Contents', []):
            # Process object
            pass
```

**Key Points:**
- Use `rh.get_boto3_client(region, service)` for AWS operations.
- Use paginators for large result sets.
- Get region from environment or parameter.

## Shared Modules

**Purpose:** Reusable integration classes for external systems.

**Location:** `shared_modules/SHM-<id>/SHM-<id>_script.py`. The metadata's `module_name` field declares the import path: `from shared_modules.<module_name> import ClassName`.

### Shared Module Template

```python
from utilities.models import ConnectionInfo
from resources.models import Resource
from common.methods import set_progress
from utilities.logger import ThreadLogger
import requests

logger = ThreadLogger(__name__)

class MyIntegration:
    """Integration with External System."""

    def __init__(self, conn_info_id):
        self.conn_info = ConnectionInfo.objects.get(id=conn_info_id)
        self.base_url = f"{self.conn_info.protocol}://{self.conn_info.ip}"
        self.headers = self._get_headers()

    def _get_headers(self):
        """Build authentication headers."""
        import base64
        creds = f"{self.conn_info.username}:{self.conn_info.password}"
        auth = base64.b64encode(creds.encode()).decode()
        return {
            "Authorization": f"Basic {auth}",
            "Content-Type": "application/json"
        }

    def request(self, method, endpoint, data=None):
        """Generic request wrapper."""
        url = f"{self.base_url}{endpoint}"
        r = requests.request(method, url, headers=self.headers, json=data, verify=False)
        r.raise_for_status()
        return r.json()

    def sync_to_resource(self, resource: Resource, data: dict):
        """Map external data to CloudBolt custom fields."""
        for key, value in data.items():
            resource.set_value_for_custom_field(f"my_integration_{key}", value)
```

**Best Practices:**
- Wrap external APIs in classes that accept `ConnectionInfo` or `ResourceHandler`.
- Provide clear CRUD methods.
- Include polling/waiting methods for async operations.
- Add mapping methods to write results to Resource custom fields.
- Use `ThreadLogger` for background logging, `set_progress` for user-facing messages.
- **Never guess external APIs** — see [external-apis.md](external-apis.md). Reference the vendor's official docs at every call site.

## Checklist for New Blueprints

1. **Create Folders:** `blueprints/BP-<id>/` for the blueprint metadata, plus one or more `plugins/OHK-<id>/` folders for the plugin scripts.
2. **Build Plugin:**
   - [ ] Create `plugins/OHK-<id>/OHK-<id>_script.py` with `run(job, **kwargs)`.
   - [ ] Pair it with `plugins/OHK-<id>/OHK-<id>_metadata.json` (`type: "CloudBolt Plug-in"`, `script_filename`).
   - [ ] Wire it from `blueprints/BP-<id>/BP-<id>_metadata.json.deployment_items[].dependencies.hook = "plugins/OHK-<id>"`.
   - [ ] Create custom fields with `_ensure_custom_fields()`.
   - [ ] Implement RBAC-aware `generate_options_for_env_id()` built from `group.get_available_environments()` — entitled + unconstrained envs, never `group__in=[group]` alone (NEVER expose `resource_handler` directly).
   - [ ] Convert `env_id` to ResourceHandler in `run()` using `rh = env.resource_handler.cast()`.
   - [ ] Add dependent field generators as needed.
   - [ ] Store metadata: RH ID (not env ID), cloud ID, region, etc.
   - [ ] Use provider-specific patterns (Azure: `get_api_wrapper`; AWS: `get_boto3_client`).
   - [ ] Test with multiple environments.
3. **Discovery Plugin:**
   - [ ] Create `plugins/OHK-<id>/OHK-<id>_script.py` with `discover_resources(**kwargs)`.
   - [ ] Wire it via `blueprints/BP-<id>/BP-<id>_metadata.json.discovery_plugin.dependencies.hook`.
   - [ ] Define `RESOURCE_IDENTIFIER` (namespaced cloud ID field).
   - [ ] Loop through ALL handlers, not just one.
   - [ ] Include `name` key in all returned dicts.
   - [ ] Namespace all other keys.
   - [ ] Hydrate thin objects with `get()` calls (especially for Azure).
4. **Teardown Plugin:**
   - [ ] Create `plugins/OHK-<id>/OHK-<id>_script.py` with `run(job, **kwargs)`.
   - [ ] Wire it via `blueprints/BP-<id>/BP-<id>_metadata.json.teardown_items[].dependencies.hook`.
   - [ ] Make idempotent (return `WARNING` if already gone).
   - [ ] Handle missing metadata gracefully.
5. **Day 2 Actions** (optional):
   - [ ] Create `resource_actions/RSA-<id>/RSA-<id>_metadata.json` referencing a plugin via `dependencies.hook`.
   - [ ] Wire each into the blueprint's `management_actions[]` if you want them on the Management tab; otherwise leave standalone.
   - [ ] Implement `run(job, resource, **kwargs)` in the referenced plugin.
6. **Documentation:**
   - [ ] Add inline docstrings.
   - [ ] Note any external APIs used and link to vendor docs (see [external-apis.md](external-apis.md)).
7. **Testing:**
   - [ ] Test build → discovery → teardown flow.
   - [ ] Test RBAC (different groups, verify `env_id` gating).
   - [ ] Test idempotency.
   - [ ] Test error conditions.
   - [ ] Verify all progress messages are user-friendly.
