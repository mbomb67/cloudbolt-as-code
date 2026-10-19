# POC Onboarding: Parameter

MCP Tool Action published to agents as `custom_onb_parameter`. Creates or updates a parameter and attaches it to environments and groups with options and constraints, covering the test plan's user-defined parameter, cost-center tag source, power schedule and expiration date with a maximum lease.
Part of the POC Onboarding Toolkit ([packages/PKG-8aaadefz](../../packages/PKG-8aaadefz/README.md)) that the `cb-poc-onboarding` skill drives; procedure in [.claude/skills/cb-poc-onboarding/SKILL.md](../../.claude/skills/cb-poc-onboarding/SKILL.md).

## Contents
| Role | ID | Name |
|---|---|---|
| MCP Tool Action | MTA-70aez9bm | POC Onboarding: Parameter |
| Plugin | [OHK-f6kqrklx](../../plugins/OHK-f6kqrklx/) | POC Onboarding: Parameter |

## Inputs
| Input | Type | Meaning |
|---|---|---|
| `onb_param_name` | string, required | Internal name; lowercase letters, digits and underscores. |
| `onb_param_label` | string | Display label; defaults to the name in title case. |
| `onb_param_type` | string | Type for a new parameter; an existing parameter keeps its type. |
| `onb_param_options` | text | Choices offered on the targets; replaces the current options there. |
| `onb_param_required` | string | Whether a value is mandatory on the targets. |
| `onb_param_targets` | text | Environments and groups to attach the parameter to. |
| `onb_param_description` | string | Help text shown under the field. |
| `onb_param_max` | integer | Upper bound on the targets; for DT parameters the number of days from today. |
| `onb_param_min` | integer | Lower bound on the targets. |
| `onb_param_show_on_servers` | string | Whether the value appears on the server and can be mapped to a cloud tag. |

## Output
parameter (id, name, type, created, changes, url), attached[] per target with options, constraints and status, notes[].

## Notes
- Admin only: the plugin refuses callers who are not CloudBolt admins.
- Synchronous: the result comes back inline.
- Options on a group are shared by every environment the group orders into; options on an environment apply to that environment only.
