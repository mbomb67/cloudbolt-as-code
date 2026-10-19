# POC Onboarding: Verify LDAP

MCP Tool Action published to agents as `custom_onb_verify_ldap`. Runs an LDAP utility's Verify Connection and returns the real bind or search error with a hint and the links to fix it.
Part of the POC Onboarding Toolkit ([packages/PKG-8aaadefz](../../packages/PKG-8aaadefz/README.md)) that the `cb-poc-onboarding` skill drives; procedure in [.claude/skills/cb-poc-onboarding/SKILL.md](../../.claude/skills/cb-poc-onboarding/SKILL.md).

## Contents
| Role | ID | Name |
|---|---|---|
| MCP Tool Action | MTA-kw8krevn | POC Onboarding: Verify LDAP |
| Plugin | [OHK-j82f4ibp](../../plugins/OHK-j82f4ibp/) | POC Onboarding: Verify LDAP |

## Inputs
| Input | Type | Meaning |
|---|---|---|
| `onb_ldap` | string | Which LDAP utility; optional when only one exists. |

## Output
ldapUtility (server, port, protocol, serviceAccount), verified, error, hints[], links (detail, edit, verifyDialog, mappings), next.

## Notes
- Admin only: the plugin refuses callers who are not CloudBolt admins.
- Asynchronous: the call returns a job id; poll it with `fetch_job` and read `outputs`.
- Asynchronous by design: the check contacts the directory.
