# POC Onboarding: Group

MCP Tool Action published to agents as `custom_onb_group`. Creates or updates a group with its parent, entitled environments, auto-approval flag and server-count quota, covering the test plan's entitlement, approval and quota items.
Part of the POC Onboarding Toolkit ([packages/PKG-8aaadefz](../../packages/PKG-8aaadefz/README.md)) that the `cb-poc-onboarding` skill drives; procedure in [.claude/skills/cb-poc-onboarding/SKILL.md](../../.claude/skills/cb-poc-onboarding/SKILL.md).

## Contents
| Role | ID | Name |
|---|---|---|
| MCP Tool Action | MTA-cpq7bvz4 | POC Onboarding: Group |
| Plugin | [OHK-efxm9m2b](../../plugins/OHK-efxm9m2b/) | POC Onboarding: Group |

## Inputs
| Input | Type | Meaning |
|---|---|---|
| `onb_group_name` | string, required | Name of the group to create or update. |
| `onb_group_parent` | string | Parent group; the new group inherits its type. |
| `onb_group_environments` | text | Environments the group may order into. |
| `onb_group_auto_approval` | string | true lets members' orders run without an approver. |
| `onb_group_quota_servers` | integer | Maximum servers the group may own. |
| `onb_group_description` | string | Group description. |

## Output
group (id, name, type, parent, allowAutoApproval, created, url, quotasUrl), environments[], serverQuota, changes[], next.

## Notes
- Admin only: the plugin refuses callers who are not CloudBolt admins.
- Synchronous: the result comes back inline.
