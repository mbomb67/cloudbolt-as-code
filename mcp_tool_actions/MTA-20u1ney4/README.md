# POC Onboarding: Environment Networks

MCP Tool Action published to agents as `custom_onb_environment_networks`. Lists the networks an environment can offer on its first NIC and makes the chosen ones, up to five, its only NIC 1 choices, removing networks from every other NIC.
Part of the POC Onboarding Toolkit ([packages/PKG-8aaadefz](../../packages/PKG-8aaadefz/README.md)) that the `cb-poc-onboarding` skill drives; procedure in [.claude/skills/cb-poc-onboarding/SKILL.md](../../.claude/skills/cb-poc-onboarding/SKILL.md).

## Contents
| Role | ID | Name |
|---|---|---|
| MCP Tool Action | MTA-20u1ney4 | POC Onboarding: Environment Networks |
| Plugin | [OHK-0u7aonmo](../../plugins/OHK-0u7aonmo/) | POC Onboarding: Environment Networks |

## Inputs
| Input | Type | Meaning |
|---|---|---|
| `onb_environment` | string, required | The environment, by global id or name. |
| `onb_networks` | text | NET- ids or names, one per line, at most five; empty lists the networks. |
| `onb_refresh` | string | true syncs the handler's networks or the environment's subnets before listing or setting. |

## Output
environment, handler, category (addressing, cloud_assigns, no_effect, none); listing: nic1[], available[] (id, name, network, vlan, addressing, ipProvider, gateway, netmask, dns1, dns2, dnsDomain, ipPool, ipamNetwork), otherNics[]; setting: nic1[], added[], removedFromNic1[], removedFromOtherNics[], verified, problems[], next.

## Notes
- Admin only: the plugin refuses callers who are not CloudBolt admins.
- Asynchronous: the call returns a job id; poll it with `fetch_job` and read `outputs`.
- Asynchronous: a refresh calls the cloud or vCenter.
- Keeps servers on one NIC: networks on NIC 2 and up are removed when NIC 1 is set.
- Azure offers only subnets already on the environment; onb_refresh=true re-imports them all, so pass onb_networks with it.
