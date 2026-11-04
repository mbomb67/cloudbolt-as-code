"""
Remove Hyper-V Cluster Object

Teardown item of the SCVMM Lab on AWS blueprint (blueprints/BP-9zp8udce). It
runs before CloudBolt deletes the lab's servers (deploy_seq -1). Create
Hyper-V Cluster made a computer object for the cluster name (the CNO) in the
hosts' OU, and Windows protects that object from accidental deletion, so the
LDAP Utility is refused when it deletes it.

  1. Turn off DNS registration on every running host. A host registers its
     name again whenever its addresses change, and cluster IP addresses move
     when the cluster is destroyed or a node shuts down; its record would
     then outlive the cleanup Delete Windows Computer Object from AD does
     before CloudBolt deletes it.
  2. While a host is running, destroy the cluster from it with
     Remove-Cluster -CleanupAD, as a scheduled task signed in as the domain
     service account. That account created the CNO, and -CleanupAD deletes
     it with the cluster.
  3. When the CNO is still there, delete it over LDAP with the LDAP Utility
     that serves the lab's domain, the same utility Join Windows Server to AD
     Domain joins with, through the ad_domain shared module
     (shared_modules/SHM-gcba1u31).

The servers' own computer objects are left to Delete Windows Computer Object
from AD (HPA-q181uofy), and the servers and the storage volume are deleted
with the resource.

Idempotent: a lab that never got a cluster, or a CNO that is already gone,
ends with SUCCESS; a CNO that cannot be deleted ends with WARNING so the
rest of the delete goes on.

CloudBolt renders this file as a template before running it, so it must never
contain template tags; values reach the script through the __CB_*__ tokens.

References:
  Set-DnsClient:                        https://learn.microsoft.com/en-us/powershell/module/dnsclient/set-dnsclient
  Remove-Cluster:                       https://learn.microsoft.com/en-us/powershell/module/failoverclusters/remove-cluster
  CNO protected from accidental delete: https://learn.microsoft.com/en-us/troubleshoot/windows-server/high-availability/event-id-1222-create-failover-cluster
"""
from common.methods import set_progress
from utilities.logger import ThreadLogger

from shared_modules import ad_domain, scvmm_lab
from shared_modules.scvmm_lab import LabError

logger = ThreadLogger(__name__)

DNS_OFF = r"""
    Get-DnsClient | Where-Object { $_.RegisterThisConnectionsAddress } | ForEach-Object {
        Set-DnsClient -InterfaceIndex $_.InterfaceIndex -RegisterThisConnectionsAddress $false
    }
"""

REMOVE_CLUSTER = r"""
    Import-Module FailoverClusters
    $name = Decode '__CB_NAME__'
    $cluster = Lookup { Get-Cluster -ErrorAction Stop }
    if (-not $cluster) {
        Mark 'DESTROYED' 'none'
    } elseif ($cluster.Name -ne $name) {
        throw "$env:COMPUTERNAME is in cluster $($cluster.Name), not $name."
    } else {
        Log "Destroying cluster $name and deleting its Active Directory objects"
        # Remove-Cluster's own message is generic; the reason is in the inner exceptions.
        try { Remove-Cluster -Force -CleanupAD }
        catch {
            $reasons = @(); $e = $_.Exception
            while ($e) { $reasons += $e.Message; $e = $e.InnerException }
            throw ($reasons -join ' ')
        }
        Mark 'DESTROYED' $name
    }
"""


def destroy_cluster(resource, name):
    """
    Turn off DNS registration on the running hosts, then run
    Remove-Cluster -CleanupAD on the first of them. Returns a note for the
    job's output.
    """
    try:
        lab = scvmm_lab.Lab(resource)
    except LabError as err:
        return f"Remove-Cluster was not run: {err}"
    running = [host for host in lab.hosts if host.power_status == "POWERON"]
    if not running:
        return "Remove-Cluster was not run: no host is running."
    for host in running:
        try:
            scvmm_lab.run_ps(host, DNS_OFF)
            set_progress(f"{host.hostname}: DNS registration turned off")
        except LabError as err:
            set_progress(f"{host.hostname}: could not turn off DNS registration: {err}")
    try:
        log = scvmm_lab.run_task(running[0], REMOVE_CLUSTER, f"destroy cluster {name}", tokens={"NAME": name},
                                 account=lab.account, timeout=900)
    except LabError as err:
        return f"Remove-Cluster failed: {err}"
    if scvmm_lab.markers(log).get("DESTROYED") == name:
        return f"Destroyed cluster {name} on {running[0].hostname}."
    return f"{running[0].hostname} is in no cluster."


def run(job, resource=None, **kwargs):
    resource = resource or job.resource_set.first()
    cluster = str(resource.get_value_for_custom_field("scvmm_lab_cluster") or "").strip().lower()
    domain = str(resource.get_value_for_custom_field("scvmm_lab_domain") or "").strip().lower()
    if not cluster or not domain:
        return "SUCCESS", "The lab has no cluster recorded; no computer object to delete.", ""
    name = cluster.split(".")[0]
    note = destroy_cluster(resource, name)
    set_progress(note)

    matches = [u for u in ad_domain.utilities() if ad_domain.utility_domain(u) == domain]
    if len(matches) != 1:
        return "WARNING", note, (f"{len(matches)} LDAP Utilities serve {domain}, so the computer object of "
                                 f"cluster {name} was not checked. Delete it in Active Directory if it is there.")
    dn = ""
    try:
        conn = ad_domain.connect(matches[0])
        try:
            found = ad_domain.find_computers(conn, ad_domain.search_base(matches[0], conn), name=name)
            if not found:
                return "SUCCESS", f"{note} Cluster {name} has no computer object left in {domain}.", ""
            if len(found) > 1:
                return "WARNING", note, f"Found several computer objects named {name} in {domain}; none was deleted."
            dn = found[0]["dn"]
            dns_name = (found[0]["dns_host_name"] or "").lower()
            if dns_name and dns_name != cluster:
                return "WARNING", note, (f"Computer object {dn} has DNS name {dns_name}, not {cluster}; "
                                         "it was not deleted.")
            set_progress(f"Deleting {dn}")
            ad_domain.delete_entry(conn, dn)
        finally:
            conn.unbind_s()
    except ad_domain.ldap.INSUFFICIENT_ACCESS:
        return "WARNING", note, (f"The LDAP Utility may not delete {dn}: Windows protects a cluster's computer "
                                 "object from accidental deletion. Clear Protect from accidental deletion on it "
                                 "in Active Directory Administrative Center, then delete it.")
    except Exception as err:
        logger.exception("Deleting the computer object of cluster %s failed", name)
        return "WARNING", note, (f"Could not delete the computer object of cluster {name}: {err}. The LDAP "
                                 "Utility's account needs the right to delete computer objects in the hosts' OU.")
    return "SUCCESS", f"{note} Deleted the computer object of cluster {name} from {domain}.", ""
