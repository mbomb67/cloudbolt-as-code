# POC Onboarding: Import Environments

MCP Tool Action published to agents as `custom_onb_import_environments`. Creates one environment per listed location on a resource handler the way the Import locations dialog does, brings the networks over (AWS subnets with the environment, Azure subnets synced; vCenter networks are chosen afterwards with onb_environment_networks), and reports existing environments instead of duplicating them.
Part of the POC Onboarding Toolkit ([packages/PKG-8aaadefz](../../packages/PKG-8aaadefz/README.md)) that the `cb-poc-onboarding` skill drives; procedure in [.claude/skills/cb-poc-onboarding/SKILL.md](../../.claude/skills/cb-poc-onboarding/SKILL.md).

## Contents
| Role | ID | Name |
|---|---|---|
| MCP Tool Action | MTA-i6p4j2l7 | POC Onboarding: Import Environments |
| Plugin | [OHK-8j06wtoe](../../plugins/OHK-8j06wtoe/) | POC Onboarding: Import Environments |

## Inputs
| Input | Type | Meaning |
|---|---|---|
| `onb_handler` | string, required | The handler, by global id or name. |
| `onb_locations` | text, required | One location per line: AWS <region> or <region>|<vpc-id>; Azure location name; vCenter, SCVMM or Nutanix cluster name; GCP project id. |
| `onb_env_name` | string | Prefix for the new environments' names; left empty, CloudBolt's default '(handler) location' name is used. |

## Output
results[] per location: status created, existing or failed; environmentId, name, networks, osBuilds, url, parametersUrl, and for Azure the subnet counts; next.

## Notes
- Admin only: the plugin refuses callers who are not CloudBolt admins.
- Asynchronous: the call returns a job id; poll it with `fetch_job` and read `outputs`.
- Azure imports the environment's parameters in its own job after creation; wait a minute before trimming.
- Asynchronous by design: the import calls the cloud API.
