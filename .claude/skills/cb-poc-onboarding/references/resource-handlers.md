# Phase 1: Resource handlers

Goal: up to two resource handlers (the POC clouds), each verified. Covers prerequisite P3 (first half). The user creates each handler in the UI because the form takes the credentials; the agent prepares, links, verifies and diagnoses.

## Per handler

1. **Which cloud.** Ask one question: "Which cloud should we connect first: AWS, Azure or vCenter?" Nothing else in that message.
2. **Where it builds.** Ask one question, from the table's Second screen column: AWS "Which AWS region will the POC use? Say us-east-1 if you are not sure.", Azure "Which Azure location will the POC use? Say eastus if you are not sure.", vCenter "Which vCenter cluster will the POC build in?". The handler form's second screen needs this answer.
3. **Create it.** One message with one action: a sentence on what the handler is for, the `onb_ui_link` link for `new_resource_handler:<slug>` (it opens the technology chooser: tell the user to click the tile named in `clickTechnology`), the first-screen values from the table, the minimum-permissions link to apply to the credential first, then the second screen in one sentence: "On the second screen, choose <answer from step 2> under <field>, leave every other field as it is, and click Done." End with "tell me when it is saved". The user types the secret in the form. Ports come up only if verification fails. The form runs Verify Connection itself and, on failure, shows only "Unable to connect to handler with the information provided." Clicking Done creates the environments for what the user chose, which Phase 2 then uses.
4. **Verify.** When the user answers:
   - Saved: `onb_status` to find the new `RH-` id, then `onb_verify_resource_handler` with `onb_handler` set to it; poll `fetch_job`. `verified: true` means done; record nothing yet (P3 completes with the environments). `verified: false`: read `error` and `hints`, explain the cause in two sentences, send `links.edit` for the fix as the single ask, and verify again after the user saves.
   - Not saved (the form rejected it): `onb_recent_errors` with `onb_contains: "Failed to validate connection"` and `onb_minutes: 10`. The group's `traceback` and `hints` hold the cloud API's error. Explain it, then send the same `new_resource_handler:<slug>` link again.
5. **Image cache, right away.** As soon as `verified: true`, the next ask is the echo for `onb_sync_images` with the handler: it fetches every image the credentials can see, without the whitelist, and enables the technology's Fetch and Cache recurring job and rule plus Execute all Rules, with their `use_whitelist` input mapped to false so the nightly refresh stays complete; on AWS the recurring job itself always uses the whitelist, the rule does the full refresh per imported region. It is a long job: AWS without a whitelist can take ten minutes or more per region, Azure a minute or two per location, vCenter a few minutes. Do not wait for it; continue, and check the job before importing OS builds. Without this step the image pickers and `onb_import_os_build` see an empty cache on a new appliance, and with the default whitelist they miss images customers use. By default it refreshes the regions or locations of the handler's environments, which after the second screen is the POC region.
6. **Second cloud.** Ask one question: "Is there a second cloud for this POC? Say no to continue with one." Yes: steps 2 to 5 for it. Stop at two handlers; the test plan needs no more.

## Technologies

| Technology | Page key | First screen: non-secret values | Secret entered in the UI | Second screen | Minimum permissions | Ports from the appliance |
|---|---|---|---|---|---|---|
| AWS | `new_resource_handler:aws` | Credentials type Account Credentials; authentication Access & Secret Key; access key id | Secret access key | Available Regions: the POC region only (CloudBolt creates an environment for each VPC in it). Keep the name, leave SSM ARN, S3 billing bucket and the VM filters empty, keep Add Name & Annotation tags checked | [AWS minimum permissions](https://docs.cloudbolt.io/articles/cloudbolt-latest-docs/aws/a/h2__2010883827) (EC2 describe, run, terminate, tags, VPC and subnet describe, key pairs; S3 and IAM pass-role only if used) | 443 to the regional EC2, STS and SSM endpoints |
| Azure | `new_resource_handler:azure_arm` | Handler name; tenant id; application (client) id; subscription id or the choice to import subscriptions after | Client secret value (not the secret id) | Import locations: the POC location only (an environment per location); leave the rest | [Azure minimum permissions](https://docs.cloudbolt.io/articles/cloudbolt-latest-docs/azure-resource-manager/a/h3_736726059) (Reader on the subscription plus Virtual Machine Contributor and Network Contributor on the POC resource group, or Contributor on the subscription for a POC) | 443 to login.microsoftonline.com and management.azure.com |
| vCenter | `new_resource_handler:vmware` | Handler name; vCenter FQDN; port 443; username in user@domain form; whether to verify the vCenter certificate (off for a self-signed POC, or add the CA under Admin > SSL Certificates) | Password | Import VMware Clusters: the POC cluster only (an environment per cluster); leave the rest | [vCenter minimum permissions](https://docs.cloudbolt.io/articles/#!cloudbolt-latest-docs/minimum-required-permissions-in-vcenter-vmware) on the datacenter, cluster, datastore, network and folder used | 443 to vCenter and to every ESX host (console and ISO uploads) |
| SCVMM | `new_resource_handler:scvmm` | Server FQDN, WinRM port (5985 or 5986), domain and username | Password | When it lists locations, the POC one only | SCVMM administrator role | 5985 or 5986 to the SCVMM server |
| GCP | `new_resource_handler:gcp` | Handler name, project | Service-account JSON key file (uploaded) | When it lists regions, the POC one only | Compute Admin and Service Account User on the project | 443 to googleapis.com |
| Nutanix | `new_resource_handler:acropolis` | Prism Element or Central address, port 9440, username | Password | When it lists clusters, the POC one only | Cluster admin | 9440 to Prism |
| OpenStack | `new_resource_handler:openstack` | Auth URL ending in /v3, domain, project, username | Password | When it lists projects or regions, the POC one only | Member role on the project | 5000 and the service endpoints |

The POC test plan documents AWS, Azure and vCenter; the other rows are enough to get a handler created and verified, and the environment guidance in [phases.md](phases.md) is generic for them.

## Reading verification errors

`onb_verify_resource_handler.hints` and `onb_recent_errors.groups[].hints` already map the frequent errors to a fix. The pattern to explain to the user:

| Error says | It means | Fix |
|---|---|---|
| `InvalidClientTokenId`, `SignatureDoesNotMatch`, `AuthFailure` (AWS) | The key id or secret is wrong, often a trailing space | Re-enter both on the edit link |
| `UnauthorizedOperation`, `AccessDenied` (AWS) | A permission is missing | Apply the minimum permissions policy to the IAM identity |
| `AADSTS7000215` (Azure) | The client secret is invalid or expired | New secret on the app registration; paste the value, not the id |
| `AADSTS700016`, `AADSTS90002` (Azure) | Application or tenant id is wrong | Check both ids on the app registration overview |
| `AuthorizationFailed`, `SubscriptionNotFound` (Azure) | No role on the subscription, or wrong subscription id | Assign Reader plus the roles in the permissions document |
| `Cannot complete login due to an incorrect user name or password` (vCenter) | Credentials | user@domain form; re-enter the password |
| `Permission to perform this operation was denied` (vCenter) | vCenter role lacks a privilege | Compare with the vCenter permissions document |
| `CERTIFICATE_VERIFY_FAILED`, `SSLError` | TLS inspection or a self-signed certificate | Add the CA under Admin > SSL Certificates (`onb_ui_link ssl_certificates`) or disable verification on the handler for the POC |
| `Name or service not known` | DNS on the appliance | Fix the appliance's resolver or use the IP |
| `Connection refused`, `timed out`, `EndpointConnectionError` | Firewall or proxy | Open 443 outbound to the endpoint, or set the web proxy (`onb_ui_link proxy`) |

Hand the user the fix and the edit link, then verify again. After three failed rounds on one handler, record P3 as `pending` with the last error and continue with the other cloud.

## Hand-off to Phase 2

Both handlers verified (or one, if the POC has one cloud): `onb_status` shows each with `lastVerify.ok: true`. Continue with the environments in [phases.md](phases.md).
