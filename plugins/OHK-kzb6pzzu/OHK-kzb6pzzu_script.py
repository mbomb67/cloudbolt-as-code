"""
Remove Hyper-V Cluster Object

Teardown item of the SCVMM Lab on AWS blueprint (blueprints/BP-9zp8udce). It
runs before CloudBolt deletes the lab's servers (deploy_seq -1). Create
Hyper-V Cluster made a computer object for the cluster name (the CNO) in the
hosts' OU, and Windows protects that object from accidental deletion, so the
LDAP Utility is refused when it deletes it. The CNO owns the cluster name's
DNS record.

  1. Read the CNO's SID over LDAP with the LDAP Utility that serves the lab's
     domain, the same utility Join Windows Server to AD Domain joins with,
     through the ad_domain shared module (shared_modules/SHM-gcba1u31).
  2. Turn off DNS registration on every running host. A host registers its
     name again whenever its addresses change, and cluster IP addresses move
     when the cluster is destroyed or a node shuts down; its record would
     then outlive the cleanup Delete Windows Computer Object from AD does
     before CloudBolt deletes it.
  3. While a host is running, destroy the cluster from it with
     Remove-Cluster -CleanupAD, as a scheduled task signed in as the domain
     service account. That account created the CNO, and -CleanupAD deletes
     it with the cluster.
  4. When the CNO is still there, delete it over LDAP. When Windows' protection
     refuses that, delete it from a running host as the domain service
     account: it owns the object, so it may remove the protection's Deny
     entries for Everyone and allow itself to delete it.
  5. Delete any DNS record of the cluster name that the CNO owned, the way
     Delete Windows Computer Object from AD (plugins/OHK-bkoau0j8) deletes a
     server's own records. Remove-Cluster removes the record when it
     succeeds; this catches the record when it did not run or failed. A
     record owned by another account is left alone.

The servers' own computer objects are left to Delete Windows Computer Object
from AD (HPA-q181uofy), and the servers and the storage volume are deleted
with the resource.

Idempotent: a lab that never got a cluster, or a CNO that is already gone,
ends with SUCCESS; anything left behind ends with WARNING so the rest of the
delete goes on.

CloudBolt renders this file as a template before running it, so it must never
contain template tags; values reach the scripts through the __CB_*__ tokens.

References:
  Set-DnsClient:                        https://learn.microsoft.com/en-us/powershell/module/dnsclient/set-dnsclient
  Remove-Cluster:                       https://learn.microsoft.com/en-us/powershell/module/failoverclusters/remove-cluster
  CNO protected from accidental delete: https://learn.microsoft.com/en-us/troubleshoot/windows-server/high-availability/event-id-1222-create-failover-cluster
  Protection is Deny entries for Everyone: https://learn.microsoft.com/en-us/previous-versions/windows/it-pro/windows-server-2003/cc739350(v=ws.10)
  DirectoryEntry.ObjectSecurity:        https://learn.microsoft.com/en-us/dotnet/api/system.directoryservices.directoryentry.objectsecurity
  ActiveDirectorySecurity.RemoveAccess: https://learn.microsoft.com/en-us/dotnet/api/system.directoryservices.activedirectorysecurity.removeaccess
  ActiveDirectoryAccessRule:            https://learn.microsoft.com/en-us/dotnet/api/system.directoryservices.activedirectoryaccessrule.-ctor
  DirectoryEntry.DeleteTree:            https://learn.microsoft.com/en-us/dotnet/api/system.directoryservices.directoryentry.deletetree
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

DELETE_PROTECTED = r"""
    Add-Type -AssemblyName System.DirectoryServices
    $dn = Decode '__CB_DN__'
    $entry = New-Object System.DirectoryServices.DirectoryEntry ('LDAP://{0}/{1}' -f (Decode '__CB_DOMAIN__'), $dn)
    $entry.Options.SecurityMasks = [System.DirectoryServices.SecurityMasks]::Dacl
    $security = $entry.ObjectSecurity
    # The protection is Deny entries for Everyone, which outrank any Allow. The owner may change permissions.
    $security.RemoveAccess((New-Object Security.Principal.SecurityIdentifier 'S-1-1-0'), [Security.AccessControl.AccessControlType]::Deny)
    $me = [Security.Principal.WindowsIdentity]::GetCurrent().User
    $rights = [System.DirectoryServices.ActiveDirectoryRights]'Delete, DeleteTree'
    $security.AddAccessRule((New-Object System.DirectoryServices.ActiveDirectoryAccessRule $me, $rights, ([Security.AccessControl.AccessControlType]::Allow)))
    $entry.ObjectSecurity = $security
    $entry.CommitChanges()
    Log "Removed the protection from accidental deletion on $dn"
    $entry.DeleteTree()
    Mark 'DELETED' $dn
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


def lookup_cno(conn, base, name, cluster):
    """The cluster's computer object, or None. Raises LabError when another object could be meant."""
    found = ad_domain.find_computers(conn, base, name=name)
    if not found:
        return None
    if len(found) > 1:
        raise LabError(f"Found several computer objects named {name}; none was deleted.")
    dns_name = (found[0]["dns_host_name"] or "").lower()
    if dns_name and dns_name != cluster:
        raise LabError(f"Computer object {found[0]['dn']} has DNS name {dns_name}, not {cluster}; it was not deleted.")
    return found[0]


def delete_protected(resource, dn, domain):
    """Delete the protected CNO from a running host as the domain service account. Returns the host name."""
    lab = scvmm_lab.Lab(resource)
    running = [host for host in lab.hosts if host.power_status == "POWERON"]
    if not running:
        raise LabError("no host is running to delete it from.")
    log = scvmm_lab.run_task(running[0], DELETE_PROTECTED, f"delete {dn}", tokens={"DN": dn, "DOMAIN": domain},
                             account=lab.account, timeout=600)
    if scvmm_lab.markers(log).get("DELETED") != dn:
        raise LabError(f"{running[0].hostname} did not report {dn} deleted.")
    return running[0].hostname


def delete_cluster_dns(conn, cluster, sid):
    """
    Tombstone the DNS nodes of the cluster name that its CNO owned. Returns
    (note, problem), problem empty when nothing was left behind.
    """
    deleted, kept = [], False
    for dn, entry in ad_domain.find_dns_nodes(conn, cluster):
        if ad_domain.is_tombstoned(entry):
            continue
        if not sid or ad_domain.owner_sid(conn, dn) != sid:
            kept = True
            continue
        deleted += ad_domain.a_addresses(entry)
        ad_domain.tombstone_dns_node(conn, dn, entry)
    note = f"Deleted the DNS record of {cluster} ({', '.join(deleted) or 'no address'})." if deleted else ""
    if kept:
        why = ("another account owns it" if sid else
               "the cluster's computer object was already gone, so who owns the record could not be checked")
        return note, f"Left the DNS record of {cluster} alone: {why}. Delete it in DNS Manager."
    return note or f"{cluster} has no DNS record left.", ""


def run(job, resource=None, **kwargs):
    resource = resource or job.resource_set.first()
    cluster = str(resource.get_value_for_custom_field("scvmm_lab_cluster") or "").strip().lower()
    domain = str(resource.get_value_for_custom_field("scvmm_lab_domain") or "").strip().lower()
    if not cluster or not domain:
        return "SUCCESS", "The lab has no cluster recorded; no computer object to delete.", ""
    name = cluster.split(".")[0]
    matches = [u for u in ad_domain.utilities() if ad_domain.utility_domain(u) == domain]
    utility = matches[0] if len(matches) == 1 else None

    # Remove-Cluster -CleanupAD deletes the CNO, whose SID owns the cluster name's DNS record.
    sid = b""
    if utility:
        try:
            conn = ad_domain.connect(utility)
            try:
                cno = lookup_cno(conn, ad_domain.search_base(utility, conn), name, cluster)
                sid = cno["sid"] if cno else b""
            finally:
                conn.unbind_s()
        except Exception as err:
            logger.warning("Reading the computer object of cluster %s failed: %s", name, err)

    note = destroy_cluster(resource, name)
    set_progress(note)
    if not utility:
        return "WARNING", note, (f"{len(matches)} LDAP Utilities serve {domain}, so the computer object and DNS record "
                                 f"of cluster {name} were not checked. Delete them in Active Directory if they are there.")

    notes, problems = [note], []
    try:
        conn = ad_domain.connect(utility)
        try:
            cno = lookup_cno(conn, ad_domain.search_base(utility, conn), name, cluster)
            if not cno:
                notes.append(f"Cluster {name} has no computer object left in {domain}.")
            else:
                set_progress(f"Deleting {cno['dn']}")
                try:
                    ad_domain.delete_entry(conn, cno["dn"])
                    notes.append(f"Deleted the computer object of cluster {name} from {domain}.")
                except ad_domain.ldap.INSUFFICIENT_ACCESS:
                    try:
                        host = delete_protected(resource, cno["dn"], domain)
                        notes.append(f"Removed the protection from accidental deletion on {cno['dn']} and deleted it "
                                     f"from {host}.")
                    except LabError as err:
                        problems.append(
                            f"The LDAP Utility may not delete {cno['dn']}: Windows protects a cluster's computer object "
                            f"from accidental deletion, and deleting it from a host failed: {err} Clear Protect from "
                            "accidental deletion on it in Active Directory Administrative Center, then delete it.")
            dns_note, dns_problem = delete_cluster_dns(conn, cluster, sid)
            notes += [dns_note] if dns_note else []
            problems += [dns_problem] if dns_problem else []
        finally:
            conn.unbind_s()
    except LabError as err:
        problems.append(str(err))
    except Exception as err:
        logger.exception("Deleting the computer object or DNS record of cluster %s failed", name)
        problems.append(f"Could not delete the computer object or DNS record of cluster {name}: {err}. The LDAP "
                        f"Utility's account needs the right to delete computer objects in the hosts' OU and DNS records "
                        f"in the {domain} zone.")
    if problems:
        return "WARNING", " ".join(notes), " ".join(problems)
    return "SUCCESS", " ".join(notes), ""
