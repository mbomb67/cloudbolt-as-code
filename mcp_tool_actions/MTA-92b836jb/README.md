# POC Onboarding: Playbook

MCP Tool Action published to agents as `custom_onb_playbook`. Returns a section of the POC onboarding skill text from the public cloudbolt-as-code repository, so MCP clients without skill support follow the same procedure.
Part of the POC Onboarding Toolkit ([packages/PKG-8aaadefz](../../packages/PKG-8aaadefz/README.md)) that the `cb-poc-onboarding` skill drives; procedure in [.claude/skills/cb-poc-onboarding/SKILL.md](../../.claude/skills/cb-poc-onboarding/SKILL.md).

## Contents
| Role | ID | Name |
|---|---|---|
| MCP Tool Action | MTA-92b836jb | POC Onboarding: Playbook |
| Plugin | [OHK-1am7i73s](../../plugins/OHK-1am7i73s/) | POC Onboarding: Playbook |

## Inputs
| Input | Type | Meaning |
|---|---|---|
| `onb_section` | string | index, bootstrap, resource-handlers, phases, troubleshooting or test-plan-map. |

## Output
text (Markdown), source, browserUrl, sections[].

## Notes
- Any authenticated caller; the text is public.
- Synchronous: the result comes back inline.
- Needs outbound HTTPS to raw.githubusercontent.com; onb_install checks it.
