# IP Reservation

Holds the IP addresses the [IP Reservations](../../webhooks/IWH-x82773zb/) webhook reserves from CloudBolt IP pools, one resource per reserved IP. It is not orderable and has no build or teardown items.

## Notes
- The resource is named after its IP and carries `ip_reservation_address`, `ip_reservation_pool`, `ip_reservation_pool_id`, `ip_reservation_reference` and `ip_reservation_hostname`.
- Deleting the resource frees its IP in the pool, the same as releasing it through the webhook.
- Setup and the webhook's calls: [webhooks/IWH-x82773zb](../../webhooks/IWH-x82773zb/).
