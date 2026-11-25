# Phase 1: Resource handlers

Goal: up to two resource handlers (the POC clouds), each verified. Covers prerequisite P3 (first half). The user creates each handler in the UI because the form takes the credentials; the agent prepares, links, verifies and diagnoses.

## Per handler

1. **Pre-brief.** From the table below, tell the user the non-secret values to have ready, the minimum-permissions document to apply to the credential first, and the ports the appliance needs open. Do not ask for the secret values.
2. **Link.** `onb_ui_link` with `new_resource_handler:<slug>` and send the URL. The user fills the form, including the credential, and saves. The form runs Verify Connection itself and, on failure, shows only "Unable to connect to handler with the information provided."
3. **Verify.** Ask whether the save succeeded.
   - Saved: `onb_status` to find the new `RH-` id, then `onb_verify_resource_handler` with `onb_handler` set to it; poll `fetch_job`. `verified: true` means done; record nothing yet (P3 completes with the environments). `verified: false`: read `error` and `hints`, explain the cause, send `links.edit` for the fix, and verify again after the user saves.
   - Not saved (the form rejected it): `onb_recent_errors` with `onb_contains: "Failed to validate connection"` and `onb_minutes: 10`. The group's `traceback` and `hints` hold the cloud API's error. Explain it, then send the same `new_resource_handler:<slug>` link again.
4. **Repeat** for the second cloud. Stop at two handlers; the test plan needs no more.

## Technologies

| Technology | Page key | Non-secret values to prepare | Secret entered in the UI | Minimum permissions | Ports from the appliance |
|---|---|---|---|---|---|
| AWS | `new_resource_handler:aws` | Handler name; access key id; optionally an IAM role ARN to assume; the regions to use | Secret access key | [AWS minimum permissions](https://docs.cloudbolt.io/articles/cloudbolt-latest-docs/aws/a/h2__2010883827) (EC2 describe, run, terminate, tags, VPC and subnet describe, key pairs; S3 and IAM pass-role only if used) | 443 to the regional EC2, STS and SSM endpoints |
| Azure | `new_resource_handler:azure_arm` | Handler name; tenant id; application (client) id; subscription id or the choice to import subscriptions after; locations to use | Client secret value (not the secret id) | [Azure minimum permissions](https://docs.cloudbolt.io/articles/cloudbolt-latest-docs/azure-resource-manager/a/h3_736726059) (Reader on the subscription plus Virtual Machine Contributor and Network Contributor on the POC resource group, or Contributor on the subscription for a POC) | 443 to login.microsoftonline.com and management.azure.com |
| vCenter | `new_resource_handler:vmware` | Handler name; vCenter FQDN; port 443; username in user@domain form; whether to verify the vCenter certificate (off for a self-signed POC, or add the CA under Admin > SSL Certificates) | Password | [vCenter minimum permissions](https://docs.cloudbolt.io/articles/#!cloudbolt-latest-docs/minimum-required-permissions-in-vcenter-vmware) on the datacenter, cluster, datastore, network and folder used | 443 to vCenter and to every ESX host (console and ISO uploads) |
| SCVMM | `new_resource_handler:scvmm` | Server FQDN, WinRM port (5985 or 5986), domain and username | Password | SCVMM administrator role | 5985 or 5986 to the SCVMM server |
| GCP | `new_resource_handler:gcp` | Handler name, project | Service-account JSON key file (uploaded) | Compute Admin and Service Account User on the project | 443 to googleapis.com |
| Nutanix | `new_resource_handler:acropolis` | Prism Element or Central address, port 9440, username | Password | Cluster admin | 9440 to Prism |
| OpenStack | `new_resource_handler:openstack` | Auth URL ending in /v3, domain, project, username | Password | Member role on the project | 5000 and the service endpoints |

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
