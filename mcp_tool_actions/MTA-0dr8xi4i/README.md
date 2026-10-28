# POC Onboarding: Import OS Build

MCP Tool Action published to agents as `custom_onb_import_os_build`. Lists a handler's cached images for a location or imports one (AWS AMI, Azure marketplace image, vCenter template) into an OS build, reusing a build of the same name so one build spans handlers, refreshing an empty cache without the whitelist, and attaching the build to the environments.
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
| `onb_image` | string | AMI id or part of an AMI name; Azure template name or publisher:offer:sku[:version]; vCenter template name. Empty lists the cached images for the location. |
| `onb_os_build_name` | string | Name of the OS build to create or reuse; required when onb_image names an image. |
| `onb_environments` | text | ENV- ids or names, one per line; default every environment of the handler. |
| `onb_region` | string | Region or location of the image when it cannot be read from the environments. |
| `onb_replace_existing` | string | `true` replaces the handler's other image in the OS build after the user confirms; CloudBolt allows one per handler (per region on AWS and Azure). |

## Output
When listing: images[] (ids, names, OS). When importing: osBuild (id, name, requestedName, osFamily, reused, url), image, location, attachedToEnvironments[], replacedImages[], verified, unresolved[], notes[], next. When the build already holds another image of the handler: confirmationRequired, existingImages[], next, and nothing is changed.

## Notes
- Admin only: the plugin refuses callers who are not CloudBolt admins.
- Asynchronous: the call returns a job id; poll it with `fetch_job` and read `outputs`.
- Asynchronous by design: the lookup calls the cloud API.
- Run onb_sync_images first on a new appliance so the caches hold the customer's images.
