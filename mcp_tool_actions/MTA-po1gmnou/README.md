# POC Onboarding: Sync Images

MCP Tool Action published to agents as `custom_onb_sync_images`. Fills a resource handler's image cache now without the image whitelist and enables the recurring job and rule that keep it fresh, so the image pickers and onb_import_os_build see every image the credentials can.
Part of the POC Onboarding Toolkit ([packages/PKG-8aaadefz](../../packages/PKG-8aaadefz/README.md)) that the `cb-poc-onboarding` skill drives; procedure in [.claude/skills/cb-poc-onboarding/SKILL.md](../../.claude/skills/cb-poc-onboarding/SKILL.md).

## Contents
| Role | ID | Name |
|---|---|---|
| MCP Tool Action | MTA-po1gmnou | POC Onboarding: Sync Images |
| Plugin | [OHK-bbhlwspa](../../plugins/OHK-bbhlwspa/) | POC Onboarding: Sync Images |

## Inputs
| Input | Type | Meaning |
|---|---|---|
| `onb_handler` | string, required | The handler, by global id or name. |
| `onb_locations` | text | Regions (AWS) or locations (Azure) to refresh; default the handler's environments' locations. |
| `onb_enable_jobs` | string | true also enables the technology's Fetch and Cache recurring job and rule, and Execute all Rules. |

## Output
refresh (scope), cachedImages (before, after), secondsTaken, scheduledRefresh[] (recurring job and rule rows with status and schedule), next.

## Notes
- Admin only: the plugin refuses callers who are not CloudBolt admins.
- Asynchronous: the call returns a job id; poll it with `fetch_job` and read `outputs`.
- Asynchronous by design; AWS without a whitelist is slow, start it early and continue.
- Per Mike's direction (2026-10-19) the whitelist is never applied here.
