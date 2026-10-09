# Run Blueprint Discovery

MCP Tool Action published to agents as `custom_run_blueprint_discovery`. Launches the Sync Resources job for one blueprint, the same job the Sync Resources button on the blueprint's Discovery tab starts, so an agent can run a discovery plugin on demand and poll it. Part of [docs/agents/testing-loop.md](../../docs/agents/testing-loop.md).

## Contents
| Role | ID | Name |
|---|---|---|
| MCP Tool Action | MTA-9oihpumd | Run Blueprint Discovery |
| Plugin | [OHK-dwh5kqxq](../../plugins/OHK-dwh5kqxq/) | Run Blueprint Discovery |

## Inputs
| Input | Type | Meaning |
|---|---|---|
| `discovery_blueprint` | string, required | `BP-` global id or the exact blueprint name |

## Output
`jobId` (poll with `fetch_job_log`), `blueprintId`, `discoveryPlugin`, `autoHistoricalResources`.

## Notes
- The caller must manage the blueprint or be a CloudBolt admin. Historical blueprints and blueprints without a discovery plugin are refused.
- With auto-historical resources on, resources the plugin no longer returns are marked Historical. Check the flag in the output before running against a blueprint with live resources.
- Needs the stock `Sync Resources` plug-in and its recurring job action on the appliance.
