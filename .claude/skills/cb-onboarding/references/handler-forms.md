# Handler forms by technology

What the user fills on each screen of the Add resource handler modal, per technology, read from the CloudBolt source (2026.3). Use it for Phase 1 steps 2 and 3 in [resource-handlers.md](resource-handlers.md): one action message per handler that covers both screens.

## How the modal works

- **Screen 1, credentials.** Continue checks the connection. When it passes, CloudBolt saves the handler under a default name, so closing the modal after this point leaves a handler without settings; send its edit link (`handler_edit:RH-...`) to finish. When it fails, the form shows only "Unable to connect to handler with the information provided"; SCVMM shows the real error.
- **Screen 2, settings.** Done saves. Some technologies create environments here, some create one environment named after the handler, some create none. For those, Phase 2 imports the POC location with `onb_handler_locations` and `onb_import_environments`; when the tools cannot list that technology's locations, send the handler page (`onb_ui_link` with the RH- id) and name the tab from the table as the single ask.
- **Every secret is typed in the form**, never in chat (SKILL.md rule 2).
- **Leave these as they are wherever they appear:** Tenant on screen 1 (unless the POC uses CloudBolt tenants), and on screen 2 Name (prefilled; renaming is fine), Description, VM Names to Ignore, VM Tags to Include, VM folder filters, Console feature, Split by region or cluster, Job Labels.
- **AWS closes screen 2.** On AWS, AWS GovCloud, AWS China and AWS JIT, Done creates one environment for every VPC in the chosen region. The POC needs one, so the user closes screen 2 with the X instead of clicking Done; the handler was saved by Continue. The agent then lists the region's VPCs and imports the one the user picks (Phase 1 step 5).
- **The "where" question.** Every environment is one more to set up and test, so ask for the one region, location, cluster or project the POC needs; more can be added after the POC passes. When screen 2 has a required region, location, cluster, project or datacenter field, ask for it before sending the link (Phase 1 step 2). When screen 2 has nothing to set, skip that question; the second-screen sentence is "On the second screen, leave everything as it is and click Done."

## Shown in the chooser by default

| Tile (slug) | Screen 1: what to enter | Screen 2: what to set | Environments on Done |
|---|---|---|---|
| Amazon Web Services (`aws`) | Credentials Type Account Credentials; Authentication Type Access & Secret Key; Access Key ID; Secret Access Key (secret); SSL Verification on | Nothing: close it with the X, do not click Done (Available Regions would create one environment per VPC) | None: the agent imports the one VPC the user picks, with the Name and Annotation tags |
| AWS GovCloud (US) (`aws_govcloud`), AWS China (`aws_china`) | Access Key ID; Secret Access Key (secret); IAM Role ARN only if one is assumed. GovCloud also offers optional commercial-account keys | Same as AWS: close it with the X | Same as AWS |
| Azure (`azure_arm`) | Cloud Environment Public (Default); Subscription ID; Application ID (the app's client id); Directory ID (the tenant id); Authentication Key = the client secret value (secret) | **Import locations** (required): the POC location only. Leave Azure Disk Name Template empty | A job creates one per location, then syncs networks |
| VMware vCenter (`vmware`) | IP address (the vCenter FQDN); Port 443; Account username (user@domain); Password (secret); SSL Verification off for a self-signed vCenter, or add its CA under Admin > SSL Certificates first | **Import VMware Clusters** (required): the POC cluster only. Keep Virtual folder path as prefilled | A job creates one per cluster, then syncs networks, datastores and templates |
| Google Cloud Platform (`gcp`) | Choose Upload .json file and upload a **service-account key** (secret). An OAuth web-client JSON instead sends the user to Google consent and skips screen 2 | **Projects** (required): the POC project only | A job creates one per project, with its networks |
| SCVMM (`scvmm`) | IP address (the FQDN when Kerberos is used); Account username (user@domain); Password (secret); WinRM Protocol HTTPS (5986) or HTTP (5985); WinRM Transport NTLM | **Import clusters** (optional): check the POC cluster | One per checked cluster; none if nothing is checked |
| Hyper-V (`hyperv`) | Hyper-V instance (host, or the cluster address); Account username (user@domain or domain\user); Password (secret); WinRM Protocol HTTPS; WinRM Transport NTLM (avoid CredSSP, which looks broken in this release); check Clustered Hyper-V for a cluster | **Images Directory** (required, prefilled): keep it if that folder exists on the host, otherwise the folder that holds the VM images | One, named after the handler |
| Nutanix Acropolis (`acropolis`) | Protocol https; IP address (Prism); Port 9440; Account username; Password (secret); Nutanix API version matching Prism (v4 by default) | **Import Nutanix Clusters** (required): the POC cluster only | One per cluster |
| OpenStack (`openstack`) | Protocol: HTTPS when Keystone uses TLS (HTTP is preselected); Domain; Project ID; IP address (the Keystone host); Port 5000; Account username; Password (secret) | Nothing | None: Phase 2 imports the POC project (the handler's Projects tab) |
| Oracle Cloud Infrastructure (`oci`) | Region (for example us-ashburn-1); User OCID; Certificate Fingerprint; Key File = the PEM private key pasted in (secret, shown unmasked); Passphrase (secret, optional); Tenancy OCID; Compartment OCID (optional) | Nothing | One, for the region on screen 1 |
| Alibaba Cloud (`alibaba`) | Region ID; Access Key; Access Key Secret (secret). An IllegalTimestamp error means the appliance clock has drifted | Nothing | One, named after the handler |
| IBM Cloud (`ibm_cloud`), IBM Cloud for Government (`ibm_gov`) | API Username; API Key (secret) | **Import IBM Cloud datacenters** (required): the POC datacenter only | One per datacenter |
| MAAS (`maas`) | Region Controller; Protocol https; Port; Account username; Password (secret) | Nothing | None: Phase 2 imports the POC availability zone (Locations tab) |
| MetalSoft (`metalsoft`) | IP address; Protocol: HTTPS for a TLS endpoint (HTTP is preselected); Port 443; API User Email; API Key (secret); Admin API Connection Info only if one is set up | Nothing | None: Phase 2 imports the POC datacenter (Datacenters tab) |
| IPMI (`ipmi`) | One screen only, with no credentials: each server's IPMI address and login are order parameters. The tile disappears once an IPMI handler exists | **Name** (required, empty): type one | None |

## Hidden unless the appliance enables the technology

These tiles appear only when the feature `resourcehandler::<slug>` is enabled on the appliance (off by default). If the user picks one and the tile is missing, say so and record the handler `pending` with that note.

| Tile (slug) | Screen 1: what to enter | Screen 2: what to set | Environments on Done |
|---|---|---|---|
| Proxmox Virtual Environment (`proxmox`) | IP address; Protocol HTTPS (HTTP is preselected); Port: the API port, usually 8006 (the field shows 443); Username without the realm; Password (secret); Realm Type PAM or PVE | Nothing | None: Phase 2 imports the POC node (Nodes tab) |
| OpenShift Virtualization (`ovirt`) | Protocol HTTPS; IP address (the OpenShift API host); Port 6443; Authentication Type Service Account Token (default) with the token (secret), or Account Credentials with a username and password (secret) | Nothing | None: Phase 2 imports the POC namespace (Namespaces tab) |
| Oracle Linux Virtualization Manager (`olvm`) | IP address; Port 443; Account username; Password (secret) | Nothing | None: Phase 2 imports the POC cluster (Clusters tab) |
| Azure Local (`azurelocal`) | Subscription ID; Application ID; Authentication Key (secret); Directory ID | **Import Clusters** (required): the POC cluster only | One per cluster |
| Outscale (`outscale`) | Region; Access Key; Access Key Secret (secret) | Nothing | None: Phase 2 imports the POC VPC (Import VPCs on the handler page) |
| VMware Cloud on AWS (`vmc_aws`) | IP address (the vCenter host); Port 443; Account username; Password (secret); Api token (secret, shown in plain text) | Nothing | None: add environments from the handler page |
| Amazon Web Services - JIT (`awsjit`) | User Certificate (PEM file); User Certificate Key (PEM file, secret); Credentials endpoint; Agency; Mission; Role | Same as AWS: close it with the X | Same as AWS |
| MacStadium Orka (`orka`) | Controller IP/Host; Service Account Token (secret) | Nothing | One, named after the handler |
| H3C CAS (`h3ccas`) | Protocol https; IP address; Port 443; Account username; Password (secret) | **Import Clusters** (optional): check the POC cluster; leave VM Group empty | One per checked cluster |

Not offered on a fresh appliance: vCloud Director appears only where it was seeded separately (screen 1: Protocol https, IP, Port 443, a System-org username, Password; screen 2: nothing; environments come from its Organizations tab). Dimension Data, Nebula and the private-AWS type have no tile.
