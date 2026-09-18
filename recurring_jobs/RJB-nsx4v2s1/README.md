# Expire Servers

Recurring job that scans ACTIVE and PROVFAILED servers for a passed expiration date and spawns one CloudBolt `expire` job for the batch. The expire job runs the platform's Pre-Expire and Post-Expire hook points, which is where you attach your own decommission or notification automation.

## Contents
| Role | ID | Name |
|---|---|---|
| Plugin | OHK-59t2apzf | Expire Servers |

Schedule: `0 0 * * *` (daily at 00:00 appliance time). `allow_parallel_jobs` is false.

## Prerequisites
- Servers carry an `expiration_date` value (`orchestration_actions/HPA-qb0w86mi` defaults it on order forms); `Server.get_expired_servers` decides which are due.
- At least one active superuser; the spawned expire job is owned by the lowest-id active superuser.

## Setup
1. Nothing is required after sync; the job imports enabled with the schedule above. Change the cron expression on the recurring job in the UI if midnight is not suitable.
2. Attach the actions you want at expiry to the Pre-Expire and Post-Expire orchestration hook points; this job only queues the expire job.

## Notes
- Exits without creating a job when no server has expired.
- All expired servers found in one run are batched into a single expire job (`ServerExpireParameters`).
- This plugin does not itself power off or delete anything; that is the platform expire job and the hook-point actions you configure.
