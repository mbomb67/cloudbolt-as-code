# Onboarding: Status

MCP Tool Action published to agents as `custom_onb_status`. Reports the appliance's POC readiness in one read: licence, portal, SMTP, proxy, Content Library, Setup Wizard, resource handlers with environments and last verify result, the Custom Server blueprint, groups, users, LDAP, SSO, installed onboarding tools, recorded POC items, and the next steps with UI links.
Part of the Onboarding Toolkit ([packages/PKG-8aaadefz](../../packages/PKG-8aaadefz/README.md)) that the `cb-onboarding` skill drives; procedure in [.claude/skills/cb-onboarding/SKILL.md](../../.claude/skills/cb-onboarding/SKILL.md).

## Contents
| Role | ID | Name |
|---|---|---|
| MCP Tool Action | MTA-1985b88g | Onboarding: Status |
| Plugin | [OHK-5zg1dkcr](../../plugins/OHK-5zg1dkcr/) | Onboarding: Status |

## Inputs
None.

## Output
One report: appliance, license, preferences (smtp, proxy), contentLibrary, setupWizard, resourceHandlers[] with environments[] (each with sshKeys[]: the key pairs it offers and materialUploaded) and sshKeysUrl, customServerBlueprint, failedServers (count, newest servers[]), parameterConflicts[] (existing parameters named like the toolkit's, which a sync or onb_parameter would reuse), groupsAndUsers (groups[], users[] with roles, passwordSet, passwordNeeded and their own url, passwordFixSteps), authentication, toolkit, pocItems, install, nextSteps[].

## Notes
- Admin only: the plugin refuses callers who are not CloudBolt admins.
- Synchronous: the result comes back inline.
- The only network call is a HEAD against the Content Library host with a five-second timeout.
