# Onboarding: Scorecard

MCP Tool Action published to agents as `custom_onb_scorecard`. Builds the POC test plan scorecard from the recorded item statuses and live evidence on the appliance (orders, handlers, environments, groups, mappings, actions, jobs), per category with weights.
Part of the Onboarding Toolkit ([packages/PKG-8aaadefz](../../packages/PKG-8aaadefz/README.md)) that the `cb-onboarding` skill drives; procedure in [.claude/skills/cb-onboarding/SKILL.md](../../.claude/skills/cb-onboarding/SKILL.md).

## Contents
| Role | ID | Name |
|---|---|---|
| MCP Tool Action | MTA-zfoxwh12 | Onboarding: Scorecard |
| Plugin | [OHK-yqu8b25o](../../plugins/OHK-yqu8b25o/) | Onboarding: Scorecard |

## Inputs
| Input | Type | Meaning |
|---|---|---|
| `onb_score_section` | string | all (default), self-service, rbac, resource-management or extensibility. |

## Output
categories[] with items[] (id, title, weight, recorded, recordedNote, evidence, evidenceFound, suggested), pointsPossible per category, totals, prerequisites[], recordedPendingWithEvidence[].

## Notes
- Admin only: the plugin refuses callers who are not CloudBolt admins.
- Synchronous: the result comes back inline.
- Any recorded status wins, pending included; evidence only suggests a status for unrecorded items.
- Evidence is strict and indicative; the customer's score column is their judgement. Built-in roles, stock recurring jobs and stock rules are excluded using CloudBolt's seed definitions (`initialize.cb_minimal`); 4.2.1 needs the Content Library index cache.
