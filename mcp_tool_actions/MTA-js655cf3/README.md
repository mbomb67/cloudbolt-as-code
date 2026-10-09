# Cancel Jobs

MCP Tool Action published to agents as `custom_cancel_jobs`. Sets each named job and its unfinished descendants to `TO_CANCEL`, the same walk the v3 jobs cancel endpoint performs, so an agent can clear a hung test job before retrying. Finished jobs are left alone. Part of [docs/agents/testing-loop.md](../../docs/agents/testing-loop.md).

## Contents
| Role | ID | Name |
|---|---|---|
| MCP Tool Action | MTA-js655cf3 | Cancel Jobs |
| Plugin | [OHK-2lzwvo0j](../../plugins/OHK-2lzwvo0j/) | Cancel Jobs |

## Inputs
| Input | Type | Meaning |
|---|---|---|
| `cancel_job_ids` | text, required | `JOB-` global ids, one per line or comma-separated |

## Output
`results`: one row per id with `result` (`TO_CANCEL`, `ALREADY_FINISHED`, `NOT_FOUND`, `NOT_PERMITTED`) and `jobsTouched`.

## Notes
- The caller must own each job or be a CloudBolt admin.
- Cancellation is cooperative: a job in `TO_CANCEL` stops at its next checkpoint, so poll `fetch_job_log` before assuming it is gone.
