# IP Address Reservation

Orders the next free address in a CloudBolt IP pool for a server or device CloudBolt does not build, such as a physical server or an appliance. The resource is named after the address, and the pool's Values tab links the address to it. Deleting the resource returns the address to the pool.

The [IP Reservations](../../webhooks/IWH-x82773zb/) webhook creates the same resource without an order.

## Contents
| Role | ID | Name |
|---|---|---|
| Build | OHK-yeykzvdv | IP Address Reservation Build |
| Shared module | SHM-s3k9ov0z | ip_reservations |
| Webhook (synced separately) | IWH-x82773zb | IP Reservations |

## Setup
1. Sync `blueprints/BP-j25t4xpv` (the build plugin and shared module come with it), or import the [IP Reservations](../../packages/PKG-g151nn7r/) content package.
2. On the blueprint's Permissions tab, let the groups that may reserve addresses deploy it. A sync removes every group, so grant them again after each sync.

## Order form
| Input | Required | Notes |
|---|---|---|
| IP Pool | Yes | Every pool that provides IP addresses: global pools, plus the orderer's tenant's. Each option shows how many addresses are free. The value is the pool's ID. |
| Hostname | No | Recorded on the resource. |
| Reference | No | A ticket, asset tag or other ID. A second order with a reference that already holds an address in the pool fails. |

The address is picked the way provisioning picks one: the pool row is locked while it is claimed, and any enabled Validate IP Address orchestration actions check it first.

## Ordering through the API
```bash
curl -sS -X POST "https://<appliance>/api/v3/cmp/blueprints/BP-j25t4xpv/deploy/" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"group": "/api/v3/cloudbolt/groups/<GRP-id>/", "resourceName": "web01-ip",
       "deploymentItems": {"plugin-bdi-41baqvaz": {"parameters": {"ip_pool_id": "<pool ID>", "ip_hostname": "web01", "ip_reference": "<ticket>"}}}}'
```

The answer is the order. Read the address in three more calls: the order's first job is the deploy job (`GET /api/v3/cmp/orders/<ORD-id>/`), poll that job until `status` is `SUCCESS` (`GET /api/v3/cmp/jobs/<JOB-id>/`), then read `ip_reservation_address` from the resource the job links to (`GET /api/v3/cmp/resources/<RSC-id>/`).

## Order or webhook
| | Order | Webhook |
|---|---|---|
| Calls to get an address | Four, with polling | One |
| Who may call | Groups the blueprint lets deploy | CloudBolt admins and holders of Manage Resource Pools |
| Approvals and order history | Yes | No |
| Retry with the same reference | Fails the order | Returns the existing reservation |
| Release | Delete action (a job) | One call, or the Delete action |

## Notes
- The resource's Overview tab shows `ip_reservation_address`, `ip_reservation_pool`, `ip_reservation_pool_id`, `ip_reservation_reference` and `ip_reservation_hostname`. The first reservation creates these fields with Show as attribute on.
- A shared module change reaches running jobs only after the job engine service restarts, and the webhook only after the web service restarts.
