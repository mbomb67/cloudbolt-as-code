"""
Add Hyper-V Cluster to SCVMM

Build item 7 of the SCVMM Lab on AWS blueprint (blueprints/BP-9zp8udce), after
Install SCVMM and Create Hyper-V Cluster. It runs on the SCVMM server as a
scheduled task signed in as the domain service account (an SCVMM
administrator, because it installed SCVMM):

  1. Create the Run As account RUN_AS for the domain service account, unless
     it exists. SCVMM uses it to install its agent on every node; it must be
     a domain account in each node's local Administrators group.
  2. Add the cluster to the All Hosts group with Add-SCVMHostCluster, unless
     SCVMM already manages it, and report each node's state.

Re-running it after a success changes nothing.

CloudBolt renders this file as a template before running it, so it must never
contain template tags; values reach the scripts through the __CB_*__ tokens.

References:
  Add existing Hyper-V hosts and clusters:  https://learn.microsoft.com/en-us/system-center/vmm/hyper-v-existing?view=sc-vmm-2025
  Ports and protocols:                      https://learn.microsoft.com/en-us/system-center/vmm/plan-ports-protocols?view=sc-vmm-2025
  Get-SCVMMServer:                          https://learn.microsoft.com/en-us/powershell/module/virtualmachinemanager/get-scvmmserver?view=systemcenter-ps-2025
  New-SCRunAsAccount:                       https://learn.microsoft.com/en-us/powershell/module/virtualmachinemanager/new-scrunasaccount?view=systemcenter-ps-2025
  Get-SCVMHostGroup:                        https://learn.microsoft.com/en-us/powershell/module/virtualmachinemanager/get-scvmhostgroup?view=systemcenter-ps-2025
  Add-SCVMHostCluster:                      https://learn.microsoft.com/en-us/powershell/module/virtualmachinemanager/add-scvmhostcluster?view=systemcenter-ps-2025
  Get-SCVMHostCluster:                      https://learn.microsoft.com/en-us/powershell/module/virtualmachinemanager/get-scvmhostcluster?view=systemcenter-ps-2025
"""
from common.methods import set_progress

from shared_modules import scvmm_lab
from shared_modules.scvmm_lab import LabError

RUN_AS = "CloudBolt SCVMM Lab Hosts"

ADD_CLUSTER = r"""
    Import-Module VirtualMachineManager
    $vmm = Get-SCVMMServer -ComputerName $env:COMPUTERNAME
    $clusterName = Decode '__CB_CLUSTER__'
    $runAsName = Decode '__CB_RUN_AS__'
    $runAs = Get-SCRunAsAccount -VMMServer $vmm -Name $runAsName
    if (-not $runAs) {
        $secret = ConvertTo-SecureString -String (Decode '__CB_PASSWORD__') -AsPlainText -Force
        $credential = New-Object System.Management.Automation.PSCredential -ArgumentList (Decode '__CB_USER__'), $secret
        $runAs = New-SCRunAsAccount -VMMServer $vmm -Name $runAsName -Credential $credential -Description 'Domain service account of the CloudBolt SCVMM lab; local administrator on every Hyper-V host.'
        Log "Created Run As account $runAsName"
    }
    $cluster = Get-SCVMHostCluster -VMMServer $vmm -Name $clusterName
    if (-not $cluster) {
        $allHosts = Get-SCVMHostGroup -VMMServer $vmm | Where-Object { $_.Path -eq 'All Hosts' }
        Log "Adding cluster $clusterName to All Hosts"
        Add-SCVMHostCluster -VMMServer $vmm -Name $clusterName -VMHostGroup $allHosts -Credential $runAs | Out-Null
        $cluster = Get-SCVMHostCluster -VMMServer $vmm -Name $clusterName
    }
    if (-not $cluster) { throw "SCVMM does not list cluster $clusterName after Add-SCVMHostCluster." }
    $nodes = @($cluster.Nodes)
    foreach ($node in $nodes) { Log "$($node.Name): $($node.OverallState), $($node.CommunicationState)" }
    Mark 'NODES' $nodes.Count
    Mark 'SHARED_VOLUMES' ((@($cluster.SharedVolumes) | ForEach-Object { $_.Name }) -join ' ')
"""


def run(job, resource=None, **kwargs):
    resource = resource or job.resource_set.first()
    try:
        lab = scvmm_lab.Lab(resource)
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
