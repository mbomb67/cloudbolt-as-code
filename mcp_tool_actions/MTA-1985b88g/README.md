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
A summary report: appliance, license, preferences (smtp, proxy), contentLibrary, setupWizard, resourceHandlers[] (imagesUrl, sshKeysUrl, compact lastVerify) with environments[] (osBuildProblems, guestCredentials[]: per image whether a username, password and key are set, AWS username only; newPasswordSet on vCenter; sshKeys[] with materialUploaded), customServerBlueprint, failedServers (count, newest servers[]), parameterConflicts[], groupsAndUsers (users[] with roles, passwordSet, passwordNeeded and url), authentication, lifecycleJobs.expireServers (enabled, url), toolkit (count, missing, disabled, notSynchronous, lastSync, listUrl), pocItems (id: status; onb_scorecard has the notes), install (with olderThanToolkitSync), nextSteps[]. Credentials are booleans only.

## Notes
- Admin only: the plugin refuses callers who are not CloudBolt admins.
- Synchronous: the result comes back inline.
- The only network call is a HEAD against the Content Library host with a five-second timeout.
