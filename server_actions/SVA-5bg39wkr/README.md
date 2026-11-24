# Restart Apache

Server action that restarts Apache on a Linux server and checks that it came back. It works on any Linux server: OpenShift Virtualization VMs are reached through the cluster API tunnel, every other server through CloudBolt's script execution over SSH. Windows servers are refused.

## What it checks
It restarts `httpd` (or `apache2` where that unit exists), then checks up to six times, five seconds apart, that the unit is active and that a local request to `https://localhost/` (or `http://localhost/` when HTTPS does not answer) returns an HTTP code from 200 to 499. The job succeeds on the first passing check. If none passes it fails and prints `systemctl status` and the last journal lines.

## When it escalates
Only when an alert started the job: the [Monitoring Alerts](../../webhooks/IWH-i7b44zvs/) webhook passes the alert, and someone pressing the button passes none. On a failed health check the job opens a ServiceNow incident, then posts to Microsoft Teams with the incident number, and lists what it did at the end of the job output. An alert that already has an open incident (matched on its fingerprint) gets a work note on that incident instead of a new one. Each half is independent: one failing or not being set up does not stop the other.

## Connections escalation needs
| Connection | Setup |
|---|---|
| ServiceNow | Admin > ITSM integration or Admin > Connection Info named `ServiceNow`, the same connection the ServiceNow request content uses. The user needs permission to create incidents. |
| Microsoft Teams | Admin > Connection Info named `Microsoft Teams Alerts`. Create a Workflows webhook in Teams ("When a Teams webhook request is received", [how to](https://learn.microsoft.com/en-us/microsoftteams/platform/webhooks-and-connectors/how-to/add-incoming-webhook)) and paste its URL into the **Password** field. IP/Hostname is required by the form but not used. |

With neither configured the job still fails with the diagnostics.
