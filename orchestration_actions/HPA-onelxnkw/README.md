# Generate options for 'Maintenance Window'

Generated Parameter Options action for the `maintenance_window` parameter of the [Standard Server](../../blueprints/BP-x78p48gz/README.md) blueprint. The plugin computes weekly two-hour windows in UTC (02:00 and 22:00 starts on each day), so the options come from code rather than a fixed list (4.3.1).

## Contents
| Role | ID | Name |
|---|---|---|
| Orchestration action | HPA-onelxnkw | Generate options for 'Maintenance Window' |
| Plugin | [OHK-vwpjfa9d](../../plugins/OHK-vwpjfa9d/) | Generate options for 'Maintenance Window' |

## Setup
Syncs with the Standard Server blueprint, whose `maintenance_window` parameter names this action in `gen_options_hooks`. Edit `DAYS`, `START_HOURS` or `LENGTH_HOURS` in the plugin to match the change calendar.
