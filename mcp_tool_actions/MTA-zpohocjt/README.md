# POC Onboarding: UI Link

MCP Tool Action published to agents as `custom_onb_ui_link`. Turns a CloudBolt global id or a named admin page (new resource handler per technology, LDAP, SMTP, user edit, environment parameters, group quotas, and more) into an absolute UI link so the agent can send the user to the exact page where a secret is entered or an object is edited.
Part of the POC Onboarding Toolkit ([packages/PKG-8aaadefz](../../packages/PKG-8aaadefz/README.md)) that the `cb-poc-onboarding` skill drives; procedure in [.claude/skills/cb-poc-onboarding/SKILL.md](../../.claude/skills/cb-poc-onboarding/SKILL.md).

## Contents
| Role | ID | Name |
|---|---|---|
| MCP Tool Action | MTA-zpohocjt | POC Onboarding: UI Link |
| Plugin | [OHK-1gp25bmt](../../plugins/OHK-1gp25bmt/) | POC Onboarding: UI Link |

## Inputs
| Input | Type | Meaning |
|---|---|---|
| `onb_link_target` | string, required | A global id such as RH-abc12345, or a page key such as email_settings or user_edit:USR-abc12345. An unknown key returns the list of pages. |

## Output
url (absolute when the default portal has a domain), plus model/pk/label for an object or page/meaning for a named page, and click (the button that opens the form) when the form is a dialog.

## Notes
- Admin only: the plugin refuses callers who are not CloudBolt admins.
- Synchronous: the result comes back inline.
- Pages that take an argument accept a global id or a numeric pk after the colon.
