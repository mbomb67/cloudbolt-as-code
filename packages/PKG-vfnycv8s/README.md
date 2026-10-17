# Server Workflows

Adds a Run Workflow server action that runs a chosen workflow of CloudBolt actions in order on a server, the webhook its form calls, the Server Workflows admin page for building workflows, and an example blueprint that runs a workflow on the server it deploys.

Content package `PKG-vfnycv8s`, version 1.0.0. Sync it from the Content Packages admin page (Content Package Manager UI extension). Script steps run through the `server_tasks` shared module, which the server action brings; the [Server Tasks](../PKG-93egd3on/) package adds Run Task and example task scripts.

| Type | Name | Folder |
|---|---|---|
| Server action | Run Workflow | [server_actions/SVA-j7rbsh39](../../server_actions/SVA-j7rbsh39/) |
| Inbound webhook | Server Workflows | [webhooks/IWH-xjlrmvc8](../../webhooks/IWH-xjlrmvc8/) |
| UI extension | Server Workflows | [extensions/XUI-jbm5ac9e](../../extensions/XUI-jbm5ac9e/) |
| Blueprint | Server with Workflow | [blueprints/BP-vy9ytey6](../../blueprints/BP-vy9ytey6/) |
