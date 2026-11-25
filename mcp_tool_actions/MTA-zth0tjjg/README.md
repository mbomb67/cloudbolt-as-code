# Rerun Job

MCP Tool Action published to agents as `custom_rerun_job`. Reruns a finished job exactly like the Rerun button on its job page, which is the v3 `POST /api/v3/cmp/jobs/<id>/clone/` endpoint: the new job reuses the old job's parameters, resource, servers and arguments, and runs the plug-in code as it is now. Use it to rerun a failed blueprint build item on the existing resource after syncing a fix. In resume mode it resumes a paused job like the Resume button, which is `PATCH /api/v3/cmp/jobs/<id>/resume/`: a paused blueprint deployment reruns its failed build item and carries on with the items after it. Part of [docs/agents/testing-loop.md](../../docs/agents/testing-loop.md).

## Contents
| Role | ID | Name |
|---|---|---|
| MCP Tool Action | MTA-zth0tjjg | Rerun Job |
| Plugin | [OHK-ukp8n10w](../../plugins/OHK-ukp8n10w/) | Rerun Job |

## Inputs
| Input | Type | Meaning |
|---|---|---|
| `rerun_job_id` | string, required | `JOB-` global id or the numeric job id |
| `rerun_job_mode` | `rerun` (default) or `resume` | Rerun a finished job, or resume a paused one |

## Output
Rerun: `newJobId` (poll with `fetch_job_log`), `rerunOfJobId`, `type`, `label`, `previousStatus`, `parentJobId`, `pausedAncestorJobId` (a paused deployment to resume once the rerun passes), `resourceIds`, `serverIds`, and `cleanupJobIds` (a decom job CloudBolt adds when a failed provision job left a server behind).

Resume: `jobId` (poll with `fetch_job_log`), `jobStatus`, `type`, `label`, `previousStatus`, `parentJobId`, and `failedItemJobIds` (the failed build items the deployment reruns).

## Notes
- The caller needs the `job.rerun` permission on the job. CloudBolt admins have it, and so do job owners through the built-in Job Owner role.
- It refuses a job that is still active, a job already rerun (it names the latest rerun), and any job that CloudBolt's own rule, `Job.rerun_allowed`, refuses. Non-admins can rerun only failed jobs, and never a blueprint deployment or a parameter-change action. Admins can also rerun successful plug-in, trigger, server provision, blueprint deployment and decommission jobs.
- Resume is for CloudBolt admins only and needs a `PAUSED` job. A blueprint deployment pauses on a failed build item only when the blueprint has Pause Job on Failure and Misc Settings has Allow resuming failed Blueprint's Job on; a provision job in it pauses the same way when an orchestration action fails. Give the tool the paused job: for a failed build item it names the paused parent. It refuses a deployment whose failed item is still being rerun.
- A job engine restart cancels a paused job, so resume only after a plug-in-only fix. After a shared module fix, restart, rerun the failed item, and order again if items after it never ran.
- `canBeRequeued` in the v3 job API is not part of that rule: CloudBolt never reads it.
- Rerunning a build item runs that item only. If the deployment stopped at it, the build items after it never ran and the parent `deploy_blueprint` job stays failed.
- The job engine caches shared modules until it restarts, so restart it after syncing a shared module fix and before the rerun.
- Setup (import, Synchronous Action, client reconnect): [docs/mcp-testing-setup.md](../../docs/mcp-testing-setup.md).
