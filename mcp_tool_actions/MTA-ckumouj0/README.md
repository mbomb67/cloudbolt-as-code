# Onboarding: CIT Test from Order

MCP Tool Action published to agents as `custom_onb_cit_from_order`. Turns a finished blueprint order into a Continuous Infrastructure Testing test, the same as the order page's Add to CIT action.
Part of the Onboarding Toolkit ([packages/PKG-8aaadefz](../../packages/PKG-8aaadefz/README.md)) that the `cb-onboarding` skill drives; procedure in [.claude/skills/cb-onboarding/SKILL.md](../../.claude/skills/cb-onboarding/SKILL.md).

## Contents
| Role | ID | Name |
|---|---|---|
| MCP Tool Action | MTA-ckumouj0 | Onboarding: CIT Test from Order |
| Plugin | [OHK-yjow1qql](../../plugins/OHK-yjow1qql/) | Onboarding: CIT Test from Order |

## Inputs
| Input | Type | Meaning |
|---|---|---|
| `onb_cit_order` | string, required | The finished blueprint order to replay. |
| `onb_cit_name` | string | Name of the CIT test; defaults to the order's name. |

## Output
test (id, name, expectedStatus, enabled, url), order, created, citUrl, next.

## Notes
- Admin only: the plugin refuses callers who are not CloudBolt admins.
- Synchronous: the result comes back inline.
