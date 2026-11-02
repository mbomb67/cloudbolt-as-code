"""
Configure Hyper-V Hosts

Build item 5 of the SCVMM Lab on AWS blueprint (blueprints/BP-9zp8udce). It
runs while Install SCVMM (item 4, in parallel) sets up the SCVMM server.
Every step is skipped when it is already done, so the item can be re-run.

  1. On every host at once, as a SYSTEM scheduled task: install the Hyper-V
     and Failover Clustering features with their management tools, and start
     the Microsoft iSCSI Initiator service.
  2. Restart the hosts together when a feature asked for it, and wait for
     them.
  3. Per host: check the Hyper-V management service runs, open the firewall
     rule groups SCVMM uses to install its agent (File and Printer Sharing,
     WMI, Remote Service Management), take the cluster IP address off the
     network adapter if anything bound it there (the cluster brings it
     online itself; AWS's clustering guidance keeps the NIC on DHCP), create
     an internal switch with a NAT network for nested VMs (the same prefix
     on every host, so a VM keeps its address when it moves), and read the
     host's iSCSI initiator name.
  4. SCVMM server: create the iSCSI target for exactly those initiator
     names, with two dynamically expanding virtual disks on the lab volume:
     a 1 GB disk witness and the cluster shared volume, which takes the rest
     of the volume minus RESERVE_GB for setup downloads and the VMM library.
  5. Per host: add the SCVMM server as a target portal, connect the target
     persistently and check both disks are visible. They stay offline until
     Create Hyper-V Cluster formats them on the first host.

Nested VMs reach the network only through their host's NAT: EC2 network
interfaces drop frames from MAC addresses they do not know.

CloudBolt renders this file as a template before running it, so it must never
contain template tags; values reach the scripts through the __CB_*__ tokens.

References:
  Install-WindowsFeature:              https://learn.microsoft.com/en-us/powershell/module/servermanager/install-windowsfeature
  Hyper-V on Windows Server:           https://learn.microsoft.com/en-us/windows-server/virtualization/hyper-v/get-started/install-hyper-v
  New-VMSwitch:                        https://learn.microsoft.com/en-us/powershell/module/hyper-v/new-vmswitch
  New-NetNat:                          https://learn.microsoft.com/en-us/powershell/module/netnat/new-netnat
  New-IscsiServerTarget:               https://learn.microsoft.com/en-us/powershell/module/iscsitarget/new-iscsiservertarget
  New-IscsiVirtualDisk:                https://learn.microsoft.com/en-us/powershell/module/iscsitarget/new-iscsivirtualdisk
  Add-IscsiVirtualDiskTargetMapping:   https://learn.microsoft.com/en-us/powershell/module/iscsitarget/add-iscsivirtualdisktargetmapping
  New-IscsiTargetPortal:               https://learn.microsoft.com/en-us/powershell/module/iscsi/new-iscsitargetportal
  Connect-IscsiTarget:                 https://learn.microsoft.com/en-us/powershell/module/iscsi/connect-iscsitarget
  SQL Server clustering on EC2 (IPs):  https://docs.aws.amazon.com/sql-server-ec2/latest/userguide/aws-sql-ec2-clustering.html
"""
from common.methods import set_progress

from shared_modules import scvmm_lab
from shared_modules.scvmm_lab import LabError

SWITCH = "CBLab-NAT"
NAT_PREFIX = "192.168.250.0/24"
NAT_GATEWAY = "192.168.250.1"
TARGET = "cblab-cluster"
RESERVE_GB = 40

FEATURES = r"""
    $result = Install-WindowsFeature -Name Hyper-V, Failover-Clustering -IncludeManagementTools
    if (-not $result.Success) { throw "Install-WindowsFeature Hyper-V, Failover-Clustering failed (exit code $($result.ExitCode))." }
    $running = (Get-Service -Name vmms -ErrorAction SilentlyContinue).Status -eq 'Running'
    Log "Features: $(@($result.FeatureResult | ForEach-Object { $_.Name }) -join ', '); restart needed: $($result.RestartNeeded)"
    Mark 'RESTART' $(if ($running) { 0 } else { 1 })
    Set-Service -Name MSiSCSI -StartupType Automatic
    Start-Service -Name MSiSCSI
"""

HOST_SETUP = r"""
    if ((Get-Service -Name vmms -ErrorAction SilentlyContinue).Status -ne 'Running') {
        throw 'The Hyper-V Virtual Machine Management service (vmms) is not running after the restart.'
    }
    foreach ($group in 'File and Printer Sharing', 'Windows Management Instrumentation (WMI)', 'Remote Service Management') {
        Enable-NetFirewallRule -DisplayGroup $group
    }
    $clusterIp = Decode '__CB_CLUSTER_IP__'
    if ((Get-Service -Name ClusSvc -ErrorAction SilentlyContinue).Status -ne 'Running') {
        Get-NetIPAddress -IPAddress $clusterIp -ErrorAction SilentlyContinue | Remove-NetIPAddress -Confirm:$false
    }
    $switch = Decode '__CB_SWITCH__'
    $gateway = Decode '__CB_GATEWAY__'
    $prefix = Decode '__CB_PREFIX__'
    if (-not (Get-VMSwitch -Name $switch -ErrorAction SilentlyContinue)) {
        New-VMSwitch -Name $switch -SwitchType Internal | Out-Null
        Log "Created internal switch $switch"
    }
    $alias = "vEthernet ($switch)"
    if (-not (Get-NetIPAddress -InterfaceAlias $alias -IPAddress $gateway -ErrorAction SilentlyContinue)) {
        New-NetIPAddress -InterfaceAlias $alias -IPAddress $gateway -PrefixLength ([int]($prefix -split '/')[1]) | Out-Null
    }
    if (-not (Get-NetNat -Name $switch -ErrorAction SilentlyContinue)) {
        New-NetNat -Name $switch -InternalIPInterfaceAddressPrefix $prefix | Out-Null
        Log "Created NAT $switch for $prefix"
    }
    Set-Service -Name MSiSCSI -StartupType Automatic
    Start-Service -Name MSiSCSI
    $iqn = (Get-InitiatorPort | Where-Object { $_.ConnectionType -eq 'iSCSI' } | Select-Object -First 1).NodeAddress
    if (-not $iqn) { throw 'The Microsoft iSCSI Initiator has no initiator name.' }
    Mark 'IQN' $iqn
"""

TARGET_SETUP = r"""
    $target = Decode '__CB_TARGET__'
    $initiators = @((Decode '__CB_IQNS__') -split ',' | ForEach-Object { "IQN:$_" })
    $reserve = [int64](Decode '__CB_RESERVE_GB__') * 1GB
    $volume = Get-Volume -FileSystemLabel (Decode '__CB_LABEL__') -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $volume) { throw 'The lab volume is missing; run Prepare SCVMM Lab on AWS again.' }
    $dir = '{0}:\CBLab\iSCSI' -f $volume.DriveLetter
    if (Lookup { Get-IscsiServerTarget -TargetName $target -ErrorAction Stop }) {
        Set-IscsiServerTarget -TargetName $target -InitiatorIds $initiators
    } else {
        New-IscsiServerTarget -TargetName $target -InitiatorIds $initiators | Out-Null
        Log "Created iSCSI target $target"
    }
    $csvBytes = [math]::Floor(($volume.Size - $reserve) / 1GB) * 1GB
    if ($csvBytes -lt 20GB) { throw "The lab volume is too small for a cluster shared volume; make scvmm_lab_storage_gb at least $([math]::Ceiling(($reserve + 20GB) / 1GB))." }
    $disks = [ordered]@{ 'witness.vhdx' = 1GB; 'csv01.vhdx' = $csvBytes }
    foreach ($file in $disks.Keys) {
        $path = Join-Path $dir $file
        if (-not (Lookup { Get-IscsiVirtualDisk -Path $path -ErrorAction Stop })) {
            New-IscsiVirtualDisk -Path $path -SizeBytes $disks[$file] | Out-Null
            Log "Created iSCSI virtual disk $path ($([math]::Round($disks[$file] / 1GB)) GB, dynamically expanding)"
        }
        $mapped = @((Get-IscsiServerTarget -TargetName $target).LunMappings | ForEach-Object { $_.Path })
        if ($mapped -notcontains $path) { Add-IscsiVirtualDiskTargetMapping -TargetName $target -Path $path }
    }
    Mark 'TARGET_IQN' (Get-IscsiServerTarget -TargetName $target).TargetIqn
"""

HOST_CONNECT = r"""
    $portal = Decode '__CB_PORTAL__'
    $targetIqn = Decode '__CB_TARGET_IQN__'
    $targetName = Decode '__CB_TARGET__'
    if (-not (Lookup { Get-IscsiTargetPortal -TargetPortalAddress $portal -ErrorAction Stop })) {
        New-IscsiTargetPortal -TargetPortalAddress $portal | Out-Null
    } else {
        Update-IscsiTargetPortal -TargetPortalAddress $portal | Out-Null
    }
    # Microsoft target names end in -<target name>-target; match on that if the server did not report the IQN.
    $target = Get-IscsiTarget | Where-Object {
        $_.NodeAddress -eq $targetIqn -or (-not $targetIqn -and $_.NodeAddress -like "*-$targetName-target")
    } | Select-Object -First 1
    if (-not $target) { throw "$portal does not offer target $targetName to this host; check TCP 3260 between the servers and the target's initiator list." }
    if (-not $target.IsConnected) {
        Connect-IscsiTarget -NodeAddress $target.NodeAddress -TargetPortalAddress $portal -IsPersistent $true | Out-Null
        Log "Connected $($target.NodeAddress)"
    }
    $count = 0
    for ($i = 0; $i -lt 12 -and $count -lt 2; $i++) {
        Update-HostStorageCache
        $count = @(Get-Disk | Where-Object { $_.BusType -eq 'iSCSI' }).Count
        if ($count -lt 2) { Start-Sleep -Seconds 5 }
    }
    if ($count -lt 2) { throw "Only $count of the 2 iSCSI disks are visible." }
    Mark 'DISKS' $count
"""


def run(job, resource=None, **kwargs):
    resource = resource or job.resource_set.first()
    try:
        lab = scvmm_lab.Lab(resource)
        tasks = [scvmm_lab.start_task(host, FEATURES, "install Hyper-V and Failover Clustering")
                 for host in lab.hosts]
        logs = scvmm_lab.wait_tasks(tasks, timeout=2700)
        restart = [host for host, log in zip(lab.hosts, logs) if scvmm_lab.markers(log).get("RESTART") == "1"]
        if restart:
            scvmm_lab.restart(restart, "finish installing Hyper-V")

        iqns, portal = [], ""
        for host in lab.hosts:
            client = scvmm_lab.ec2_client(host)
            found, _ = scvmm_lab.run_ps(host, HOST_SETUP, tokens={
                "CLUSTER_IP": scvmm_lab.cluster_ip(client, host), "SWITCH": SWITCH,
                "GATEWAY": NAT_GATEWAY, "PREFIX": NAT_PREFIX,
            })
            iqns.append(found["IQN"])
            set_progress(f"{host.hostname}: Hyper-V ready, NAT switch {SWITCH}, initiator {found['IQN']}")

        found, _ = scvmm_lab.run_ps(lab.vmm, TARGET_SETUP, tokens={
            "TARGET": TARGET, "IQNS": ",".join(iqns), "RESERVE_GB": RESERVE_GB, "LABEL": scvmm_lab.STORAGE_LABEL,
        }, timeout=600)
        target_iqn = found.get("TARGET_IQN", "")
        portal = scvmm_lab.describe_instance(scvmm_lab.ec2_client(lab.vmm), lab.vmm)["PrivateIpAddress"]
        set_progress(f"{lab.vmm.hostname}: iSCSI target {target_iqn or TARGET} on {portal}")

        for host in lab.hosts:
            scvmm_lab.run_ps(host, HOST_CONNECT, tokens={"PORTAL": portal, "TARGET_IQN": target_iqn, "TARGET": TARGET})
            set_progress(f"{host.hostname}: connected to {target_iqn or TARGET}")
    except LabError as err:
        return "FAILURE", "", str(err)
    return "SUCCESS", (f"{len(lab.hosts)} Hyper-V hosts ready with NAT switch {SWITCH} ({NAT_PREFIX}) and "
                       f"the lab's iSCSI disks from {lab.vmm.hostname}."), ""
