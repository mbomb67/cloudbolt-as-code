# Runbooks

Adds Run Task and Run Workflow, actions that run any Remote Script that fits a VM or an ordered workflow of CloudBolt actions on it, with base, example and installer task scripts, the Runbooks page for building workflows, base actions and file archives, example Linux and Windows workflows whose scripts read a config file from File Archives, and Linux and Windows blueprints that run a workflow.

Content package `PKG-v20qv308`, version 1.1.0. Sync it from the Content Packages admin page (Content Package Manager UI extension). Restart the web server after the first sync (`systemctl restart httpd`): the UI extension adds URL paths, and the web process keeps shared modules it already loaded.

| Type | Name | Folder |
|---|---|---|
| Plugin | Runbook Task Template (Windows) | [plugins/OHK-lhm2916o](../../plugins/OHK-lhm2916o/) |
| Plugin | Runbook Task Template (Linux) | [plugins/OHK-iub9a2jg](../../plugins/OHK-iub9a2jg/) |
| Plugin | Manage Windows Service | [plugins/OHK-01kmavro](../../plugins/OHK-01kmavro/) |
| Plugin | Manage Linux Service | [plugins/OHK-5yh12jiq](../../plugins/OHK-5yh12jiq/) |
| Plugin | Run Installer from File Archive (Linux) | [plugins/OHK-hgv6wp65](../../plugins/OHK-hgv6wp65/) |
| Plugin | Run Installer from File Archive (Windows) | [plugins/OHK-ow68kerl](../../plugins/OHK-ow68kerl/) |
| Inbound webhook | Runbook Tasks | [webhooks/IWH-cdf543g3](../../webhooks/IWH-cdf543g3/) |
| Inbound webhook | Runbook Workflows | [webhooks/IWH-xjlrmvc8](../../webhooks/IWH-xjlrmvc8/) |
| Server action | Run Task | [server_actions/SVA-4sin3s0q](../../server_actions/SVA-4sin3s0q/) |
| Server action | Run Workflow | [server_actions/SVA-j7rbsh39](../../server_actions/SVA-j7rbsh39/) |
| UI extension | Runbooks | [extensions/XUI-jbm5ac9e](../../extensions/XUI-jbm5ac9e/) |
| Blueprint | Linux VM with Workflow | [blueprints/BP-vy9ytey6](../../blueprints/BP-vy9ytey6/) |
| Blueprint | Windows VM with Workflow | [blueprints/BP-6ddlyguv](../../blueprints/BP-6ddlyguv/) |
| Runbook workflow | Prepare Linux app host | [runbook_workflows/SWF-69r5mmdb](../../runbook_workflows/SWF-69r5mmdb/) |
| Runbook workflow | Prepare Windows app host | [runbook_workflows/SWF-7s01lyyk](../../runbook_workflows/SWF-7s01lyyk/) |

The example workflows' config files travel in their workflow folders, and the sync stores them in File Archives, so the examples run without an upload.
