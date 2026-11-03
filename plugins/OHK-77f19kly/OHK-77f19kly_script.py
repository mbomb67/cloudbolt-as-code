"""
Create Hyper-V Cluster

Build item 6 of the SCVMM Lab on AWS blueprint (blueprints/BP-9zp8udce). It
runs on the first host as a scheduled task signed in as the domain service
account. That account creates the cluster's computer object (CNO) in the
hosts' OU, so it needs Create Computer objects there, and it must be in each
host's local Administrators group. Each step is skipped when it is already
done, so the item can be re-run.

  0. Check that each lab server's name resolves to its own address
     (scvmm_lab.check_dns): New-Cluster reaches the hosts by name.
  1. Create the cluster hvc-<resource ID> on every host with no storage and
     the first host's secondary private IP as its address, ignoring the NAT
     network. Add any host the cluster is missing.
  2. Give the core network name one IP address resource per host, each
     host's secondary private IP, possible owner that host only, joined by
     an OR dependency. EC2 delivers an address only to the interface it is
     assigned to, so the cluster name comes online with whichever address
     belongs to the node that owns the core group. This is the IP layout and
     possible-owner setting of AWS's guidance for clusters on EC2, which
     describes nodes in separate subnets; the hosts here share one subnet.
  3. Keep the NAT network (every host has the same gateway address on it)
     out of cluster traffic, and raise SameSubnetThreshold to 10 as AWS
     recommends.
  4. Format the two iSCSI disks on this host, add them to the cluster, make
     the large one a cluster shared volume and the 1 GB one the disk witness.

Cluster validation (Test-Cluster) is not run; for a supported cluster run it
from Failover Cluster Manager.

CloudBolt renders this file as a template before running it, so it must never
contain template tags; values reach the scripts through the __CB_*__ tokens.

References:
  New-Cluster:                    https://learn.microsoft.com/en-us/powershell/module/failoverclusters/new-cluster
  Add-ClusterNode:                https://learn.microsoft.com/en-us/powershell/module/failoverclusters/add-clusternode
  Add-ClusterResource:            https://learn.microsoft.com/en-us/powershell/module/failoverclusters/add-clusterresource
  Set-ClusterParameter:           https://learn.microsoft.com/en-us/powershell/module/failoverclusters/set-clusterparameter?view=windowsserver2019-ps
  IP Address private properties:  https://learn.microsoft.com/en-us/previous-versions/windows/desktop/mscs/ip-address-private-properties
  Set-ClusterOwnerNode:           https://learn.microsoft.com/en-us/powershell/module/failoverclusters/set-clusterownernode
  Set-ClusterResourceDependency:  https://learn.microsoft.com/en-us/powershell/module/failoverclusters/set-clusterresourcedependency
  Add-ClusterDisk:                https://learn.microsoft.com/en-us/powershell/module/failoverclusters/add-clusterdisk
  Add-ClusterSharedVolume:        https://learn.microsoft.com/en-us/powershell/module/failoverclusters/add-clustersharedvolume
  Set-ClusterQuorum:              https://learn.microsoft.com/en-us/powershell/module/failoverclusters/set-clusterquorum
  Prestage cluster objects in AD: https://learn.microsoft.com/en-us/windows-server/failover-clustering/prestage-cluster-adds
  SQL Server clustering on EC2:   https://docs.aws.amazon.com/sql-server-ec2/latest/userguide/aws-sql-ec2-clustering.html
"""
from common.methods import set_progress

from shared_modules import scvmm_lab
from shared_modules.scvmm_lab import LabError

NAT_PREFIX = "192.168.250.0/24"

CREATE_CLUSTER = r"""
    Import-Module FailoverClusters
    $name = Decode '__CB_NAME__'
    $nodes = @((Decode '__CB_NODES__') -split ',')
    $ips = @((Decode '__CB_IPS__') -split ',')
    $natPrefix = Decode '__CB_NAT_PREFIX__'
    $natAddress = ($natPrefix -split '/')[0]

    $cluster = Lookup { Get-Cluster -ErrorAction Stop }
    if ($cluster -and $cluster.Name -ne $name) { throw "$env:COMPUTERNAME is already in cluster $($cluster.Name)." }
    if (-not $cluster) {
        Log "Creating cluster $name on $($nodes -join ', ') with address $($ips[0])"
        New-Cluster -Name $name -Node $nodes -StaticAddress $ips[0] -IgnoreNetwork $natPrefix -NoStorage | Out-Null
    }
    $members = @(Get-ClusterNode -Cluster $name | ForEach-Object { $_.Name.ToLower() })
    foreach ($node in $nodes) {
        if ($members -notcontains $node.ToLower()) {
            Log "Adding node $node"
            Add-ClusterNode -Cluster $name -Name $node -NoStorage | Out-Null
        }
    }

    $nat = Get-ClusterNetwork -Cluster $name | Where-Object { $_.Address -eq $natAddress }
    foreach ($n in $nat) { if ($n.Role -ne 0) { $n.Role = 0; Log "Cluster network $($n.Name) ($natPrefix) set to no cluster traffic" } }
    $network = Get-ClusterNetwork -Cluster $name | Where-Object { $_.Address -ne $natAddress -and $_.Role -ne 0 } | Select-Object -First 1
    if (-not $network) { throw 'The cluster has no network for its IP addresses.' }

    $core = Get-ClusterResource -Cluster $name | Where-Object { $_.OwnerGroup -eq 'Cluster Group' }
    $nameResource = $core | Where-Object { $_.ResourceType -eq 'Network Name' } | Select-Object -First 1
    $ipResources = @()
    for ($i = 0; $i -lt $nodes.Count; $i++) {
        $resource = $core | Where-Object { $_.ResourceType -eq 'IP Address' } |
            Where-Object { (Get-ClusterParameter -InputObject $_ -Name Address).Value -eq $ips[$i] } | Select-Object -First 1
        if (-not $resource) {
            $resource = Add-ClusterResource -Cluster $name -Name "Cluster IP Address $($ips[$i])" -ResourceType 'IP Address' -Group 'Cluster Group'
            $resource | Set-ClusterParameter -Multiple @{ Address = $ips[$i]; SubnetMask = $network.AddressMask; Network = $network.Name; EnableDhcp = 0 }
            Log "Added IP address resource $($ips[$i]) for $($nodes[$i])"
        }
        Set-ClusterOwnerNode -Cluster $name -Resource $resource.Name -Owners $nodes[$i] | Out-Null
        $ipResources += $resource.Name
    }
    $expression = ($ipResources | ForEach-Object { "[$_]" }) -join ' or '
    Set-ClusterResourceDependency -Cluster $name -Resource $nameResource.Name -Dependency $expression | Out-Null
    Start-ClusterGroup -Cluster $name -Name 'Cluster Group' | Out-Null
    $settings = Get-Cluster -Name $name
    if ($settings.SameSubnetThreshold -lt 10) { $settings.SameSubnetThreshold = 10 }

    $witnessName = 'CBLab Witness'
    $csvName = 'CBLab CSV'
    $diskResources = @(Get-ClusterResource -Cluster $name | Where-Object { $_.ResourceType -eq 'Physical Disk' } | ForEach-Object { $_.Name })
    # The smaller iSCSI disk (1 GB) is the witness, the larger the cluster shared volume.
    $iscsi = @(Get-Disk | Where-Object { $_.BusType -eq 'iSCSI' } | Sort-Object Size)
    $plan = @(
        @{ Resource = $witnessName; Label = 'CBLabWitness'; Disk = $iscsi[0] },
        @{ Resource = $csvName; Label = 'CBLabCSV'; Disk = $iscsi[-1] }
    )
    foreach ($item in $plan) {
        if ($diskResources -contains $item.Resource) { continue }
        if ($iscsi.Count -lt 2) { throw "Only $($iscsi.Count) of the 2 iSCSI disks are visible on $env:COMPUTERNAME." }
        $disk = $item.Disk
        if ($disk.PartitionStyle -eq 'RAW') {
            if ($disk.IsOffline) { Set-Disk -Number $disk.Number -IsOffline $false }
            if ($disk.IsReadOnly) { Set-Disk -Number $disk.Number -IsReadOnly $false }
            Initialize-Disk -Number $disk.Number -PartitionStyle GPT
            New-Partition -DiskNumber $disk.Number -UseMaximumSize | Format-Volume -FileSystem NTFS -NewFileSystemLabel $item.Label -Confirm:$false | Out-Null
            Log "Formatted iSCSI disk $($disk.Number) ($([math]::Round($disk.Size / 1GB)) GB) as $($item.Label)"
        }
        $available = Get-ClusterAvailableDisk -Cluster $name -Disk (Get-Disk -Number $disk.Number)
        if (-not $available) { throw "iSCSI disk $($disk.Number) cannot be added to the cluster; check every host sees it." }
        $added = $available | Add-ClusterDisk
        $added.Name = $item.Resource
        Log "Added cluster disk $($item.Resource)"
    }
    if ((Get-ClusterQuorum -Cluster $name).QuorumResource.Name -ne $witnessName) {
        Set-ClusterQuorum -Cluster $name -DiskWitness $witnessName | Out-Null
    }
    $csv = Lookup { Get-ClusterSharedVolume -Cluster $name -Name $csvName -ErrorAction Stop }
    if (-not $csv) { $csv = Add-ClusterSharedVolume -Cluster $name -Name $csvName }
    Mark 'CSV_PATH' $csv.SharedVolumeInfo.FriendlyVolumeName
    Mark 'OWNER' (Get-ClusterGroup -Cluster $name -Name 'Cluster Group').OwnerNode.Name
"""


def run(job, resource=None, **kwargs):
    resource = resource or job.resource_set.first()
    try:
        lab = scvmm_lab.Lab(resource)
        nodes, ips = [], []
        for host in lab.hosts:
            ip = scvmm_lab.cluster_ip(scvmm_lab.ec2_client(host), host)
            if not ip:
                raise LabError(f"{host.hostname} has no secondary private IP; run Prepare SCVMM Lab on AWS again.")
            nodes.append(scvmm_lab.computer_name(host))
            ips.append(ip)
        scvmm_lab.check_dns(lab)
        log = scvmm_lab.run_task(lab.hosts[0], CREATE_CLUSTER, f"create cluster {lab.cluster_name}", tokens={
            "NAME": lab.cluster_name, "NODES": ",".join(nodes), "IPS": ",".join(ips), "NAT_PREFIX": NAT_PREFIX,
        }, account=lab.account, timeout=2700)
        found = scvmm_lab.markers(log)
        set_progress(f"Cluster {lab.cluster_fqdn}: cluster shared volume {found.get('CSV_PATH')}, "
                     f"core group on {found.get('OWNER')}")
    except LabError as err:
        return "FAILURE", "", str(err)
    return "SUCCESS", (f"Cluster {lab.cluster_fqdn} has {len(nodes)} nodes, IP addresses {', '.join(ips)}, "
                       f"cluster shared volume {found.get('CSV_PATH')} and a disk witness."), ""
