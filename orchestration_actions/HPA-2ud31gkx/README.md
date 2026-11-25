# Generate options for 'Application'

Generated Parameter Options action for the `application` parameter of the [Standard Server](../../blueprints/BP-x78p48gz/README.md) blueprint. The plugin returns one list of business applications from Python that every group sees, which is the test plan's Python-backed parameter options item (4.3.1); replace the list with a CMDB lookup to show live options.

## Contents
| Role | ID | Name |
|---|---|---|
| Orchestration action | HPA-2ud31gkx | Generate options for 'Application' |
| Plugin | [OHK-f1ol9j5k](../../plugins/OHK-f1ol9j5k/) | Generate options for 'Application' |

## Setup
Syncs with the Standard Server blueprint, whose `application` parameter names this action in `gen_options_hooks`. To use it elsewhere, add the parameter to an environment or blueprint and pick this action under the parameter's Generated Options.
