"""
Prepare SCVMM Lab on AWS

Build item 3 of the SCVMM Lab on AWS blueprint (blueprints/BP-9zp8udce),
after both server tiers. Each step is skipped when it is already done, so the
item can be re-run.

  1. Check that every lab server is in the domain under its CloudBolt
     hostname (Join Windows Server to AD Domain, HPA-jjp1vlfi, joins them at
     Post-Provision) and record the domain on the resource. Check that each
     server's name resolves to its own address (scvmm_lab.check_dns), so a
     stale DNS record fails the build here rather than at Create Hyper-V
     Cluster.
  2. Per Hyper-V host: check the instance type, stop the instance, turn on
     nested virtualization and start it again (nothing on .metal sizes), and
     assign one secondary private IP to its primary interface for the
     cluster's IP address resource on that host.
  3. SCVMM server: grow its root volume to ROOT_GB GB and extend C: (SQL
     Server, the Windows ADK and SCVMM install there), create a gp3 volume
     of scvmm_lab_storage_gb GB, attach it with delete-on-termination,
     format it as NTFS labeled CBLab, install
     the iSCSI Target Server role and allow TCP 3260 from the hosts. The
     iSCSI disks are created in Configure Hyper-V Hosts, once the hosts'
     initiator names are known. The role is installed here so it never runs
     at the same time as the SQL Server setup in Install SCVMM.

The EC2 calls use the AWS handler's own boto3 client (scvmm_lab.ec2_client).
The appliance's botocore predates nested virtualization, so the shared
module adds that field to the signed request itself.

CloudBolt renders this file as a template before running it, so it must never
contain template tags; values reach the scripts through the __CB_*__ tokens.

References:
  Install-WindowsFeature:  https://learn.microsoft.com/en-us/powershell/module/servermanager/install-windowsfeature
  Resize-Partition:        https://learn.microsoft.com/en-us/powershell/module/storage/resize-partition
  Extend a Windows volume: https://docs.aws.amazon.com/ebs/latest/userguide/recognize-expanded-volume-windows.html
  Initialize-Disk:         https://learn.microsoft.com/en-us/powershell/module/storage/initialize-disk
  New-Partition:           https://learn.microsoft.com/en-us/powershell/module/storage/new-partition
  Format-Volume:           https://learn.microsoft.com/en-us/powershell/module/storage/format-volume
  New-NetFirewallRule:     https://learn.microsoft.com/en-us/powershell/module/netsecurity/new-netfirewallrule
  iSCSI Target Server:     https://learn.microsoft.com/en-us/windows-server/storage/iscsi/iscsi-target-server
  EC2 calls:               see shared_modules/SHM-dpj24dfj
"""
from common.methods import set_progress

from shared_modules import scvmm_lab
from shared_modules.scvmm_lab import LabError

ROOT_GB = 80

DOMAIN_CHECK = r"""
    $cs = Get-CimInstance -ClassName Win32_ComputerSystem
    $name = Decode '__CB_NAME__'
    if (-not $cs.PartOfDomain) {
        throw "$($cs.Name) is not in a domain. Set domain_ou and domain_service_account where the lab is ordered, so Join Windows Server to AD Domain joins it at Post-Provision."
    }
    if ($cs.Name -ne $name) { throw "The computer is named $($cs.Name), not $name." }
    Mark 'DOMAIN' $cs.Domain.ToLower()
"""

STORAGE = r"""
    $label = Decode '__CB_LABEL__'
    $size = [int64](Decode '__CB_SIZE_GB__') * 1GB
    $hosts = @((Decode '__CB_HOST_IPS__') -split ',')
    Update-HostStorageCache
    $max = (Get-PartitionSupportedSize -DriveLetter C).SizeMax
    if ($max -gt (Get-Partition -DriveLetter C).Size + 1GB) {
        Resize-Partition -DriveLetter C -Size $max
        Log "Extended C: to $([math]::Round($max / 1GB)) GB"
    }
    $volume = Get-Volume -FileSystemLabel $label -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $volume) {
        # The lab volume is the only raw disk of its size; it can take a moment to appear after the attach.
        $disk = $null
        for ($i = 0; $i -lt 24 -and -not $disk; $i++) {
            $disk = Get-Disk | Where-Object { $_.PartitionStyle -eq 'RAW' -and $_.Size -eq $size } | Select-Object -First 1
            if (-not $disk) { Start-Sleep -Seconds 5; Update-HostStorageCache }
        }
        if (-not $disk) { throw "No raw $($size / 1GB) GB disk appeared after the volume was attached." }
        if ($disk.IsOffline) { Set-Disk -Number $disk.Number -IsOffline $false }
        if ($disk.IsReadOnly) { Set-Disk -Number $disk.Number -IsReadOnly $false }
        Initialize-Disk -Number $disk.Number -PartitionStyle GPT
        $partition = New-Partition -DiskNumber $disk.Number -UseMaximumSize -AssignDriveLetter
        $volume = Format-Volume -Partition $partition -FileSystem NTFS -NewFileSystemLabel $label -Confirm:$false
        Log "Formatted the lab volume as $($volume.DriveLetter):"
    }
    $root = '{0}:\CBLab' -f $volume.DriveLetter
    foreach ($dir in 'iSCSI', 'Media', 'Temp', 'Library') {
        New-Item -ItemType Directory -Force -Path (Join-Path $root $dir) | Out-Null
    }
    $feature = Get-WindowsFeature -Name FS-iSCSITarget-Server
    if (-not $feature.Installed) {
        $result = Install-WindowsFeature -Name FS-iSCSITarget-Server -IncludeManagementTools
        if (-not $result.Success) { throw "Install-WindowsFeature FS-iSCSITarget-Server failed: $($result.ExitCode)" }
        Log "Installed the iSCSI Target Server role (restart needed: $($result.RestartNeeded))"
    }
    $rule = 'CBLab-iSCSI-Target'
    Remove-NetFirewallRule -Name $rule -ErrorAction SilentlyContinue
    New-NetFirewallRule -Name $rule -DisplayName 'CloudBolt SCVMM lab: iSCSI target' -Direction Inbound -Protocol TCP -LocalPort 3260 -RemoteAddress $hosts -Action Allow | Out-Null
    Mark 'DRIVE' $volume.DriveLetter
"""


def _check_domains(lab):
    domains = {}
    for server in lab.servers:
        found, _ = scvmm_lab.run_ps(server, DOMAIN_CHECK, tokens={"NAME": scvmm_lab.computer_name(server)})
        domains[server.hostname] = found["DOMAIN"]
    if len(set(domains.values())) != 1:
        raise LabError("The lab servers are in different domains: "
                       + ", ".join(f"{name} in {domain}" for name, domain in domains.items()) + ".")
    return next(iter(domains.values()))


def run(job, resource=None, **kwargs):
    resource = resource or job.resource_set.first()
    try:
        scvmm_lab.ensure_fields()
        lab = scvmm_lab.Lab(resource)
        domain = _check_domains(lab)
        resource.set_value_for_custom_field("scvmm_lab_domain", domain)
        set_progress(f"All {len(lab.servers)} lab servers are in {domain}")
        scvmm_lab.check_dns(lab)

        host_ips, cluster_ips = [], []
        for host in lab.hosts:
            client = scvmm_lab.ec2_client(host)
            done = scvmm_lab.enable_nested_virtualization(client, host)
            set_progress(f"{host.hostname}: {done}")
            ip = scvmm_lab.cluster_ip(client, host, assign=True)
            cluster_ips.append(ip)
            host_ips.append(scvmm_lab.describe_instance(client, host)["PrivateIpAddress"])
            set_progress(f"{host.hostname}: cluster IP address {ip}")
        # A stopped and started host needs its guest agent back before the next step.
        for host in lab.hosts:
            scvmm_lab.run_ps(host, "    Mark 'READY' 1")

        client = scvmm_lab.ec2_client(lab.vmm)
        set_progress(f"{lab.vmm.hostname}: {scvmm_lab.ensure_root_size(client, lab.vmm, ROOT_GB)}")
        size_gb = int(resource.get_value_for_custom_field("scvmm_lab_storage_gb") or 250)
        volume_id, done = scvmm_lab.ensure_storage_volume(client, lab.vmm, size_gb, resource.id)
        set_progress(f"{lab.vmm.hostname}: storage volume {volume_id}: {done}")
        log = scvmm_lab.run_task(lab.vmm, STORAGE, "prepare the lab volume and the iSCSI Target Server role", tokens={
            "LABEL": scvmm_lab.STORAGE_LABEL, "SIZE_GB": size_gb, "HOST_IPS": ",".join(host_ips),
        }, timeout=1800)
        drive = scvmm_lab.markers(log).get("DRIVE", "?")

        resource.set_value_for_custom_field("scvmm_lab_cluster", lab.cluster_fqdn)
        resource.set_value_for_custom_field("scvmm_lab_cluster_ips", ", ".join(cluster_ips))
        resource.set_value_for_custom_field("scvmm_lab_vmm_server", lab.fqdn(lab.vmm))
    except LabError as err:
        return "FAILURE", "", str(err)
    return "SUCCESS", (f"Nested virtualization and cluster IP addresses ready on {len(lab.hosts)} hosts; "
                       f"lab storage on {lab.vmm.hostname} {drive}: with the iSCSI Target Server role."), ""
