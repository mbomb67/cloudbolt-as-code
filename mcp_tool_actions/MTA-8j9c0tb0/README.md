# Fetch Job Log

MCP Tool Action published to agents as `custom_fetch_job_log`. Returns what the built-in `fetch_job` leaves out: the job's progress messages, its child jobs, every failed descendant with its errors, the tail of the job's log file, and, for a Source Control Repository sync job, the per-object results as rows. Read-only. Part of [docs/agents/testing-loop.md](../../docs/agents/testing-loop.md).

## Contents
| Role | ID | Name |
|---|---|---|
| MCP Tool Action | MTA-8j9c0tb0 | Fetch Job Log |
| Plugin | [OHK-2p0rhzup](../../plugins/OHK-2p0rhzup/) | Fetch Job Log |

## Inputs
| Input | Type | Meaning |
|---|---|---|
| `log_job_id` | string, required | `JOB-` global id (numeric id also accepted) |
| `log_tail` | integer | Newest progress messages to return, oldest first (default 100, max 1000) |
| `log_children` | `true`/`false` | Include direct children and failed descendants (default true) |
| `log_file_tail` | integer | Lines from the end of the job log file, for tracebacks (default 0, max 2000) |

## Output
`job` (status, output, errors, outputs), `progress`, `children`, `failedDescendants`, `syncResults` (`objectType`, `path`, `status`, `message`), `logFileTail`.

## Notes
- The caller must own the job or be a CloudBolt admin.
- The log file is read from the host running the tool; on a multi-worker appliance it may be elsewhere, and `logFileTail.note` says so.
- Setup (import, Synchronous Action, client reconnect): [docs/mcp-testing-setup.md](../../docs/mcp-testing-setup.md).
