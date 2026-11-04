"""
CloudBolt build plug-in: IP Address Reservation (blueprints/BP-j25t4xpv).

Reserves the next free address in the chosen CloudBolt IP pool for the ordered
resource and names the resource after the address, with the hostname in
parentheses when one is given. Deleting the resource returns the address to
the pool. The logic lives in the ip_reservations shared
module (shared_modules/SHM-s3k9ov0z), which the IP Reservations webhook
(webhooks/IWH-x82773zb) uses too.

Inputs: IP Pool (pools that provide IP addresses, global ones plus the
orderer's tenant's), and the optional Hostname and Reference. An order whose
reference already holds an address in the pool fails instead of taking a
second one.
"""
from common.methods import set_progress
from shared_modules import ip_reservations


def generate_options_for_ip_pool_id(field=None, **kwargs):
    profile = kwargs.get("profile")
    pools = ip_reservations.ip_pools(tenant_id=getattr(profile, "tenant_id", None))
    options = [(str(pool.id), ip_reservations.pool_label(pool)) for pool in pools]
    return options or [("", "------ No IP pools available ------")]


def run(job, resource=None, **kwargs):
    pool_id = "{{ ip_pool_id }}".strip()
    hostname = "{{ ip_hostname }}".strip()
    reference = "{{ ip_reference }}".strip()
    try:
        pool = ip_reservations.pool_for_order(pool_id, job.owner)
        set_progress(f"Reserving the next free IP address in '{pool}'")
        rpvs = ip_reservations.reserve(resource, pool, reference=reference, hostname=hostname, job=job)
    except ip_reservations.ReservationError as err:
        return "FAILURE", "", str(err)
    return "SUCCESS", f"Reserved {rpvs.ip} from IP pool '{pool}'.", ""
