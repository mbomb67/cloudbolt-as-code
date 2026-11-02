"""
Remove Hyper-V Cluster Object

Teardown item of the SCVMM Lab on AWS blueprint (blueprints/BP-9zp8udce).
Create Hyper-V Cluster made a computer object for the cluster name (the CNO)
in the hosts' OU. This deletes it over LDAP with the LDAP Utility that serves
the lab's domain, the same utility Join Windows Server to AD Domain joins with,
through the ad_domain shared module (shared_modules/SHM-gcba1u31). The
servers' own computer objects are left to Delete Windows Computer Object from
AD (HPA-q181uofy), and the servers and the storage volume are deleted with
the resource.

Idempotent: a lab that never got a cluster, or a CNO that is already gone,
ends with SUCCESS; a CNO that cannot be deleted ends with WARNING so the
rest of the delete goes on.

CloudBolt renders this file as a template before running it, so it must never
contain template tags.
"""
from common.methods import set_progress
from utilities.logger import ThreadLogger

from shared_modules import ad_domain

logger = ThreadLogger(__name__)


def run(job, resource=None, **kwargs):
    resource = resource or job.resource_set.first()
    cluster = str(resource.get_value_for_custom_field("scvmm_lab_cluster") or "").strip().lower()
    domain = str(resource.get_value_for_custom_field("scvmm_lab_domain") or "").strip().lower()
    if not cluster or not domain:
        return "SUCCESS", "The lab has no cluster recorded; no computer object to delete.", ""
    name = cluster.split(".")[0]

    matches = [u for u in ad_domain.utilities() if ad_domain.utility_domain(u) == domain]
    if len(matches) != 1:
        return "WARNING", "", (f"{len(matches)} LDAP Utilities serve {domain}, so the computer object of cluster "
                               f"{name} was not deleted. Delete it in Active Directory.")
    try:
        conn = ad_domain.connect(matches[0])
        try:
            found = ad_domain.find_computers(conn, ad_domain.search_base(matches[0], conn), name=name)
            if not found:
                return "SUCCESS", f"Cluster {name} has no computer object in {domain}; nothing to delete.", ""
            if len(found) > 1:
                return "WARNING", "", f"Found several computer objects named {name} in {domain}; none was deleted."
            dns_name = (found[0]["dns_host_name"] or "").lower()
            if dns_name and dns_name != cluster:
                return "WARNING", "", (f"Computer object {found[0]['dn']} has DNS name {dns_name}, not {cluster}; "
                                       "it was not deleted.")
            set_progress(f"Deleting {found[0]['dn']}")
            ad_domain.delete_entry(conn, found[0]["dn"])
        finally:
            conn.unbind_s()
    except Exception as err:
        logger.exception("Deleting the computer object of cluster %s failed", name)
        return "WARNING", "", (f"Could not delete the computer object of cluster {name}: {err}. The LDAP Utility's "
                               "account needs the right to delete computer objects in the hosts' OU.")
    return "SUCCESS", f"Deleted the computer object of cluster {name} from {domain}.", ""
