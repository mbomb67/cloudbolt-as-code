# POC Onboarding: Handler Locations

MCP Tool Action published to agents as `custom_onb_handler_locations`. Lists the regions, locations or clusters a resource handler can turn into environments, flags the ones already imported, lists the existing environments, and for an AWS region lists its VPCs.
Part of the POC Onboarding Toolkit ([packages/PKG-8aaadefz](../../packages/PKG-8aaadefz/README.md)) that the `cb-poc-onboarding` skill drives; procedure in [.claude/skills/cb-poc-onboarding/SKILL.md](../../.claude/skills/cb-poc-onboarding/SKILL.md).

## Contents
| Role | ID | Name |
|---|---|---|
| MCP Tool Action | MTA-89dxppus | POC Onboarding: Handler Locations |
| Plugin | [OHK-0nuhjdxl](../../plugins/OHK-0nuhjdxl/) | POC Onboarding: Handler Locations |

## Inputs
| Input | Type | Meaning |
|---|---|---|
| `onb_handler` | string, required | The handler, by global id or name. |
| `onb_region` | string | AWS only: also list the VPCs of this region, with the default VPC flagged. |

## Output
locationKind, locations[] (name, display, imported), existingEnvironments[], vpcs[] for an AWS region, subscriptions[] for a tenant-level Azure handler, next.

## Notes
- Admin only: the plugin refuses callers who are not CloudBolt admins.
- Asynchronous: the call returns a job id; poll it with `fetch_job` and read `outputs`.
- Asynchronous by design: the listing calls the cloud API.
