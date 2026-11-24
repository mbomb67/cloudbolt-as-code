# Restart Web Workers (Dev Appliances Only)

> **Warning: development appliances only. Do not deploy this to a production CloudBolt appliance.** A restart interrupts every in-flight web request, synchronous action, inbound webhook and order-form call, and open console sessions may drop. The tool hands an AI agent the ability to take the web tier down. Do not add it to any content package.

MCP Tool Action published to agents as `custom_dev_restart_web_workers`. Recycles CloudBolt's web worker processes so an agent that just synced a UI extension can load its new routes and views without asking a human to run `systemctl restart httpd`. Part of [docs/agents/testing-loop.md](../../docs/agents/testing-loop.md).

## What it does and why
Extension routes and views, and shared-module code, are cached in the web workers and load only when a worker process starts. The tool signals the mod_wsgi daemon processes that run CloudBolt's Django app (`SIGINT`, which mod_wsgi documents as the way to restart a daemon process), and Apache respawns them. That reimports Django, which has the same effect for extensions as `systemctl restart httpd`, but it does not reload Apache's own configuration. The `cloudbolt` account cannot restart httpd itself, so this is the only route available to a plugin.

## Enable on a dev appliance
One time, as root on the appliance:

```bash
touch /var/opt/cloudbolt/proserv/allow_dev_web_restart
```

Without that file the tool refuses to restart anything. To disable it, delete the file.

## Setup
- Import the tool as described in [docs/mcp-testing-setup.md](../../docs/mcp-testing-setup.md), but leave **Synchronous Action** off. This is the opposite of the other testing tools: a synchronous run executes inside the web request that the restart kills, so the tool refuses to run without a job.
- Reconnect the MCP client after the first sync, because tool lists are cached per session.

## Contents
| Role | ID | Name |
|---|---|---|
| MCP Tool Action | MTA-8x7ay37f | Restart Web Workers (Dev Appliances Only) |
| Plugin | [OHK-26ozyhui](../../plugins/OHK-26ozyhui/) | Restart Web Workers (Dev Appliances Only) |

## Inputs
| Input | Type | Meaning |
|---|---|---|
| `dev_web_restart_dry_run` | string, optional | `true`, `yes` or `1` lists the matched workers without signalling them and does not need the opt-in file; anything else restarts |
| `dev_web_restart_mode` | string, optional | `rolling` (default) or `all`; anything else is refused |

`rolling` recycles one worker at a time. After each signal the job waits for the replacement to appear, then for it to finish importing Django (its CPU time stops growing: running at least 3 seconds and under 0.05 seconds of CPU in the last one-second sample, up to 120 seconds per worker). The other workers keep serving, so the portal stays up. `all` signals every worker at once: faster, but the web tier is unavailable for the whole re-import window.

## Output
`mode`, `oldWorkerPids`, `newWorkerPids`, `signalledCount`, `perWorker` (a list of `oldPid`, `newPid`, `secondsToRespawn`, `secondsToSettle`, `settled`; empty in `all` mode), `secondsUntilWorkersReplaced`, `totalSeconds`, `httpProbeStatus`, `secondsUntilHttpOk`, `message`. A dry run returns `workerPids`, `apacheParentPids` and `optInFileExists` instead. Poll the returned job id with `fetch_job_log`.

The job succeeds once every old worker is replaced by a new one (up to 180 seconds in `all` mode, 15 minutes in `rolling`), then probes the appliance's own site until it answers with a status below 500. If the workers were replaced but the probe never succeeded, the job still succeeds and the message carries a warning.

## Effects on a running appliance
- Jobs keep running, because the job engine is a separate process. A job that calls back to the appliance over HTTP may see errors during the window.
- Work inside a web request is cut off.
- UI logins survive, because sessions are stored outside the workers.
- Rolling (default): the portal keeps answering. For a few minutes some requests may still reach old-code workers, so a new extension route can return 404 until the roll finishes, and a request in flight on the worker being recycled is cut off. A worker that mod_wsgi already recycled is skipped and recorded; only the workers that existed when the run started are ever signalled.
- All: the web tier is unavailable for a few seconds to a minute, so MCP calls may fail briefly. Retry them.
- The job engine is not restarted, so shared-module code cached there still needs a job-engine restart.

## Notes
- Admin only.
- Only processes whose command line is `cloudbolt`, run as the job's user under the root-owned Apache parent, are signalled. Regular Apache workers and the job engine are never touched.
