# POC Onboarding: LDAP Mapping

MCP Tool Action published to agents as `custom_onb_ldap_mapping`. Maps an AD or LDAP group DN to a CloudBolt group with roles on an existing LDAP utility, so directory users land in the right group at first login.
Part of the POC Onboarding Toolkit ([packages/PKG-8aaadefz](../../packages/PKG-8aaadefz/README.md)) that the `cb-poc-onboarding` skill drives; procedure in [.claude/skills/cb-poc-onboarding/SKILL.md](../../.claude/skills/cb-poc-onboarding/SKILL.md).

## Contents
| Role | ID | Name |
|---|---|---|
| MCP Tool Action | MTA-e5qv0zay | POC Onboarding: LDAP Mapping |
| Plugin | [OHK-s76tknmk](../../plugins/OHK-s76tknmk/) | POC Onboarding: LDAP Mapping |

## Inputs
| Input | Type | Meaning |
|---|---|---|
| `onb_ldap` | string | Which LDAP utility; optional when only one exists. |
| `onb_ldap_group_dn` | string, required | Distinguished name of the AD or LDAP group. |
| `onb_ldap_cb_group` | string, required | Group that members join. |
| `onb_ldap_roles` | text | Role names granted in that group. |

## Output
ldapUtility, mapping (groupDn, created), cloudboltGroup, roles[], rolesAdded[], next.

## Notes
- Admin only: the plugin refuses callers who are not CloudBolt admins.
- Synchronous: the result comes back inline.
