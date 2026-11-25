# POC Onboarding: Action Approval

MCP Tool Action published to agents as `custom_onb_action_approval`. Turns Requires approval on or off for a server or resource action, which the API cannot change, so a day-2 action pauses as an order for an approver.
Part of the POC Onboarding Toolkit ([packages/PKG-8aaadefz](../../packages/PKG-8aaadefz/README.md)) that the `cb-poc-onboarding` skill drives; procedure in [.claude/skills/cb-poc-onboarding/SKILL.md](../../.claude/skills/cb-poc-onboarding/SKILL.md).

## Contents
| Role | ID | Name |
|---|---|---|
| MCP Tool Action | MTA-bp0rkcq9 | POC Onboarding: Action Approval |
| Plugin | [OHK-ndsuxv0k](../../plugins/OHK-ndsuxv0k/) | POC Onboarding: Action Approval |

## Inputs
| Input | Type | Meaning |
|---|---|---|
| `onb_action` | string, required | SVA- or RSA- global id, or the action's label. |
| `onb_requires_approval` | string | true pauses the action for approval; false runs it directly. |

## Output
action (id, label, kind, url), requiresApproval (before, after), next.

## Notes
- Admin only: the plugin refuses callers who are not CloudBolt admins.
- Synchronous: the result comes back inline.
