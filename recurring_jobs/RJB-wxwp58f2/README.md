# Refresh CloudBolt Engine Budgets

Recurring job that keeps the CloudBolt Engine budgets of groups current: it creates any budget still waiting for its tag to appear in billing data, then refreshes each group's budget snapshot and month-end trend that the group's Budgets tab shows. Calls CloudBolt Engine read and write APIs through the `cloudbolt_engine` shared module.

## Contents
| Role | ID | Name |
|---|---|---|
| Plugin | OHK-vq5w4vq2 | Refresh CloudBolt Engine Budgets |
| Shared module | SHM-6v7fxtdd | CloudBolt Engine |

Schedule: `0 */4 * * *` (every 4 hours, appliance time), imported **enabled**. Run it on demand with **Run Now**.

## Notes
- SUCCESS: every registered group refreshed. WARNING: some failed; the job errors list each failed group and the rest still refreshed. FAILURE: none could be refreshed.
- Needs a Connection Info with the `cloudbolt_engine` label (protocol, Engine API host, client ID as username, client secret as password). Repository syncs redact the secret; re-enter it after every sync.
- With no registered groups it does nothing and succeeds.
