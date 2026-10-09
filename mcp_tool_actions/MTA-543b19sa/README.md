# Sync From Source Control Repo

MCP Tool Action published to agents as `custom_sync_from_source_control_repo`. Creates the same Source Control Repository sync job the REST `syncFromRepo` endpoint creates, so an agent that pushed content to a branch can import or refresh it through MCP and poll the job. Part of the testing loop in [docs/agents/testing-loop.md](../../docs/agents/testing-loop.md).

## Contents
| Role | ID | Name |
|---|---|---|
| MCP Tool Action | MTA-543b19sa | Sync From Source Control Repo |
| Plugin | [OHK-0wtrj2wb](../../plugins/OHK-0wtrj2wb/) | Sync From Source Control Repo |

## Inputs
| Input | Type | Meaning |
|---|---|---|
| `sync_branch` | string, required | Branch or tag; must not contain a slash |
| `sync_paths` | text, required | Repo-relative content paths, one per line: `blueprints/BP-...`, `resource_actions/RSA-.../RSA-..._metadata.json`, ... |
| `sync_repo` | string | `SCR-` id or label; optional when the appliance has one repository |
| `sync_refresh_if_exists` | boolean | Refresh existing objects (default true) |
| `sync_ignore_action_enabled` | boolean | Keep each action's current enabled flag (default false) |

Forms and form functions cannot be listed; they import with the blueprint or action that references them, as do plugins and shared modules.

## Output
`syncJobId` (poll it with `fetch_job_log`), `objectsToSync` as sent to CloudBolt, and `skipped` paths with the reason.

## Notes
- Admin only: the plugin refuses callers who are not CloudBolt admins.
- Every synced object's remote source URL is re-pointed at the branch; sync the same paths from `main` after the branch merges.
- Bootstrap and the Synchronous Action flag: see the prerequisites in testing-loop.md.
