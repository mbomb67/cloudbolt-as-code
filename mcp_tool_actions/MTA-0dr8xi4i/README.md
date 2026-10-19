# POC Onboarding: Import OS Build

MCP Tool Action published to agents as `custom_onb_import_os_build`. Imports one image (AWS AMI, Azure marketplace image, vCenter template) into an OS build, reusing an existing build of the same name so one build spans handlers, and attaches the build to the handler's environments.
Part of the POC Onboarding Toolkit ([packages/PKG-8aaadefz](../../packages/PKG-8aaadefz/README.md)) that the `cb-poc-onboarding` skill drives; procedure in [.claude/skills/cb-poc-onboarding/SKILL.md](../../.claude/skills/cb-poc-onboarding/SKILL.md).

## Contents
| Role | ID | Name |
|---|---|---|
| MCP Tool Action | MTA-0dr8xi4i | POC Onboarding: Import OS Build |
| Plugin | [OHK-k2p8ll4p](../../plugins/OHK-k2p8ll4p/) | POC Onboarding: Import OS Build |

## Inputs
| Input | Type | Meaning |
|---|---|---|
| `onb_handler` | string, required | The handler, by global id or name. |
| `onb_image` | string, required | AWS AMI id or exact name; Azure template name or publisher:offer:sku[:version]; vCenter template name. |
| `onb_os_build_name` | string, required | Name of the OS build to create or reuse. |
| `onb_environments` | text | ENV- ids or names, one per line; default every environment of the handler. |
| `onb_region` | string | Region or location of the image when it cannot be read from the environments. |

## Output
osBuild (id, name, osFamily, reused, url), image, location, attachedToEnvironments[], next.

## Notes
- Admin only: the plugin refuses callers who are not CloudBolt admins.
- Asynchronous: the call returns a job id; poll it with `fetch_job` and read `outputs`.
- Asynchronous by design: the lookup calls the cloud API.
