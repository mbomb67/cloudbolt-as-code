# Generate options for 'Expiration Date'

Generated Parameter Options hook that pre-fills the `expiration_date` (DT) parameter on order forms with a default of seven days from now. It returns only an `initial_value`; the requester can change or clear the date.

## Contents
| Role | ID | Name |
|---|---|---|
| Plugin | OHK-cfciy0fo | Generate options for 'Expiration Date' |

Bound through `gen_options_hooks` on the `expiration_date` parameter (CF-pai7wi1z) in BP-122nbdt5, BP-anonytrx, BP-b91c5f90, BP-psw7rclb, and BP-tikkhf2y.

## Prerequisites
- An `expiration_date` custom field of type DT on the instance (the blueprints above declare it with `show_on_servers: true`).

## Setup
1. Nothing is required after sync; the action imports enabled at `run_seq` 21.
2. To change the default offset, edit `datetime.timedelta(days=7)` in `plugins/OHK-cfciy0fo/OHK-cfciy0fo_script.py`.
3. To apply it to another blueprint, add this action as a Generated Options hook on that blueprint's Expiration Date parameter in the UI, or add a `gen_options_hooks` entry referencing `orchestration_actions/HPA-qb0w86mi` in the blueprint metadata.

## Notes
- The plugin's inline comment says "one day from now"; the code uses 7 days.
- Setting a date does nothing by itself; expiry is enforced by the Expire Servers recurring job (`recurring_jobs/RJB-nsx4v2s1`).
