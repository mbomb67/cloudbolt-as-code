# Run CIT Tests

MCP Tool Action published to agents as `custom_run_cit_tests`. Starts the same functionaltest job the play button on the CIT Tests page starts, for the named tests, with the caller as owner. Part of [docs/agents/testing-loop.md](../../docs/agents/testing-loop.md).

## Contents
| Role | ID | Name |
|---|---|---|
| MCP Tool Action | MTA-gnx1ckno | Run CIT Tests |
| Plugin | [OHK-ieb4qio7](../../plugins/OHK-ieb4qio7/) | Run CIT Tests |

## Inputs
| Input | Type | Meaning |
|---|---|---|
| `cit_test_refs` | text, required | `CIT-` global ids or exact names, one per line or comma-separated |

## Output
`jobId` (poll with `fetch_job_log`; each test's pass or fail is in the progress log), `tests`, `notFound`.

## Notes
- Admin only.
- Failure emails go to the global admin address, as they do for CIT runs started in the UI.
- Setup (import, Synchronous Action, client reconnect): [docs/mcp-testing-setup.md](../../docs/mcp-testing-setup.md).
