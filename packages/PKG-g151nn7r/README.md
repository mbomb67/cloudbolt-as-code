# IP Reservations

Lets external systems reserve and release IP addresses from CloudBolt IP pools through an inbound webhook, tracking each reservation as a resource.

Content package `PKG-g151nn7r`, version 1.0.0. Sync it from the Content Packages admin page (Content Package Manager UI extension).

| Type | Name | Folder |
|---|---|---|
| Inbound webhook | IP Reservations | [webhooks/IWH-x82773zb](../../webhooks/IWH-x82773zb/) |
| Blueprint | IP Reservation | [blueprints/BP-j25t4xpv](../../blueprints/BP-j25t4xpv/) |

## After the first sync
Create the service account the external system calls with: [webhooks/IWH-x82773zb](../../webhooks/IWH-x82773zb/#setup).
