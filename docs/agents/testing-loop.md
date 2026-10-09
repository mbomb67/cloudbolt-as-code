# Testing content on a live CloudBolt through MCP

Create or change content, then prove it works on an appliance before the PR: push the branch, sync it into CloudBolt, run the content, read the job log, fix, repeat, and remove everything the test created. The procedure is tool-neutral: every step is a CloudBolt MCP tool call, a git command, or a file edit, so any agent with the same MCP connection can follow it. `cb-test-content` under `.claude/skills/` is the Claude Code entry point.

## Prerequisites

1. **An MCP connection to the appliance with `read write` scope.** The built-in CloudBolt MCP tools used here: `fetch_identity`, `fetch_catalog_items`, `fetch_blueprint_schema`, `fetch_orderable_groups`, `fetch_orderable_environments`, `fetch_blueprint_item_parameters`, `order_catalog_item`, `fetch_order`, `approve_order`, `fetch_job`, `fetch_resources`, `fetch_resource_details`, `fetch_resource_actions`, `fetch_resource_action_parameter_options`, `run_resource_action`, `fetch_server_list`, `fetch_server_actions`, `run_server_action`, `fetch_mcp_tool_actions`, `run_mcp_tool_action`, and the `fetch_*_link` tools for the report.
2. **The six testing tools from this repo, synced and enabled on the appliance.** They fill the gaps in the built-in tool set.

   | Tool (`mcp_tool_name`) | Folder | Does |
   |---|---|---|
   | `sync_from_source_control_repo` | `mcp_tool_actions/MTA-543b19sa` | Creates the sync job for a branch and a list of content paths |
   | `fetch_job_log` | `mcp_tool_actions/MTA-8j9c0tb0` | Job status, progress log, child jobs, failed descendants, log-file tail, flattened sync results |
   | `run_blueprint_discovery` | `mcp_tool_actions/MTA-9oihpumd` | Runs one blueprint's discovery plugin (the Sync Resources job) |
   | `run_recurring_job` | `mcp_tool_actions/MTA-thkvyt0v` | Run Now for a recurring job |
   | `run_cit_tests` | `mcp_tool_actions/MTA-gnx1ckno` | Starts a functionaltest job for named CIT tests |
   | `cancel_jobs` | `mcp_tool_actions/MTA-js655cf3` | Cancels hung jobs and their descendants |

   **Bootstrap.** The sync tool cannot install itself. The first time, import the six `mcp_tool_actions/MTA-*` folders from the UI (Actions > MCP Tool Actions > Create from Repository, which pulls each tool's plugin with it) or with `POST /api/v3/cmp/sourceCodeRepos/<SCR-id>/syncFromRepo/`. The importer leaves **Synchronous Action** off (it restores only the tool's name, description, title and hints) and the v3 API refuses `PATCH` on MCP tool actions, so open each tool's edit form once (Actions > MCP Tool Actions > the tool > Edit), tick Synchronous Action, and save; later refreshes keep it. Then reconnect the MCP client, because tool lists are cached per session. Until the flag is set every tool still works, through the job route described below. Every write tool refuses callers who are not CloudBolt admins; roles are optional on top of that.
3. **A local config file**, `.claude/cb-test.local.json`, gitignored because it names an instance. Copy `.claude/cb-test.example.json` and fill it in:

   | Key | Meaning |
   |---|---|
   | `source_code_repo` | The repository's `SCR-` global id or label on the appliance; required when the appliance has more than one repository (the sync tool lists them when it is missing) |
   | `group_id` | `GRP-` id to order under; must be allowed to order the blueprints under test |
   | `resource_name_prefix` | Prefix for everything the loop creates, default `cbtest` |
   | `test_server_id` | `SVR-` id for exercising standalone server actions (optional) |
   | `test_resource_id` | `RES-` id for exercising standalone resource actions (optional) |

   If a key the current test needs is missing, ask the user once and write the answer to the file. Never put these ids in committed files.

## Calling the testing tools

`run_mcp_tool_action(mcp_tool_name="<name>", parameters={...})`. Parameter names are the tool's action-input names (`fetch_mcp_tool_actions` lists them). Two response shapes exist, so handle both every time:

- **Synchronous tool:** the plugin's result comes back inline; read `outputs`.
- **Asynchronous tool:** the response is `{"success": true, "ids": ["JOB-..."]}`; call `fetch_job` on that id and read its `outputs`. If a testing tool answers this way, its Synchronous Action flag is off; tell the user to turn it on, and continue with the job route meanwhile.

Everything that passes through `fetch_job` has its dict keys camelCased by the API renderer, including the path keys inside a sync job's `outputs` (`mcp_tool_actions/MTA-x/MTA-x_metadata.json` arrives as `mcpToolActions/MTA-x/MTA-xMetadata.json`). Match on the global id, or use `fetch_job_log`, whose `syncResults` rows carry the path as a value. A synchronous tool's inline result keeps its keys exactly as the plugin wrote them.

## The loop

Keep a ledger file, `.claude/cb-test-ledger.local.json` (gitignored), with one entry per order, resource, server, and job the loop creates: `{"type": "RES", "id": "RES-...", "name": "...", "blueprint": "BP-...", "status": "active"}`. Update it on every create and delete. The loop is finished only when the ledger holds no active resource or server.

### 0. Scope the sync

1. Changed content: `git diff --name-only origin/main...HEAD` (plus untracked files from `git status --short`). Reduce each path to its content folder, `<top-level dir>/<GLOBAL_ID>`.
2. Replace dependencies with their parents. Plugins, shared modules, forms, and form functions import with the object that references them, and an object imported that way cannot refresh itself afterwards (its remote source URL points at a folder). For each changed `plugins/`, `shared_modules/`, `forms/`, or `form_functions/` folder, grep the metadata for `"<dir>/<GID>"` and sync every referencing blueprint or action instead. List a plugin or shared module directly only when nothing references it.
3. Webhooks are not pulled in by the blueprint whose form calls them; list `webhooks/IWH-*` explicitly when its plugin changed.

### 1. Preflight and push

```bash
python tools/validate_metadata.py
python tools/build_catalog.py
```

Commit, then push to a branch whose name has no slash. CloudBolt reads the branch from one URL path segment, so `claude/feature` syncs as branch `claude` and fails. From a worktree on a slash branch: `git push origin HEAD:refs/heads/<name>` and open the PR with `--head <name>`.

### 2. Sync

```text
run_mcp_tool_action("sync_from_source_control_repo", {
  "sync_branch": "<branch>",
  "sync_paths": "blueprints/BP-...\nresource_actions/RSA-...",
  "sync_repo": "<SCR id or label, if configured>"
})
```

Poll `fetch_job_log` with `syncJobId` every five seconds; a sync takes 5 to 60 seconds. Every `syncResults[].status` must be `success`. Any other status carries CloudBolt's serializer message (missing field, bad enum, dangling reference, template error in a script). Fix it in the repo, commit, push the same branch, sync again. Each fix counts toward the cap in step 4.

### 3. Exercise

Run every function the content has. Record ids in the ledger as they appear.

**Blueprint** (`blueprints/BP-*`), in this order:

1. `fetch_blueprint_schema(blueprint_id, group_id)`. For each server or Terraform item that reports `requires_environment_selection`, call `fetch_orderable_environments` and then `fetch_blueprint_item_parameters` for the chosen environment. Pick any environment the user may order into; when a parameter offers sizes, choose the smallest. Set `resource_name` to `<prefix>-<branch>-<n>`. Supply every plugin input in `deployment_items`; an MCP order bypasses the custom form, so values the form would pin are not applied.
2. `order_catalog_item`. Poll `fetch_order` until the order is `SUCCESS`, `FAILURE`, or `DENIED`. If it is pending approval, `approve_order` (the MCP user is an admin). Add the order and the resulting resource (`fetch_resources` filtered by the name, or the order's details) to the ledger.
3. Discovery, when the blueprint has `discovery_plugin`: `run_blueprint_discovery` and poll. Then `fetch_resources` for the blueprint: the deployed resource must still be the same `RES-` id, not a duplicate, and the progress log must report it as found or updated. A duplicate means the plugin's `RESOURCE_IDENTIFIER` does not match the attributes the build plugin wrote.
4. Every day-2 action: the blueprint's `management_actions[]` in the metadata name the `resource_actions/RSA-*`; `fetch_resource_actions(resource_id)` gives their ids and parameter schema (`fetch_resource_action_parameter_options` for dynamic options). `run_resource_action` each, then poll the job or order it returns. Skip the `Delete` action here.
5. Teardown last: run the `Delete` action from `fetch_resource_actions`, poll, then confirm with `fetch_resource_details` that the resource is historical or gone and that any servers it owned are gone. Remove them from the ledger.

**Standalone resource action** (`resource_actions/RSA-*`): target `test_resource_id` from the config or a resource built in this run; `fetch_resource_actions` to find it, `run_resource_action`, poll.

**Server action** (`server_actions/SVA-*`): target `test_server_id` or a server built in this run; `fetch_server_actions(server_id)`, `run_server_action`, poll.

**Recurring job** (`recurring_jobs/RJB-*`): `run_recurring_job` with the `RJB-` id, poll. Values entered on the job in the UI do not sync, so a job whose inputs are empty after a sync fails by design; say so instead of looping.

**CIT test** (`cit_tests/CIT-*`): `run_cit_tests` with the `CIT-` id, poll; the progress log holds each test's pass or fail.

**Not covered by this loop** (say so in the report): orchestration actions and flow control actions fire only inside a provisioning or decommissioning job, inbound webhooks need an HTTP call with the webhook's own authentication, and UI extensions need a browser.

### 4. Diagnose and fix

On any failure: `run_mcp_tool_action("fetch_job_log", {"log_job_id": "<JOB-id>", "log_file_tail": "200"})`. Read `job.errors`, `failedDescendants[].errors`, the end of `progress`, and `logFileTail.lines` for the traceback. Common causes:

| Symptom | Cause | Fix |
|---|---|---|
| Sync row status `failure` with a serializer message | Metadata problem | Fix the field named; `metadata-schemas.md` |
| `TemplateSyntaxError` or a `NameError` on a value that should be a string | A template token outside a declared input, or an unquoted one | Cardinal rule 3 in AGENTS.md |
| `ModuleNotFoundError: shared_modules.<name>` | Shared module not imported or not refreshed | Sync the parent that declares it under `dependencies.sharedModules` |
| `ImportError` from a plugin that was just changed | Stale copy | Sync the parent again; for a webhook, sync `webhooks/IWH-*` itself |
| Order `DENIED` or no orderable environment | Group or environment entitlement | Pick another environment or group; do not change RBAC in the plugin |
| Delete job fails | Teardown plugin | Fix it, re-sync the blueprint, run Delete again until the resource is gone |
| `Parameters not allowed [<name>]` from `run_mcp_tool_action` | The tool's input carries a stored default, which hides it from the schema (every BOOL input does after the edit form is saved) | Clear the default on the edit form or re-sync the tool; declare booleans as `true`/`false` strings |

Fix in the repo, commit, push the same branch, re-sync only the affected parents, and resume at the failed step (do not re-order a blueprint whose resource still exists). Stop after **five** fix-and-retry iterations for one content unit, run the cleanup in step 5, and report what is still failing with the job ids.

Use `cancel_jobs` on a job that has been `RUNNING` with no new progress message for 15 minutes, then treat it as a failure.

### 5. Clean up

Delete every resource and server in the ledger, newest first, with the `Delete` action; poll each job. A failed delete is a teardown bug: fix the plugin, re-sync the blueprint, delete again. If something is still alive after the cap, list each id with its `fetch_resource_link` or `fetch_server_link` URL at the top of the report; never end a run with an unreported live resource.

### 6. Hand off

1. `python tools/validate_metadata.py`, `python tools/build_catalog.py`, commit.
2. Open the PR from the slash-free branch. In the body list each function exercised with its job or order id and result.
3. After the merge, sync the same paths from `main` (`sync_from_source_control_repo` with `sync_branch: main`). The test sync pointed every object's remote source URL at the branch; once the branch is deleted, Refresh from Remote Source fails until this re-point. If any `mcp_tool_actions/` changed, reconnect the MCP client.

## Verified facts behind this procedure

- `POST /api/v3/cmp/sourceCodeRepos/<id>/syncFromRepo/` (`source_code_repos/api/v3/viewsets/source_code_repo.py`) accepts any branch or tag; keys are camelCase (`objectsToSync`, `refreshIfExists`); each path is `<GID>/<GID>_metadata.json` relative to the type's `path_to_*` directory; the inner keys are `source_code_repos/services.py` `OBJECT_TYPE_MAPPINGS` (`flow_control_actions`, `recurring_action_jobs`, `inbound_web_hooks`, and so on) plus `blueprints`. Proven on 2026-10-09 from a slash-free branch: 4 seconds, and a second sync from `main` re-pointed the object.
- `GitRepo.parse_repo_url` (`source_code_repos/models.py`) takes URL path segment 3 as the branch, so a slash in the branch name breaks the clone.
- Discovery is the `Sync Resources` CloudBoltHook's recurring job action run with `sync_bp_id` (`servicecatalog/views.py sync_resources_on_discovery_tab`). Run Now is `RecurringJob.spawn_new_job()` (`jobs/views.py run_recurring_job`). CIT runs are `Job(type="functionaltest")` with `FunctionalTestParameters` (`cscv/views.py _run_cit_tests`). `Delete` is a built-in `ResourceAction` backed by `cbhooks/hookmodules/delete_resource.py`, so it appears in `fetch_resource_actions`.
- The built-in `fetch_job` returns `output`, `outputs`, and `errors` but no progress messages; those are `ProgressMessage` rows and the per-job log file, which `fetch_job_log` reads.
- MCP tool action plugins run `run(job, **kwargs)` with `profile` as the caller; synchronous tools run inside the web request with `job=None` (`cbhooks/services/mcp_tool_action.py`).
- `MCPToolActionSerializer.create_resource_from_metadata` restores only `mcp_tool_name`, `mcp_tool_description`, `mcp_tool_title` and the hints, so `is_synchronous` is off after the first import; a refresh of an existing tool keeps whatever the UI set. The `mcpToolActions` viewset allows `GET` and `POST` only.
- A BOOL action input renders as a select on the tool's edit form; saving the form stores the selection as a default, after which the parameter leaves the tool schema and `validate_run` answers `Parameters not allowed`. The testing tools therefore take `true`/`false` strings.
- Live run on 2026-10-09 against mb-dev: the six tools imported in one sync (JOB-hvsmfwtl); `fetch_job_log` (JOB-qyejb1f1), `cancel_jobs` (JOB-u7r40x2j), `run_blueprint_discovery` (JOB-hxg52zl0, three resources updated) and `run_recurring_job` (JOB-3mwy1dc5) passed; `run_cit_tests` was exercised only on its refusal path because the appliance's one CIT test provisions a server. `fetch_job_log` found the one plugin bug (a hook-based recurring job has no stored job parameters) from the traceback in one call.
