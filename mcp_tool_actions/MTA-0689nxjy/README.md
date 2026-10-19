# POC Onboarding: Tag Mapping

MCP Tool Action published to agents as `custom_onb_tag_mapping`. Lists, adds or removes a resource handler's mappings from CloudBolt attributes or parameters to cloud tags, giving cross-cloud tagging and the cost-center tag.
Part of the POC Onboarding Toolkit ([packages/PKG-8aaadefz](../../packages/PKG-8aaadefz/README.md)) that the `cb-poc-onboarding` skill drives; procedure in [.claude/skills/cb-poc-onboarding/SKILL.md](../../.claude/skills/cb-poc-onboarding/SKILL.md).

## Contents
| Role | ID | Name |
|---|---|---|
| MCP Tool Action | MTA-0689nxjy | POC Onboarding: Tag Mapping |
| Plugin | [OHK-b8c90uok](../../plugins/OHK-b8c90uok/) | POC Onboarding: Tag Mapping |

## Inputs
| Input | Type | Meaning |
|---|---|---|
| `onb_handler` | string, required | The handler, by global id or name. |
| `onb_tag_mappings` | text | Attribute or parameter name, equals sign, the tag name to write in the cloud. |
| `onb_tag_action` | string | add creates or updates, remove deletes, list reports. |

## Output
results[] per line, mappings[] (the handler's mappings after the call), attributes (built-in names), next.

## Notes
- Admin only: the plugin refuses callers who are not CloudBolt admins.
- Synchronous: the result comes back inline.
