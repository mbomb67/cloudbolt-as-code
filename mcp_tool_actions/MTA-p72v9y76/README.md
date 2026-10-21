# POC Onboarding: Set Item Status

MCP Tool Action published to agents as `custom_onb_set_item_status`. Records a POC test plan item or prerequisite as done, skipped, not applicable or pending with a reason, in a namespaced CustomField store that onb_status and the scorecard read.
Part of the POC Onboarding Toolkit ([packages/PKG-8aaadefz](../../packages/PKG-8aaadefz/README.md)) that the `cb-poc-onboarding` skill drives; procedure in [.claude/skills/cb-poc-onboarding/SKILL.md](../../.claude/skills/cb-poc-onboarding/SKILL.md).

## Contents
| Role | ID | Name |
|---|---|---|
| MCP Tool Action | MTA-p72v9y76 | POC Onboarding: Set Item Status |
| Plugin | [OHK-5885tete](../../plugins/OHK-5885tete/) | POC Onboarding: Set Item Status |

## Inputs
| Input | Type | Meaning |
|---|---|---|
| `onb_item_id` | string, required | Item id from the POC test plan, or P1 to P12 for the prerequisites. |
| `onb_item_status` | string | done, skipped, not_applicable or pending. |
| `onb_item_note` | string | Why the item is skipped or not applicable, or any note; the scorecard prints it. |

## Output
item, status, note, items{} (every recorded item), summary.

## Notes
- Admin only: the plugin refuses callers who are not CloudBolt admins.
- Synchronous: the result comes back inline.
- State lives in the poc_onboarding_state parameter (namespace poc_onboarding), created on first use.
