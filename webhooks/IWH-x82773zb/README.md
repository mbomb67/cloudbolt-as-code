# IP Reservations

Inbound webhook that lets an external system reserve the next free IP address in a CloudBolt IP pool, release it, and check how many are free, in one call each and without placing an order. Each reservation is an [IP Address Reservation](../../blueprints/BP-j25t4xpv/) resource, the same resource an order of that blueprint creates, so the webhook can look up and release ordered reservations too. That README compares the two ways in.

## Contents
| Role | ID | Name |
|---|---|---|
| Webhook | IWH-x82773zb | IP Reservations |
| Plugin | OHK-axepfitm | IP Reservations |
| Shared module | SHM-s3k9ov0z | ip_reservations |
| Blueprint | BP-j25t4xpv | IP Address Reservation |

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
| Release | `POST {"action": "release", "reservation_id"}` or `{"action": "release", "pool", "ip"}` | 200 and the reservation, `status` `released`; 200 again for a released `reservation_id`, 404 for an IP that is already free |
| Pool status | `GET ?pool=<pool>` | `{"pool", "total", "free", "in_use", "reserved"}` |
| Look up | `GET ?reservation_id=<RSC-id>` or `GET ?pool=<pool>&reference=<key>` | The reservation |

A reservation answers `reservation_id` (the resource's `RSC-` ID), `status`, `ip`, `pool`, `group`, `reference` and `hostname`. While it holds the IP it also carries `pool_hostname` and `mac` when the pool provides them, `parameters` (the pool's parameter values for that IP), and `network` (`netmask`, `gateway`, `dns1`, `dns2`, `dns_domain`, `vlan`) when the pool is mapped to a network. Errors answer `{"error": "..."}` with 400, 403, 404 or 409 (pool full, or the IP is held by a server).

## Notes
- `pool` is the number in the pool's URL (`/resourcepools/<id>/`) or its exact name; a name shared by two pools is refused.
- `group` is a group name, ID or `GRP-` ID, and may be left out when the user belongs to exactly one group.
- The IP is picked the same way provisioning picks one: the pool row is locked while it is claimed, and enabled Validate IP Address orchestration actions run on it, skipping any IP they reject. Each IP they check makes the call slower (a ping check took about 2 seconds per IP in testing). They run with no job, so one that writes to the job fails the call with a 500.
- A webhook reservation skips the blueprint's group permissions and approvals.
- Send a `reference` and retry with the same one after a timeout: you get the IP you already reserved, not a second one. Two calls with the same reference at the same moment can still reserve two IPs.
- Release frees only IP Address Reservation resources. An IP held by a server is refused with 409.
- The webhook runs in the web service, which keeps the shared module it loaded until the service restarts.
