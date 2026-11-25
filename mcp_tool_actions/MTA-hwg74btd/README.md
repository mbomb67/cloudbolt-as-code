# POC Onboarding: Recent Errors

MCP Tool Action published to agents as `custom_onb_recent_errors`. Reads the tail of a CloudBolt log and returns the recent WARNING-and-above entries grouped by exception and message, secrets redacted, each group with a hint for the failures a new appliance hits most.
Part of the POC Onboarding Toolkit ([packages/PKG-8aaadefz](../../packages/PKG-8aaadefz/README.md)) that the `cb-poc-onboarding` skill drives; procedure in [.claude/skills/cb-poc-onboarding/SKILL.md](../../.claude/skills/cb-poc-onboarding/SKILL.md).

## Contents
| Role | ID | Name |
|---|---|---|
| MCP Tool Action | MTA-hwg74btd | POC Onboarding: Recent Errors |
| Plugin | [OHK-4vwq70dg](../../plugins/OHK-4vwq70dg/) | POC Onboarding: Recent Errors |

## Inputs
| Input | Type | Meaning |
|---|---|---|
| `onb_log` | string | application, jobengine, authentication, mcp or saml2. |
| `onb_minutes` | integer | How far back to look; 0 scans the whole 8 MB tail. |
| `onb_contains` | string | Only entries whose message or traceback contains this text. |
| `onb_limit` | integer | Cap on the number of groups returned. |
| `onb_min_level` | string | DEBUG, INFO, WARNING, ERROR or CRITICAL. |

## Output
groups[] with level, logger, where, count, firstSeen, lastSeen, sample, traceback[] and hints[]; entriesScanned, entriesMatched, secretsRedacted.

## Notes
- Admin only: the plugin refuses callers who are not CloudBolt admins.
- Synchronous: the result comes back inline.
- Log timestamps are appliance-local; the window is computed in the appliance's local time.
