# Monitoring Alerts

Inbound webhook that takes a Prometheus Alertmanager alert, maps it to a CloudBolt server, and starts the remediation server action for it as a job. The job health-checks the fix and, when the server is still unhealthy, opens a ServiceNow incident and posts to Microsoft Teams ([Restart Apache](../../server_actions/SVA-5bg39wkr/)).

## Contents
| Role | ID | Name |
|---|---|---|
| Webhook | IWH-i7b44zvs | Monitoring Alerts |
| Plugin | OHK-ogu8lqt2 | Monitoring Alerts |
| Shared module | SHM-qlvol7k8 | alert_escalation |
| Server action | SVA-5bg39wkr | Restart Apache |

## Setup
1. Sync `webhooks/IWH-i7b44zvs` and `server_actions/SVA-5bg39wkr`, or import the [Alert Remediation](../../packages/PKG-yo8vupwq/) content package. Syncing one does not bring the other.
2. Admin > Inbound Web Hooks > Monitoring Alerts: set a token (a long random string) and save. A sync sets the token to empty, so set it again after every sync; until you do, the webhook answers 503 and starts nothing.
3. Admin > Security > Users: add a local user named `monitoring-alerts`. In the groups whose servers an alert may act on, give it a role that can run Restart Apache. Its visible servers and permitted actions bound what an alert can do, and the jobs are owned by it.
4. Set up the escalation connections described in the [Restart Apache](../../server_actions/SVA-5bg39wkr/) README.

## Calling it
URL: `/api/v3/cmp/inboundWebHooks/monitoring-alerts/run/` (trailing slash required). The webhook uses token authentication: the caller adds `?token=` and needs no CloudBolt login, and the job runs as `monitoring-alerts`. A call that also carries a CloudBolt session or a bearer token from the `apiToken` endpoint acts as that user instead.

```bash
curl -sS -X POST "https://cloudbolt.example.com/api/v3/cmp/inboundWebHooks/monitoring-alerts/run/?token=<webhook token>"   -H "Content-Type: application/json"   -d '{"version": "4", "status": "firing", "alerts": [{"status": "firing", "labels": {"alertname": "ApacheDown", "severity": "critical", "namespace": "demo", "name": "web-001"}, "annotations": {"summary": "Apache is down on web-001"}, "startsAt": "2026-01-01T00:00:00Z", "fingerprint": "abc123"}]}'
```

The body is the Alertmanager [webhook payload](https://prometheus.io/docs/alerting/latest/configuration/#webhook_config). Only each alert's `status`, `labels`, `annotations`, `fingerprint`, `startsAt` and `generatorURL` are read.

### Alertmanager
Keep the token in a file and point `url_file` at it, so it stays out of the configuration ([docs](https://prometheus.io/docs/alerting/latest/configuration/#webhook_config)). The file holds the whole URL, token included.

```yaml
receivers:
  - name: cloudbolt-remediation
    webhook_configs:
      - url_file: /etc/alertmanager/cloudbolt-monitoring-alerts.url
        send_resolved: false
```

```
https://cloudbolt.example.com/api/v3/cmp/inboundWebHooks/monitoring-alerts/run/?token=<webhook token>
```

The server comes from these labels, tried in order: `cloudbolt_server` (a `SVR-` ID), `name`, `vmi`, `hostname`, `host`, `instance` (a `:port` suffix is dropped), `nodename`. It is matched to a server's hostname, ignoring case, and only when none matches, to its IP. If the first label matches no server the next one is tried; a label that matches more than one server stops the search as `ambiguous server`. VM names can repeat across namespaces, so for a precise target send the server's global ID (`SVR-...`) in a `cloudbolt_server` label.

The answer is `{"received", "started", "results": [...]}`, status 202 when at least one job started and 200 otherwise. Each result has `alert` and `result`, plus `server`, `server_url`, `action`, `job` and `job_url` when they apply.

| `result` | Meaning |
|---|---|
| `started` | The job is running; follow `job_url` |
| `already running` | A job of that action is already active on the server, so a duplicate post while a restart runs returns that job in `job` and `job_url` |
| `ignored: resolved` | The alert is not firing |
| `no remediation mapped` | Nothing in `REMEDIATIONS` for that `alertname` |
| `no matching server` | No active server the caller can see matches the labels |
| `ambiguous server` | More than one does |
| `not permitted to run <action> on this server` | The calling user cannot run the action on that server |
| `action not found or disabled` | No enabled server action has the mapped label |
| `submitted for approval` | The action requires approval, so an order was created instead of a job |
| `action did not start (server not eligible)` | CloudBolt skipped the server, for example because of the action's OS or technology limits |
| `ignored: too many alerts in one request` | Only the first 50 alerts of a request are handled |
| `ignored: not an alert object` | The list item is not an object |
| `error: <type>` | An unexpected error on that alert; the others are still handled |

A 400 means the body has no `alerts` list, a wrong token is refused by CloudBolt before the plug-in runs, and a 503 means the webhook has no token yet or the `monitoring-alerts` user is missing or inactive.

## Mapping another alert
Add a line to `REMEDIATIONS` in `plugins/OHK-ogu8lqt2/OHK-ogu8lqt2_script.py`: the alert's `alertname` and the label of the server action to run.

```python
REMEDIATIONS = {"ApacheDown": "Restart Apache", "NginxDown": "Restart Nginx"}
```

The action's `run()` must accept `alert` or `**kwargs` to receive the alert.

## Try it
Stop Apache on a test server with the Run Script action:

```bash
sudo systemctl stop httpd
```

Post the alert above with `name` set to that server's hostname, then watch the Restart Apache job in CloudBolt. It restarts Apache and succeeds.

For the failure path, break Apache so it cannot start, then post the alert again:

```bash
echo 'NotADirective' | sudo tee /etc/httpd/conf.d/zz-broken.conf && sudo systemctl stop httpd
```

The job fails with Apache's status and journal output, opens a ServiceNow incident and posts to Teams. Undo it with:

```bash
sudo rm /etc/httpd/conf.d/zz-broken.conf
```

## Notes
- The webhook runs in the web service, so it only starts jobs and never waits for them.
- The web service keeps the shared module it loaded until it restarts.
