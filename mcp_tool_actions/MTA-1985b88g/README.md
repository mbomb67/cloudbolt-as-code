# POC Onboarding: Status

MCP Tool Action published to agents as `custom_onb_status`. Reports the appliance's POC readiness in one read: licence, portal, SMTP, proxy, Content Library, Setup Wizard, resource handlers with environments and last verify result, the Custom Server blueprint, groups, users, LDAP, SSO, installed onboarding tools, recorded POC items, and the next steps with UI links.
Part of the POC Onboarding Toolkit ([packages/PKG-8aaadefz](../../packages/PKG-8aaadefz/README.md)) that the `cb-poc-onboarding` skill drives; procedure in [.claude/skills/cb-poc-onboarding/SKILL.md](../../.claude/skills/cb-poc-onboarding/SKILL.md).

## Contents
| Role | ID | Name |
|---|---|---|
| MCP Tool Action | MTA-1985b88g | POC Onboarding: Status |
| Plugin | [OHK-5zg1dkcr](../../plugins/OHK-5zg1dkcr/) | POC Onboarding: Status |

## Inputs
None.

## Output
One report: appliance, license, preferences (smtp, proxy), contentLibrary, setupWizard, resourceHandlers[] with environments[], customServerBlueprint, failedServers (count, newest servers[]), groupsAndUsers (groups[], users[] with roles, passwordSet, passwordNeeded and their own url, passwordFixSteps), authentication, toolkit, pocItems, install, nextSteps[].

## Notes
- Admin only: the plugin refuses callers who are not CloudBolt admins.
- Synchronous: the result comes back inline.
- The only network call is a HEAD against the Content Library host with a five-second timeout.
