# POC Onboarding: Network Addressing

MCP Tool Action published to agents as `custom_onb_network_addressing`. Sets how servers on a network get their IP address (DHCP, static with gateway and netmask, a CloudBolt IP pool, an IPAM endpoint, or an IP typed per order) and verifies it with the network's IP provider.
Part of the POC Onboarding Toolkit ([packages/PKG-8aaadefz](../../packages/PKG-8aaadefz/README.md)) that the `cb-poc-onboarding` skill drives; procedure in [.claude/skills/cb-poc-onboarding/SKILL.md](../../.claude/skills/cb-poc-onboarding/SKILL.md).

## Contents
| Role | ID | Name |
|---|---|---|
| MCP Tool Action | MTA-z3h0o9t3 | POC Onboarding: Network Addressing |
| Plugin | [OHK-gjfc20pv](../../plugins/OHK-gjfc20pv/) | POC Onboarding: Network Addressing |

## Inputs
| Input | Type | Meaning |
|---|---|---|
| `onb_network` | string, required | The network, by global id or name; onb_environment_networks lists them. |
| `onb_addressing` | string | dhcp, static, or both (the requester chooses per order); empty keeps the current schema. |
| `onb_gateway` | string | Gateway IPv4 address; required for static and both. |
| `onb_netmask` | string | Netmask, dotted or as a prefix length; required for static and both. |
| `onb_dns1` | string | Primary DNS server, optional. |
| `onb_dns2` | string | Secondary DNS server, optional. |
| `onb_dns_domain` | string | DNS domain, optional; Infoblox needs it for host records. |
| `onb_ip_source` | string | For static: pool (CloudBolt IP pool), ipam (an IPAM endpoint) or manual (typed on each order). |
| `onb_pool_range` | text | For pool: a range such as 10.0.5.10-10.0.5.60, or addresses separated by commas or new lines. |
| `onb_ipam` | string | For ipam: the IPAM endpoint the user added in the UI. |

## Output
network (id, name, url, addressing, ipProvider, gateway, netmask, dns1, dns2, dnsDomain, ipPool {name, total, free, url}, ipamNetwork), changes[], verified, problems[], notes[]; for IPAM before the mapping: needsUiStep, ipamUrl, next; with only onb_network: network and ipamEndpoints[].

## Notes
- Admin only: the plugin refuses callers who are not CloudBolt admins.
- Asynchronous: the call returns a job id; poll it with `fetch_job` and read `outputs`.
- Asynchronous: the IPAM check calls the IPAM endpoint.
- IPAM credentials are entered in the UI (/setup/add-ipam/); the tool never takes them.
- Enabling an IPAM orchestration action applies to every endpoint of that IPAM technology.
- Static addressing without gateway and netmask fails at provisioning on vCenter, so the tool requires both.
