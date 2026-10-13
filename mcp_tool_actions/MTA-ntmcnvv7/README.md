# Grant Blueprint Permission

MCP Tool Action published to agents as `custom_grant_blueprint_permission`. Gives a group DEPLOY (default) or MANAGE permission on a blueprint, the same row the blueprint's Permissions tab creates. A Source Control Repos sync of a blueprint whose metadata has `any_group_can_deploy: false` removes every group's deploy and manage permission, because the importer keeps only the groups the metadata names and repo exports name none; this tool restores them. Part of [docs/agents/testing-loop.md](../../docs/agents/testing-loop.md).

## Contents
| Role | ID | Name |
|---|---|---|
| MCP Tool Action | MTA-ntmcnvv7 | Grant Blueprint Permission |
| Plugin | [OHK-mgta9oez](../../plugins/OHK-mgta9oez/) | Grant Blueprint Permission |

## Inputs
| Input | Type | Meaning |
|---|---|---|
| `grant_blueprint` | string, required | `BP-` global id or the exact blueprint name |
| `grant_group` | string, required | `GRP-` global id or the exact group name |
| `grant_permission` | string | `DEPLOY` (default) or `MANAGE` |

## Output
`created` (false when the permission already existed), `groupsThatCanDeploy`, `groupsThatCanManage`, `anyGroupCanDeploy`.

## Notes
- Admin only. Idempotent.
- Group bindings never round-trip through the repository, so run it after every sync of such a blueprint, on every appliance.
- Setup (import, Synchronous Action, client reconnect): [docs/mcp-testing-setup.md](../../docs/mcp-testing-setup.md).
