# POC Onboarding: Failed Servers

MCP Tool Action published to agents as `custom_onb_failed_servers`. Lists servers left in Provision Failed state by failed orders and deletes the ones named, removing their VMs from the cloud when they exist.
Part of the POC Onboarding Toolkit ([packages/PKG-8aaadefz](../../packages/PKG-8aaadefz/README.md)) that the `cb-poc-onboarding` skill drives; procedure in [.claude/skills/cb-poc-onboarding/SKILL.md](../../.claude/skills/cb-poc-onboarding/SKILL.md).

## Contents
| Role | ID | Name |
|---|---|---|
| MCP Tool Action | MTA-36j661yp | POC Onboarding: Failed Servers |
| Plugin | [OHK-ep5vo2nu](../../plugins/OHK-ep5vo2nu/) | POC Onboarding: Failed Servers |

## Inputs
| Input | Type | Meaning |
|---|---|---|
| `onb_delete_servers` | string | SVR- ids to delete, separated by commas or spaces; only servers in Provision Failed state. Empty lists the failed servers. |

## Output
List: failedServerCount, failedServers[] (id, hostname, environment, group, owner, created, provisionJob, order, inCloud, locked, url), next.
Delete: deleted[] (the same fields plus deleteJob, or deleteOrder and deleteOrderStatus), skipped[] (id, reason), next.

## Notes
- Admin only: the plugin refuses callers who are not CloudBolt admins.
- Synchronous: the result comes back inline; each delete runs as its own decommission job.
- The delete is CloudBolt's own decommission, as from the server's Delete button: with fast-track decommission off it is an order that may need approval.
- Destructive; the skill confirms with the user first.
