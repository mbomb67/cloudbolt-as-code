# POC Onboarding: Remove Environment

MCP Tool Action published to agents as `custom_onb_remove_environment`. Deletes an environment imported by mistake so the POC can start over; refuses while active servers live in it.
Part of the POC Onboarding Toolkit ([packages/PKG-8aaadefz](../../packages/PKG-8aaadefz/README.md)) that the `cb-poc-onboarding` skill drives; procedure in [.claude/skills/cb-poc-onboarding/SKILL.md](../../.claude/skills/cb-poc-onboarding/SKILL.md).

## Contents
| Role | ID | Name |
|---|---|---|
| MCP Tool Action | MTA-pz5o2gjl | POC Onboarding: Remove Environment |
| Plugin | [OHK-1wmzl7sp](../../plugins/OHK-1wmzl7sp/) | POC Onboarding: Remove Environment |

## Inputs
| Input | Type | Meaning |
|---|---|---|
| `onb_environment` | string, required | The environment, by global id or name. |

## Output
environmentId, name, handler, osBuildsDetached, groupsDetached.

## Notes
- Admin only: the plugin refuses callers who are not CloudBolt admins.
- Synchronous: the result comes back inline.
- Destructive; the skill confirms with the user first.
