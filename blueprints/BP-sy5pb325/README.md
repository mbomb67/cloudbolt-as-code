# 3-Tier Web App on OpenShift Virtualization

Builds a web, app and database VM on OpenShift Virtualization and wires them into a working app: Apache as a TLS reverse proxy, Apache with PHP serving the Request Log sample page, and MariaDB. Every step runs as its own job, so the order's job log shows the whole build. CloudBolt needs no network route to the VMs: guest setup goes through the cluster API with the [KubeVirt API tunnel](../../shared_modules/SHM-wkubiao7/).

## Build order

Steps in a group run in parallel; the groups run in order.

| Group | Build items | What happens |
|---|---|---|
| 1 | DB, App, Web | The three VMs are built. |
| 2 | Install MariaDB, Install Apache and PHP, Install Apache TLS Proxy | Packages are installed and started. The database, its `entries` table and its user are created. |
| 3 | Service: Database 3306, Service: App 8080, Service: Web 443, Publish Web Route | Each VM gets a Service named after it, so the tiers find each other by DNS name. The web VM gets a public HTTPS Route. |
| 4 | Deploy App, Configure Reverse Proxy | The app is deployed with the database Service's name in its config. The web VM proxies to the app Service. |
| 5 | Key Vault Certificate | A certificate is issued in Azure Key Vault, installed on the web VM with its private key, and given to the Route. |
| 6 | NetworkPolicy: Database, App, Web | The database accepts 3306 from the app VM only, the app accepts 8080 from the web VM only, the web VM accepts 443 from the cluster's routers only. |
| 7 | Verify App | An entry is added through the app and read back, the web VM is shown to be blocked from the database, and the public URL is opened. |

Deleting the resource removes the Route, Services, NetworkPolicies and the Key Vault certificate, then the VMs.

## Order form

- **Environment** for each VM, when more than one is enabled on its build item.
- **Database Password**, on the Install MariaDB step: the password of the database user the app connects as. That step stores it encrypted on the resource, and the Deploy App step reads it from there.
- **Azure Environment** and **Key Vault**: the vault that issues the certificate. The Azure environment's resource handler signs in to it.

Nothing else is asked. Every other input of every step has a default on its build item, and an input with a default is left off the form.

## Changing the defaults

Open the build item on the blueprint's Build tab and edit its inputs: tier names, ports, database name and user, app title, PHP stream. Clearing a default puts that input back on the order form. To take **Azure Environment** or **Key Vault** off the form too, give them a default there; on the Build tab their lists show every Azure environment and its vaults, not only one group's. A sync from the repository sets the defaults the repository lists back to its values.

## Setup

1. The OpenShift environment needs a pod network and whatever your VMs need to boot with SSH access for the server's CloudBolt credentials (cloud-init user data, SSH key, size).
2. The guests need package repositories. A RHEL guest that is not registered with Red Hat gets CentOS Stream 9 BaseOS and AppStream added, which suits a lab; register the guests for anything else. The guests need outbound HTTPS to reach the repositories.
3. Grant the Azure resource handler's service principal **Key Vault Certificates Officer** and **Key Vault Secrets User** on the vault. A vault that uses access policies needs certificate Get, Create and Delete, and secret Get.
4. After the first import, open each of the three server build items on the blueprint's Build tab and enable your OpenShift Virtualization environment, with the CPU, memory and disk you want for that tier. The repository enables no environment, because environments differ on every CloudBolt, so the blueprint cannot be ordered until you do. With one environment enabled the order form does not ask for it. A later sync keeps what you enabled.

## What you get

- The resource's **Application URL** parameter is a link to the app.
- The page footer shows the hostname of the app VM that answered and its PHP version. The app VM runs the distribution's own PHP (8.0 on Enterprise Linux 9) because the PHP Stream input of **Install Apache and PHP** is `default`; set it to a stream, such as 8.3, when you want to move it later.
- Browsers see the cluster router's certificate. The Key Vault certificate secures the connection from the router to the web VM, and the Route trusts only that certificate.

## Contents

| Role | ID | Name |
|---|---|---|
| Build | OHK-92i4lt1y | Install MariaDB |
| Build | OHK-d9ujne9z | Install Apache and PHP |
| Build | OHK-lwhuaezv | Install Apache TLS Proxy |
| Build | OHK-vmmz9jfq | Expose VM Port as Service |
| Build | OHK-njnsxqbf | Publish VM HTTPS Route |
| Build | OHK-ulnznmqb | Deploy Request Log App |
| Build | OHK-0q5dbdq6 | Configure Reverse Proxy |
| Build | OHK-ca90z6g2 | Issue Key Vault Certificate for Web Server |
| Build | OHK-yf2kpzkx | Restrict VM Ingress |
| Build | OHK-jru4jomd | Verify 3-Tier Web App |
| Teardown | OHK-mfcauahq | Teardown 3-Tier Web App Networking |
| Shared module | SHM-5t7bom9y | openshift_vm_network: Services, Routes, NetworkPolicies |
| Shared module | SHM-eanos2e0 | vm_tier_scripts: find a tier's VM and run a script on it |
| Shared module | SHM-ny8h0nx0 | azure_key_vault_certificates |
| Shared module | SHM-wkubiao7 | kubevirt_api_tunnel |

## Reusing the steps

Each step names the server build item it works on in its **Tier** input, so any blueprint with server build items on OpenShift Virtualization can use them. Add a CloudBolt Plug-in build item after the servers, choose the action, and set its inputs on the Build tab:

- **Expose VM Port as Service**: Tier and Port.
- **Restrict VM Ingress**: Tier, Port, and Allowed From (another tier's name, or `router`). It always keeps SSH from the KubeVirt API pods open, which the API tunnel needs.
- **Publish VM HTTPS Route**: Tier and Port. Add a Service for the same port first.
