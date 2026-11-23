# Alert Remediation

Maps monitoring alerts posted to an inbound webhook to a Restart Apache server action that health-checks the server and escalates to ServiceNow and Microsoft Teams when it is still unhealthy. It also enables an ApacheDown alert on OpenShift Virtualization VMs.

Content package `PKG-yo8vupwq`, version 1.1.0. Sync it from the Content Packages admin page (Content Package Manager UI extension).

| Type | Name | Folder |
|---|---|---|
| Server action | Restart Apache | [server_actions/SVA-5bg39wkr](../../server_actions/SVA-5bg39wkr/) |
| Server action | Enable Apache Down Alert | [server_actions/SVA-t1z9yt6d](../../server_actions/SVA-t1z9yt6d/) |
| Inbound webhook | Monitoring Alerts | [webhooks/IWH-i7b44zvs](../../webhooks/IWH-i7b44zvs/) |
