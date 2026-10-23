# Troubleshooting: root cause before retry

Order of investigation for any failure:

1. The tool's own `error_message` and `outputs.error`.
2. An order or job: `onb_failure_trace` with the `ORD-` or `JOB-` id. Read `failedJobs[].errors`, `progressTail`, `hints`, then `fetch_job_log` with `log_job_id` set to the job it names and `log_file_tail: 200` for the traceback.
3. Anything that failed in the UI or with no job: `onb_recent_errors` with `onb_minutes: 10` (add `onb_contains` for a handler name, hostname or error text; `onb_log: jobengine` for provisioning, `authentication` for logins, `mcp` for tool calls).
4. The table below.
5. Explain the cause and the fix in two sentences, give the link, and only then let the user retry.

| Symptom | First tool | Usual cause | Fix |
|---|---|---|---|
| Handler form: "Unable to connect to handler with the information provided" | `onb_recent_errors` with `onb_contains: Failed to validate connection` | Credentials, permissions, TLS, DNS, firewall or proxy; the traceback names which | See the error table in [resource-handlers.md](resource-handlers.md) |
| `onb_verify_resource_handler` returns `verified: false` | its `hints` | Same causes, for a handler that was saved | Edit link, verify again |
| Handler verified but no regions, locations or clusters offered | the import-locations page | Credentials can authenticate but not list (describe) resources; an Azure subscription not yet imported as a child handler | Apply the permissions document; import subscriptions first on Azure |
| `onb_import_os_build` lists no images, or not the image the customer uses | `onb_status` handler `cachedImages` | The image cache is empty on a new appliance, or was filled with the default whitelist | `onb_sync_images` for the handler (no whitelist; enables the cache jobs), wait for the job, list again; an AMI id can be passed directly |
| Order fails with `There is no AMI available in ... for ... in <region>` | `onb_import_os_build` again for that AMI | The AMI was stored without its region (toolkit before this fix), or the image is in another region than the environment | Run the import again: it sets the region and returns `verified: true` only when the environment resolves the image |
| Order form offers no OS build or no network for an environment | `fetch_environment_details`, `onb_status` env counts | The OS build's image is in another region or location; no subnet or network attached | Import an image for that region and attach the OS build; sync subnets or attach a network (sc_nic_0) |
| Order refused before submission with a missing deployment item | the schema | The order payload left out an item | Include every item from `fetch_blueprint_schema`, with `"parameters": {}` when it has none |
| Order `DENIED` or no orderable group or environment | `fetch_orderable_groups`, `fetch_orderable_environments` | Entitlement: the group cannot deploy the blueprint or use the environment | Fix the entitlement on the blueprint or environment, or `manage_blueprint_permissions` |
| Order `PENDING` for long | `fetch_order` | Awaiting approval | `approve_order` as an approver, or that is the test (2.4.1) |
| Provision job fails: `is required for Azure windows servers` | `onb_failure_trace` hints | Windows on Azure needs `password_arm` | Add the parameter to the environment or order form |
| `vmware_datastore parameter must be specified`, `Memory and CPU are required` | `onb_failure_trace` hints | vCenter environment missing pinned parameters | Set datastore, cpu_cnt, mem_size options on the environment |
| `InvalidKeyPair`, `key_name` | `onb_failure_trace` hints | AWS key pair named on the environment does not exist in the region | Pin an existing key pair |
| `InsufficientInstanceCapacity`, `QuotaExceeded` | `onb_failure_trace` hints | Cloud account quota or capacity | Smaller size or another region or zone |
| Guest script step fails (`WinRM`, `SSH`, refused, timed out) | `fetch_job_log` | Credentials on the OS build or server; port 22 or 5985/5986 blocked from the appliance | Fix credentials on the OS build; open the port |
| LDAP user cannot log in | `onb_recent_errors` with `onb_log: authentication` | Bind DN or password, base DN, port 389 vs 636, group DN typo in the mapping | Verify the utility (`onb_ui_link ldap_verify:<id>`), fix the mapping |
| Content Library item install fails, library page empty | `onb_status.contentLibrary`, `onb_recent_errors` with `onb_contains: content.cloudbolt.app` | No egress to content.cloudbolt.app, proxy not set, SSL inspection | Set the proxy (`onb_ui_link proxy`), add the inspecting CA (`ssl_certificates`) |
| A `custom_onb_*` tool is missing from the client | `fetch_mcp_tool_actions` | Client cached the tool list; tool not synced; tool disabled; caller not admin; token without write scope | Reconnect the client; sync and `onb_install`; check the admin flag and scope |
| A toolkit tool answers `{"success": true, "ids": [...]}` | `fetch_job` on the id | Synchronous Action still off on that tool | Run `onb_install`, reconnect; meanwhile poll the job |
| `Parameters not allowed [name]` from `run_mcp_tool_action` | the tool's edit form | A stored default hides the input (every BOOL does after an edit-form save) | Clear the default on the edit form or re-sync the tool |
| `onb_playbook` fails | its `error` | No egress to raw.githubusercontent.com | Open the `browserUrl` in a browser and read the section there |
| Email never arrives (approval, completion, test) | `onb_ui_link email_test`, `onb_recent_errors` with `onb_contains: smtp` | SMTP host, port, TLS or credentials | Fix on `email_settings` (password entered there); or record P7 as not applicable |

When the cause is outside CloudBolt (a cloud permission, a firewall rule, a DNS record), say so, name the owner on the customer side, and record the item `pending` with that note so the scorecard shows what is waiting on whom.
