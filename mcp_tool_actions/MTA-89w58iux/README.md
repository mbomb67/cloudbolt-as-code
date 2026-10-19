# POC Onboarding: SMTP Settings

MCP Tool Action published to agents as `custom_onb_smtp`. Sets the non-secret email settings and returns the link where the SMTP password is entered; with no inputs it reports the current settings.
Part of the POC Onboarding Toolkit ([packages/PKG-8aaadefz](../../packages/PKG-8aaadefz/README.md)) that the `cb-poc-onboarding` skill drives; procedure in [.claude/skills/cb-poc-onboarding/SKILL.md](../../.claude/skills/cb-poc-onboarding/SKILL.md).

## Contents
| Role | ID | Name |
|---|---|---|
| MCP Tool Action | MTA-89w58iux | POC Onboarding: SMTP Settings |
| Plugin | [OHK-be18kqal](../../plugins/OHK-be18kqal/) | POC Onboarding: SMTP Settings |

## Inputs
| Input | Type | Meaning |
|---|---|---|
| `onb_smtp_host` | string | Relay hostname. |
| `onb_smtp_port` | integer | Relay port: 25, 465 or 587. |
| `onb_smtp_user` | string | Relay login; the password is entered in the UI. |
| `onb_smtp_tls` | string | STARTTLS on the connection. |
| `onb_smtp_from` | string | Sender address on CloudBolt emails. |
| `onb_smtp_admin_email` | string | Where test emails and admin notifications go. |

## Output
settings (host, port, user, useTls, fromAddress, adminEmail, passwordSet), changes[], editUrl, testUrl, next.

## Notes
- Admin only: the plugin refuses callers who are not CloudBolt admins.
- Synchronous: the result comes back inline.
- No password is ever taken by this tool.
