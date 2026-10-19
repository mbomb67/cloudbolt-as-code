# POC Onboarding: Scorecard

MCP Tool Action published to agents as `custom_onb_scorecard`. Builds the POC test plan scorecard from the recorded item statuses and live evidence on the appliance (orders, handlers, environments, groups, mappings, actions, jobs), per category with weights.
Part of the POC Onboarding Toolkit ([packages/PKG-8aaadefz](../../packages/PKG-8aaadefz/README.md)) that the `cb-poc-onboarding` skill drives; procedure in [.claude/skills/cb-poc-onboarding/SKILL.md](../../.claude/skills/cb-poc-onboarding/SKILL.md).

## Contents
| Role | ID | Name |
|---|---|---|
| MCP Tool Action | MTA-zfoxwh12 | POC Onboarding: Scorecard |
| Plugin | [OHK-yqu8b25o](../../plugins/OHK-yqu8b25o/) | POC Onboarding: Scorecard |

## Inputs
| Input | Type | Meaning |
|---|---|---|
| `onb_score_section` | string | all (default), self-service, rbac, resource-management or extensibility. |

## Output
categories[] with items[] (id, title, weight, recorded, evidence, suggested, note), pointsPossible per category, totals, prerequisites[].

## Notes
- Admin only: the plugin refuses callers who are not CloudBolt admins.
- Synchronous: the result comes back inline.
- Evidence is indicative; the customer's score column is their judgement.
