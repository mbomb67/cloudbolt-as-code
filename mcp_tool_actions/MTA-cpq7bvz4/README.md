# Onboarding: Group

MCP Tool Action published to agents as `custom_onb_group`. Creates or updates a group with its parent, entitled environments, auto-approval flag and server-count quota, covering the test plan's entitlement, approval and quota items.
Part of the Onboarding Toolkit ([packages/PKG-8aaadefz](../../packages/PKG-8aaadefz/README.md)) that the `cb-onboarding` skill drives; procedure in [.claude/skills/cb-onboarding/SKILL.md](../../.claude/skills/cb-onboarding/SKILL.md).

## Contents
| Role | ID | Name |
|---|---|---|
| MCP Tool Action | MTA-cpq7bvz4 | Onboarding: Group |
| Plugin | [OHK-efxm9m2b](../../plugins/OHK-efxm9m2b/) | Onboarding: Group |

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
group (id, name, type, parent, allowAutoApproval, created, url, quotasUrl, quotasClick); the group as it now stands: environments[] (its own entitlements), inheritedEnvironments[] and unconstrainedEnvironments[] when there are any, memberCount, members[] (username, email, roles), serverQuota (limit, unlimited, used); changes[], next.

## Notes
- Admin only: the plugin refuses callers who are not CloudBolt admins.
- Synchronous: the result comes back inline.
- Empty inputs change nothing. With only `onb_group_name` it reports an existing group and refuses to create a new one.
