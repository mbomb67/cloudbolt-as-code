# SCVMM Lab on AWS

Builds an SCVMM 2025 test lab on AWS:
- Two or more Windows Server 2025 Hyper-V hosts on nested-virtualization EC2 instances, in a failover cluster.
- Shared storage over iSCSI from a gp3 volume.
- One SCVMM server that manages the cluster.

Every server joins an existing Active Directory domain. The build runs unattended and takes about an hour, mostly SQL Server and SCVMM setup.

## Contents
| Role | ID | Name |
|---|---|---|
| Server tier (seq 1, parallel) | — | Hyper-V Hosts (OS build "Windows Server 2025"; hostname `hvlab-00X`; all environments enabled; order 2 or more) |
| Server tier (seq 2) | — | SCVMM Server (OS build "Windows Server 2025"; hostname `vmmlab-00X`; all environments enabled; order 1) |
| Build (Plug-in, seq 3) | OHK-q6fb9833 | Prepare SCVMM Lab on AWS |
| Build (Plug-in, seq 4, parallel) | OHK-8f9o1496 | Install SCVMM |
| Build (Plug-in, seq 5) | OHK-8ioy2cbi | Configure Hyper-V Hosts |
| Build (Plug-in, seq 6) | OHK-77f19kly | Create Hyper-V Cluster |
| Build (Plug-in, seq 7) | OHK-ive3ka22 | Add Hyper-V Cluster to SCVMM |
| Teardown (before the servers are deleted) | OHK-kzb6pzzu | Remove Hyper-V Cluster Object: destroys the cluster with `Remove-Cluster -CleanupAD` on a running host, which deletes its computer object |
| Shared module | SHM-dpj24dfj | `scvmm_lab`: guest scripts, scheduled-task steps, EC2 calls |

Blueprint parameters, each pinned as its only option, so the order form hides them:
- `scvmm_media`: where `SCVMM_2025.zip` comes from. The default is the Evaluation Center's link, `https://go.microsoft.com/fwlink/?linkid=2292412`, which downloads without registration. For a lab without internet access, set it to a File Archives reference instead, such as `scvmm/SCVMM_2025.zip`.
- `scvmm_lab_storage_gb`: size of the lab volume (default 250).

## What gets built
| Piece | How |
|---|---|
| Nested virtualization | Each host is stopped, gets `NestedVirtualization=enabled`, and is started again. Not needed on `.metal` sizes. The appliance's botocore predates the field, so the shared module adds it to the signed request itself. |
| Cluster addresses | Each host gets one secondary private IP. The cluster has one IP address resource per host, each owned only by its host, joined by an OR dependency. The cluster name is `hvc-<resource ID>`. |
| Shared storage | The SCVMM server gets a gp3 volume (labeled `CBLab`, deleted with the server) and runs the iSCSI Target Server role. The volume holds a 1 GB disk witness and a cluster shared volume of the volume size minus 40 GB, both dynamically expanding. |
| SCVMM server | Root volume grown to 80 GB. SQL Server 2022 Developer (default instance), Windows ADK Deployment Tools + WinPE, and SCVMM 2025 evaluation. The VMM service runs as Local System; the library share is on the lab volume. |
| Hosts | Hyper-V and Failover Clustering are installed. An internal switch `CBLab-NAT` with NAT `192.168.250.0/24` (gateway `.1`) is created on every host; its address is not registered in DNS. |
| SCVMM | Run As account `CloudBolt SCVMM Lab Hosts` for the domain service account; the cluster is added under All Hosts. |

Resource attributes: `scvmm_lab_domain`, `scvmm_lab_cluster`, `scvmm_lab_cluster_ips`, `scvmm_lab_vmm_server`.

## Prerequisites
- **Domain join content.** Install [PKG-nf407lm5](../../packages/PKG-nf407lm5/) (Join Windows Server to AD Domain), with `domain_ou` and `domain_service_account` set on the AWS environment. Every lab server must get the same `domain_service_account`. That account:
  - is a member of the local Administrators group on the lab servers, for example through a Group Policy linked to their OU;
  - has **Create Computer objects** on that OU, for the cluster's computer object.
- **Teardown rights.** The LDAP Utility's account must be able to delete computer objects in that OU. Windows protects the cluster's computer object from accidental deletion, so the teardown deletes it from a host, as the domain service account that created it; the LDAP Utility deletes it only when no host is running and the object is not protected.
- **Runbooks package.** [PKG-v20qv308](../../packages/PKG-v20qv308/) provides the `file_archives` shared module that Install SCVMM imports. File Archives itself is needed only when `scvmm_media` names a File Archives file.
- **AWS environment.** Windows Server 2025 instances CloudBolt can run scripts on, through SSM (with an instance profile allowing `AmazonSSMManagedInstanceCore`) or through WinRM.
  - **Security group:** allow all traffic between its own members (a rule whose source is the group itself).
  - **Internet access** from the SCVMM server, to download SQL Server, the ADK and the SCVMM media from Microsoft.
- **IAM.** The AWS handler's credentials also need:
  - `ec2:ModifyInstanceCpuOptions`, `ec2:StopInstances`, `ec2:StartInstances`, `ec2:DescribeInstanceTypes`
  - `ec2:AssignPrivateIpAddresses`
  - `ec2:CreateVolume`, `ec2:AttachVolume`, `ec2:CreateTags`, `ec2:ModifyInstanceAttribute`
  - `ec2:ModifyVolume`, `ec2:DescribeVolumesModifications`

## Setup
1. On the AWS environment's **Parameters** tab:
   - Add instance types that support nested virtualization to **Instance type**, for example `m7i.xlarge`, `m8i.xlarge` or `r7i.xlarge`. Bare metal sizes also work. SCVMM's documented minimum is 8 cores and 16 GB of RAM; `m7i.2xlarge` meets it, and `m7i.xlarge` saves cost in a lab.
   - Add **Quantity** with minimum 1 and maximum 8. Without it the order form has no host count and an order with more than one host is refused. Adding it to the ordering group instead works too.
2. Sync the blueprint, then open it in **Catalog**:
   - Both tiers have all environments enabled. The tier's OS build limits them to environments that offer it; re-point both tiers to your Windows Server 2025 build (`OSB-bocl2o1o` is instance-specific), or restrict their environments.
   - Without internet access from the lab, upload `SCVMM_2025.zip` from the [Evaluation Center](https://www.microsoft.com/en-us/evalcenter/download-system-center-2025) unchanged to File Archives (**Admin > Custom Extensions > Runbooks > File Archives**; raise **Largest upload** to 1100 MB). Then set the `scvmm_media` option to its reference.
3. Grant the groups that may order it.
4. Order it with **Hyper-V Hosts** quantity 2 or more and **SCVMM Server** quantity 1.

## Notes
- **Re-running a build item.** Every step is skipped when it is already done.
- **DNS names.** Prepare and Create Hyper-V Cluster check that every lab server's name resolves to its own address, because the cluster and SCVMM reach the servers by name. The usual cause of a failure is a record left by an earlier server with the same name. The zone lets only a record's owner update it, so the new server cannot replace it. Delete the record named in the error, then re-run the item. HPA-q181uofy deletes the records of the servers it deletes.
- **The cluster's name** is registered with one domain controller while the cluster is created. Other DNS servers answer for it only after directory replication. A resolver that cached "no such name" during creation keeps that answer until it expires, which can take up to the zone's minimum TTL (an hour by default). So Add Hyper-V Cluster to SCVMM waits up to an hour for the name to resolve from the SCVMM server; in testing it took about 18 minutes. Create Hyper-V Cluster does not need the name.
- **Slow steps** run as scheduled tasks under `\CloudBolt\` and are polled every 30 seconds. Steps that reach AD or other servers sign in as the domain service account, so there is no double hop. Their logs are in the job's progress.
- **The service account's password** goes to the guest inside the script body, base64 encoded for quoting only. On AWS that body stays in Systems Manager's command history.
- **Scale-up is not supported.** Order a new lab for more hosts.
- **Nested VMs** reach the network only through their host's NAT (`192.168.250.0/24`), because EC2 drops frames from unknown MAC addresses. They are not reachable from the VPC.
- **SCVMM has no templates, logical networks or IP pools yet.** Add them in the console.
- **Deleting the lab** deletes the instances. The storage volume goes with the SCVMM server, and the teardown item destroys the cluster and deletes its computer object. The cluster name's DNS record stays; the name is unique per lab. The servers' own computer objects and DNS records are deleted only by HPA-q181uofy, when it is installed.

### Not yet run live
Domain join through Install SCVMM, and New-Cluster itself, have run on a live appliance. These have not:
- **The rest of Create Hyper-V Cluster and Add Hyper-V Cluster to SCVMM.**
- **Cluster address in one subnet.** One IP resource per host is AWS's clustering pattern for hosts in separate subnets; here the hosts share a subnet. If the cluster name does not move with the core group, keep only the first host's address and pin the group to that host.
- **The teardown's `Remove-Cluster -CleanupAD`.** Deleting the cluster's computer object over LDAP was refused, because Windows protects it.

### Caveats
- **SQL Server media download.** The ISO URL in Install SCVMM (`SQL_ISO_URL`) is Microsoft's file, but no Microsoft page documents it, so it can move.
- **SQL Server edition.** SCVMM 2025 lists Standard, Enterprise and Datacenter. Developer is the Enterprise engine, for test use only.
- **SCSI-3 persistent reservations** on the iSCSI target are not validated: `Test-Cluster` is not run. Run cluster validation in Failover Cluster Manager for a supported configuration.
