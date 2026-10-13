# Run Recurring Job

MCP Tool Action published to agents as `custom_run_recurring_job`. Runs a recurring job immediately, exactly like Run Now on the Recurring Jobs page, with the caller as the job's owner. A disabled recurring job still runs, as in the UI. Part of [docs/agents/testing-loop.md](../../docs/agents/testing-loop.md).

## Contents
| Role | ID | Name |
|---|---|---|
| MCP Tool Action | MTA-thkvyt0v | Run Recurring Job |
| Plugin | [OHK-qduf79xu](../../plugins/OHK-qduf79xu/) | Run Recurring Job |

## Inputs
| Input | Type | Meaning |
|---|---|---|
| `recurring_job_ref` | string, required | `RJB-` global id or the exact recurring job name |

## Output
`jobId` (poll with `fetch_job_log`), `recurringJobId`, `recurringJobName`, `enabled`, `schedule`.

## Notes
- Admin only.
- The job runs with the inputs saved on the recurring job. Values entered in the UI do not sync, so a job synced from the repo may need its inputs set before the first run.
- Setup (import, Synchronous Action, client reconnect): [docs/mcp-testing-setup.md](../../docs/mcp-testing-setup.md).
