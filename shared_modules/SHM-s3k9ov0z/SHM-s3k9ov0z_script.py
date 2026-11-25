"""
Shared module ip_reservations (shared_modules/SHM-s3k9ov0z).

Reserves and releases IP addresses from CloudBolt IP pools (Admin > Resource
Pools) as IP Address Reservation resources (blueprints/BP-j25t4xpv). The
blueprint's build plug-in (plugins/OHK-yeykzvdv) and the IP Reservations
webhook (plugins/OHK-axepfitm) both use it, so an ordered reservation and one
made through the webhook are the same kind of resource.

A pool row counts as used only while a server or a resource is attached to
it, so each reserved IP is attached to its reservation resource. CloudBolt's
Delete action on the resource frees the IP.
"""
from django.db.models import Q

from infrastructure.models import CustomField, ResourcePool, ResourcePoolValueSet
from resources.models import Resource
from servicecatalog.models import ServiceBlueprint
from utilities.events import add_resource_event
from utilities.exceptions import CloudBoltException

BLUEPRINT_ID = "BP-j25t4xpv"
BLUEPRINT_NAME = "IP Address Reservation"

CF_ADDRESS = "ip_reservation_address"
CF_POOL = "ip_reservation_pool"
CF_POOL_ID = "ip_reservation_pool_id"
CF_REFERENCE = "ip_reservation_reference"
CF_HOSTNAME = "ip_reservation_hostname"

CUSTOM_FIELDS = (
    (CF_ADDRESS, "STR", "Reserved IP", "IP address reserved from the pool."),
    (CF_POOL, "STR", "IP Pool", "Name of the IP pool the address was reserved from."),
    (CF_POOL_ID, "INT", "IP Pool ID", "ID of the IP pool the address was reserved from."),
    (CF_REFERENCE, "STR", "Reservation Reference", "The requester's key for this reservation."),
    (CF_HOSTNAME, "STR", "Requested Hostname", "Hostname the address will be used for."),
)

NETWORK_FIELDS = ("name", "network", "vlan", "netmask", "gateway", "dns1", "dns2", "dns_domain")


class ReservationError(Exception):
    """A request that cannot be met; status is the HTTP code the webhook answers with."""

    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def _jsonable(value):
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def blueprint(required=True):
    """The IP Address Reservation blueprint, by global ID, then by name."""
    found = (
        ServiceBlueprint.objects.filter(global_id=BLUEPRINT_ID).first()
        or ServiceBlueprint.objects.filter(name=BLUEPRINT_NAME).first()
    )
    if required and (found is None or found.resource_type_id is None):
        raise ReservationError(
            500, f"The {BLUEPRINT_NAME} blueprint ({BLUEPRINT_ID}) is not on this CloudBolt with its resource type; sync blueprints/{BLUEPRINT_ID}."
        )
    return found


def ensure_custom_fields():
    """Create the reservation's fields with Show as attribute on, so the resource's
    Overview tab lists them, and turn it on for fields an older version created."""
    for name, cf_type, label, description in CUSTOM_FIELDS:
        cf, _ = CustomField.objects.get_or_create(
            name=name,
            defaults={"type": cf_type, "label": label, "description": description, "show_as_attribute": True},
        )
        if not cf.show_as_attribute:
            cf.show_as_attribute = True
            cf.save()


def ip_pools(tenant_id=None):
    """Pools that provide IP addresses: global ones, plus the tenant's when tenant_id is set."""
    pools = ResourcePool.objects.filter(include_ipaddress=True)
    if tenant_id:
        pools = pools.filter(Q(tenant_id=tenant_id) | Q(tenant=None))
    else:
        pools = pools.filter(tenant=None)
    return pools.order_by("name", "id")


def pool_label(pool):
    values = pool.get_values()
    free = values.filter(server=None, resource=None).count()
    return f"{pool.name or pool} ({free} of {values.count()} free)"


def pool_for_order(pool_ref, profile):
    """The pool an order names, if the orderer's tenant may use it."""
    pool = None
    if str(pool_ref).strip().isdigit():
        pool = ip_pools(getattr(profile, "tenant_id", None)).filter(id=int(pool_ref)).first()
    if pool is None:
        raise ReservationError(400, f"No IP pool you can order from has ID '{pool_ref}'.")
    return pool


def find_by_reference(bp, pool, reference):
    """The active reservation in pool with this reference, or None."""
    return (
        Resource.objects.filter(
            blueprint=bp,
            lifecycle="ACTIVE",
            resourcepoolvalueset__resource_pool=pool,
            attributes__field__name=CF_REFERENCE,
            attributes__str_value=reference,
        )
        .distinct()
        .first()
    )


def free(resource):
    for rpvs in resource.resourcepoolvalueset_set.all():
        rpvs.free()


def reserve(resource, pool, reference="", hostname="", job=None):
    """Claim the next free IP in pool for resource, the way provisioning picks one,
    and record it on the resource. Returns the claimed pool row."""
    if not pool.include_ipaddress:
        raise ReservationError(400, f"Resource pool '{pool}' does not provide IP addresses.")
    if reference:
        existing = find_by_reference(resource.blueprint, pool, reference)
        if existing and existing.id != resource.id:
            raise ReservationError(
                409, f"Reference '{reference}' already holds {existing.name} in '{pool}' ({existing.global_id})."
            )
    ensure_custom_fields()
    try:
        rpvs = pool.pick_rpvs(resource=resource, job=job)
    except CloudBoltException as err:
        free(resource)
        raise ReservationError(409, str(err))

    # pick_rpvs can hand back a row it did not end up claiming after a database
    # lock timeout, so trust only a row that now points at this resource.
    claimed = ResourcePoolValueSet.objects.filter(id=getattr(rpvs, "id", None), resource=resource).first()
    if claimed is None or not claimed.ip:
        free(resource)
        raise ReservationError(409, f"Could not claim an IP address from '{pool}'; try again.")

    resource.name = (f"{claimed.ip} ({hostname})" if hostname else claimed.ip)[:255]
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
    return claimed


def release(resource, profile=None, how="through the IP Reservations webhook"):
    """Free the reservation's IP and mark it Historical, as CloudBolt's Delete does."""
    ip = resource.get_value_for_custom_field(CF_ADDRESS) or resource.name
    free(resource)
    resource.lifecycle = "HISTORICAL"
    resource.save()
    add_resource_event("DECOMMISSION", resource, f"Released {ip} {how}.", profile=profile)
    return ip


def discard(resource):
    """Undo a reservation that got no IP: free whatever it holds and delete it."""
    free(resource)
    resource.delete()


def held_values(resource):
    return resource.resourcepoolvalueset_set.select_related("resource_pool")


def pool_id(resource):
    value = resource.get_value_for_custom_field(CF_POOL_ID)
    if value is None:
        held = held_values(resource).first()
        value = held.resource_pool_id if held else None
    return value


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


def describe(resource):
    held = held_values(resource).first()
    body = {
        "reservation_id": resource.global_id,
        "status": {"ACTIVE": "reserved", "HISTORICAL": "released"}.get(resource.lifecycle, "pending"),
        "ip": resource.get_value_for_custom_field(CF_ADDRESS) or (held.ip if held else ""),
        "pool": {"id": pool_id(resource), "name": resource.get_value_for_custom_field(CF_POOL) or ""},
        "group": resource.group.name,
        "reference": resource.get_value_for_custom_field(CF_REFERENCE) or "",
        "hostname": resource.get_value_for_custom_field(CF_HOSTNAME) or "",
    }
    if held:
        body.update(_pool_values(held.resource_pool, held))
    return body
