# MCP Content Testing — Setup Runbook

Stand up what the `cb-test-content` skill needs: an MCP client connected to
your appliance as an admin, the six testing tools from `mcp_tool_actions/`
imported and set to synchronous, and a per-machine config file. A fresh
operator should be able to follow this end to end with no other source. The
procedure the agent runs afterwards is
[agents/testing-loop.md](agents/testing-loop.md); its preflight sends you to
the section here that matches whatever is missing.

## 1. Appliance prerequisites

1. CloudBolt 2026.2.48 or later (MCP Tool Actions). The MCP server is on by
   default at `https://<appliance>/mcp`. Verified on 2026.3.78.
2. A Source Code Repository entry for your fork of this repo: follow
   [README.md, "Using this repo"](../README.md#using-this-repo), step 2. Note
   its label; the sync tool accepts the label or the `SCR-` global id.
3. The account the MCP client signs in with must be a CloudBolt admin (super
   admin or CB admin). `syncFromRepo` is admin-only on the API, and every write
   tool here refuses other callers in code.

## 2. Connect the MCP client

Claude Code:

```bash
claude mcp add --transport http cloudbolt https://<appliance>/mcp
```

The equivalent entry in `.claude.json` is
`"cloudbolt": {"type": "http", "url": "https://<appliance>/mcp"}`. Then run
`/mcp`, choose the server, and authenticate: a browser window opens and you
sign in to CloudBolt as the admin from §1. CloudBolt's MCP server uses OAuth
2.1 with dynamic client registration, and the token must carry the `write`
scope: without it every write tool and every custom tool fails. The profile
page's API token and the `/api/v3/cmp/apiToken/` JWT are not accepted by
`/mcp`.

Any other Streamable HTTP MCP client with OAuth support connects the same way.

Check: the `fetch_identity` tool returns your user with `superAdmin` or
`cmpAdmin` true.

## 3. Import the six tools

| Tool | Folder |
|---|---|
| `sync_from_source_control_repo` | `mcp_tool_actions/MTA-543b19sa` |
| `fetch_job_log` | `mcp_tool_actions/MTA-8j9c0tb0` |
| `run_blueprint_discovery` | `mcp_tool_actions/MTA-9oihpumd` |
| `run_recurring_job` | `mcp_tool_actions/MTA-thkvyt0v` |
| `run_cit_tests` | `mcp_tool_actions/MTA-gnx1ckno` |
| `cancel_jobs` | `mcp_tool_actions/MTA-js655cf3` |

Each tool's plugin imports with it. Pick one of:

**A. One API call** (recommended). Get a token (valid for five minutes), then
post the six paths to your repository's `syncFromRepo` endpoint. Find the `SCR-` id with
`GET https://<appliance>/api/v3/cmp/sourceCodeRepos/`.

```bash
CB_API_TOKEN=$(curl -sS -X POST "https://<appliance>/api/v3/cmp/apiToken/" \
  -H "Content-Type: application/json" \
  -d '{"username": "<admin>", "password": "<password>"}' | python -c "import json,sys; print(json.load(sys.stdin)['token'])")

curl -sS -X POST "https://<appliance>/api/v3/cmp/sourceCodeRepos/<SCR-id>/syncFromRepo/" \
  -H "Authorization: Bearer $CB_API_TOKEN" -H "Content-Type: application/json" \
  -d '{"branch": "main", "refreshIfExists": true, "objectsToSync": {"mcp_tool_actions": [
        "MTA-543b19sa/MTA-543b19sa_metadata.json", "MTA-8j9c0tb0/MTA-8j9c0tb0_metadata.json",
        "MTA-9oihpumd/MTA-9oihpumd_metadata.json", "MTA-thkvyt0v/MTA-thkvyt0v_metadata.json",
        "MTA-gnx1ckno/MTA-gnx1ckno_metadata.json", "MTA-js655cf3/MTA-js655cf3_metadata.json"]}}'
```

The response is the sync job. Watch it under Jobs, or
`GET /api/v3/cmp/jobs/<JOB-id>/`: `outputs` lists each tool with `success`.
It takes a few seconds.

**B. The UI.** Actions > MCP Tool Actions > Create from Repository; choose the
repository, the branch, and one metadata file. The dialog takes one item, so
repeat it six times.

**C. No repository on the appliance.** Build one zip per tool with
`python tools/package_zip.py mcp_tool_actions/MTA-543b19sa` (and the other
five) and upload each at Actions > MCP Tool Actions > Import.

## 4. Turn on Synchronous Action and reconnect

The importer leaves **Synchronous Action** off on first import and the API
refuses to change it (CMPITK-1391). Until it is on, every call returns
`{"success": true, "ids": ["JOB-..."]}` and the agent has to poll instead of
reading the result inline.

1. Actions > MCP Tool Actions > the tool > Edit > tick **Synchronous Action**
   > Save. Six times. Later refreshes from the repository keep it.
2. Optional: on the same form, restrict the write tools (sync, run recurring
   job, run CIT tests, cancel jobs) to a role. The code already refuses
   non-admins; roles narrow it further and never sync.
3. Reconnect the MCP client (`/mcp` > reconnect, or restart it). Tool lists
   are cached per session.

Check: `fetch_mcp_tool_actions` lists the six names, and
`run_mcp_tool_action("fetch_job_log", {"log_job_id": "JOB-00000000"})` answers
inline with a not-found failure. An answer of the form
`{"success": true, "ids": [...]}` means the flag is still off on that tool.

## 5. Per-machine config

Copy `.claude/cb-test.example.json` to `.claude/cb-test.local.json` (it is
gitignored because it names your appliance) and fill in:

| Key | Where to find it |
|---|---|
| `source_code_repo` | The repository label or `SCR-` id from §1 |
| `group_id` | A `GRP-` id the admin may order under: `fetch_orderable_groups` from the client, or the group's API link on its page |
| `resource_name_prefix` | Keep `cbtest` unless it collides with real names |
| `test_server_id`, `test_resource_id` | Optional targets for standalone server and resource actions |

## 6. After every merge

A branch sync points each synced object's remote source URL at that branch.
Once the branch is merged and deleted, sync the same paths from `main`, now
through the tool itself:

```text
run_mcp_tool_action("sync_from_source_control_repo", {
  "sync_branch": "main",
  "sync_paths": "mcp_tool_actions/MTA-543b19sa\nmcp_tool_actions/MTA-8j9c0tb0\n...",
  "sync_repo": "<label or SCR-id>"
})
```

Reconnect the client when a tool's inputs or description changed.

## 7. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| The client lists no CloudBolt tools, or `fetch_identity` is unavailable | Not connected or not authenticated | §2 |
| A tool call fails with a scope or permission error | Token without `write`, or the account is not an admin | §2, §1.3 |
| `fetch_mcp_tool_actions` is missing some of the six names | Not imported, disabled, or the client's cached list | §3, then §4 step 3 |
| `{"success": true, "ids": [...]}` instead of a result | Synchronous Action off | §4 |
| `Parameters not allowed [<name>]` | The input carries a stored default (a value saved on the edit form) | Clear it on the edit form, or re-sync the tool |
| `sync_repo is required because N repositories exist` | Several repositories on the appliance | Pass the label; put it in the config (§5) |
| `Branch '...' contains a slash` | Branch name with `/` | Push the same commit under a slash-free name |
| HTTP 403 from `syncFromRepo`, or `Only CloudBolt admins may ...` | Account is not an admin | §1.3 |
