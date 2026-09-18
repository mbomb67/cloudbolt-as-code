# Io Cloudbolt Widgets (XUI)

Adds nine dashboard widgets, available to users from the dashboard's widget picker:

- **# Servers by Environment** - pie chart of active server counts (live, via `/xui/io_cloudbolt_widgets/api/envs`).
- **Cloud Availability** - connection status and response time per resource handler.
- **Server Stats** - active server counts and cost by resource handler and environment.
- **Group Stats** - server/resource counts and cost per top-level group.
- **Job Counts** and **Order Counts** - totals with day/week/month deltas.
- **Newest Servers**, **Newest Resources** - five most recent in the user's scope (live).
- **Featured Blueprints** - favorited, active blueprints with order links (live).

Widgets are scoped to the viewing user's groups; super admins see global data.

## Prerequisites
- CloudBolt 8.6 or later.
- Directory `<PROSERV_DIR>/data/` (normally `/var/opt/cloudbolt/proserv/data/`) must exist and be writable by the job engine; the generator plugins do not create it.

## Setup
1. Create recurring jobs (Admin > Recurring Jobs) for each generator in `cb_plugins/`: `gen_rh_status.py`, `gen_rh_stats.py`, `gen_group_stats.py`, `gen_job_stats.py`, `gen_order_stats.py`. Each writes a JSON file to `<PROSERV_DIR>/data/` that the matching widget reads; until it runs, the widget is empty.
2. Optional: to give new users a default widget layout, add `cb_plugins/copy_widget_json_on_login.py` as an orchestration action at the SSO User Update hook point and set its `source_username` action input to the user whose layout should be copied.

## Notes
- `cb_plugins/copy_widget_json.py` overwrites every user's widget layout with the layout of `SOURCE_USERNAME`; set that constant before running it, or use the on-login variant instead.
- `gen_rh_status.py` calls `verify_connection()` on every resource handler with a 30-second timeout and two retries; schedule it accordingly.
