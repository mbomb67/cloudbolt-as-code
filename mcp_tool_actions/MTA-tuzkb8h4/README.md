# POC Onboarding: User

MCP Tool Action published to agents as `custom_onb_user`. Creates a local user with no usable password and assigns roles in a group, returning the edit link where the admin sets the password; an existing user only gets the roles.
Part of the POC Onboarding Toolkit ([packages/PKG-8aaadefz](../../packages/PKG-8aaadefz/README.md)) that the `cb-poc-onboarding` skill drives; procedure in [.claude/skills/cb-poc-onboarding/SKILL.md](../../.claude/skills/cb-poc-onboarding/SKILL.md).

## Contents
| Role | ID | Name |
|---|---|---|
| MCP Tool Action | MTA-tuzkb8h4 | POC Onboarding: User |
| Plugin | [OHK-8in52yhn](../../plugins/OHK-8in52yhn/) | POC Onboarding: User |

## Inputs
| Input | Type | Meaning |
|---|---|---|
| `onb_username` | string, required | Login name of the user to create or update. |
| `onb_user_email` | string | Email address; notifications go here. |
| `onb_user_first_name` | string | First name. |
| `onb_user_last_name` | string | Last name. |
| `onb_user_group` | string | Group in which the roles are assigned. |
| `onb_user_roles` | text | Role names: requestor, approver, resource_admin, viewer, or a custom role. |
| `onb_user_super_admin` | string | Grant or remove the super admin flag. |

## Output
user (id, username, created, url, setPasswordUrl, setPasswordSteps), memberships[], changes[], next.

## Notes
- Admin only: the plugin refuses callers who are not CloudBolt admins.
- Synchronous: the result comes back inline.
- No password is ever taken or set by this tool.
