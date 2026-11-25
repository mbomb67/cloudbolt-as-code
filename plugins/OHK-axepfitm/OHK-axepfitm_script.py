"""
CloudBolt inbound webhook plug-in: IP Reservations (webhooks/IWH-x82773zb).

Lets an external system reserve the next free IP address in a CloudBolt IP
pool (Admin > Resource Pools), release it, and check a pool's free count,
without placing an order. Each reservation is an IP Address Reservation
resource (blueprints/BP-j25t4xpv), the same resource an order of that
blueprint creates; the shared logic is in the ip_reservations shared module
(shared_modules/SHM-s3k9ov0z). The pool's Values tab links every reserved IP
to its reservation, and deleting that resource in CloudBolt frees the IP.

URL (trailing slash required): /api/v3/cmp/inboundWebHooks/ip-reservations/run/

POST {"action": "reserve", "pool": <id or name>, "group": <name, id or GRP- id>,
      "reference": <caller's key>, "hostname": ..., "description": ...}
    Reserves the next free IP the way provisioning picks one: the pool row is
    locked while it is claimed and the Validate IP Address orchestration
    actions run on it. Answers 201 with the reservation, or 200 with the
    existing one when a reservation with this reference is already active in
    the pool (a retry). group may be left out when the caller belongs to
    exactly one group. Only pool is required.
POST {"action": "release", "reservation_id": <RSC- id>}
  or {"action": "release", "pool": <id or name>, "ip": <address>}
    Frees the IP and marks the reservation Historical, whether it was ordered
    or made here. Releases only IP Address Reservation resources, never an IP
    a server or another resource holds. Releasing an already released
    reservation_id answers 200 again; an IP that is already free answers 404.
GET ?pool=<id or name>
    {"pool": {"id", "name"}, "total", "free", "in_use", "reserved"}; reserved
    is the part of in_use held by IP Address Reservation resources.
GET ?reservation_id=<RSC- id>   or   ?pool=<id or name>&reference=<key>
    The reservation, in the shape reserve answers with.

A reservation is {"reservation_id", "status" (reserved, released, or pending
while it is being claimed), "ip", "pool": {"id", "name"}, "group",
"reference", "hostname"} plus, while it holds the IP, what the pool supplies
with it: "pool_hostname" and "mac" when the pool provides them, "parameters"
(the pool's parameter values for that IP) and "network" (netmask, gateway,
DNS) when the pool is mapped to a network.

Errors answer {"error": "..."} with 400, 403, 404, 409 (pool full, or the IP
is held by something else) or 500 (the blueprint is not on this CloudBolt).

Authentication: 'normal' mode, so callers send a CloudBolt API token
(Authorization: Bearer) for a service account. The webhook endpoint checks no
permissions itself, so this plug-in does: the caller must be a CloudBolt admin
or hold the Manage Resource Pools admin permission (limited to their tenant
when they have one) for the pool, and a new reservation is filed only under a
group the caller belongs to. Unlike an order, a webhook reservation skips
approvals and the blueprint's group permissions.
"""
from accounts.models import Group
from infrastructure.models import ResourcePool
from resources.models import Resource
from utilities.events import add_resource_event
from utilities.logger import ThreadLogger

from shared_modules import ip_reservations as ipr

logger = ThreadLogger(__name__)


def _error(status, message):
    return {"iwh_status_code": status, "iwh_embedded_response": {"error": message}}


def _param(parameters, name):
    value = parameters.get(name) if parameters is not None else None
    if isinstance(value, list):
        value = value[0] if value else None
    return "" if value is None else str(value).strip()


def _manageable_pools(profile):
    """Pools the caller may reserve from: all of them for a CloudBolt admin, the
    tenant's (or every) pool for Manage Resource Pools, none otherwise."""
    return ResourcePool.objects_for_profile(profile, permission="MANAGE")


def _resolve_pool(profile, ref):
    if not ref:
        raise ipr.ReservationError(400, "pool is required: the IP pool's numeric ID or its exact name.")
    pools = _manageable_pools(profile)
    pool = pools.filter(id=int(ref)).first() if ref.isdigit() else None
    if pool is None:
        matches = list(pools.filter(name__iexact=ref)[:2])
        if len(matches) > 1:
            raise ipr.ReservationError(400, f"More than one IP pool is named '{ref}'; pass the pool's numeric ID.")
        pool = matches[0] if matches else None
    if pool is None:
        raise ipr.ReservationError(404, f"No IP pool you can manage matches '{ref}'.")
    if not pool.include_ipaddress:
        raise ipr.ReservationError(400, f"Resource pool '{pool}' does not provide IP addresses.")
    return pool


def _resolve_group(profile, ref):
    if not ref:
        own = list(profile.get_groups(include_global_roles=False)[:2])
        if len(own) == 1:
            return own[0]
        raise ipr.ReservationError(400, "group is required: the CloudBolt group that owns the reservation.")
    if ref.upper().startswith("GRP-"):
        matches = list(Group.objects.filter(global_id=ref)[:1])
    elif ref.isdigit():
        matches = list(Group.objects.filter(id=int(ref))[:1])
    else:
        matches = list(Group.objects.filter(name=ref)[:2])
    if len(matches) > 1:
        raise ipr.ReservationError(400, f"More than one group is named '{ref}'; pass its GRP- ID.")
    group = matches[0] if matches else None
    if group is None or not (
        profile.is_cbadmin or profile.get_groups(include_inherited=True).filter(id=group.id).exists()
    ):
        raise ipr.ReservationError(404, f"No group you belong to matches '{ref}'.")
    return group


def _resolve_reservation(profile, blueprint, ref):
    query = Resource.objects.filter(blueprint=blueprint)
    if ref.upper().startswith("RSC-"):
        resource = query.filter(global_id=ref).first()
    elif ref.isdigit():
        resource = query.filter(id=int(ref)).first()
    else:
        resource = None
    pool_id = ipr.pool_id(resource) if resource else None
    if resource is None or pool_id is None or not _manageable_pools(profile).filter(id=pool_id).exists():
        raise ipr.ReservationError(404, f"No IP reservation you can manage matches '{ref}'.")
    return resource


def _pool_summary(pool):
    values = pool.get_values()
    total = values.count()
    free = values.filter(server=None, resource=None).count()
    blueprint = ipr.blueprint(required=False)
    reserved = values.filter(server=None, resource__blueprint=blueprint).count() if blueprint else 0
    return {
        "pool": {"id": pool.id, "name": pool.name or str(pool)},
        "total": total,
        "free": free,
        "in_use": total - free,
        "reserved": reserved,
    }


def _reserve(profile, parameters):
    pool = _resolve_pool(profile, _param(parameters, "pool"))
    group = _resolve_group(profile, _param(parameters, "group"))
    reference = _param(parameters, "reference")
    blueprint = ipr.blueprint()
    if reference:
        existing = ipr.find_by_reference(blueprint, pool, reference)
        if existing:
            return 200, dict(ipr.describe(existing), existing=True)

    resource = Resource.objects.create(
        name=f"Reserving an IP from {pool}"[:255],
        description=_param(parameters, "description") or None,
        blueprint=blueprint,
        resource_type=blueprint.resource_type,
        group=group,
        owner=profile,
        lifecycle="PROV",
    )
    try:
        rpvs = ipr.reserve(resource, pool, reference=reference, hostname=_param(parameters, "hostname"))
    except Exception:
        ipr.discard(resource)
        raise

    resource.lifecycle = "ACTIVE"
    resource.save()
    add_resource_event(
        "CREATION", resource, f"Reserved {rpvs.ip} from IP pool '{pool}' through the IP Reservations webhook.",
        profile=profile,
    )
    logger.info("IP Reservations: %s reserved %s from pool %s (%s)", profile, rpvs.ip, pool, resource.global_id)
    return 201, dict(ipr.describe(resource), existing=False)


def _release(profile, parameters):
    blueprint = ipr.blueprint()
    ref = _param(parameters, "reservation_id")
    if ref:
        resource = _resolve_reservation(profile, blueprint, ref)
    else:
        pool = _resolve_pool(profile, _param(parameters, "pool"))
        ip = _param(parameters, "ip")
        if not ip:
            raise ipr.ReservationError(400, "Pass reservation_id, or pool and ip.")
        rpvs = pool.get_values().filter(ip=ip).select_related("server", "resource").first()
        if rpvs is None:
            raise ipr.ReservationError(404, f"{ip} is not in IP pool '{pool}'.")
        if rpvs.server_id or (rpvs.resource_id and rpvs.resource.blueprint_id != blueprint.id):
            holder = rpvs.server or rpvs.resource
            raise ipr.ReservationError(409, f"{ip} is held by {holder}, not by an IP reservation; release it there.")
        if rpvs.resource_id is None:
            raise ipr.ReservationError(404, f"{ip} in IP pool '{pool}' is not reserved.")
        resource = rpvs.resource

    if resource.lifecycle == "HISTORICAL":
        return 200, dict(ipr.describe(resource), already_released=True)
    ip = ipr.release(resource, profile=profile)
    logger.info("IP Reservations: %s released %s (%s)", profile, ip, resource.global_id)
    return 200, dict(ipr.describe(resource), already_released=False)


def inbound_web_hook_get(*args, parameters=None, profile=None, **kwargs):
    if profile is None:
        return _error(403, "Authenticate with a CloudBolt API token.")
    try:
        reservation_id = _param(parameters, "reservation_id")
        if reservation_id:
            return ipr.describe(_resolve_reservation(profile, ipr.blueprint(), reservation_id))
        pool = _resolve_pool(profile, _param(parameters, "pool"))
        reference = _param(parameters, "reference")
        if reference:
            resource = ipr.find_by_reference(ipr.blueprint(), pool, reference)
            if resource is None:
                raise ipr.ReservationError(404, f"No active reservation in '{pool}' has reference '{reference}'.")
            return ipr.describe(resource)
        return _pool_summary(pool)
    except ipr.ReservationError as err:
        return _error(err.status, str(err))


def inbound_web_hook_post(*args, parameters=None, profile=None, **kwargs):
    if profile is None:
        return _error(403, "Authenticate with a CloudBolt API token.")
    action = _param(parameters, "action").lower()
    try:
        if action == "reserve":
            status, body = _reserve(profile, parameters)
        elif action == "release":
            status, body = _release(profile, parameters)
        else:
            return _error(400, "action must be reserve or release.")
    except ipr.ReservationError as err:
        return _error(err.status, str(err))
    return {"iwh_status_code": status, "iwh_embedded_response": body}
