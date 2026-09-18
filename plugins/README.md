# Plugins

Python and remote-script actions. Plugins that belong to a blueprint or action are documented in that parent's README; standalone plugins have their own.

| Plugin | ID | Type | Used by |
|---|---|---|---|
| AD DNS - Create A Record | `OHK-dnsadd01` | CloudBolt Plug-in | AD DNS - Create A Record (HPA-dnscrt01) |
| AD DNS - Delete A Record | `OHK-dnsdel01` | CloudBolt Plug-in | AD DNS - Delete A Record (HPA-dnsdec01) |
| Add Resource Group to Environment | `OHK-r1imfgdx` | CloudBolt Plug-in | Azure Resource Group - Bicep (BP-p7zmh96m) |
| Ansible Ad-Hoc | `OHK-xdjd5o9b` | CloudBolt Plug-in | Create VPC (BP-n454hj40) |
| Apply Public-Exposure Policy | `OHK-b2az2bn6` | CloudBolt Plug-in | Apply Policy (RSA-mgh3dn9p) |
| AWS S3 Bucket | `OHK-ez6r3rqe` | CloudBolt Plug-in | AWS S3 Bucket (BP-91ygq6me) |
| Azure CMK - Per-VM Disk Encryption Set | `OHK-vklpnqhq` | CloudBolt Plug-in | Azure CMK - Per-VM Disk Encryption Set (HPA-w1dmx20b) |
| Azure CMK - Remove Per-VM Disk Encryption Set | `OHK-2vpg4pff` | CloudBolt Plug-in | Azure CMK - Remove Per-VM Disk Encryption Set (HPA-h7g0i0dx) |
| Azure Network Security Group Build | `OHK-7987st2p` | CloudBolt Plug-in | Azure Network Security Group (BP-3fdhnw54) |
| Azure Network Security Group Sync | `OHK-kdne1t5s` | CloudBolt Plug-in | Azure Network Security Group (BP-3fdhnw54) |
| Azure NSG - Attach VM to NSG | `OHK-t8fjx0kz` | CloudBolt Plug-in | Azure NSG - Attach VM to NSG (HPA-vo2ghe4x) |
| Azure NSG - Generate Options | `OHK-vswf8b1m` | CloudBolt Plug-in | standalone |
| Azure Price Sheet Refresh | `OHK-bjgpsxoq` | CloudBolt Plug-in | Azure Price Sheet Refresh (RJB-reblryol) |
| Azure Resource Group | `OHK-7c5xywbx` | CloudBolt Plug-in | Azure Resource Group (BP-zmeot1ff) |
| Azure Resource Manager Rate Hook | `OHK-vg0rmi7i` | CloudBolt Plug-in | Azure Resource Manager Rate Hook (HPA-t7hlyvyy) |
| Azure Storage Account | `OHK-qev70tpa` | CloudBolt Plug-in | Azure Storage Account (BP-nszj7jop) |
| Build Azure Subscription | `OHK-96zebx6i` | CloudBolt Plug-in | Azure Cross-Tenant Subscription (BP-5pei9cno) |
| Change Access Tier | `OHK-stdxttnw` | CloudBolt Plug-in | Change Access Tier (RSA-0ldsokuc) |
| Change SKU | `OHK-fel441xh` | CloudBolt Plug-in | Change SKU (RSA-gr2wsfzx) |
| Create Blob Container | `OHK-l25x6wd8` | CloudBolt Plug-in | Create Blob Container (RSA-xzs5f3a2) |
| Create Environment | `OHK-yd1af0eu` | CloudBolt Plug-in | Create VPC (BP-n454hj40) |
| Delete Blob Container | `OHK-y1120h61` | CloudBolt Plug-in | Delete Blob Container (RSA-zmi15uts) |
| Deploy AWS App Migration Agent | `OHK-5wpzgw68` | CloudBolt Plug-in | Deploy AWS App Migration Agent (SVA-mwbtjfr0) |
| Deploy Bicep Template | `OHK-gqvi9kv4` | CloudBolt Plug-in | Azure Resource Group - Bicep (BP-p7zmh96m), Bicep Deployment (BP-nibk4erf) |
| Discover Azure Resource Groups | `OHK-e5a4m2bm` | CloudBolt Plug-in | Azure Resource Group (BP-zmeot1ff) |
| Discover Azure Storage Accounts | `OHK-h9lmvlkb` | CloudBolt Plug-in | Azure Storage Account (BP-nszj7jop) |
| Discover Azure Subscriptions | `OHK-av52dzqm` | CloudBolt Plug-in | Azure Cross-Tenant Subscription (BP-5pei9cno) |
| Discover OpenShift Project Landing Zones | `OHK-e7albpni` | CloudBolt Plug-in | OpenShift Project Landing Zone (BP-tikkhf2y) |
| Discover S3 Buckets | `OHK-j8umy9e2` | CloudBolt Plug-in | AWS S3 Bucket (BP-91ygq6me) |
| DNS Record - Build | `OHK-dnsbld01` | CloudBolt Plug-in | DNS A Record (BP-dnsrec01) |
| DNS Record - Discover | `OHK-dnsdsc01` | CloudBolt Plug-in | DNS A Record (BP-dnsrec01) |
| DNS Record - Teardown | `OHK-dnstrd01` | CloudBolt Plug-in | DNS A Record (BP-dnsrec01) |
| Drift Check | `OHK-9n4wfasa` | CloudBolt Plug-in | Drift Check (RSA-5jeixn92) |
| Empty Bucket | `OHK-pimbei86` | CloudBolt Plug-in | Empty Bucket (RSA-fk574ax9) |
| Expire Servers | `OHK-59t2apzf` | CloudBolt Plug-in | Expire Servers (RJB-nsx4v2s1) |
| Extend Expiration | `OHK-3w9nejn3` | CloudBolt Plug-in | Extend Expiration (RSA-kx7mdgva) |
| Generate options for 'Expiration Date' | `OHK-cfciy0fo` | CloudBolt Plug-in | Generate options for 'Expiration Date' (HPA-qb0w86mi) |
| Generate Tags from Resource Handler Tag Map | `OHK-xoajww7v` | CloudBolt Plug-in | Azure Resource Group (BP-zmeot1ff) |
| Grant Restricted Contributor Access | `OHK-pr8q2szp` | CloudBolt Plug-in | Grant Access (RSA-1kpl3w0d) |
| HCP Terraform No-Code Module | `OHK-axtt0yqq` | CloudBolt Plug-in | HCP Terraform No-Code Module (BP-00meiwwz) |
| HCP Terraform VM | `OHK-pvo05e24` | CloudBolt Plug-in | HCP Terraform VM (BP-b0qm83lh) |
| Initialize Windows Disk | `OHK-2evtyysr` | Remote Script | Windows File Server (BP-psw7rclb) |
| Install IIS Windows | `OHK-0ql0870k` | Remote Script | IIS Web Application (BP-122nbdt5) |
| Install NGINX OEL8 | `OHK-2vdm93hu` | Remote Script | NGINX Web Application (BP-anonytrx) |
| Install Postgres | `OHK-29uent0x` | Remote Script | Postgres Database (BP-b91c5f90) |
| Install Windows File Server | `OHK-up7829pv` | Remote Script | Windows File Server (BP-psw7rclb) |
| Join Linux Server to AD Domain | `OHK-exqmrl0e` | CloudBolt Plug-in | Join Linux Server to AD Domain (HPA-o6ctckmt) |
| Launch Helm Chart (2) | `OHK-tb703wqx` | CloudBolt Plug-in | Launch Helm Chart (RSA-tqy61754) |
| List Blob Containers | `OHK-3kp5czyb` | CloudBolt Plug-in | List Blob Containers (RSA-h93k53ld) |
| List Resources in Group | `OHK-iy92hhvh` | CloudBolt Plug-in | List Resources in Group (RSA-a38h23ms) |
| List Subscription Access | `OHK-ovt0z46j` | CloudBolt Plug-in | List Access (RSA-f7wb11ny) |
| Manage Delete Lock | `OHK-sf6w5pfn` | CloudBolt Plug-in | Manage Delete Lock (RSA-qwku9lip) |
| Manage Team Access | `OHK-9zzqqz7t` | CloudBolt Plug-in | Manage Team Access (RSA-yj1c4b5s) |
| Migrate VM to OpenShift | `OHK-kx3qcgmy` | CloudBolt Plug-in | Migrate VM to OpenShift (SVA-yhcr56mm) |
| Network Security Group Teardown | `OHK-9jouejv8` | CloudBolt Plug-in | Azure Network Security Group (BP-3fdhnw54) |
| Node Size - Generate Options by OS Build Architecture | [OHK-9csbq3zd](OHK-9csbq3zd/README.md) | CloudBolt Plug-in | standalone |
| Node Size - Generate Options by Region, OS Image, Security, Networking and Storage (Azure SKU capabilities) | [OHK-kujhsds0](OHK-kujhsds0/README.md) | CloudBolt Plug-in | standalone |
| OpenShift Project Landing Zone | `OHK-prew0osh` | CloudBolt Plug-in | OpenShift Project Landing Zone (BP-tikkhf2y) |
| Remove Resource Group from Environments | `OHK-vm5p34w3` | CloudBolt Plug-in | Azure Resource Group - Bicep (BP-p7zmh96m) |
| Request Certificate (Windows CA) | `OHK-67bw7wgu` | CloudBolt Plug-in | Request Certificate (Windows CA) (BP-lt6a3yzf) |
| Request Quota Change | `OHK-ug53cdbx` | CloudBolt Plug-in | Request Quota Change (RSA-4e8lmj2r) |
| Resize | `OHK-9xffkz53` | CloudBolt Plug-in | Resize (RSA-e59s1v24) |
| Retrieve Pending Certificate | `OHK-ul8wbswa` | CloudBolt Plug-in | Retrieve Pending Certificate (RSA-7cjsqrwy) |
| Revoke Restricted Contributor Access | `OHK-myrfeogg` | CloudBolt Plug-in | Revoke Access (RSA-8zwsrcbv) |
| Run SQL Command | `OHK-4w05hdd3` | Remote Script | Run SQL Command (RSA-jaitfhwp) |
| Set Resource Name From Field | `OHK-yw0klpjg` | CloudBolt Plug-in | Postgres Database (BP-b91c5f90) |
| Set URL Parameter | `OHK-1rel6s64` | CloudBolt Plug-in | IIS Web Application (BP-122nbdt5), NGINX Web Application (BP-anonytrx) |
| Set VPC Name | `OHK-50ntkska` | CloudBolt Plug-in | Create VPC (BP-n454hj40) |
| Teardown AWS S3 Bucket | `OHK-8g9ljnp4` | CloudBolt Plug-in | AWS S3 Bucket (BP-91ygq6me) |
| Teardown Azure Resource Group | `OHK-4xqzbdtx` | CloudBolt Plug-in | Azure Resource Group (BP-zmeot1ff) |
| Teardown Azure Storage Account | `OHK-8px9e3ws` | CloudBolt Plug-in | Azure Storage Account (BP-nszj7jop) |
| Teardown Azure Subscription | `OHK-597w4hvq` | CloudBolt Plug-in | Azure Cross-Tenant Subscription (BP-5pei9cno) |
| Teardown Bicep Deployment | `OHK-t2gs5caq` | CloudBolt Plug-in | Azure Resource Group - Bicep (BP-p7zmh96m), Bicep Deployment (BP-nibk4erf) |
| Teardown HCP Terraform No-Code Module | `OHK-y9d1uwhw` | CloudBolt Plug-in | HCP Terraform No-Code Module (BP-00meiwwz) |
| Teardown HCP Terraform VM | `OHK-2b9qu490` | CloudBolt Plug-in | HCP Terraform VM (BP-b0qm83lh) |
| Teardown OpenShift Project Landing Zone | `OHK-mpe8fl3d` | CloudBolt Plug-in | OpenShift Project Landing Zone (BP-tikkhf2y) |
| Terraform Update | `OHK-lvy5tj0y` | CloudBolt Plug-in | Terraform Update (RSA-dxrh4m6j) |
| Test Postgres Connection | `OHK-wr079u8q` | CloudBolt Plug-in | Test Postgres Connection (RSA-9tfwebk7) |
| Update Bicep Deployment | `OHK-9f45ede7` | CloudBolt Plug-in | Update Bicep Deployment (RSA-fa7r7cg7) |
| Update Tags | `OHK-nwcyoto7` | CloudBolt Plug-in | Update Tags (RSA-kbikieh7) |
| Update Variables | `OHK-oj87ukle` | CloudBolt Plug-in | Update Variables (RSA-qofayikp) |
