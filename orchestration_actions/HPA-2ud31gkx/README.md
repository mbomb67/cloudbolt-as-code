# Generate options for 'POC Application Tier'

Generated Parameter Options action for the `poc_application_tier` parameter of the [POC Server](../../blueprints/BP-x78p48gz/README.md) blueprint. The plugin returns web, app and db from Python and drops db when the ordering group's name contains "prod", which is the POC Test Plan's Python-backed parameter options item (4.3.1).

## Contents
| Role | ID | Name |
|---|---|---|
| Orchestration action | HPA-2ud31gkx | Generate options for 'POC Application Tier' |
| Plugin | [OHK-f1ol9j5k](../../plugins/OHK-f1ol9j5k/) | Generate options for 'POC Application Tier' |

## Setup
Syncs with the POC Server blueprint, whose `poc_application_tier` parameter names this action in `gen_options_hooks`. To use it elsewhere, add the parameter to an environment or blueprint and pick this action under the parameter's Generated Options.
