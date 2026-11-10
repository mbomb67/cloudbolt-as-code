# Onboarding: Install

MCP Tool Action published to agents as `custom_onb_install`. Finishes the toolkit install after a sync or package import: sets Synchronous Action on the inline tools, enables every tool, checks outbound access to the Content Library and GitHub, and records the toolkit version.
Part of the Onboarding Toolkit ([packages/PKG-8aaadefz](../../packages/PKG-8aaadefz/README.md)) that the `cb-onboarding` skill drives; procedure in [.claude/skills/cb-onboarding/SKILL.md](../../.claude/skills/cb-onboarding/SKILL.md).

## Contents
| Role | ID | Name |
|---|---|---|
| MCP Tool Action | MTA-6zxrhdls | Onboarding: Install |
| Plugin | [OHK-j60flmb3](../../plugins/OHK-j60flmb3/) | Onboarding: Install |

## Inputs
| Input | Type | Meaning |
|---|---|---|
| `onb_action` | string | install applies the flags; status only reports. |

## Output
counts (expected, present, missing, changed), missing[], changed[] (tool, id, changes, editUrl) for the tools it enabled or made synchronous or asynchronous, egress (contentLibrary, playbook), notes[]. With `onb_action=status` nothing changes and the same rows come back as needsChange[]. Tools already in the wanted state are only counted.

## Notes
- Admin only: the plugin refuses callers who are not CloudBolt admins.
- Asynchronous: the call returns a job id; poll it with `fetch_job` and read `outputs`.
- Asynchronous by design so it works before any Synchronous flag is set.
