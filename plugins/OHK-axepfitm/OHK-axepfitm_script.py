"""
CloudBolt inbound webhook plug-in: IP Reservations (webhooks/IWH-x82773zb).

Lets an external system reserve the next free IP address in a CloudBolt IP
pool (Admin > Resource Pools), release it, and check a pool's free count.
Each reservation is a Resource of the IP Reservation blueprint
(blueprints/BP-j25t4xpv), so the pool's Values tab links every reserved IP to
its reservation, and deleting that resource in CloudBolt frees the IP.

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
    Frees the IP and marks the reservation Historical. Releases only IPs this
    webhook reserved, never one a server or another resource holds. Releasing
    an already released reservation_id answers 200 again; an IP that is
    already free answers 404.
GET ?pool=<id or name>
    {"pool": {"id", "name"}, "total", "free", "in_use", "reserved"}; reserved
    is the part of in_use held by IP reservations.
GET ?reservation_id=<RSC- id>   or   ?pool=<id or name>&reference=<key>
    The reservation, in the shape reserve answers with.

A reservation is {"reservation_id", "status" (reserved, released, or pending
while it is being claimed), "ip",
"pool": {"id", "name"}, "group", "reference", "hostname"} plus, while it holds
the IP, what the pool supplies with it: "pool_hostname" and "mac" when the
pool provides them, "parameters" (the pool's parameter values for that IP)
and "network" (netmask, gateway, DNS) when the pool is mapped to a network.

Errors answer {"error": "..."} with 400, 403, 404, 409 (pool full, or the IP
is held by something else) or 500 (the blueprint is not on this CloudBolt).

Authentication: 'normal' mode, so callers send a CloudBolt API token
(Authorization: Bearer) for a service account. The webhook endpoint checks no
permissions itself, so this plug-in does: the caller must be a CloudBolt admin
or hold the Manage Resource Pools admin permission (limited to their tenant
when they have one) for the pool, and a new reservation is filed only under a
group the caller belongs to.
"""
from accounts.models import Group
from infrastructure.models import CustomField, ResourcePool, ResourcePoolValueSet
from resources.models import Resource
from servicecatalog.models import ServiceBlueprint
from utilities.events import add_resource_event
from utilities.exceptions import CloudBoltException
from utilities.logger import ThreadLogger

logger = ThreadLogger(__name__)

BLUEPRINT_ID = "BP-j25t4xpv"
BLUEPRINT_NAME = "IP Reservation"

CF_ADDRESS = "ip_reservation_address"
CF_POOL = "ip_reservation_pool"
CF_POOL_ID = "ip_reservation_pool_id"
CF_REFERENCE = "ip_reservation_reference"
CF_HOSTNAME = "ip_reservation_hostname"

CUSTOM_FIELDS = (
    (CF_ADDRESS, "STR", "Reserved IP", "IP address reserved from the pool."),
    (CF_POOL, "STR", "IP Pool", "Name of the IP pool the address was reserved from."),
    (CF_POOL_ID, "INT", "IP Pool ID", "ID of the IP pool the address was reserved from."),
    (CF_REFERENCE, "STR", "Reservation Reference", "The requesting system's key for this reservation."),
    (CF_HOSTNAME, "STR", "Requested Hostname", "Hostname the requesting system will use the address for."),
)

NETWORK_FIELDS = ("name", "network", "vlan", "netmask", "gateway", "dns1", "dns2", "dns_domain")


class ReservationError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def _error(status, message):
    return {"iwh_status_code": status, "iwh_embedded_response": {"error": message}}


def _param(parameters, name):
    value = parameters.get(name) if parameters is not None else None
    if isinstance(value, list):
        value = value[0] if value else None
    return "" if value is None else str(value).strip()


def _jsonable(value):
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _blueprint(required=True):
    blueprint = (
        ServiceBlueprint.objects.filter(global_id=BLUEPRINT_ID).first()
        or ServiceBlueprint.objects.filter(name=BLUEPRINT_NAME).first()
    )
    if required and (blueprint is None or blueprint.resource_type_id is None):
        raise ReservationError(
            500, f"The {BLUEPRINT_NAME} blueprint ({BLUEPRINT_ID}) is not on this CloudBolt with its resource type; sync blueprints/{BLUEPRINT_ID}."
        )
    return blueprint


def _manageable_pools(profile):
    """Pools the caller may reserve from: all of them for a CloudBolt admin, the
    tenant's (or every) pool for Manage Resource Pools, none otherwise."""
    return ResourcePool.objects_for_profile(profile, permission="MANAGE")


def _resolve_pool(profile, ref):
    if not ref:
        raise ReservationError(400, "pool is required: the IP pool's numeric ID or its exact name.")
    pools = _manageable_pools(profile)
    pool = pools.filter(id=int(ref)).first() if ref.isdigit() else None
    if pool is None:
        matches = list(pools.filter(name__iexact=ref)[:2])
        if len(matches) > 1:
            raise ReservationError(400, f"More than one IP pool is named '{ref}'; pass the pool's numeric ID.")
        pool = matches[0] if matches else None
    if pool is None:
        raise ReservationError(404, f"No IP pool you can manage matches '{ref}'.")
    if not pool.include_ipaddress:
        raise ReservationError(400, f"Resource pool '{pool}' does not provide IP addresses.")
    return pool


def _resolve_group(profile, ref):
    if not ref:
        own = list(profile.get_groups(include_global_roles=False)[:2])
        if len(own) == 1:
            return own[0]
        raise ReservationError(400, "group is required: the CloudBolt group that owns the reservation.")
    if ref.upper().startswith("GRP-"):
        matches = list(Group.objects.filter(global_id=ref)[:1])
    elif ref.isdigit():
        matches = list(Group.objects.filter(id=int(ref))[:1])
    else:
        matches = list(Group.objects.filter(name=ref)[:2])
    if len(matches) > 1:
        raise ReservationError(400, f"More than one group is named '{ref}'; pass its GRP- ID.")
    group = matches[0] if matches else None
    if group is None or not (
        profile.is_cbadmin or profile.get_groups(include_inherited=True).filter(id=group.id).exists()
    ):
        raise ReservationError(404, f"No group you belong to matches '{ref}'.")
    return group


def _ensure_custom_fields():
    for name, cf_type, label, description in CUSTOM_FIELDS:
        CustomField.objects.get_or_create(
            name=name, defaults={"type": cf_type, "label": label, "description": description}
        )


def _find_by_reference(blueprint, pool, reference):
    return (
        Resource.objects.filter(
            blueprint=blueprint,
            lifecycle="ACTIVE",
            resourcepoolvalueset__resource_pool=pool,
            attributes__field__name=CF_REFERENCE,
            attributes__str_value=reference,
        )
        .distinct()
        .first()
    )


def _held_values(resource):
    return resource.resourcepoolvalueset_set.select_related("resource_pool")


def _pool_id(resource):
    pool_id = resource.get_value_for_custom_field(CF_POOL_ID)
    if pool_id is None:
        held = _held_values(resource).first()
        pool_id = held.resource_pool_id if held else None
    return pool_id


def _resolve_reservation(profile, blueprint, ref):
    query = Resource.objects.filter(blueprint=blueprint)
    if ref.upper().startswith("RSC-"):
        resource = query.filter(global_id=ref).first()
    elif ref.isdigit():
        resource = query.filter(id=int(ref)).first()
    else:
        resource = None
    pool_id = _pool_id(resource) if resource else None
    if resource is None or pool_id is None or not _manageable_pools(profile).filter(id=pool_id).exists():
        raise ReservationError(404, f"No IP reservation you can manage matches '{ref}'.")
    return resource


def _network(pool):
    mapping = pool.resourcepoolmapping_set.exclude(network=None).select_related("network").first()
    if mapping is None:
        return None
    values = {field: getattr(mapping.network, field, None) for field in NETWORK_FIELDS}
    return {field: _jsonable(value) for field, value in values.items() if value not in (None, "")}


def _pool_values(pool, rpvs):
    """What the pool supplies with the IP, as provisioning would apply it to a server."""
    values = {}
    if pool.include_hostname and rpvs.hostname:
        values["pool_hostname"] = rpvs.hostname
    if pool.include_mac and rpvs.mac:
        values["mac"] = rpvs.mac
    parameters = {
        cfv.field.name: _jsonable(cfv.value) for cfv in rpvs.custom_field_values.select_related("field")
    }
    if parameters:
        values["parameters"] = parameters
    network = _network(pool)
    if network:
        values["network"] = network
    return values


def _describe(resource):
    held = _held_values(resource).first()
    body = {
        "reservation_id": resource.global_id,
        "status": {"ACTIVE": "reserved", "HISTORICAL": "released"}.get(resource.lifecycle, "pending"),
        "ip": resource.get_value_for_custom_field(CF_ADDRESS) or (held.ip if held else ""),
        "pool": {"id": _pool_id(resource), "name": resource.get_value_for_custom_field(CF_POOL) or ""},
        "group": resource.group.name,
        "reference": resource.get_value_for_custom_field(CF_REFERENCE) or "",
        "hostname": resource.get_value_for_custom_field(CF_HOSTNAME) or "",
    }
    if held:
        body.update(_pool_values(held.resource_pool, held))
    return body


def _pool_summary(pool):
    values = pool.get_values()
    total = values.count()
    free = values.filter(server=None, resource=None).count()
    blueprint = _blueprint(required=False)
    reserved = values.filter(server=None, resource__blueprint=blueprint).count() if blueprint else 0
    return {
        "pool": {"id": pool.id, "name": pool.name or str(pool)},
        "total": total,
        "free": free,
        "in_use": total - free,
        "reserved": reserved,
    }


def _free(resource):
    for rpvs in resource.resourcepoolvalueset_set.all():
        rpvs.free()


def _discard(resource):
    """Undo a reservation that got no IP: free whatever it holds and delete it."""
    _free(resource)
    resource.delete()


def _reserve(profile, parameters):
    pool = _resolve_pool(profile, _param(parameters, "pool"))
    group = _resolve_group(profile, _param(parameters, "group"))
    reference = _param(parameters, "reference")
    hostname = _param(parameters, "hostname")
    blueprint = _blueprint()
    if reference:
        existing = _find_by_reference(blueprint, pool, reference)
        if existing:
            return 200, dict(_describe(existing), existing=True)

    _ensure_custom_fields()
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
        rpvs = pool.pick_rpvs(resource=resource)
    except CloudBoltException as err:
        _discard(resource)
        raise ReservationError(409, str(err))
    except Exception:
        _discard(resource)
        raise

    # pick_rpvs can hand back a row it did not end up claiming after a database
    # lock timeout, so trust only a row that now points at this resource.
    claimed = ResourcePoolValueSet.objects.filter(id=getattr(rpvs, "id", None), resource=resource).first()
    if claimed is None or not claimed.ip:
        _discard(resource)
        raise ReservationError(409, f"Could not claim an IP address from '{pool}'; try again.")

    resource.name = claimed.ip
    resource.lifecycle = "ACTIVE"
    resource.save()
    attributes = (
        (CF_ADDRESS, claimed.ip),
        (CF_POOL, pool.name or str(pool)),
        (CF_POOL_ID, pool.id),
        (CF_REFERENCE, reference),
        (CF_HOSTNAME, hostname),
    )
    for name, value in attributes:
        if value != "":
            resource.set_value_for_custom_field(name, value)
    add_resource_event(
        "CREATION", resource, f"Reserved {claimed.ip} from IP pool '{pool}' through the IP Reservations webhook.",
        profile=profile,
    )
    logger.info("IP Reservations: %s reserved %s from pool %s (%s)", profile, claimed.ip, pool, resource.global_id)
    return 201, dict(_describe(resource), existing=False)


def _release(profile, parameters):
    blueprint = _blueprint()
    ref = _param(parameters, "reservation_id")
    if ref:
        resource = _resolve_reservation(profile, blueprint, ref)
    else:
        pool = _resolve_pool(profile, _param(parameters, "pool"))
        ip = _param(parameters, "ip")
        if not ip:
            raise ReservationError(400, "Pass reservation_id, or pool and ip.")
        rpvs = pool.get_values().filter(ip=ip).select_related("server", "resource").first()
        if rpvs is None:
            raise ReservationError(404, f"{ip} is not in IP pool '{pool}'.")
        if rpvs.server_id or (rpvs.resource_id and rpvs.resource.blueprint_id != blueprint.id):
            holder = rpvs.server or rpvs.resource
            raise ReservationError(409, f"{ip} is held by {holder}, not by an IP reservation; release it there.")
        if rpvs.resource_id is None:
            raise ReservationError(404, f"{ip} in IP pool '{pool}' is not reserved.")
        resource = rpvs.resource

    if resource.lifecycle == "HISTORICAL":
        return 200, dict(_describe(resource), already_released=True)
    ip = resource.get_value_for_custom_field(CF_ADDRESS) or resource.name
    _free(resource)
    resource.lifecycle = "HISTORICAL"
    resource.save()
    add_resource_event(
        "DECOMMISSION", resource, f"Released {ip} through the IP Reservations webhook.", profile=profile
    )
    logger.info("IP Reservations: %s released %s (%s)", profile, ip, resource.global_id)
    return 200, dict(_describe(resource), already_released=False)


def inbound_web_hook_get(*args, parameters=None, profile=None, **kwargs):
    if profile is None:
        return _error(403, "Authenticate with a CloudBolt API token.")
    try:
        reservation_id = _param(parameters, "reservation_id")
        if reservation_id:
            return _describe(_resolve_reservation(profile, _blueprint(), reservation_id))
        pool = _resolve_pool(profile, _param(parameters, "pool"))
        reference = _param(parameters, "reference")
        if reference:
            resource = _find_by_reference(_blueprint(), pool, reference)
            if resource is None:
                raise ReservationError(404, f"No active reservation in '{pool}' has reference '{reference}'.")
            return _describe(resource)
        return _pool_summary(pool)
    except ReservationError as err:
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
    except ReservationError as err:
        return _error(err.status, str(err))
    return {"iwh_status_code": status, "iwh_embedded_response": body}
