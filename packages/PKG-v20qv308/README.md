# Server Automation

Adds Run Task and Run Workflow, server actions that run any Remote Script that fits a server and ordered workflows of CloudBolt actions on it, with base, example and file-installer task scripts, the Server Workflows page for building workflows and base actions, the File Archives page, and an example blueprint that runs a workflow.

Content package `PKG-v20qv308`, version 1.0.0. Sync it from the Content Packages admin page (Content Package Manager UI extension). Restart the web server after the first sync (`systemctl restart httpd`): both UI extensions add URL paths.

| Type | Name | Folder |
|---|---|---|
| Plugin | Server Task Template (Windows) | [plugins/OHK-lhm2916o](../../plugins/OHK-lhm2916o/) |
| Plugin | Server Task Template (Linux) | [plugins/OHK-iub9a2jg](../../plugins/OHK-iub9a2jg/) |
| Plugin | Manage Windows Service | [plugins/OHK-01kmavro](../../plugins/OHK-01kmavro/) |
| Plugin | Manage Linux Service | [plugins/OHK-5yh12jiq](../../plugins/OHK-5yh12jiq/) |
| Plugin | Run Installer from File Archive (Linux) | [plugins/OHK-hgv6wp65](../../plugins/OHK-hgv6wp65/) |
| Plugin | Run Installer from File Archive (Windows) | [plugins/OHK-ow68kerl](../../plugins/OHK-ow68kerl/) |
| Inbound webhook | Server Tasks | [webhooks/IWH-cdf543g3](../../webhooks/IWH-cdf543g3/) |
| Inbound webhook | Server Workflows | [webhooks/IWH-xjlrmvc8](../../webhooks/IWH-xjlrmvc8/) |
| Server action | Run Task | [server_actions/SVA-4sin3s0q](../../server_actions/SVA-4sin3s0q/) |
| Server action | Run Workflow | [server_actions/SVA-j7rbsh39](../../server_actions/SVA-j7rbsh39/) |
| UI extension | Server Workflows | [extensions/XUI-jbm5ac9e](../../extensions/XUI-jbm5ac9e/) |
| UI extension | File Archives | [extensions/XUI-0hv6t61x](../../extensions/XUI-0hv6t61x/) |
| Blueprint | Server with Workflow | [blueprints/BP-vy9ytey6](../../blueprints/BP-vy9ytey6/) |
