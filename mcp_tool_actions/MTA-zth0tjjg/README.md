# Rerun Job

MCP Tool Action published to agents as `custom_rerun_job`. Reruns a finished job exactly like the Rerun button on its job page, which is the v3 `POST /api/v3/cmp/jobs/<id>/clone/` endpoint: the new job reuses the old job's parameters, resource, servers and arguments, and runs the plug-in code as it is now. Use it to rerun a failed blueprint build item on the existing resource after syncing a fix. Part of [docs/agents/testing-loop.md](../../docs/agents/testing-loop.md).

## Contents
| Role | ID | Name |
|---|---|---|
| MCP Tool Action | MTA-zth0tjjg | Rerun Job |
| Plugin | [OHK-ukp8n10w](../../plugins/OHK-ukp8n10w/) | Rerun Job |

## Inputs
| Input | Type | Meaning |
|---|---|---|
| `rerun_job_id` | string, required | `JOB-` global id or the numeric job id |

## Output
`newJobId` (poll with `fetch_job_log`), `rerunOfJobId`, `type`, `label`, `previousStatus`, `parentJobId`, `resourceIds`, `serverIds`, and `cleanupJobIds` (a decom job CloudBolt adds when a failed provision job left a server behind).

## Notes
- The caller needs the `job.rerun` permission on the job. CloudBolt admins have it, and so do job owners through the built-in Job Owner role.
- It refuses a job that is still active, a job already rerun (it names the latest rerun), and any job that CloudBolt's own rule, `Job.rerun_allowed`, refuses. Non-admins can rerun only failed jobs, and never a blueprint deployment or a parameter-change action. Admins can also rerun successful plug-in, trigger, server provision, blueprint deployment and decommission jobs.
- `canBeRequeued` in the v3 job API is not part of that rule: CloudBolt never reads it.
- Rerunning a build item runs that item only. If the deployment stopped at it, the build items after it never ran and the parent `deploy_blueprint` job stays failed.
- The job engine caches shared modules until it restarts, so restart it after syncing a shared module fix and before the rerun.
- Setup (import, Synchronous Action, client reconnect): [docs/mcp-testing-setup.md](../../docs/mcp-testing-setup.md).
