# POC Onboarding: Verify Resource Handler

MCP Tool Action published to agents as `custom_onb_verify_resource_handler`. Runs a resource handler's Verify Connection and returns the real exception text with a technology-specific hint, the minimum-permissions document and the handler's edit and verify links, recording the result for the status tool.
Part of the POC Onboarding Toolkit ([packages/PKG-8aaadefz](../../packages/PKG-8aaadefz/README.md)) that the `cb-poc-onboarding` skill drives; procedure in [.claude/skills/cb-poc-onboarding/SKILL.md](../../.claude/skills/cb-poc-onboarding/SKILL.md).

## Contents
| Role | ID | Name |
|---|---|---|
| MCP Tool Action | MTA-r5v1eblg | POC Onboarding: Verify Resource Handler |
| Plugin | [OHK-s7cdvkpr](../../plugins/OHK-s7cdvkpr/) | POC Onboarding: Verify Resource Handler |

## Inputs
| Input | Type | Meaning |
|---|---|---|
| `onb_handler` | string, required | The handler to verify, by global id or name. |

## Output
verified (true/false), error, hints[], links (detail, edit, verifyDialog, importLocations, permissionsDoc), next.

## Notes
- Admin only: the plugin refuses callers who are not CloudBolt admins.
- Asynchronous: the call returns a job id; poll it with `fetch_job` and read `outputs`.
- Asynchronous by design: the check calls the cloud API. The result is also stored for onb_status.
