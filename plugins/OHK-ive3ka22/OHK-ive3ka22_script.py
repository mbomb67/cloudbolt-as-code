"""
Add Hyper-V Cluster to SCVMM

Build item 7 of the SCVMM Lab on AWS blueprint (blueprints/BP-9zp8udce), after
Install SCVMM and Create Hyper-V Cluster. It runs on the SCVMM server as a
scheduled task signed in as the domain service account (an SCVMM
administrator, because it installed SCVMM):

  0. Wait up to CLUSTER_DNS_WAIT seconds for the cluster's name to resolve,
     from the SCVMM server, to one of the cluster's addresses. The cluster
     registers its name with one domain controller; DNS servers that read
     the zone from other domain controllers answer for it only after
     directory replication, and a resolver that cached "no such name" keeps
     answering that until the cached answer expires.
  1. Create the Run As account RUN_AS for the domain service account, in
     DOMAIN\\user form, or correct it when it exists with another user name.
     SCVMM uses it to install its agent on every node; it must be a domain
     account in each node's local Administrators group. Given a user@domain
     name, SCVMM signs in to the nodes with it as a local account.
  2. Add the cluster to the All Hosts group with Add-SCVMHostCluster, unless
     SCVMM already manages it, and report each node's state.

Re-running it after a success changes nothing.

CloudBolt renders this file as a template before running it, so it must never
contain template tags; values reach the scripts through the __CB_*__ tokens.

References:
  Resolve-DnsName:                          https://learn.microsoft.com/en-us/powershell/module/dnsclient/resolve-dnsname
  Add existing Hyper-V hosts and clusters:  https://learn.microsoft.com/en-us/system-center/vmm/hyper-v-existing?view=sc-vmm-2025
  Ports and protocols:                      https://learn.microsoft.com/en-us/system-center/vmm/plan-ports-protocols?view=sc-vmm-2025
  Get-SCVMMServer:                          https://learn.microsoft.com/en-us/powershell/module/virtualmachinemanager/get-scvmmserver?view=systemcenter-ps-2025
  New-SCRunAsAccount:                       https://learn.microsoft.com/en-us/powershell/module/virtualmachinemanager/new-scrunasaccount?view=systemcenter-ps-2025
  Set-SCRunAsAccount:                       https://learn.microsoft.com/en-us/powershell/module/virtualmachinemanager/set-scrunasaccount?view=systemcenter-ps-2025
  Get-SCVMHostGroup:                        https://learn.microsoft.com/en-us/powershell/module/virtualmachinemanager/get-scvmhostgroup?view=systemcenter-ps-2025
  Add-SCVMHostCluster:                      https://learn.microsoft.com/en-us/powershell/module/virtualmachinemanager/add-scvmhostcluster?view=systemcenter-ps-2025
  Get-SCVMHostCluster:                      https://learn.microsoft.com/en-us/powershell/module/virtualmachinemanager/get-scvmhostcluster?view=systemcenter-ps-2025
"""
import time

from common.methods import set_progress

from shared_modules import scvmm_lab
from shared_modules.scvmm_lab import LabError

RUN_AS = "CloudBolt SCVMM Lab Hosts"
CLUSTER_DNS_WAIT = 3600

ADD_CLUSTER = r"""
    Import-Module VirtualMachineManager
    $vmm = Get-SCVMMServer -ComputerName $env:COMPUTERNAME
    $clusterName = Decode '__CB_CLUSTER__'
    $shortName = ($clusterName -split '\.')[0]
    $runAsName = Decode '__CB_RUN_AS__'
    # SCVMM signs in with a user@domain name as a local account of that name (event 4625, domain localhost),
    # so the Run As account gets DOMAIN\user, resolved through the account's SID.
    $sid = (New-Object Security.Principal.NTAccount (Decode '__CB_USER__')).Translate([Security.Principal.SecurityIdentifier])
    $user = $sid.Translate([Security.Principal.NTAccount]).Value
    $secret = ConvertTo-SecureString -String (Decode '__CB_PASSWORD__') -AsPlainText -Force
    $credential = New-Object System.Management.Automation.PSCredential -ArgumentList $user, $secret
    $runAs = Get-SCRunAsAccount -VMMServer $vmm -Name $runAsName
    if (-not $runAs) {
        $runAs = New-SCRunAsAccount -VMMServer $vmm -Name $runAsName -Credential $credential -Description 'Domain service account of the CloudBolt SCVMM lab; local administrator on every Hyper-V host.'
        Log "Created Run As account $runAsName for $user"
    } elseif ("$($runAs.Domain)\$($runAs.UserName)" -ne $user) {
        $runAs = Set-SCRunAsAccount -RunAsAccount $runAs -Credential $credential
        Log "Run As account $runAsName now signs in as $user"
    }
    # Get-SCVMHostCluster -Name throws for a cluster SCVMM does not manage, so list them and match the name.
    $cluster = Get-SCVMHostCluster -VMMServer $vmm | Where-Object { $_.Name -in $clusterName, $shortName } | Select-Object -First 1
    if (-not $cluster) {
        $allHosts = Get-SCVMHostGroup -VMMServer $vmm | Where-Object { $_.Path -eq 'All Hosts' }
        Log "Adding cluster $clusterName to All Hosts"
        Add-SCVMHostCluster -VMMServer $vmm -Name $clusterName -VMHostGroup $allHosts -Credential $runAs | Out-Null
        $cluster = Get-SCVMHostCluster -VMMServer $vmm | Where-Object { $_.Name -in $clusterName, $shortName } | Select-Object -First 1
    }
    if (-not $cluster) { throw "SCVMM does not list cluster $clusterName after Add-SCVMHostCluster." }
    $nodes = @($cluster.Nodes)
    foreach ($node in $nodes) { Log "$($node.Name): $($node.OverallState), $($node.CommunicationState)" }
    Mark 'NODES' $nodes.Count
    Mark 'SHARED_VOLUMES' ((@($cluster.SharedVolumes) | ForEach-Object { $_.Name }) -join ' ')
"""


def wait_for_cluster_name(lab):
    """Wait until the cluster's name resolves from the SCVMM server to one of its addresses."""
    addresses = {scvmm_lab.cluster_ip(scvmm_lab.ec2_client(host), host) for host in lab.hosts} - {None}
    started = time.time()
    deadline = started + CLUSTER_DNS_WAIT
    next_report = started
    while True:
        found, _ = scvmm_lab.run_ps(lab.vmm, scvmm_lab.DNS_RESOLVE, tokens={"NAMES": lab.cluster_fqdn})
        answer = set(found.get("DNS_0", "").split())
        if answer & addresses:
            set_progress(f"DNS: {lab.cluster_fqdn} is {', '.join(sorted(answer))}")
            return
        if time.time() + scvmm_lab.POLL_INTERVAL > deadline:
            break
        if time.time() >= next_report:
            set_progress(f"DNS: waiting for {lab.cluster_fqdn} to resolve from {lab.vmm.hostname} "
                         f"({int(time.time() - started) // 60} of {CLUSTER_DNS_WAIT // 60} minutes)")
            next_report += 600
        time.sleep(scvmm_lab.POLL_INTERVAL)
    raise LabError(
        f"{lab.cluster_fqdn} still resolves to {', '.join(sorted(answer)) or 'nothing'} from {lab.vmm.hostname} "
        f"after {CLUSTER_DNS_WAIT // 60} minutes, not to the cluster's {', '.join(sorted(addresses))}. The cluster "
        "registers its name with one domain controller; the other DNS servers answer for it after directory "
        "replication. Run this build item again once the name resolves.")


def run(job, resource=None, **kwargs):
    resource = resource or job.resource_set.first()
    try:
        lab = scvmm_lab.Lab(resource)
        wait_for_cluster_name(lab)
        log = scvmm_lab.run_task(lab.vmm, ADD_CLUSTER, f"add cluster {lab.cluster_fqdn} to SCVMM", tokens={
            "CLUSTER": lab.cluster_fqdn, "RUN_AS": RUN_AS, "USER": lab.username, "PASSWORD": lab.password,
        }, account=lab.account, hidden=[lab.password], timeout=2700)
        found = scvmm_lab.markers(log)
        nodes = int(found.get("NODES") or 0)
        if nodes < len(lab.hosts):
            raise LabError(f"SCVMM lists {nodes} of the {len(lab.hosts)} nodes of {lab.cluster_fqdn}.")
        set_progress(f"SCVMM on {lab.fqdn(lab.vmm)} manages {lab.cluster_fqdn} ({nodes} nodes); "
                     f"shared volumes: {found.get('SHARED_VOLUMES') or 'none reported'}")
    except LabError as err:
        return "FAILURE", "", str(err)
    return "SUCCESS", (f"SCVMM on {lab.fqdn(lab.vmm)} manages cluster {lab.cluster_fqdn} with {nodes} Hyper-V "
                       f"hosts, using Run As account '{RUN_AS}'."), ""
