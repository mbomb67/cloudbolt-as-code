# POC Onboarding: Install Content Library Item

MCP Tool Action published to agents as `custom_onb_install_content_library_item`. Lists a Content Library collection or downloads one item from content.cloudbolt.app and imports it the way the library page does, for UI extensions, recurring jobs, server and resource actions, rules and blueprints.
Part of the POC Onboarding Toolkit ([packages/PKG-8aaadefz](../../packages/PKG-8aaadefz/README.md)) that the `cb-poc-onboarding` skill drives; procedure in [.claude/skills/cb-poc-onboarding/SKILL.md](../../.claude/skills/cb-poc-onboarding/SKILL.md).

## Contents
| Role | ID | Name |
|---|---|---|
| MCP Tool Action | MTA-znqpide5 | POC Onboarding: Install Content Library Item |
| Plugin | [OHK-kst5rkhi](../../plugins/OHK-kst5rkhi/) | POC Onboarding: Install Content Library Item |

## Inputs
| Input | Type | Meaning |
|---|---|---|
| `onb_cl_collection` | string, required | blueprints, server-actions, resource-actions, orchestration-actions, rules, recurring-jobs, ui-extension-packages or cb-applets. |
| `onb_cl_item` | string | Display name or id of the item to install; empty returns the collection's items. |
| `onb_cl_replace` | string | true refreshes an item that already exists on the appliance. |

## Output
items[] when listing; otherwise collection, item (name, id, version), imported (model, global id, url), next.

## Notes
- Admin only: the plugin refuses callers who are not CloudBolt admins.
- Asynchronous: the call returns a job id; poll it with `fetch_job` and read `outputs`.
- Asynchronous by design: the download comes from the internet.
- UI extensions need a web server restart before their pages open.
