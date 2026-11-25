# IP Reservations

Reserves IP addresses from CloudBolt IP pools for servers and devices CloudBolt does not build, through an order or an inbound webhook, and tracks each reservation as a resource.

Content package `PKG-g151nn7r`, version 1.0.0. Sync it from the Content Packages admin page (Content Package Manager UI extension).

| Type | Name | Folder |
|---|---|---|
| Inbound webhook | IP Reservations | [webhooks/IWH-x82773zb](../../webhooks/IWH-x82773zb/) |
| Blueprint | IP Address Reservation | [blueprints/BP-j25t4xpv](../../blueprints/BP-j25t4xpv/) |

## After the first sync
- Let the groups that may order addresses deploy the blueprint: [blueprints/BP-j25t4xpv](../../blueprints/BP-j25t4xpv/#setup).
- Create the service account the webhook is called with: [webhooks/IWH-x82773zb](../../webhooks/IWH-x82773zb/#setup).
