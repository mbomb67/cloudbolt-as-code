# Blueprints

Orderable resources. Each folder has a README with contents, prerequisites, and setup.

| Blueprint | ID | What it does |
|---|---|---|
| AWS S3 Bucket | [BP-91ygq6me](BP-91ygq6me/README.md) | Scaffold for a blueprint that provisions, discovers, empties, and tears down AWS S3 buckets as CloudBolt resources of type `s3_bucket`. |
| Azure Cross-Tenant Subscription | [BP-5pei9cno](BP-5pei9cno/README.md) | Create Azure subscriptions with cross-tenant billing. |
| Azure Network Security Group | [BP-3fdhnw54](BP-3fdhnw54/README.md) | Create, delete and synchronise Azure Network Security Groups |
| Azure Resource Group | [BP-zmeot1ff](BP-zmeot1ff/README.md) | Manage the full lifecycle of Azure Resource Groups: create (build), delete (teardown), and inventory existing groups (discovery), plus day-... |
| Azure Resource Group - Bicep | [BP-p7zmh96m](BP-p7zmh96m/README.md) | Create an Azure resource group from Microsoft's public Azure Quickstart Templates repository (subscription-deployments/create-rg/main.bicep... |
| Azure Storage Account | [BP-nszj7jop](BP-nszj7jop/README.md) | Manage the full lifecycle of Azure Storage Accounts: provision (build), delete (teardown), and inventory existing accounts (discovery). |
| Bicep Deployment | [BP-nibk4erf](BP-nibk4erf/README.md) | Deploy infrastructure from an Azure Bicep template hosted in GitHub. |
| Create VPC | [BP-n454hj40](BP-n454hj40/README.md) | Creates an AWS VPC with Ansible Playbook |
| DNS A Record | [BP-dnsrec01](BP-dnsrec01/README.md) | Order an Active Directory-integrated DNS A record. |
| HCP Terraform No-Code Module | [BP-00meiwwz](BP-00meiwwz/README.md) | Provisions infrastructure through HCP Terraform using its no-code provisioning workflow: one blueprint targets one pinned no-code registry ... |
| HCP Terraform VM | [BP-b0qm83lh](BP-b0qm83lh/README.md) | Provisions a VM through HCP Terraform using one dedicated TFC workspace per deployment: the order form's cascading dropdowns pick the Terra... |
| IIS Web Application | [BP-122nbdt5](BP-122nbdt5/README.md) | Deploys a Microsoft IIS Web Application |
| NGINX Web Application | [BP-anonytrx](BP-anonytrx/README.md) | Creates an NGINX Web Application |
| OpenShift Project Landing Zone | [BP-tikkhf2y](BP-tikkhf2y/README.md) | Get a governed OpenShift project for your team. |
| Postgres Database | [BP-b91c5f90](BP-b91c5f90/README.md) | Deploys one Oracle Linux 8 VM, installs PostgreSQL (15-18) from the PGDG repository, creates one database and owning role with the requeste... |
| Request Certificate (Windows CA) | [BP-lt6a3yzf](BP-lt6a3yzf/README.md) | Ad-hoc certificate requests against a Microsoft AD CS (Windows) Certificate Authority via Web Enrollment. |
| Windows File Server | [BP-psw7rclb](BP-psw7rclb/README.md) | Deploys a Windows Server, then installs the File Server Role. |
