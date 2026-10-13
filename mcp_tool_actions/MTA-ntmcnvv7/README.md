# Manage Blueprint Permissions

MCP Tool Action published to agents as `custom_manage_blueprint_permissions`. Lists, grants, or revokes a group's DEPLOY (default) or MANAGE permission on a blueprint, the same rows the blueprint's Permissions tab manages. A Source Control Repos sync of a blueprint whose metadata has `any_group_can_deploy: false` removes every group's deploy and manage permission, because the importer keeps only the groups the metadata names and repo exports name none; the testing loop lists before the sync, restores afterwards, and revokes any grant it added once the test is over. Part of [docs/agents/testing-loop.md](../../docs/agents/testing-loop.md).

## Contents
| Role | ID | Name |
|---|---|---|
| MCP Tool Action | MTA-ntmcnvv7 | Manage Blueprint Permissions |
| Plugin | [OHK-mgta9oez](../../plugins/OHK-mgta9oez/) | Manage Blueprint Permissions |

## Inputs
| Input | Type | Meaning |
|---|---|---|
| `grant_action` | string | `grant` (default), `revoke`, or `list` |
| `grant_blueprint` | string, required | `BP-` global id or the exact blueprint name |
| `grant_group` | string | `GRP-` global id or the exact group name; required for grant and revoke |
| `grant_permission` | string | `DEPLOY` (default) or `MANAGE` |

## Output
`anyGroupCanDeploy`, `groupsThatCanDeploy` and `groupsThatCanManage` (id and name, every group), plus `changed` for grant and revoke.

## Notes
- Admin only. Idempotent.
- `list` reports every entitled group; the built-in `fetch_orderable_groups` reports only groups the caller belongs to.
- Setup (import, Synchronous Action, client reconnect): [docs/mcp-testing-setup.md](../../docs/mcp-testing-setup.md).
