# Onboarding: Failure Trace

MCP Tool Action published to agents as `custom_onb_failure_trace`. Walks an order or job down to the jobs that actually failed and returns their errors, output, progress tail, failed children and a hint for the common provisioning failures, naming the job id to pass to fetch_job_log for the traceback.
Part of the Onboarding Toolkit ([packages/PKG-8aaadefz](../../packages/PKG-8aaadefz/README.md)) that the `cb-onboarding` skill drives; procedure in [.claude/skills/cb-onboarding/SKILL.md](../../.claude/skills/cb-onboarding/SKILL.md).

## Contents
| Role | ID | Name |
|---|---|---|
| MCP Tool Action | MTA-jzscq0mk | Onboarding: Failure Trace |
| Plugin | [OHK-y95w8ai0](../../plugins/OHK-y95w8ai0/) | Onboarding: Failure Trace |

## Inputs
| Input | Type | Meaning |
|---|---|---|
| `onb_target_id` | string, required | The order or job to trace. |

## Output
target, failedJobs[] (errors, output, progressTail, hints, logTailChecked, url), failedServers[] (id, hostname, environment, inCloud, url) left in Provision Failed state, hints[], next.

## Notes
- Admin only: the plugin refuses callers who are not CloudBolt admins.
- Synchronous: the result comes back inline.
- A failed job with empty errors gets its hints from the ERROR lines in the last 256 KB of its log file; logTailChecked marks it.
