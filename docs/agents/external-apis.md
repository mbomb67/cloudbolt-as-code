# External Vendor APIs: Do Not Guess

The single highest-leverage correctness rule for CloudBolt plugin development: **when your code calls a third-party service, you must reference that vendor's current official documentation. Do not extrapolate from training data.**

This applies to every external system a CloudBolt plugin touches — Azure ARM, AWS, ServiceNow, Okta, Datadog, ZScaler, Infoblox, vCenter, anything that isn't CloudBolt itself.

## Why This Matters

CloudBolt plugins are nearly all "call vendor X, map the result to a CloudBolt Resource." Guessed API calls fail in customer production environments — against the wrong region, the wrong API version, the wrong auth scope — and the failure mode rarely surfaces in local development. The customer sees the failure first.

LLM training data tends to favor (a) older API versions, (b) the SDK shape that was popular when the data was scraped, and (c) common-case parameters rather than the ones a specific customer's environment needs. Every vendor moves: endpoints get versioned, parameters get renamed, auth scopes get tightened, error codes get reshuffled. The vendor's current docs are the only authoritative source.

## The Rule

Before writing or modifying any code that calls a third-party service:

1. **Fetch the vendor's official documentation** for the specific endpoint or SDK operation you're about to use. Not a blog post, not a Stack Overflow answer, not your memory — the vendor's docs.
2. **Use the contract verbatim.** Endpoint paths, request schemas, response schemas, error codes, retry semantics, pagination, auth scopes, content-types — match what the docs say, not what feels right.
3. **Cite the doc URL** (and API version where applicable) in a code comment at the call site. If the call site would get noisy, put the citation in the PR description instead.
4. **Stop if the docs are unreachable.** Network errors, login walls, or 404s on the doc URL are reasons to escalate, not to guess. Surface the blocker to the user.
5. **Use the vendor's own SDK when one exists** and is already on the CloudBolt environment. The SDK author already encoded the contract; don't reinvent it with raw `requests` calls.

## Skip Clause

You do NOT need to re-fetch vendor docs every time when:
- The operation is already correctly modeled in `typings/` at the repo root.
- The operation is wrapped by an existing `shared_modules/SHM-*` module this repo exercises in working code.
- You are reading an existing CloudBolt-shipped helper (e.g. `rh.get_boto3_client(...)`, `configure_arm_client(...)`) and not changing the underlying call.

In these cases the existing wrapper is the source of truth — verify it's load-bearing on a working production code path, then use it.

## Worked Example: Azure REST

**The wrong thing to do:** write an Azure REST call from memory.

```python
# ❌ DO NOT — endpoint path, API version, and required headers all guessed
import requests

token = get_azure_token(...)
resp = requests.get(
    f"https://management.azure.com/subscriptions/{sub_id}/keyvaults",
    headers={"Authorization": f"Bearer {token}"},
)
```

The path is wrong (`keyvaults` is namespaced under `Microsoft.KeyVault/vaults`), no `api-version` query parameter is set (Azure ARM requires one), and the response shape is whatever you remember.

**The right thing to do:** open the current Microsoft Learn docs for the operation, then mirror it.

```python
# ✅ DO — endpoint, api-version, and response shape all anchored to vendor docs.
# Docs: https://learn.microsoft.com/en-us/rest/api/keyvault/keyvault/vaults/list
#       (Vaults - List, api-version 2023-07-01, REST API for Key Vault Management)
import requests

token = get_azure_token(...)
resp = requests.get(
    f"https://management.azure.com/subscriptions/{sub_id}/providers/Microsoft.KeyVault/vaults",
    headers={"Authorization": f"Bearer {token}"},
    params={"api-version": "2023-07-01"},
)
resp.raise_for_status()
vaults = resp.json().get("value", [])
```

Better yet, use the Azure SDK via CloudBolt's wrapper — the SDK author already encoded all of this:

```python
# ✅ Even better — use the SDK; the wrapper carries the same contract verbatim.
from azure.mgmt.keyvault import KeyVaultManagementClient
from resourcehandlers.azure_arm.azure_wrapper import configure_arm_client

kv_client = configure_arm_client(wrapper, KeyVaultManagementClient)
for vault in kv_client.vaults.list_by_subscription():
    ...
```

## Worked Example: AWS boto3

**The wrong thing to do:** invent parameter names that "feel right."

```python
# ❌ DO NOT — `region` is not a boto3 paginator argument and the call will raise
paginator = client.get_paginator('list_buckets')
for page in paginator.paginate(region="us-east-1"):
    ...
```

**The right thing to do:** open the boto3 docs for the specific operation and match its signature exactly.

```python
# ✅ DO — paginator and method shape match boto3 docs.
# Docs: https://boto3.amazonaws.com/v1/documentation/api/latest/reference/services/s3/client/list_buckets.html
#       (S3.Client.list_buckets — no paginator; flat response with Buckets[] key.)
resp = client.list_buckets()
for bucket in resp.get("Buckets", []):
    ...
```

Or for a genuinely paginated operation:

```python
# ✅ DO — paginator chosen against docs; PaginationConfig from docs.
# Docs: https://boto3.amazonaws.com/v1/documentation/api/latest/reference/services/s3/paginator/ListObjectsV2.html
paginator = client.get_paginator("list_objects_v2")
for page in paginator.paginate(Bucket=bucket_name):
    for obj in page.get("Contents", []):
        ...
```

## When the Vendor Docs Are Behind a Login Wall

Some vendors (Cisco, BeyondTrust, ServiceNow on certain endpoints, internal enterprise systems) put parts of their API docs behind authentication. If you cannot reach the docs:

1. **Stop and surface the blocker** to the user. Don't proceed with a guess.
2. Ask whether (a) the user can supply the relevant doc excerpt, (b) there's an internal Confluence/SharePoint copy you should be pointed at, or (c) an existing `shared_modules/SHM-*` already wraps the call and should be reused.
3. Never substitute a different vendor's docs ("Salesforce's REST conventions are similar, so I'll assume ServiceNow uses the same shape"). They aren't.

## How To Cite

A short comment block at the call site is enough. Include enough to find the page again in a year:

```python
# Docs: https://docs.servicenow.com/bundle/utah-application-development/...
#       (Table API > GET /api/now/table/{tableName}, Utah release.)
resp = requests.get(url, headers=headers, params=params)
```

For a chunk of code that calls several related operations on the same vendor surface, a single citation at the top of the helper function is cleaner than repeating it per call.

## Checklist

Before merging code that calls a vendor API:

- [ ] Did I read the vendor's current docs for the specific operation?
- [ ] Is the endpoint path verbatim from the docs (including provider namespace, API version segment, etc.)?
- [ ] Is the request schema (query params, headers, body) verbatim from the docs?
- [ ] Did I cite the doc URL (and version where applicable) at the call site or in the PR?
- [ ] Am I using the vendor's SDK or the CloudBolt-shipped wrapper when one exists?
- [ ] Did I handle the specific error codes the docs list — not generic `Exception`?
- [ ] If I couldn't reach the docs, did I stop and surface that rather than guess?
