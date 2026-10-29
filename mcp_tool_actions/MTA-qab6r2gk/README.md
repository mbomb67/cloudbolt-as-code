# POC Onboarding: SMTP Test

MCP Tool Action published to agents as `custom_onb_smtp_test`. Sends a test email through the saved SMTP settings and returns the relay's real error with a hint when it fails.
Part of the POC Onboarding Toolkit ([packages/PKG-8aaadefz](../../packages/PKG-8aaadefz/README.md)) that the `cb-poc-onboarding` skill drives; procedure in [.claude/skills/cb-poc-onboarding/SKILL.md](../../.claude/skills/cb-poc-onboarding/SKILL.md).

## Contents
| Role | ID | Name |
|---|---|---|
| MCP Tool Action | MTA-qab6r2gk | POC Onboarding: SMTP Test |
| Plugin | [OHK-jdyan3in](../../plugins/OHK-jdyan3in/) | POC Onboarding: SMTP Test |

## Inputs
| Input | Type | Meaning |
|---|---|---|
| `onb_smtp_to` | string | Address to send the test to; defaults to the CloudBolt admin email. |

## Output
recipient, settings, sent, error, hints[], editUrl with editClick.

## Notes
- Admin only: the plugin refuses callers who are not CloudBolt admins.
- Asynchronous: the call returns a job id; poll it with `fetch_job` and read `outputs`.
- Asynchronous by design: the send talks to the mail relay.
