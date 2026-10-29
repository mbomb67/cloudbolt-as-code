# POC Onboarding: Trim Environment Order Form

MCP Tool Action published to agents as `custom_onb_trim_environment`. Shortens an environment's order form for a POC by pinning infrastructure parameters to one value (which hides a required field) and removing the optional technology parameters, leaving the size, the NIC 1 networks chosen with onb_environment_networks and, where needed, the password; the report profile only lists the current form.
Part of the POC Onboarding Toolkit ([packages/PKG-8aaadefz](../../packages/PKG-8aaadefz/README.md)) that the `cb-poc-onboarding` skill drives; procedure in [.claude/skills/cb-poc-onboarding/SKILL.md](../../.claude/skills/cb-poc-onboarding/SKILL.md).

## Contents
| Role | ID | Name |
|---|---|---|
| MCP Tool Action | MTA-iyknuxox | POC Onboarding: Trim Environment Order Form |
| Plugin | [OHK-qn4k1pme](../../plugins/OHK-qn4k1pme/) | POC Onboarding: Trim Environment Order Form |

## Inputs
| Input | Type | Meaning |
|---|---|---|
| `onb_environment` | string, required | The environment, by global id or name. |
| `onb_profile` | string | poc_minimal applies the per-technology trim; report only lists the current order form. |
| `onb_pin` | text | Fields to pin to a given value; the value must be one of the field's options, or a new value for text fields. |
| `onb_keep` | text | Field names the profile must not pin or remove. |
| `onb_remove` | text | Field names to remove from the environment in addition to the profile's list. |

## Output
before[] and after[] (every parameter with options, required, hiddenOnOrderForm), actions[] (pinned, removed, left with reason), protectedFromSync[], visibleNow[], next.

## Notes
- Admin only: the plugin refuses callers who are not CloudBolt admins.
- Synchronous: the result comes back inline.
- Handler-level fields cannot be removed from one environment; they are pinned instead.
- On Azure node_size, availability_set_arm and azure_authentication_type never hide.
