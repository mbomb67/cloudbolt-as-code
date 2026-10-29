# POC Onboarding: Configure Custom Server

MCP Tool Action published to agents as `custom_onb_configure_custom_server`. Makes the shipped Custom Server blueprint orderable for the POC: recreates it when missing, activates it, enables every environment on its server tier, and sets a hostname template on the tier or on the listed environments.
Part of the POC Onboarding Toolkit ([packages/PKG-8aaadefz](../../packages/PKG-8aaadefz/README.md)) that the `cb-poc-onboarding` skill drives; procedure in [.claude/skills/cb-poc-onboarding/SKILL.md](../../.claude/skills/cb-poc-onboarding/SKILL.md).

## Contents
| Role | ID | Name |
|---|---|---|
| MCP Tool Action | MTA-i1kmy5fd | POC Onboarding: Configure Custom Server |
| Plugin | [OHK-ilstyopk](../../plugins/OHK-ilstyopk/) | POC Onboarding: Configure Custom Server |

## Inputs
| Input | Type | Meaning |
|---|---|---|
| `onb_environments` | text | ENV- ids or names, one per line, that receive the hostname template; left empty the template goes on the server tier. |
| `onb_hostname_template` | string | CloudBolt hostname template with a sequence: zeros followed by X, for example `poc-00X` (becomes poc-001, poc-002). A template with `#` or without a sequence is refused. |
| `onb_all_environments` | string | true enables every environment on the server tier; false leaves the tier's environment setting unchanged. |

## Output
blueprint (id, status, isOrderable, anyGroupCanDeploy, url), serverTiers[], environments[], changes[], next.

## Notes
- Admin only: the plugin refuses callers who are not CloudBolt admins.
- Synchronous: the result comes back inline.
