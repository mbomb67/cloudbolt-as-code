# IP Reservations

Inbound webhook that lets an external system reserve the next free IP address in a CloudBolt IP pool, release it, and check how many are free. Each reserved IP becomes an IP Reservation resource ([BP-j25t4xpv](../../blueprints/BP-j25t4xpv/)) in the group the caller names. The pool's Values tab links the IP to that resource, and deleting the resource in CloudBolt frees the IP.

## Contents
| Role | ID | Name |
|---|---|---|
| Webhook | IWH-x82773zb | IP Reservations |
| Plugin | OHK-axepfitm | IP Reservations |
| Holder blueprint | BP-j25t4xpv | IP Reservation |

## Setup
1. Sync `webhooks/IWH-x82773zb` and `blueprints/BP-j25t4xpv`, or import the [IP Reservations](../../packages/PKG-g151nn7r/) content package. Syncing one does not bring the other.
2. Admin > Security > Users: add a local user for the external system.
3. Admin > Security > Roles > Admin Roles: add a role with the Manage Resource Pools permission and add that user to it. A user in a tenant can reserve only from that tenant's pools.
4. Add the user to the group that will own the reservations.

## Calling it
Get a token (valid for five minutes), then call the webhook with it:

```bash
TOKEN=$(curl -sS -X POST "https://<appliance>/api/v3/cmp/apiToken/" \
  -H "Content-Type: application/json" \
  -d '{"username": "<user>", "password": "<password>"}' | python -c "import json,sys; print(json.load(sys.stdin)['token'])")

curl -sS -X POST "https://<appliance>/api/v3/cmp/inboundWebHooks/ip-reservations/run/" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"action": "reserve", "pool": "<pool ID or name>", "group": "<group>", "reference": "<your ticket or VM ID>", "hostname": "web01"}'
```

| Call | Body or query | Answer |
|---|---|---|
| Reserve | `POST {"action": "reserve", "pool", "group", "reference", "hostname", "description"}` | 201 and the reservation; 200 and the existing reservation when `reference` is already active in the pool |
| Release | `POST {"action": "release", "reservation_id"}` or `{"action": "release", "pool", "ip"}` | 200 and the reservation, `status` `released` |
| Pool status | `GET ?pool=<pool>` | `{"pool", "total", "free", "in_use", "reserved"}` |
| Look up | `GET ?reservation_id=<RSC-id>` or `GET ?pool=<pool>&reference=<key>` | The reservation |

A reservation answers `reservation_id` (the resource's `RSC-` ID), `status`, `ip`, `pool`, `group`, `reference` and `hostname`. While it holds the IP it also carries `pool_hostname` and `mac` when the pool provides them, `parameters` (the pool's parameter values for that IP), and `network` (`netmask`, `gateway`, `dns1`, `dns2`, `dns_domain`, `vlan`) when the pool is mapped to a network. Errors answer `{"error": "..."}` with 400, 403, 404 or 409 (pool full, or the IP is held by a server).

## Notes
- `pool` is the number in the pool's URL (`/resourcepools/<id>/`) or its exact name; a name shared by two pools is refused.
- `group` is a group name, ID or `GRP-` ID, and may be left out when the user belongs to exactly one group.
- The IP is picked the same way provisioning picks one: the pool row is locked while it is claimed, and enabled Validate IP Address orchestration actions run on it, skipping any IP they reject. Each IP they check makes the call slower. They run with no job, so one that writes to the job fails the call with a 500.
- Send a `reference` and retry with the same one after a timeout: you get the IP you already reserved, not a second one. Two calls with the same reference at the same moment can still reserve two IPs.
- Release frees only IPs this webhook reserved. An IP held by a server is refused with 409.
- Custom fields `ip_reservation_address`, `ip_reservation_pool`, `ip_reservation_pool_id`, `ip_reservation_reference` and `ip_reservation_hostname` are created on the first reservation.
